#!/usr/bin/env python3
"""Monitor new House PTR (Periodic Transaction Report) filings and post them to Slack."""

import argparse
import sys
from datetime import datetime, timezone

import requests

from house_disclosures import (
    PDF_URL_TMPL,
    PTR_FILING_TYPE,
    describe_asset,
    fetch_index_years,
    fetch_ptr_text,
    filing_delay_days,
    filing_sort_key,
    format_filer_name,
    index_years,
    is_late_filing,
    parse_transactions,
)
from monitor_common import (
    STATE_DIR,
    add_common_args,
    load_seen_doc_ids,
    load_webhook_url,
    log,
    save_seen_doc_ids,
    seed_baseline,
    send_slack_message,
)

STATE_PATH = STATE_DIR / "seen_docids.json"


def build_message(row: dict, transactions: list[dict]) -> str:
    filing_date = row.get("FilingDate", "?")
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    lines = [
        ":ballot_box: *New PTR Filing*",
        f"*{format_filer_name(row)}* ({row.get('StateDst', '?')})",
        f"Filed: {filing_date}",
        pdf_link,
    ]
    if transactions:
        lines.append("")
        for t in transactions:
            late = ""
            if is_late_filing(t["txn_date"], filing_date):
                delay = filing_delay_days(t["txn_date"], filing_date)
                late = f" :rotating_light: LATE ({delay} days)"
            partial = " partial" if t["partial"] else ""
            lines.append(
                f"- [{t['owner']}] {t['type']}{partial} -- {describe_asset(t)} -- "
                f"{t['txn_date']} -- {t['amount']}{late}"
            )
    else:
        lines.append("_(could not auto-parse transaction details -- see PDF)_")
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
    for row in new_rows:
        doc_id = row["DocID"]
        try:
            pdf_text = fetch_ptr_text(row["Year"], doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = []

        message = build_message(row, transactions)
        if args.verbose or args.dry_run:
            print(message)
            print("---")
        if args.dry_run:
            sent += 1
            continue
        try:
            send_slack_message(webhook_url, message)
            sent += 1
        except requests.RequestException as exc:
            print(f"Failed to send Slack message for {doc_id}: {exc}", file=sys.stderr)
            continue  # don't mark as seen if we couldn't alert
        seen.add(doc_id)
        save_seen_doc_ids(STATE_PATH, seen)  # persist per alert so a crash can't resend

    verb = "Would send" if args.dry_run else "Sent"
    log(f"{verb} {sent} new PTR alert(s) out of {len(new_rows)} new filing(s)."
        + (f" {backlog} deferred." if backlog else ""))


if __name__ == "__main__":
    main()
