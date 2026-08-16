#!/usr/bin/env python3
"""Daily check: did anyone on the top-10 watchlist (watchlist.json) file a new
PTR containing a Purchase? Alerts fast on buys specifically, separate from the
general congress_monitor.py (which alerts on every new PTR, any type)."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from house_disclosures import (
    PDF_URL_TMPL,
    PTR_FILING_TYPE,
    fetch_index_years,
    fetch_ptr_text,
    filing_sort_key,
    index_years,
    member_key,
    parse_transactions,
)
from monitor_common import (
    DEFAULT_BASELINE_WINDOW_DAYS,
    LATE_LEGEND,
    STATE_DIR,
    add_common_args,
    any_late,
    baseline_docids,
    escape_markdown,
    format_transaction,
    load_watch_state,
    load_webhook_url,
    log,
    plural,
    save_watch_state,
    send_discord_message,
)

REPO_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = REPO_DIR / "watchlist.json"
STATE_PATH = STATE_DIR / "watchlist_seen_docids.json"


def collapse_buys(buys: list[dict]) -> list[tuple[dict, int]]:
    """Group identical purchase rows into (transaction, count) pairs. A filing routinely
    discloses the same ticker, date and amount range several times over (one row per
    account), which renders as a wall of duplicate lines otherwise."""
    grouped: dict[tuple, list] = {}
    for t in buys:
        key = (t["owner"], t["asset"], t["txn_date"], t["amount"], t["partial"])
        grouped.setdefault(key, [t, 0])[1] += 1
    return [(t, n) for t, n in grouped.values()]


def build_message(member: dict, row: dict, buys: list[dict], watchlist_size: int) -> str:
    filing_date = row.get("FilingDate", "?")
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    name = escape_markdown(member["member"])
    collapsed = collapse_buys(buys)

    lines = [
        f"🚨 **{name}** ({member['state_dist']}) — watchlist buy",
        # Rank alone ("watchlist #3") never said out of how many, or what the ranking
        # measures; both fit in the line that establishes why this member is watched.
        f"#{member['rank']} of {watchlist_size} on the watchlist · "
        f"{member['return_pct_estimate']:+.1f}% estimated return",
    ]
    if not buys:
        # Silence here would read as "this filing had no purchases", which is the one
        # thing it does not mean.
        lines += [
            f"Filed {filing_date} · transactions could not be read from the PDF, so "
            f"this may or may not contain a buy",
            pdf_link,
        ]
        return "\n".join(lines)

    # Count the disclosed rows, not the collapsed lines -- the ×N on a line is what
    # reconciles the two, so a header saying "3 purchases" above a "×2" would not add up.
    lines += [f"Filed {filing_date} · {plural(len(buys), 'purchase')}", ""]
    lines += [format_transaction(t, filing_date, show_direction=False, count=n)
              for t, n in collapsed]
    lines.append("")
    if any_late(buys, filing_date):
        lines.append(LATE_LEGEND)
    lines.append(f"Full PTR: {pdf_link}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument(
        "--baseline-window", type=int, default=DEFAULT_BASELINE_WINDOW_DAYS,
        help=f"When a member joins the watchlist, still alert on filings from the last "
             f"N days (default {DEFAULT_BASELINE_WINDOW_DAYS}); 0 keeps only same-day ones")
    args = parser.parse_args()

    if not WATCHLIST_PATH.exists():
        sys.exit(f"Missing {WATCHLIST_PATH} -- run build_watchlist.py first")
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    watched_by_key = {
        (m["last"], m["first"], m["state_dist"]): dict(m, rank=rank)
        for rank, m in enumerate(watchlist["members"], start=1)
    }

    webhook_url = None if args.dry_run else load_webhook_url()
    now = datetime.now(timezone.utc)

    rows = fetch_index_years(index_years(now))
    watched_rows = sorted(
        (r for r in rows
         if r.get("FilingType") == PTR_FILING_TYPE and member_key(r) in watched_by_key),
        key=filing_sort_key,
    )
    if args.verbose:
        print(f"-- {len(rows)} filings, {len(watched_rows)} PTRs from watchlist members --")

    first_run = not STATE_PATH.exists()
    seen, previously_watched = load_watch_state(STATE_PATH)
    watched_members = set(watched_by_key)

    # Baseline any member who wasn't on the watchlist last run -- on the very first run
    # that's everyone, and later it's whoever the latest build_watchlist.py promoted.
    # Without this, every rotation replays the new member's back catalogue as alerts.
    # Their last few days stay alertable though: a member is promoted precisely because
    # they trade well, and suppressing the trade that earned them the slot is backwards.
    #
    # State holds last run's membership rather than every member ever seen, so someone
    # who drops off and is promoted again months later is re-baselined instead of
    # replaying their whole absence. build_watchlist.py runs daily, so that happens.
    newly_watched = watched_members - previously_watched
    if newly_watched:
        stale = baseline_docids(watched_rows, newly_watched, now, args.baseline_window,
                                apply_window=not first_run)
        seen |= stale
        label = "watchlist member(s)" if first_run else "newly promoted member(s)"
        log(f"Baselined {len(stale)} existing PTR(s) for {len(newly_watched)} "
            f"{label}, no alerts sent for those.")
    if watched_members != previously_watched and not args.dry_run:
        save_watch_state(STATE_PATH, seen, watched_members)
    if first_run:
        return

    new_rows = [r for r in watched_rows if r["DocID"] not in seen]
    backlog = max(0, len(new_rows) - args.max_alerts)
    if backlog:
        log(f"{len(new_rows)} new watchlist PTR(s) exceeds --max-alerts={args.max_alerts}; "
            f"handling {args.max_alerts} now, leaving {backlog} for the next run.")
        new_rows = new_rows[: args.max_alerts]
    if args.verbose:
        print(f"-- {len(new_rows)} new watchlist PTR(s) --")

    sent = 0
    for row in new_rows:
        doc_id = row["DocID"]
        member = watched_by_key[member_key(row)]
        try:
            pdf_text = fetch_ptr_text(row["Year"], doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = None

        buys = [t for t in transactions if t["type"] == "Purchase"] if transactions else []
        # A filing we couldn't read is not a filing without purchases. Staying silent on
        # it is indistinguishable from "nothing happened", so send it with a link instead
        # and let the reader open the PDF.
        if buys or not transactions:
            message = build_message(member, row, buys, len(watched_by_key))
            if args.verbose or args.dry_run:
                print(message)
                print("---")
            if not args.dry_run:
                try:
                    send_discord_message(webhook_url, message)
                except requests.RequestException as exc:
                    print(f"Failed to send Discord message for {doc_id}: {exc}", file=sys.stderr)
                    continue  # don't mark as seen if we couldn't alert
            sent += 1
        if not args.dry_run:
            seen.add(doc_id)
            save_watch_state(STATE_PATH, seen, watched_members)

    verb = "Would send" if args.dry_run else "Sent"
    log(f"{verb} {sent} buy alert(s) out of {len(new_rows)} new watchlist filing(s)."
        + (f" {backlog} deferred." if backlog else ""))


if __name__ == "__main__":
    main()
