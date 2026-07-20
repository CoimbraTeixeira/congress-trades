"""Shared fetch/parse logic for House Clerk financial disclosure (PTR) data.

Data source: the House Clerk's public bulk disclosure archive
(disclosures-clerk.house.gov). No auth, no bot-blocking, ToS-safe.
Covers House only -- the Senate's eFD site prohibits bulk/automated access.
"""

import re
import subprocess
import tempfile
import zipfile
from datetime import datetime
from io import BytesIO

import requests

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
TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")

AMOUNT_RANGE_RE = re.compile(r"\$([\d,]+)\s*-\s*\$([\d,]+)")
AMOUNT_SINGLE_RE = re.compile(r"\$([\d,]+)")

_NAME_CLEAN_PATTERNS = [
    (re.compile(r"\s*-\s*Common Stock\s*", re.I), " "),
    (re.compile(r"\bCommon Stock\b", re.I), " "),
    (re.compile(r"\bCommon\b\.?", re.I), " "),
    (re.compile(r"\bClass [A-Z]\b.*$", re.I), " "),
    (re.compile(r"\bOrdinary Shares\b", re.I), " "),
]
_ticker_by_name_cache: dict[str, str | None] = {}


def clean_asset_name(name: str) -> str:
    for pattern, repl in _NAME_CLEAN_PATTERNS:
        name = pattern.sub(repl, name)
    return name.strip(" ,.")


def resolve_ticker_by_name(asset_name: str) -> str | None:
    """Best-effort company-name -> US equity ticker lookup via Yahoo's search endpoint.
    Used as a fallback when the disclosure doesn't print a ticker in parens (common --
    many filers just write the company name). Bonds/funds/etc. correctly resolve to
    nothing. Results are cached in-process since the same names repeat across filings."""
    cleaned = clean_asset_name(asset_name)
    if cleaned in _ticker_by_name_cache:
        return _ticker_by_name_cache[cleaned]

    ticker = None
    try:
        resp = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/search",
            params={"q": cleaned, "quotesCount": 5, "newsCount": 0},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        resp.raise_for_status()
        quotes = [q for q in resp.json().get("quotes", []) if q.get("quoteType") == "EQUITY"]
        # Prefer a plain US ticker (no exchange-suffix dot) over foreign listings.
        us_quotes = [q for q in quotes if "." not in q.get("symbol", "")]
        pick = (us_quotes or quotes or [None])[0]
        ticker = pick.get("symbol") if pick else None
    except Exception:
        ticker = None

    _ticker_by_name_cache[cleaned] = ticker
    return ticker


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
    callers should fall back to the index metadata alone (e.g. name/date/PDF link)."""
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


def amount_midpoint(amount: str) -> float | None:
    """Convert a disclosed amount string ("$15,001 - $50,000" or "$1,001") to a
    representative dollar figure -- the midpoint of the range, or the value itself."""
    m = AMOUNT_RANGE_RE.search(amount)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        return (lo + hi) / 2
    m = AMOUNT_SINGLE_RE.search(amount)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


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
