#!/usr/bin/env python3
"""Monitor new House PTR (Periodic Transaction Report) filings and post them to Slack."""

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
    format_filer_name,
    is_late_filing,
    parse_transactions,
)

REPO_DIR = Path(__file__).resolve().parent
STATE_PATH = REPO_DIR / "state" / "seen_docids.json"
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


def build_message(row: dict, transactions: list[dict]) -> str:
    year = row["Year"]
    doc_id = row["DocID"]
    pdf_link = PDF_URL_TMPL.format(year=year, doc_id=doc_id)
    lines = [
        ":ballot_box: *New PTR Filing*",
        f"*{format_filer_name(row)}* ({row.get('StateDst', '?')})",
        f"Filed: {row.get('FilingDate', '?')}",
        pdf_link,
    ]
    if transactions:
        lines.append("")
        for t in transactions:
            late = " :rotating_light: LATE (>45 days)" if is_late_filing(t["txn_date"], t["notif_date"]) else ""
            ticker = f" ({t['ticker']})" if t["ticker"] else ""
            lines.append(
                f"- [{t['owner']}] {t['type']}{ticker} -- {t['asset']} -- "
                f"{t['txn_date']} -> notified {t['notif_date']} -- {t['amount']}{late}"
            )
    else:
        lines.append("_(could not auto-parse transaction details -- see PDF)_")
    return "\n".join(lines)


def send_slack_message(webhook_url: str, text: str) -> None:
    resp = requests.post(webhook_url, json={"text": text}, timeout=15)
    resp.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true", help="Print per-filing debug info")
    args = parser.parse_args()

    webhook_url = load_webhook_url()
    year = datetime.now(timezone.utc).year
    now = datetime.now(timezone.utc)

    rows = fetch_index(year)
    ptr_rows = [r for r in rows if r.get("FilingType") == PTR_FILING_TYPE]
    if args.verbose:
        print(f"-- {len(rows)} total filings, {len(ptr_rows)} PTRs for {year} --")

    first_run = not STATE_PATH.exists()
    seen = load_seen_doc_ids()

    if first_run:
        # Baseline run: record everything that already exists without alerting on
        # the historical backlog. Only filings that show up in future runs are new.
        all_ids = {r["DocID"] for r in ptr_rows}
        save_seen_doc_ids(all_ids)
        print(f"[{now.isoformat()}] First run: seeded baseline with {len(all_ids)} existing "
              f"PTR(s), no alerts sent. Future runs will alert on genuinely new filings.")
        return

    new_rows = [r for r in ptr_rows if r["DocID"] not in seen]
    if args.verbose:
        print(f"-- {len(new_rows)} new PTR(s) not yet seen --")

    sent = 0
    for row in new_rows:
        doc_id = row["DocID"]
        try:
            pdf_text = fetch_ptr_text(year, doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"[{now.isoformat()}] Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = []

        message = build_message(row, transactions)
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
        save_seen_doc_ids(seen)  # persist immediately so a crash mid-run can't cause resends

    print(f"[{now.isoformat()}] Sent {sent} new PTR alert(s) out of {len(new_rows)} new filing(s).")


if __name__ == "__main__":
    main()
