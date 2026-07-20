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
    fetch_index,
    fetch_ptr_text,
    parse_transactions,
)

REPO_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = REPO_DIR / "watchlist.json"
STATE_PATH = REPO_DIR / "state" / "watchlist_seen_docids.json"
CONFIG_PATH = Path.home() / ".config" / "fare-monitor" / "config.json"  # webhook shared with fare-monitor


def load_webhook_url() -> str:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing config file: {CONFIG_PATH}")
    config = json.loads(CONFIG_PATH.read_text())
    url = config.get("webhook_url")
    if not url:
        sys.exit(f"No 'webhook_url' key in {CONFIG_PATH}")
    return url


def load_seen_doc_ids() -> set[str]:
    if not STATE_PATH.exists():
        return set()
    return set(json.loads(STATE_PATH.read_text()))


def save_seen_doc_ids(doc_ids: set[str]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(sorted(doc_ids)))


def send_slack_message(webhook_url: str, text: str) -> None:
    resp = requests.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def build_message(member: dict, row: dict, buys: list[dict]) -> str:
    year = row["Year"]
    doc_id = row["DocID"]
    pdf_link = PDF_URL_TMPL.format(year=year, doc_id=doc_id)
    lines = [
        ":rotating_light: *Watchlist Buy Alert*",
        f"*{member['member']}* ({member['state_dist']}) -- top-10 performer "
        f"(+{member['return_pct_estimate']:.1f}% est. return) just filed a new purchase:",
        "",
    ]
    for t in buys:
        ticker = f" ({t['ticker']})" if t["ticker"] else ""
        lines.append(f"- [{t['owner']}] {t['asset']}{ticker} -- {t['txn_date']} -- {t['amount']}")
    lines.append(pdf_link)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not WATCHLIST_PATH.exists():
        sys.exit(f"Missing {WATCHLIST_PATH} -- run build_watchlist.py first")
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    watched_by_key = {(m["last"], m["first"], m["state_dist"]): m for m in watchlist["members"]}

    webhook_url = load_webhook_url()
    year = datetime.now(timezone.utc).year
    now = datetime.now(timezone.utc)

    rows = fetch_index(year)
    ptr_rows = [r for r in rows if r.get("FilingType") == PTR_FILING_TYPE]
    watched_rows = [
        r for r in ptr_rows
        if (r.get("Last", ""), r.get("First", ""), r.get("StateDst", "")) in watched_by_key
    ]
    if args.verbose:
        print(f"-- {len(ptr_rows)} total PTRs, {len(watched_rows)} from watchlist members --")

    first_run = not STATE_PATH.exists()
    seen = load_seen_doc_ids()

    if first_run:
        all_ids = {r["DocID"] for r in watched_rows}
        save_seen_doc_ids(all_ids)
        print(f"[{now.isoformat()}] First run: seeded baseline with {len(all_ids)} existing "
              f"watchlist PTR(s), no alerts sent.")
        return

    new_rows = [r for r in watched_rows if r["DocID"] not in seen]
    if args.verbose:
        print(f"-- {len(new_rows)} new watchlist PTR(s) --")

    sent = 0
    for row in new_rows:
        doc_id = row["DocID"]
        key = (row.get("Last", ""), row.get("First", ""), row.get("StateDst", ""))
        member = watched_by_key[key]
        try:
            pdf_text = fetch_ptr_text(year, doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"[{now.isoformat()}] Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = []

        buys = [t for t in transactions if t["type"] == "Purchase"]
        if buys:
            message = build_message(member, row, buys)
            if args.verbose:
                print(message)
                print("---")
            try:
                send_slack_message(webhook_url, message)
                sent += 1
            except requests.RequestException as exc:
                print(f"[{now.isoformat()}] Failed to send Slack message for {doc_id}: {exc}", file=sys.stderr)
                continue  # don't mark as seen if we couldn't alert
        seen.add(doc_id)
        save_seen_doc_ids(seen)

    print(f"[{now.isoformat()}] Sent {sent} buy alert(s) out of {len(new_rows)} new watchlist filing(s).")


if __name__ == "__main__":
    main()
