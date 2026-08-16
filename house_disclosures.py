"""Shared fetch/parse logic for House Clerk financial disclosure (PTR) data.

Data source: the House Clerk's public bulk disclosure archive
(disclosures-clerk.house.gov). No auth, no bot-blocking, ToS-safe.
Covers House only -- the Senate's eFD site prohibits bulk/automated access.
"""

import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime
from io import BytesIO

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

HEADERS = {"User-Agent": "congress-trade-monitor/1.0"}
INDEX_URL_TMPL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PDF_URL_TMPL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
PTR_FILING_TYPE = "P"

# Filers must report within 45 days of the transaction (STOCK Act, 5 U.S.C. app. 101).
LATE_FILING_DAYS = 45

# How far into a new year to keep reading the previous year's index (see index_years).
INDEX_OVERLAP_DAYS = 60

# House asset-type codes (fd.house.gov/reference/asset-type-codes.aspx) that denote
# something with a US equity ticker. Everything else -- GS (government securities),
# CT (crypto), OT (other funds), HN (hedge notes) -- must not be fed to the
# company-name ticker lookup, which would happily match "US Treasury Note" to a stock.
EQUITY_ASSET_TYPES = {"ST", "OP"}


def _make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update(HEADERS)
    return session


_thread_local = threading.local()


def _session() -> requests.Session:
    """One session per thread -- build_leaderboard fetches PDFs from a thread pool and
    a single Session's connection pool is not safe to share across them."""
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = _make_session()
        _thread_local.session = session
    return session

# --- Transaction table parsing -------------------------------------------------
#
# `pdftotext -layout` renders each transaction as a block whose first line carries
# the transaction type and the two dates, e.g.:
#
#     SP    American Water Works Company,     S    01/14/2026 02/04/2026    $50,001 -
#           Inc. Common Stock (AWK) [ST]                                    $100,000
#
# The asset name, its ticker, its asset-type code and the range's upper bound all
# routinely wrap onto continuation lines, so a transaction has to be read as a
# block rather than a line. The date pair is the only reliably positioned anchor.

DATE_PAIR_RE = re.compile(
    r"(?P<txn_date>\d{1,2}/\d{1,2}/\d{4})\s+(?P<notif_date>\d{1,2}/\d{1,2}/\d{4})"
)
# Everything left of the dates: optional row id, optional owner code, asset, type.
HEAD_RE = re.compile(
    r"^\s*(?:\d+\s+)?"
    r"(?:(?P<owner>SP|DC|JT)\s+)?"
    r"(?P<asset>.*?)\s+"
    r"(?P<ttype>[PSE])(?P<partial>\s*\(partial\))?\s*$"
)
ASSET_TYPE_RE = re.compile(r"\[(?P<code>[A-Z]{2})\]")
AMOUNT_TOKEN_RE = re.compile(r"\$[\d,]+")
# Sub-field lines below a transaction ("Filing Status: New", "Subholding Of: ...").
# pdftotext drops the small-caps labels, leaving a stray letter before the colon.
FIELD_LINE_RE = re.compile(r"^\s*\S.{0,40}?\s:\s")
TICKER_RE = re.compile(r"\(([A-Z]{1,5}(?:[.\-][A-Z]{1,2})?)\)")
MAX_CONTINUATION_LINES = 4

TXN_TYPES = {"P": "Purchase", "S": "Sale", "E": "Exchange"}

_HEADER_TOKENS = {
    "id", "owner", "asset", "transaction", "type", "notification", "date",
    "amount", "cap.", "gains", ">", "$200?", "filing", "#",
}

AMOUNT_RANGE_RE = re.compile(r"\$([\d,]+)\s*-\s*\$([\d,]+)")
AMOUNT_SINGLE_RE = re.compile(r"\$([\d,]+)")

_NAME_CLEAN_PATTERNS = [
    (re.compile(r"\s*-\s*Common Stock\s*", re.I), " "),
    (re.compile(r"\bCommon Stock\b", re.I), " "),
    (re.compile(r"\bCommon\b\.?", re.I), " "),
    (re.compile(r"\bClass [A-Z]\b.*$", re.I), " "),
    (re.compile(r"\bOrdinary Shares\b", re.I), " "),
    (re.compile(r"\([A-Z]{1,5}(?:[.\-][A-Z]{1,2})?\)"), " "),
]
_ticker_by_name_cache: dict[str, str | None] = {}


def clean_asset_name(name: str) -> str:
    for pattern, repl in _NAME_CLEAN_PATTERNS:
        name = pattern.sub(repl, name)
    return re.sub(r"\s+", " ", name).strip(" ,.-")


def resolve_ticker_by_name(asset_name: str) -> str | None:
    """Best-effort company-name -> US equity ticker lookup via Yahoo's search endpoint.
    Used as a fallback when the disclosure doesn't print a ticker in parens. Callers
    must gate this on an equity asset type -- the endpoint returns a best match for
    anything, so a bond or fund name would silently resolve to an unrelated stock.
    Results are cached in-process since the same names repeat across filings."""
    cleaned = clean_asset_name(asset_name)
    if not cleaned:
        return None
    if cleaned in _ticker_by_name_cache:
        return _ticker_by_name_cache[cleaned]

    ticker = None
    try:
        resp = _session().get(
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


def resolve_ticker(txn: dict) -> str | None:
    """Ticker for a parsed transaction: the one printed in the filing if present,
    otherwise a name lookup -- but only for asset types that are actually equities."""
    if txn.get("ticker"):
        return txn["ticker"]
    if txn.get("asset_type") not in EQUITY_ASSET_TYPES:
        return None
    return resolve_ticker_by_name(txn["asset"])


def fetch_index(year: int) -> list[dict]:
    resp = _session().get(INDEX_URL_TMPL.format(year=year), timeout=30)
    resp.raise_for_status()
    with zipfile.ZipFile(BytesIO(resp.content)) as zf:
        txt_name = next(n for n in zf.namelist() if n.endswith(".txt"))
        raw = zf.read(txt_name).decode("utf-8", errors="replace")

    lines = raw.splitlines()
    header = lines[0].split("\t")
    rows = []
    malformed = 0
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != len(header):
            malformed += 1
            continue
        rows.append(dict(zip(header, fields)))
    if malformed:
        print(f"warning: skipped {malformed} malformed row(s) in the {year} index",
              file=sys.stderr)
    return rows


def index_years(now: datetime) -> list[int]:
    """Index years still worth fetching. Each year is a separate archive, so a PTR
    filed on Dec 30 stays in that year's file -- a monitor that only ever reads the
    current year goes blind to it the moment the calendar rolls over. Keep the
    previous year in scope until this year's filings are well clear of the boundary."""
    years = [now.year]
    if now.timetuple().tm_yday <= INDEX_OVERLAP_DAYS:
        years.append(now.year - 1)
    return years


def fetch_index_years(years: list[int]) -> list[dict]:
    """Merge several years of index rows, first occurrence of a DocID winning."""
    rows: list[dict] = []
    seen: set[str] = set()
    for year in years:
        for row in fetch_index(year):
            doc_id = row.get("DocID", "")
            if doc_id and doc_id not in seen:
                seen.add(doc_id)
                rows.append(row)
    return rows


def filing_sort_key(row: dict) -> tuple:
    """Order index rows oldest-first by filing date, DocID breaking ties. The archive's
    own row order is not chronological, so without this an --max-alerts truncation
    would keep an arbitrary slice rather than the oldest part of the backlog."""
    filed = parse_date(row.get("FilingDate", ""))
    return (filed or datetime.max, row.get("DocID", ""))


def fetch_ptr_text(year: int | str, doc_id: str) -> str | None:
    if shutil.which("pdftotext") is None:
        sys.exit("pdftotext not found -- install poppler-utils (apt install poppler-utils)")
    resp = _session().get(PDF_URL_TMPL.format(year=year, doc_id=doc_id), timeout=30)
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


def _is_page_furniture(line: str) -> bool:
    tokens = line.split()
    if not tokens:
        return False
    if tokens[0] == "Clerk" or line.lstrip().startswith("* For the complete list"):
        return True
    return all(t.lower() in _HEADER_TOKENS or t.lower().startswith("#2") for t in tokens)


def _strip_page_furniture(lines: list[str]) -> list[str]:
    """Drop the transaction-table header that pdftotext repeats at the top of every page,
    along with the blank padding around it.

    A page break lands mid-transaction often enough to matter: without this the record is
    read as ending at the page's trailing blank line, losing its ticker, its asset-type
    code and the upper bound of its amount range. (str.splitlines already swallows the
    form feed itself, so the repeated header is the only marker left to key off.)"""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if _is_page_furniture(lines[i]):
            while out and not out[-1].strip():
                out.pop()  # blank padding belonging to the page break, not the record
            while i < len(lines) and (not lines[i].strip() or _is_page_furniture(lines[i])):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    return out


def parse_transactions(pdf_text: str) -> list[dict]:
    """Extract transaction rows from `pdftotext -layout` output of a PTR.

    Returns [] for filings with no parseable transaction table -- callers should fall
    back to the index metadata alone (e.g. name/date/PDF link)."""
    lines = _strip_page_furniture(pdf_text.splitlines())
    transactions = []
    i = 0
    while i < len(lines):
        dates = DATE_PAIR_RE.search(lines[i])
        if not dates:
            i += 1
            continue
        head = HEAD_RE.match(lines[i][: dates.start()])
        if not head:
            i += 1
            continue

        tail = lines[i][dates.end():]
        asset_parts = [head.group("asset").strip()]
        amounts = AMOUNT_TOKEN_RE.findall(tail)
        over_cap = "+" in tail
        code = ASSET_TYPE_RE.search(lines[i])
        asset_type = code.group("code") if code else None

        j = i + 1
        consumed = 0
        while j < len(lines) and consumed < MAX_CONTINUATION_LINES:
            nxt = lines[j]
            if not nxt.strip() or DATE_PAIR_RE.search(nxt) or FIELD_LINE_RE.match(nxt):
                break
            amounts += AMOUNT_TOKEN_RE.findall(nxt)
            over_cap = over_cap or "+" in nxt
            if asset_type is None:
                code = ASSET_TYPE_RE.search(nxt)
                if code:
                    asset_type = code.group("code")
            fragment = AMOUNT_TOKEN_RE.sub(" ", nxt).strip()
            if fragment:
                asset_parts.append(fragment)
            j += 1
            consumed += 1
            if asset_type is not None and len(amounts) >= 2:
                break

        asset = re.sub(r"\s+", " ", " ".join(asset_parts))
        asset = ASSET_TYPE_RE.sub("", asset).strip(" ,.")

        if len(amounts) >= 2:
            amount = f"{amounts[0]} - {amounts[1]}"
        elif amounts and over_cap:
            amount = f"{amounts[0]} +"
        elif amounts:
            amount = amounts[0]
        else:
            amount = ""

        ticker = TICKER_RE.search(asset)
        transactions.append({
            "owner": head.group("owner") or "self",
            "asset": asset,
            "asset_type": asset_type,
            "ticker": ticker.group(1) if ticker else None,
            "type": TXN_TYPES[head.group("ttype")],
            "partial": bool(head.group("partial")),
            "txn_date": dates.group("txn_date"),
            "notif_date": dates.group("notif_date"),
            "amount": amount,
        })
        i = max(j, i + 1)
    return transactions


def amount_midpoint(amount: str) -> float | None:
    """Convert a disclosed amount string ("$15,001 - $50,000") to a representative
    dollar figure -- the midpoint of the range. Open-ended top-bucket amounts
    ("$1,000,001 +") and lone bounds fall back to the stated value, which
    understates rather than overstates."""
    m = AMOUNT_RANGE_RE.search(amount)
    if m:
        lo = float(m.group(1).replace(",", ""))
        hi = float(m.group(2).replace(",", ""))
        return (lo + hi) / 2
    m = AMOUNT_SINGLE_RE.search(amount)
    if m:
        return float(m.group(1).replace(",", ""))
    return None


def parse_date(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%m/%d/%Y")
    except (ValueError, AttributeError):
        return None


def filing_delay_days(txn_date: str, filing_date: str) -> int | None:
    """Days between the transaction and the day the report was actually filed."""
    txn = parse_date(txn_date)
    filed = parse_date(filing_date)
    if txn is None or filed is None:
        return None
    return (filed - txn).days


def is_late_filing(txn_date: str, filing_date: str) -> bool:
    """True if the report was filed more than 45 days after the transaction.

    Note this compares against the *filing* date from the index, not the
    notification date printed in the PDF -- the notification date is when the filer's
    broker told them about the trade, which says nothing about the filer's own delay.
    """
    delay = filing_delay_days(txn_date, filing_date)
    return delay is not None and delay > LATE_FILING_DAYS


def format_filer_name(row: dict) -> str:
    parts = [row.get("Prefix", ""), row.get("First", ""), row.get("Last", ""), row.get("Suffix", "")]
    return " ".join(p for p in parts if p).strip()


def member_key(row: dict) -> tuple[str, str, str]:
    """Stable identity for a filer across index rows -- raw fields, not display name."""
    return (row.get("Last", ""), row.get("First", ""), row.get("StateDst", ""))
