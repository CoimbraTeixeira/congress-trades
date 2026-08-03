"""Shared plumbing for the Discord monitors: webhook config, seen-doc state, delivery."""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from house_disclosures import member_key, parse_date

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


def load_seen_doc_ids(path: Path) -> set[str]:
    """Read the seen-DocID set. A corrupt state file is quarantined and the run aborts:
    treating it as empty would re-alert on the entire back catalogue."""
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text()))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        quarantine = path.with_suffix(path.suffix + ".corrupt")
        path.rename(quarantine)
        sys.exit(
            f"Corrupt state file {path} ({exc}); moved to {quarantine}. "
            f"The next run will re-seed a baseline without alerting."
        )


def save_seen_doc_ids(path: Path, doc_ids: set[str]) -> None:
    """Write atomically -- a crash mid-write would otherwise truncate the state file
    and cost us the whole seen-set."""
    write_json(path, sorted(doc_ids))


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def load_watch_state(path: Path) -> tuple[set[str], set[tuple]]:
    """Seen DocIDs plus the watchlist membership as of the last run.

    Recording who was watched is what stops a watchlist rotation from replaying a newly
    promoted member's entire back catalogue as fresh alerts. It is last run's membership
    rather than a growing union, so a member who drops off and returns is baselined again
    instead of replaying their absence.
    Tolerates the legacy bare-list format (all DocIDs, no member record)."""
    if not path.exists():
        return set(), set()
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        quarantine = path.with_suffix(path.suffix + ".corrupt")
        path.rename(quarantine)
        sys.exit(
            f"Corrupt state file {path} ({exc}); moved to {quarantine}. "
            f"The next run will re-seed a baseline without alerting."
        )
    if isinstance(raw, list):  # legacy format
        return set(raw), set()
    return set(raw.get("seen", [])), {tuple(m) for m in raw.get("baselined_members", [])}


def save_watch_state(path: Path, seen: set[str], baselined: set[tuple]) -> None:
    write_json(path, {
        "seen": sorted(seen),
        "baselined_members": sorted(list(m) for m in baselined),
    })


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
    save_seen_doc_ids(path, doc_ids)
    log(f"First run: seeded baseline with {len(doc_ids)} existing {label}, no alerts "
        f"sent. Future runs will alert on genuinely new filings.")


def add_common_args(parser) -> None:
    parser.add_argument("--verbose", action="store_true", help="Print per-filing debug info")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts without posting to Discord or recording state")
    parser.add_argument("--max-alerts", type=int, default=DEFAULT_MAX_ALERTS,
                        help=f"Cap alerts per run (default {DEFAULT_MAX_ALERTS}); "
                             f"the remainder is left for the next run")
