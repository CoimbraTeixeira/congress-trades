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
    parse_transactions,
)
from monitor_common import (
    LATE_LEGEND,
    STATE_DIR,
    add_common_args,
    any_late,
    escape_markdown,
    format_transaction,
    load_seen_doc_ids,
    load_webhook_url,
    log,
    plural,
    save_seen_doc_ids,
    seed_baseline,
    send_discord_message,
)

STATE_PATH = STATE_DIR / "seen_docids.json"

# Buys first, then sells, then exchanges. The PDF's own order is however the filer's
# broker happened to list the rows, which interleaves the two directions and makes a
# filing look busier than it is.
DIRECTION_ORDER = {"Purchase": 0, "Sale": 1, "Exchange": 2}


def build_message(row: dict, transactions: list[dict]) -> str:
    filing_date = row.get("FilingDate", "?")
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    name = escape_markdown(format_filer_name(row))
    # Who filed leads: in a channel of these, the member is what a reader scans for.
    lines = [f"🗳️ **{name}** ({row.get('StateDst', '?')}) — new trade disclosure"]

    if not transactions:
        # An unreadable PDF is not an empty filing, and the alert has to say which.
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args()

    webhook_url = None if args.dry_run else load_webhook_url()
    years = index_years(datetime.now(timezone.utc))

    rows = fetch_index_years(years)
    ptr_rows = sorted(
        (r for r in rows if r.get("FilingType") == PTR_FILING_TYPE), key=filing_sort_key
    )
    if args.verbose:
        years_label = ", ".join(str(y) for y in years)
        print(f"-- {len(rows)} total filings, {len(ptr_rows)} PTRs for {years_label} --")

    if not STATE_PATH.exists():
        # Baseline run: record everything that already exists without alerting on the
        # historical backlog. Only filings that show up in future runs are new.
        seed_baseline(STATE_PATH, {r["DocID"] for r in ptr_rows},
                      f"PTR(s) for {', '.join(str(y) for y in years)}")
        return

    seen = load_seen_doc_ids(STATE_PATH)
    new_rows = [r for r in ptr_rows if r["DocID"] not in seen]
    backlog = max(0, len(new_rows) - args.max_alerts)
    if backlog:
        log(f"{len(new_rows)} new PTR(s) exceeds --max-alerts={args.max_alerts}; "
            f"posting {args.max_alerts} now, leaving {backlog} for the next run.")
        new_rows = new_rows[: args.max_alerts]
    if args.verbose:
        print(f"-- {len(new_rows)} new PTR(s) to alert on --")

    sent = 0
    skipped = 0
    for row in new_rows:
        doc_id = row["DocID"]
        try:
            pdf_text = fetch_ptr_text(row["Year"], doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = []

        alertable = transactions if args.all_assets else equity_transactions(transactions)
        # A filing we read fine that turned out to be all bonds and funds is handled, not
        # failed: record it seen and say nothing. The unreadable case is the one that
        # still alerts below, because "no stock trades" and "we could not tell" are
        # different facts and only one of them is safe to keep quiet about.
        if transactions and not alertable:
            skipped += 1
            if args.verbose or args.dry_run:
                print(f"{doc_id}: {plural(len(transactions), 'transaction')}, "
                      f"none of them stocks — skipped")
            if not args.dry_run:
                seen.add(doc_id)
                save_seen_doc_ids(STATE_PATH, seen)
            continue

        message = build_message(row, alertable)
        if args.verbose or args.dry_run:
            print(message)
            print("---")
        if args.dry_run:
            sent += 1
            continue
        try:
            send_discord_message(webhook_url, message)
            sent += 1
        except requests.RequestException as exc:
            print(f"Failed to send Discord message for {doc_id}: {exc}", file=sys.stderr)
            continue  # don't mark as seen if we couldn't alert
        seen.add(doc_id)
        save_seen_doc_ids(STATE_PATH, seen)  # persist per alert so a crash can't resend

    verb = "Would send" if args.dry_run else "Sent"
    log(f"{verb} {sent} new PTR alert(s) out of {len(new_rows)} new filing(s)."
        + (f" {skipped} had no stock transactions." if skipped else "")
        + (f" {backlog} deferred." if backlog else ""))


if __name__ == "__main__":
    main()
