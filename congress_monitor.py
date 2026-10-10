#!/usr/bin/env python3
"""Monitor new House PTR (Periodic Transaction Report) filings and post them to Discord."""

import argparse
import sys
from datetime import datetime, timezone

import requests

from house_disclosures import (
    PDF_URL_TMPL,
    PTR_FILING_TYPE,
    fetch_index_years,
    equity_transactions,
    fetch_ptr_text,
    filing_sort_key,
    format_filer_name,
    index_years,
    member_key,
    parse_transactions,
    scanned_page_count,
)
from monitor_common import (
    LATE_LEGEND,
    PDF_ANNOUNCE,
    PDF_GIVE_UP,
    PDF_WAIT,
    STATE_DIR,
    add_common_args,
    any_late,
    escape_markdown,
    format_transaction,
    load_alert_state,
    load_top_performers,
    load_webhook_url,
    log,
    pdf_wait_action,
    plural,
    run_lock,
    save_alert_state,
    scanned_note,
    seed_baseline,
    send_discord_message,
    top_performer_line,
)

STATE_PATH = STATE_DIR / "seen_docids.json"

# Buys first, then sells, then exchanges. The PDF's own order is however the filer's
# broker happened to list the rows, which interleaves the two directions and makes a
# filing look busier than it is.
DIRECTION_ORDER = {"Purchase": 0, "Sale": 1, "Exchange": 2}


def header(row: dict, title: str, top: dict | None) -> list[str]:
    """Who filed, then -- for a top performer -- why that member is worth reading.

    The flag sits directly under the name because it changes how the rest is read: a
    sale by someone with a strong record is a different signal from the same sale by
    anyone else, and a reader scanning a channel of these decides on the first lines."""
    name = escape_markdown(format_filer_name(row))
    lines = [f"🗳️ **{name}** ({row.get('StateDst', '?')}) — {title}"]
    if top:
        lines.append(top_performer_line(top))
    return lines


def build_message(row: dict, transactions: list[dict], top: dict | None = None) -> str:
    filing_date = row.get("FilingDate", "?")
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    # Who filed leads: in a channel of these, the member is what a reader scans for.
    lines = header(row, "new trade disclosure", top)

    if not transactions:
        # An unreadable PDF is not an empty filing, and the alert has to say which. This
        # is the give-up case: pdf_wait_action() has already spent a grace period waiting
        # for the contents, so the reader is told to open the PDF themselves.
        lines += [
            f"Filed {filing_date} · transactions could not be read from the PDF",
            pdf_link,
        ]
        return "\n".join(lines)

    buys = sum(1 for t in transactions if t["type"] == "Purchase")
    sells = sum(1 for t in transactions if t["type"] == "Sale")
    breakdown = ", ".join(
        part for part in (plural(buys, "buy") if buys else "",
                          plural(sells, "sell") if sells else "")
        if part
    )
    lines.append(f"Filed {filing_date} · {plural(len(transactions), 'transaction')}"
                 + (f" ({breakdown})" if breakdown else ""))
    lines.append("")
    lines += [
        format_transaction(t, filing_date)
        for t in sorted(transactions, key=lambda t: DIRECTION_ORDER.get(t["type"], 3))
    ]
    lines.append("")
    if any_late(transactions, filing_date):
        lines.append(LATE_LEGEND)
    lines.append(f"Full PTR: {pdf_link}")
    return "\n".join(lines)


def build_landed_message(row: dict, top: dict | None = None) -> str:
    """The index-row-first alert: the filing exists, its PDF does not yet.

    Worth sending on its own because the row appears in the archive before the document
    is reachable, and an hourly poll catches that gap routinely. Saying "a filing landed"
    minutes after it is indexed beats saying nothing until the PDF publishes, and the
    transactions follow as a second alert once they can be read.
    """
    return "\n".join([
        *header(row, "new trade disclosure filed", top),
        f"Filed {row.get('FilingDate', '?')} · contents not published yet; "
        f"the transactions will follow when the PDF appears",
        PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
    ])


def build_scanned_message(row: dict, pages: int, top: dict | None = None) -> str:
    """A filing made on paper: complete and published, but a scan, so there are no
    transactions to list. Said once and immediately -- waiting a grace period for text
    that cannot appear would only delay it a day and then misreport it as unreadable."""
    return "\n".join([
        *header(row, "new trade disclosure", top),
        f"Filed {row.get('FilingDate', '?')} · {scanned_note(pages)}",
        PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args()

    # Hourly polling means a slow run can still be going when the next one starts, and
    # both would read the same seen-set and post the same alert.
    with run_lock("congress_monitor"):
        run(args)


def run(args) -> None:
    webhook_url = None if args.dry_run else load_webhook_url()
    now = datetime.now(timezone.utc)
    years = index_years(now)

    rows = fetch_index_years(years)
    ptr_rows = sorted(
        (r for r in rows if r.get("FilingType") == PTR_FILING_TYPE), key=filing_sort_key
    )
    if args.verbose:
        years_label = ", ".join(str(y) for y in years)
        print(f"-- {len(rows)} total filings, {len(ptr_rows)} PTRs for {years_label} --")

    if not STATE_PATH.exists():
        # Baseline run: record everything that already exists without alerting on the
        # historical backlog. A dry run reports the action but never mutates state.
        doc_ids = {r["DocID"] for r in ptr_rows}
        label = f"PTR(s) for {', '.join(str(y) for y in years)}"
        if args.dry_run:
            log(f"Dry run: would seed baseline with {len(doc_ids)} existing {label}.")
        else:
            seed_baseline(STATE_PATH, doc_ids, label)
        return

    state = load_alert_state(STATE_PATH)

    def persist() -> None:
        if not args.dry_run:
            save_alert_state(STATE_PATH, state)  # per alert, so a crash can't resend

    # A filing announced but still missing its PDF stays out of `seen`, so it comes back
    # here every run until it parses or the grace period runs out.
    new_rows = [r for r in ptr_rows if r["DocID"] not in state.seen]
    backlog = max(0, len(new_rows) - args.max_alerts)
    if backlog:
        log(f"{len(new_rows)} new PTR(s) exceeds --max-alerts={args.max_alerts}; "
            f"posting {args.max_alerts} now, leaving {backlog} for the next run.")
        new_rows = new_rows[: args.max_alerts]
    if args.verbose:
        print(f"-- {len(new_rows)} new PTR(s) to alert on, {len(state.pending)} awaiting "
              f"a PDF --")

    top_performers = load_top_performers()
    sent = announced = skipped = waiting = scanned = 0
    for row in new_rows:
        doc_id = row["DocID"]
        top = top_performers.get(member_key(row))
        pdf_text = None
        try:
            pdf_text = fetch_ptr_text(row["Year"], doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = []

        pages = scanned_page_count(pdf_text)
        if pages:
            if not deliver(build_scanned_message(row, pages, top), doc_id, webhook_url, args):
                continue
            scanned += 1
            state.pending.pop(doc_id, None)
            state.seen.add(doc_id)
            persist()
            continue

        if not transactions:
            action = pdf_wait_action(doc_id, state.pending, now, args.pending_grace)
            if action == PDF_WAIT:
                waiting += 1
                if args.verbose:
                    print(f"{doc_id}: PDF still unreadable, announced "
                          f"{state.pending[doc_id]} — waiting")
                continue
            message = (build_landed_message(row, top) if action == PDF_ANNOUNCE
                       else build_message(row, [], top))
            if not deliver(message, doc_id, webhook_url, args):
                continue
            if action == PDF_ANNOUNCE:
                announced += 1
                state.pending[doc_id] = now.isoformat()
            elif action == PDF_GIVE_UP:  # waited out the grace period, stop retrying
                sent += 1
                state.pending.pop(doc_id, None)
                state.seen.add(doc_id)
            persist()
            continue

        # The PDF published (possibly after an announcement), so the contents are known.
        state.pending.pop(doc_id, None)
        alertable = transactions if args.all_assets else equity_transactions(transactions)
        # A filing we read fine that turned out to be all bonds and funds is handled, not
        # failed: record it seen and say nothing. Only the unreadable case alerts, because
        # "no stock trades" and "we could not tell" are different facts and only one of
        # them is safe to keep quiet about.
        if not alertable:
            skipped += 1
            if args.verbose or args.dry_run:
                print(f"{doc_id}: {plural(len(transactions), 'transaction')}, "
                      f"none of them stocks — skipped")
            state.seen.add(doc_id)
            persist()
            continue

        if not deliver(build_message(row, alertable, top), doc_id, webhook_url, args):
            continue
        sent += 1
        state.seen.add(doc_id)
        persist()

    verb = "Would send" if args.dry_run else "Sent"
    log(f"{verb} {sent} new PTR alert(s) out of {len(new_rows)} new filing(s)."
        + (f" {announced} filing(s) announced ahead of their PDF." if announced else "")
        + (f" {waiting} still awaiting a PDF." if waiting else "")
        + (f" {skipped} had no stock transactions." if skipped else "")
        + (f" {scanned} scanned paper filing(s) linked without contents." if scanned else "")
        + (f" {backlog} deferred." if backlog else ""))


def deliver(message: str, doc_id: str, webhook_url: str | None, args) -> bool:
    """Post one alert. False means the caller must not record the filing as handled --
    a delivery that failed has to be retried, not swallowed."""
    if args.verbose or args.dry_run:
        print(message)
        print("---")
    if args.dry_run:
        return True
    try:
        send_discord_message(webhook_url, message)
    except requests.RequestException as exc:
        print(f"Failed to send Discord message for {doc_id}: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    main()
