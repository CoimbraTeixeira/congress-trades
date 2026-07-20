#!/usr/bin/env python3
"""Monitor new House PTR (Periodic Transaction Report) filings and post them to Slack.

Data source: the House Clerk's public bulk disclosure archive
(disclosures-clerk.house.gov). No auth, no bot-blocking, ToS-safe.
Covers House only -- the Senate's eFD site prohibits bulk/automated access.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests

REPO_DIR = Path(__file__).resolve().parent
STATE_PATH = REPO_DIR / "state" / "seen_docids.json"
CONFIG_PATH = Path.home() / ".config" / "fare-monitor" / "config.json"  # webhook shared with fare-monitor

HEADERS = {"User-Agent": "congress-trade-monitor/1.0"}
INDEX_URL_TMPL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL_TMPL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
PTR_FILING_TYPE = "P"

# Matches the first line of a transaction row in `pdftotext -layout` output, e.g.:
#   SP   Intuit Inc. - Common Stock (INTU)   S   06/10/2026 07/07/2026   $15,001 -
LINE1_RE = re.compile(
    r"^\s*(?:(?P<owner>SP|DC|JT)\s+)?"
    r"(?P<asset>.+?)\s+"
    r"(?P<ttype>[PSE])\s+"
    r"(?P<txn_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<notif_date>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amt_start>\$[\d,]+)\s*-?\s*$"
)
# Matches the continuation line with the asset-type code and the range's upper bound, e.g.:
#   [ST]   $50,000
LINE2_RE = re.compile(r"^\s*\[(?P<asset_type>[A-Z]{1,3})\].*?(?P<amt_end>\$[\d,]+)\s*$")
TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)\s*$")


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


def fetch_index(year: int) -> list[dict]:
    resp = requests.get(INDEX_URL_TMPL.format(year=year), headers=HEADERS, timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        txt_name = next(n for n in zf.namelist() if n.endswith(".txt"))
        raw = zf.read(txt_name).decode("utf-8", errors="replace")

    lines = raw.splitlines()
    header = lines[0].split("\t")
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != len(header):
            continue
        rows.append(dict(zip(header, fields)))
    return rows


def fetch_ptr_text(year: int, doc_id: str) -> str | None:
    resp = requests.get(PDF_URL_TMPL.format(year=year, doc_id=doc_id), headers=HEADERS, timeout=30)
    if resp.status_code != 200 or not resp.content:
        return None
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(resp.content)
        f.flush()
        result = subprocess.run(
            ["pdftotext", "-layout", f.name, "-"],
            capture_output=True, text=True, timeout=30,
        )
    return result.stdout if result.returncode == 0 else None


def parse_transactions(pdf_text: str) -> list[dict]:
    """Best-effort extraction of transaction rows. Returns [] on any parsing trouble --
    the caller still alerts using the index metadata alone."""
    transactions = []
    lines = pdf_text.splitlines()
    for i, line in enumerate(lines):
        m1 = LINE1_RE.match(line)
        if not m1:
            continue
        amount = m1.group("amt_start")
        # The upper bound of the range usually lands on the very next line.
        if i + 1 < len(lines):
            m2 = LINE2_RE.match(lines[i + 1])
            if m2:
                amount = f"{m1.group('amt_start')} - {m2.group('amt_end')}"
        ticker_match = TICKER_RE.search(m1.group("asset"))
        transactions.append({
            "owner": m1.group("owner") or "self",
            "asset": m1.group("asset").strip(),
            "ticker": ticker_match.group(1) if ticker_match else None,
            "type": {"P": "Purchase", "S": "Sale", "E": "Exchange"}.get(m1.group("ttype"), m1.group("ttype")),
            "txn_date": m1.group("txn_date"),
            "notif_date": m1.group("notif_date"),
            "amount": amount,
        })
    return transactions


def is_late_filing(txn_date: str, notif_date: str) -> bool:
    try:
        t = datetime.strptime(txn_date, "%m/%d/%Y")
        n = datetime.strptime(notif_date, "%m/%d/%Y")
    except ValueError:
        return False
    return (n - t).days > 45


def format_filer_name(row: dict) -> str:
    parts = [row.get("Prefix", ""), row.get("First", ""), row.get("Last", ""), row.get("Suffix", "")]
    return " ".join(p for p in parts if p).strip()


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
        except (requests.RequestException, subprocess.SubprocessError) as exc:
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
