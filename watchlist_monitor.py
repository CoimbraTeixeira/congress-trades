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
    describe_asset,
    fetch_index_years,
    fetch_ptr_text,
    filing_delay_days,
    filing_sort_key,
    index_years,
    is_late_filing,
    member_key,
    parse_date,
    parse_transactions,
)
from monitor_common import (
    DEFAULT_BASELINE_WINDOW_DAYS,
    STATE_DIR,
    add_common_args,
    baseline_docids,
    load_watch_state,
    load_webhook_url,
    log,
    save_watch_state,
    send_slack_message,
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


def build_message(member: dict, row: dict, buys: list[dict]) -> str:
    filing_date = row.get("FilingDate", "?")
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    headline = "just filed a new purchase:" if buys else (
        "just filed a new PTR -- transaction details could not be auto-parsed:"
    )
    lines = [
        ":rotating_light: *Watchlist Buy Alert*",
        f"*{member['member']}* ({member['state_dist']}) -- watchlist #{member['rank']} "
        f"({member['return_pct_estimate']:+.1f}% est. return) {headline}",
        "",
    ]
    for t, count in collapse_buys(buys):
        partial = " partial" if t["partial"] else ""
        repeat = f" x{count}" if count > 1 else ""
        late = ""
        if is_late_filing(t["txn_date"], filing_date):
            late = f" :rotating_light: LATE ({filing_delay_days(t['txn_date'], filing_date)} days)"
        lines.append(
            f"- [{t['owner']}]{partial} {describe_asset(t)}{repeat} -- "
            f"{t['txn_date']} -- {t['amount']}{late}"
        )
    if not buys:
        lines.append("_(see the PDF)_")
    lines += ["", f"Filed: {filing_date}", pdf_link]
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
            message = build_message(member, row, buys)
            if args.verbose or args.dry_run:
                print(message)
                print("---")
            if not args.dry_run:
                try:
                    send_slack_message(webhook_url, message)
                except requests.RequestException as exc:
                    print(f"Failed to send Slack message for {doc_id}: {exc}", file=sys.stderr)
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
