"""Shared fetch/parse logic for House Clerk financial disclosure (PTR) data.

Data source: the House Clerk's public bulk disclosure archive
(disclosures-clerk.house.gov). No auth, no bot-blocking, ToS-safe.
Covers House only -- the Senate's eFD site prohibits bulk/automated access.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

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

# The trailing window, in years, that "recent performance" means: a ranking is only
# useful if it covers long enough for picks to play out, and stale enough members to
# drop off. Defined on the TRANSACTION date, not the filing year -- see
# performance_index_years.
PERFORMANCE_WINDOW_YEARS = 3

# Extracted PDF text, cached on disk and keyed by the DocID it came from. A published
# filing never changes, and the performance window re-reads a few thousand PTRs on every
# rebuild; without the cache that is a few thousand downloads plus pdftotext runs a day
# against the Clerk's site for text that cannot have moved.
PTR_TEXT_CACHE_DIR = Path(__file__).resolve().parent / "cache" / "ptr_text"
_SAFE_DOC_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")

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
# The two directions a position can move in. `Exchange` is a reshuffle inside a holding
# -- a fund share class swap, a merger's share conversion -- with no side and no price
# to score, so it is never a buy or a sell.
TRADE_TXN_TYPES = ("Purchase", "Sale")

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


def is_equity_transaction(txn: dict) -> bool:
    """True if a transaction is a stock (or a stock option) rather than a bond, fund,
    crypto or any of the other things a PTR discloses.

    The filing's own asset-type code decides it. Where the PDF yielded no code -- it
    wraps onto a continuation line often enough to matter -- a printed ticker stands in:
    an untyped row with a ticker in parens is nearly always an equity, while an untyped
    row without one is far more often a Treasury note or a fund whose name simply has no
    ticker to print."""
    asset_type = txn.get("asset_type")
    if asset_type is None:
        return bool(txn.get("ticker"))
    return asset_type in EQUITY_ASSET_TYPES


def equity_transactions(transactions: list[dict]) -> list[dict]:
    """The stock trades in a filing. Members hold Treasuries, municipal bonds and target
    date funds in volume, and those rows drown the equity trades that are the point of
    watching at all."""
    return [t for t in transactions if is_equity_transaction(t)]


def trade_transactions(transactions: list[dict]) -> list[dict]:
    """The buy/sell rows in a filing. A transaction that is neither is disclosed in the
    same table but says nothing about a pick: an Exchange has no direction, and the
    leaderboard, the digest and the buy alerts all read this rather than the raw table
    so "a trade" means the same thing in all three."""
    return [t for t in transactions if t.get("type") in TRADE_TXN_TYPES]


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


def performance_window_start(now: datetime, years: int = PERFORMANCE_WINDOW_YEARS) -> datetime:
    """Midnight `years` years before `now`, naive.

    Naive and floored to the day because it is compared against transaction dates
    parsed out of the filings, which carry neither a time nor a zone.
    """
    anchor = now.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    try:
        return anchor.replace(year=anchor.year - years)
    except ValueError:  # Feb 29 -> the target year isn't a leap year
        return anchor.replace(year=anchor.year - years, day=28)


def performance_index_years(now: datetime,
                            years: int = PERFORMANCE_WINDOW_YEARS) -> list[int]:
    """The index years that can hold a transaction inside the trailing window.

    A filing lands in the archive for the year it was *filed*, which is never earlier
    than the year the trade happened, so covering trades back to the window's start
    means reading every archive from that year forward. Newest first, so where the same
    DocID appears twice the fresher index row wins.
    """
    start = performance_window_start(now, years)
    return list(range(now.year, start.year - 1, -1))


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


def _cached_text_path(year: int | str, doc_id: str) -> Path | None:
    """Where this filing's extracted text lives, or None if the DocID cannot be trusted
    as a filename -- the index is fetched over the network, so it is not our string."""
    if not _SAFE_DOC_ID_RE.match(str(doc_id)) or not _SAFE_DOC_ID_RE.match(str(year)):
        return None
    return PTR_TEXT_CACHE_DIR / str(year) / f"{doc_id}.txt"


def _write_cached_text(path: Path, text: str) -> None:
    """Publish cached text atomically. build_leaderboard fetches from a thread pool and
    several omp/cron runs can overlap, so a half-written file must never be readable."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp",
                                         delete=False) as f:
            f.write(text)
            tmp = f.name
        os.replace(tmp, path)
    except OSError:
        pass  # a cache that cannot be written is a slow run, not a failed one


def fetch_ptr_text(year: int | str, doc_id: str, use_cache: bool = True) -> str | None:
    """The filing's PDF as `pdftotext -layout` text, cached on disk by DocID.

    A 404 (the Clerk publishes index rows slightly ahead of some PDFs), a fetch error or
    a failed extraction returns None and is never cached, so it is retried next run --
    caching it would make a transient failure permanent. A scanned paper filing *is*
    cached: its text is only page breaks (see scanned_page_count), and a scan never
    gains a text layer, so re-downloading it every run would buy nothing.
    """
    if shutil.which("pdftotext") is None:
        sys.exit("pdftotext not found -- install poppler-utils (apt install poppler-utils)")
    cache_path = _cached_text_path(year, doc_id) if use_cache else None
    if cache_path is not None:
        try:
            cached = cache_path.read_text()
        except OSError:
            cached = ""
        if cached:
            return cached
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
    if result.returncode != 0:
        return None
    if cache_path is not None and result.stdout:
        _write_cached_text(cache_path, result.stdout)
    return result.stdout


def scanned_page_count(pdf_text: str | None) -> int:
    """Page count of a PTR filed on paper and scanned, or 0 if it was not.

    About one PTR in seven -- every DocID starting 8 or 9 -- is a scan: an image per
    page with no text layer, so pdftotext succeeds and emits nothing but one form feed
    per page. That is a permanent property of the document, not the publish gap a
    missing PDF is, so callers must say so at once rather than wait for contents that
    will never appear. Missing text (None or "") is the publish gap and returns 0.
    """
    if not pdf_text or pdf_text.strip():
        return 0
    return max(pdf_text.count("\f"), 1)


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


# Titles, honorifics and post-nominals the index sprays across its name fields. They are
# not part of anybody's identity, and which of them appear changes from archive to
# archive for the same filer.
_NAME_NOISE_TOKENS = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "hon", "rep", "sen", "the", "honorable",
    "jr", "sr", "ii", "iii", "iv", "md", "do", "dds", "dmd", "phd", "facs", "cpa", "esq",
})


def normalize_name_part(value: str) -> str:
    """A name field reduced to what identifies the person, lowercase.

    The index's Last/First fields are not stable between archives. The same filer is
    `Greene / Marjorie Taylor` in one year and `Greene / Marjorie Taylor Mrs` in the
    next; `Franklin / C. Scott` and `Franklin / Scott Scott`; `Dunn, MD, FACS / Neal
    Patrick` and `Dunn / Neal Patrick MD, Facs`. Reading one year at a time never showed
    it -- reading three splits a member into two rows, halves each half's trade history
    and spends two watchlist slots on one person.
    """
    tokens = (t.strip(".,").lower() for t in value.replace(",", " ").split())
    return " ".join(t for t in tokens if t and t not in _NAME_NOISE_TOKENS)


def member_key(row: dict) -> tuple[str, str]:
    """Stable identity for a filer across archives: normalized surname plus district.

    The first name is deliberately not part of it -- `C. Scott` and `Scott Scott` are
    the same representative, and no normalization of those two agrees. The district is
    what keeps two same-surname members apart, and the House seats one member per
    district, so the pair is unique among sitting filers. The cost is that a member
    succeeded in their own district by someone of the same surname would merge; the
    alternative cost was Marjorie Taylor Greene ranking twice on half a record each.
    """
    return (normalize_name_part(row.get("Last", "")), row.get("StateDst", ""))
