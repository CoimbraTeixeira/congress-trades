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
    fetch_index,
    fetch_ptr_text,
    member_key,
    parse_transactions,
)
from monitor_common import (
    STATE_DIR,
    add_common_args,
    load_watch_state,
    load_webhook_url,
    log,
    save_watch_state,
    send_slack_message,
)

REPO_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = REPO_DIR / "watchlist.json"
STATE_PATH = STATE_DIR / "watchlist_seen_docids.json"


def build_message(member: dict, row: dict, buys: list[dict]) -> str:
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    lines = [
        ":rotating_light: *Watchlist Buy Alert*",
        f"*{member['member']}* ({member['state_dist']}) -- top-10 performer "
        f"({member['return_pct_estimate']:+.1f}% est. return) just filed a new purchase:",
        "",
    ]
    for t in buys:
        partial = " partial" if t["partial"] else ""
        lines.append(
            f"- [{t['owner']}]{partial} {describe_asset(t)} -- {t['txn_date']} -- {t['amount']}"
        )
    lines.append(pdf_link)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    args = parser.parse_args()

    if not WATCHLIST_PATH.exists():
        sys.exit(f"Missing {WATCHLIST_PATH} -- run build_watchlist.py first")
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    watched_by_key = {
        (m["last"], m["first"], m["state_dist"]): m for m in watchlist["members"]
    }

    webhook_url = None if args.dry_run else load_webhook_url()
    year = datetime.now(timezone.utc).year

    rows = fetch_index(year)
    watched_rows = [
        r for r in rows
        if r.get("FilingType") == PTR_FILING_TYPE and member_key(r) in watched_by_key
    ]
    if args.verbose:
        print(f"-- {len(rows)} filings, {len(watched_rows)} PTRs from watchlist members --")

    first_run = not STATE_PATH.exists()
    seen, baselined = load_watch_state(STATE_PATH)

    # Baseline any member we haven't tracked before -- on the very first run that's
    # everyone, and later it's whoever the latest build_watchlist.py promoted. Without
    # this, every watchlist rotation replays the new member's back catalogue as alerts.
    new_members = set(watched_by_key) - baselined
    if new_members:
        backfill = {r["DocID"] for r in watched_rows if member_key(r) in new_members}
        seen |= backfill
        baselined |= new_members
        if not args.dry_run:
            save_watch_state(STATE_PATH, seen, baselined)
        label = "watchlist member(s)" if first_run else "newly promoted member(s)"
        log(f"Baselined {len(backfill)} existing PTR(s) for {len(new_members)} "
            f"{label}, no alerts sent for those.")
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
            transactions = []

        buys = [t for t in transactions if t["type"] == "Purchase"]
        if buys:
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
            save_watch_state(STATE_PATH, seen, baselined)

    verb = "Would send" if args.dry_run else "Sent"
    log(f"{verb} {sent} buy alert(s) out of {len(new_rows)} new watchlist filing(s)."
        + (f" {backlog} deferred." if backlog else ""))


if __name__ == "__main__":
    main()
