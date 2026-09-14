"""Shared plumbing for the Discord monitors: webhook config, seen-doc state, delivery,
and the formatting every alert renders its transactions with."""

import fcntl
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from house_disclosures import (
    LATE_FILING_DAYS,
    clean_asset_name,
    filing_delay_days,
    is_late_filing,
    member_key,
    parse_date,
)

REPO_DIR = Path(__file__).resolve().parent
STATE_DIR = REPO_DIR / "state"
CONFIG_PATH = Path.home() / ".config" / "fare-monitor" / "config.json"  # shared with fare-monitor

# A run that would post more than this many alerts is almost certainly a state-file
# problem rather than a real burst of filings. The excess is left unseen so the next
# run picks it up, rather than being silently dropped.
DEFAULT_MAX_ALERTS = 25

# Discord rejects a message body over 2000 characters outright (HTTP 400) rather than
# truncating it, so this is a hard delivery constraint rather than a style preference.
# A PTR disclosing a few dozen transactions clears it easily.
DISCORD_MESSAGE_LIMIT = 2000

# Webhooks are limited to roughly 5 requests per 2 seconds. A backlog run posts in a
# loop, so 429s are expected rather than exceptional; Discord states how long to wait.
RATE_LIMIT_RETRIES = 3
MAX_RETRY_SLEEP = 30.0

# When a member joins a watched set, their existing filings are baselined away rather
# than replayed -- but a just-promoted member's trade from a few days ago is exactly the
# signal we're watching for. Filings this recent stay alertable.
DEFAULT_BASELINE_WINDOW_DAYS = 7

# How long a filing may sit indexed-but-unreadable before the monitors stop waiting and
# say so. The Clerk routinely publishes an index row before its PDF is reachable, which
# an hourly poll hits often and a daily poll almost never did; a day's grace covers the
# publish gap without sitting on a genuinely broken filing forever.
PENDING_GRACE_HOURS = 24


def log(message: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {message}")


def load_webhook_url() -> str:
    if not CONFIG_PATH.exists():
        sys.exit(f"Missing config file: {CONFIG_PATH}")
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except json.JSONDecodeError as exc:
        sys.exit(f"Malformed JSON in {CONFIG_PATH}: {exc}")
    url = config.get("webhook_url")
    if not url:
        sys.exit(f"No 'webhook_url' key in {CONFIG_PATH}")
    if "discord.com/api/webhooks/" not in url and "discordapp.com/api/webhooks/" not in url:
        # A webhook from somewhere else would fail as an opaque 400 from Discord.
        # Naming the config is the difference between a one-line fix and an evening.
        sys.exit(
            f"{CONFIG_PATH} does not hold a Discord webhook URL. These monitors post to "
            f"Discord -- get one from Server Settings -> Integrations -> Webhooks."
        )
    return url


@dataclass
class AlertState:
    """What a monitor remembers between runs, in one shape for all three of them.

    `seen` is handled filings. `pending` is filings indexed without a readable PDF yet,
    mapped to the moment they were first sighted, so the contents can be alerted when
    they publish instead of the alert being spent on the gap. `members` is the watched
    set as of the last run -- what stops a watchlist rotation from replaying a newly
    promoted member's back catalogue (unused by congress_monitor, which watches everyone).
    """

    seen: set[str] = field(default_factory=set)
    pending: dict[str, str] = field(default_factory=dict)
    members: set[tuple] = field(default_factory=set)


def load_alert_state(path: Path) -> AlertState:
    """Read a monitor's state. A corrupt file is quarantined and the run aborts: treating
    it as empty would re-alert on the entire back catalogue.

    Tolerates both legacy shapes -- a bare DocID list, and a dict without `pending`.
    """
    if not path.exists():
        return AlertState()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        quarantine = path.with_suffix(path.suffix + ".corrupt")
        path.rename(quarantine)
        sys.exit(
            f"Corrupt state file {path} ({exc}); moved to {quarantine}. "
            f"The next run will re-seed a baseline without alerting."
        )
    if isinstance(raw, list):  # legacy: seen DocIDs only
        return AlertState(seen=set(raw))
    return AlertState(
        seen=set(raw.get("seen", [])),
        pending={str(k): str(v) for k, v in (raw.get("pending") or {}).items()},
        members={tuple(m) for m in raw.get("baselined_members", [])},
    )


def save_alert_state(path: Path, state: AlertState) -> None:
    """Write atomically -- a crash mid-write would otherwise truncate the state file and
    cost us the whole seen-set."""
    write_json(path, {
        "seen": sorted(state.seen),
        "pending": {k: state.pending[k] for k in sorted(state.pending)},
        "baselined_members": sorted(list(m) for m in state.members),
    })


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


@contextmanager
def run_lock(name: str):
    """Serialise runs of one monitor, so an hourly schedule cannot double-post.

    A run that outlives its slot -- a slow Clerk, a few hundred PDFs, a Discord 429
    backoff -- would otherwise overlap the next one, and both processes would read the
    same seen-set and post the same alert. The second run exits 0 rather than failing:
    a skipped poll is not an error, and cron mailing about it every hour would be.
    """
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{name}.lock"
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        log(f"Another {name} run still holds {path}; exiting rather than double-posting.")
        sys.exit(0)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# What to do about a filing that is indexed but whose PDF did not parse.
PDF_ANNOUNCE = "announce"   # first sighting: say it landed, contents to follow
PDF_WAIT = "wait"           # already announced, still inside the grace period
PDF_GIVE_UP = "give-up"     # waited out the grace period: report it as unreadable


def pdf_wait_action(doc_id: str, pending: dict[str, str], now: datetime,
                    grace_hours: int = PENDING_GRACE_HOURS) -> str:
    """Whether to announce, keep waiting for, or give up on an unreadable filing.

    The Clerk publishes an index row before the PDF is reachable often enough that an
    hourly poll sees it routinely. Alerting "transactions could not be read" and marking
    the filing handled would spend its only notification on the minutes the contents
    weren't published yet, so the first sighting announces the filing and the contents
    follow when they appear. A PDF still unreadable after the grace period is genuinely
    broken, and that gets said once rather than retried forever.
    """
    first_seen = parse_iso(pending.get(doc_id))
    if first_seen is None:
        return PDF_ANNOUNCE if doc_id not in pending else PDF_GIVE_UP
    return PDF_GIVE_UP if now - first_seen >= timedelta(hours=grace_hours) else PDF_WAIT


def parse_iso(value: str | None) -> datetime | None:
    """An ISO timestamp from state, always returned UTC-aware. A value written by an
    older build without an offset is read as UTC rather than crashing the comparison."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def is_within_window(row: dict, now: datetime, window_days: int) -> bool:
    """True if the filing is recent enough to alert on despite its member being new to
    the watched set. An unparseable filing date is treated as old -- baselining something
    stale is a missed alert, but replaying a whole back catalogue is a pager storm."""
    filed = parse_date(row.get("FilingDate", ""))
    return filed is not None and (now.replace(tzinfo=None) - filed).days <= window_days


def baseline_docids(rows: list[dict], members: set[tuple], now: datetime,
                    window_days: int, apply_window: bool = True) -> set[str]:
    """DocIDs to record as seen *without* alerting, for members new to a watched set.

    `apply_window=False` on a first-ever run, where there is no prior membership to
    compare against and the whole back catalogue should go quiet."""
    return {
        row["DocID"] for row in rows
        if member_key(row) in members
        and not (apply_window and is_within_window(row, now, window_days))
    }


def escape_markdown(text: str) -> str:
    """Neutralise Discord markdown in third-party text.

    Filer and asset names come from the Clerk's PDFs, so an underscore or asterisk in
    one would otherwise italicise the rest of an alert. This escapes markdown rather
    than HTML entities: Discord renders `&amp;` literally, so entity escaping would
    show up as visible noise."""
    for char in ("\\", "*", "_", "~", "`", "|"):
        text = text.replace(char, "\\" + char)
    return text


# --- Transaction rendering ---------------------------------------------------------
#
# One formatter shared by all three alerts, so a transaction reads the same wherever it
# turns up. The filing's own vocabulary is the thing being translated: it labels the
# account holder `SP`, the asset class `[GS]`, and the size as `$15,001 - $50,000`, none
# of which mean anything to someone reading a phone notification.

# `self` is the default and says nothing worth a word in the line.
OWNER_LABELS = {"SP": "spouse", "DC": "child", "JT": "joint"}

# Asset classes worth naming, because "bought a Treasury note" and "bought Nvidia" are
# different events and the filing distinguishes them only by a two-letter code. Stocks
# carry no tag -- they are the assumed case -- and an unrecognised code is dropped rather
# than shown raw, since a bare `[VI]` is no better than nothing.
ASSET_TYPE_LABELS = {
    "AB": "asset-backed", "CS": "corporate bond", "CT": "crypto", "EF": "ETF",
    "ET": "ETN", "FE": "currency", "FN": "annuity", "FU": "futures", "GS": "govt bond",
    "HE": "hedge fund", "IP": "royalties", "MF": "fund", "OI": "private co",
    "OL": "private co", "OP": "option", "RE": "REIT", "RP": "real estate",
    "SA": "stock rights", "VA": "annuity",
}

DIRECTION_LABELS = {
    "Purchase": "🟢 **BUY**",
    "Sale": "🔴 **SELL**",
    "Exchange": "🔁 **EXCH**",
}

# The clock marks a filing that missed the STOCK Act deadline. It is explained once at the
# foot of a message rather than in every line: the explanation is longer than the fact.
LATE_MARK = "⏰"
LATE_LEGEND = (f"_{LATE_MARK} = disclosed more than {LATE_FILING_DAYS} days after the "
               f"trade, past the STOCK Act deadline (number shown is days taken)_")

# Long enough to recognise a company, short enough that a line stays one line on a phone.
MAX_ASSET_NAME = 38

_DOLLAR_RE = re.compile(r"\$[\d,]+")

# Corporate suffixes carry no information once the ticker is on the line, and they are
# what pushes "International Business Machines Corporation" past the width where a name
# gets cut. Display only -- clean_asset_name() feeds the ticker lookup, where the legal
# form does help disambiguate.
_LEGAL_SUFFIX_RE = re.compile(
    r"[\s,]+(?:Inc|Incorporated|Corp|Corporation|Co|Company|Ltd|Limited|LLC|L\.?P|"
    r"PLC|N\.?V|S\.?A|A[/\s]?S|Holdings?|Group|Trust)\b\.?",
    re.I,
)


def plural(count: int, noun: str, suffix: str = "s") -> str:
    """"1 filing" / "2 filings" -- the old "filing(s)" everywhere read like a form letter."""
    return f"{count} {noun}{'' if count == 1 else suffix}"


def display_name(member: dict) -> str:
    """'McCormick, Richard' from a leaderboard/watchlist member's identity fields.

    Built from the clean `first`/`last` the leaderboard already carries rather than by
    un-formatting the composed `member` string: stripping titles back out needs a
    hardcoded honorific list, and anything missing from it (a stray 'Dr' that came from
    the Suffix column) leaks through. Shared so the search index and the monitors that
    look rows up in it agree on the spelling.
    """
    last, first = (member.get("last") or "").strip(), (member.get("first") or "").strip()
    if last and first:
        return f"{last}, {first}"
    return last or first or (member.get("member") or "").strip()


def compact_dollars(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:g}M"
    if value >= 1_000:
        return f"${round(value / 1_000):g}k"
    return f"${value:,.0f}"


def format_amount(amount: str) -> str:
    """`$15,001 - $50,000` -> `$15k-$50k`.

    The STOCK Act only ever discloses a bracket, and the brackets are conventionally
    quoted by their round bounds; the exact `,001` is an artefact of how they abut, not
    information. An amount that doesn't parse is passed through rather than guessed at."""
    values = [int(v.lstrip("$").replace(",", "")) for v in _DOLLAR_RE.findall(amount or "")]
    if not values:
        return amount.strip() if amount else "amount not disclosed"
    if len(values) == 1:
        # The top bracket is open-ended ("Over $50,000,000") and has no upper bound.
        prefix = "over " if "over" in amount.lower() else ""
        return prefix + compact_dollars(values[0])
    return f"{compact_dollars(values[0])}-{compact_dollars(values[1])}"


def format_owner(owner: str | None) -> str:
    """`SP` -> `spouse`. Empty for the filer's own account, which needs no saying."""
    return OWNER_LABELS.get(owner or "", "")


def shorten(text: str, limit: int = MAX_ASSET_NAME) -> str:
    """Trim on a word boundary. Cutting mid-word produced things like `US Treasury Note
    3.5% DU`, which reads as a parser bug rather than an abbreviation."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0] or text[:limit]
    return cut.rstrip(" ,.-") + "…"


def format_asset(txn: dict, *, limit: int = MAX_ASSET_NAME) -> str:
    """`**AWK** American Water Works Company` -- ticker first.

    The filing prints the ticker at the tail of a long legal name (`American Water Works
    Company, Inc. Common Stock (AWK)`), which is the wrong way round for scanning: the
    ticker is the identifier a reader is looking for, and the name is context."""
    ticker = txn.get("ticker")
    raw = txn.get("asset", "") or ""
    name = clean_asset_name(raw) or raw
    # Only worth dropping the legal form when the ticker already identifies the company;
    # for an untickered bond or fund the full name is all the reader has.
    if ticker:
        name = _LEGAL_SUFFIX_RE.sub("", name).strip(" ,.-") or name
    name = shorten(name, limit)
    label = escape_markdown(name)
    if ticker:
        return f"**{escape_markdown(ticker)}** {label}".strip()
    return label or "_(unnamed asset)_"


def format_transaction(txn: dict, filing_date: str, *,
                       show_direction: bool = True, count: int = 1) -> str:
    """One transaction as a single scannable line, most-important-first: what direction,
    what asset, how much, when it happened. Everything after that is a qualifier."""
    parts = []
    if show_direction:
        parts.append(DIRECTION_LABELS.get(txn.get("type"), f"**{txn.get('type', '?')}**"))

    asset = format_asset(txn)
    type_label = ASSET_TYPE_LABELS.get(txn.get("asset_type") or "")
    if type_label:
        asset += f" [{type_label}]"
    if count > 1:
        asset += f" ×{count}"  # one row per account; the same trade, disclosed N times
    parts.append(asset)

    tail = [format_amount(txn.get("amount", "")), f"traded {txn.get('txn_date', '?')}"]
    owner = format_owner(txn.get("owner"))
    if owner:
        tail.append(owner)
    if txn.get("partial"):
        tail.append("partial")
    if is_late_filing(txn.get("txn_date", ""), filing_date):
        tail.append(f"{LATE_MARK} {filing_delay_days(txn['txn_date'], filing_date)}d")

    return " ".join(parts) + " · " + " · ".join(tail)


def any_late(transactions: list[dict], filing_date: str) -> bool:
    return any(is_late_filing(t.get("txn_date", ""), filing_date) for t in transactions)


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split an over-long message into Discord-sized pieces on line boundaries.

    Splitting mid-line would cut a filing in half, so lines are kept whole; a single
    line longer than the limit (a filing with a very long asset list) is hard-split as
    a last resort. Returns [""] for empty input so callers still post something rather
    than silently sending nothing."""
    chunks: list[str] = []
    current: list[str] = []
    length = 0

    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current, length = [], 0
            chunks.append(line[:limit])
            line = line[limit:]
        # +1 for the newline joining this line to the previous one.
        if current and length + 1 + len(line) > limit:
            chunks.append("\n".join(current))
            current, length = [], 0
        current.append(line)
        length += len(line) + (1 if length else 0)

    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _post_chunk(webhook_url: str, content: str) -> None:
    """POST one chunk, honouring Discord's stated wait on a rate-limit response."""
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        resp = requests.post(webhook_url, json={"content": content}, timeout=15)
        if resp.status_code != 429 or attempt == RATE_LIMIT_RETRIES:
            resp.raise_for_status()  # Discord returns 204 No Content on success
            return
        try:
            wait = float(resp.json().get("retry_after", 1.0))
        except (ValueError, AttributeError, requests.exceptions.JSONDecodeError):
            wait = float(resp.headers.get("Retry-After", 1.0) or 1.0)
        # Cap it: a malformed retry_after should not park a cron run for hours.
        time.sleep(min(max(wait, 0.0), MAX_RETRY_SLEEP))


def send_discord_message(webhook_url: str, text: str) -> None:
    """Deliver a message, splitting it if it exceeds Discord's per-message limit.

    Chunking lives here rather than in the callers so every monitor is covered: any of
    them can generate an over-limit message given a filing with enough transactions."""
    for chunk in split_message(text):
        _post_chunk(webhook_url, chunk)


def seed_baseline(path: Path, doc_ids: set[str], label: str) -> None:
    save_alert_state(path, AlertState(seen=set(doc_ids)))
    log(f"First run: seeded baseline with {len(doc_ids)} existing {label}, no alerts "
        f"sent. Future runs will alert on genuinely new filings.")


def add_pending_grace_arg(parser) -> None:
    """How long to wait for a filing's PDF to publish before reporting it unreadable."""
    parser.add_argument("--pending-grace", type=int, default=PENDING_GRACE_HOURS,
                        help=f"Hours to keep retrying a filing whose PDF has not "
                             f"published yet (default {PENDING_GRACE_HOURS}); 0 reports "
                             f"it as unreadable on the first sighting")


def add_all_assets_arg(parser) -> None:
    """Opt back in to the non-equity rows. Shared with daily_summary.py, which builds its
    own parser rather than using add_common_args."""
    parser.add_argument("--all-assets", action="store_true",
                        help="Report every asset class. By default only stocks and stock "
                             "options are alerted on; bonds, funds, crypto and the rest "
                             "are filtered out")


def add_common_args(parser) -> None:
    parser.add_argument("--verbose", action="store_true", help="Print per-filing debug info")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts without posting to Discord or recording state")
    parser.add_argument("--max-alerts", type=int, default=DEFAULT_MAX_ALERTS,
                        help=f"Cap alerts per run (default {DEFAULT_MAX_ALERTS}); "
                             f"the remainder is left for the next run")
    add_all_assets_arg(parser)
    add_pending_grace_arg(parser)
