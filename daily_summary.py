#!/usr/bin/env python3
"""Daily digest of trades filed by the top-N members of the leaderboard.

One consolidated Slack post per run rather than one per filing: a roll-up of the most
traded tickers, then a per-member breakdown. Complements the two per-filing monitors --
congress_monitor.py alerts on everything as it lands, watchlist_monitor.py alerts fast
on the top 10's buys, and this gives the wider top-N a once-a-day read.

Scope is "filings we haven't summarised yet", not "filings dated today": the Clerk's
archive publishes on its own schedule and a filing routinely shows up several days after
its filing date, so a fixed 24-hour date window would silently drop it.
"""

import argparse
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

from house_disclosures import (
    PDF_URL_TMPL,
    PTR_FILING_TYPE,
    amount_midpoint,
    clean_asset_name,
    fetch_index_years,
    fetch_ptr_text,
    filing_sort_key,
    index_years,
    member_key,
    parse_transactions,
)
from monitor_common import (
    DEFAULT_BASELINE_WINDOW_DAYS,
    STATE_DIR,
    baseline_docids,
    load_watch_state,
    load_webhook_url,
    log,
    save_watch_state,
    send_slack_message,
)

REPO_DIR = Path(__file__).resolve().parent
LEADERBOARD_PATH = REPO_DIR / "leaderboard.json"
STATE_PATH = STATE_DIR / "summary_seen_docids.json"

DEFAULT_TOP = 50
# Slack renders a webhook post as one message; past a few thousand characters it stops
# being readable long before it hits the API limit. Trim the tail and say so.
MAX_MESSAGE_CHARS = 12_000
MAX_TICKERS_PER_LINE = 8
FETCH_WORKERS = 6


def fmt_dollars(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


def select_members(leaderboard: dict, top: int, rank_by: str) -> list[dict]:
    """The members whose filings this digest covers.

    `return` ranks by estimated return, which is only defined for members who clear the
    leaderboard's thresholds -- there are usually far fewer of those than the requested
    top-N, because most members are sell-only in a given year and so have no computable
    return at all. `volume` ranks by dollars traded, which every filer has.
    """
    members = leaderboard["members"]
    if rank_by == "return":
        pool = [m for m in members if m.get("ranked")]
        key = lambda m: m["return_pct_estimate"]  # noqa: E731
    else:
        pool = [m for m in members if m.get("invested_total_estimate")]
        key = lambda m: m["invested_total_estimate"]  # noqa: E731
    return sorted(pool, key=key, reverse=True)[:top]


def summarise_filing(transactions: list[dict]) -> dict:
    """Fold a filing's transaction rows into per-ticker buy/sell totals."""
    buys: dict[str, list] = defaultdict(lambda: [0, 0.0])
    sells: dict[str, list] = defaultdict(lambda: [0, 0.0])
    for t in transactions:
        bucket = buys if t["type"] == "Purchase" else sells if t["type"] == "Sale" else None
        if bucket is None:  # Exchange -- counted in the totals, not as a direction
            continue
        label = t["ticker"] or clean_asset_name(t["asset"])[:24] or "?"
        bucket[label][0] += 1
        bucket[label][1] += amount_midpoint(t["amount"]) or 0.0
    return {"buys": dict(buys), "sells": dict(sells)}


def format_side(side: dict[str, list]) -> str:
    """"NVDA x3 (~$24k), AMAT (~$8k)" -- biggest estimated dollars first."""
    # Most trades sit in the same disclosure bucket, so dollar totals tie constantly;
    # the label breaks ties to keep a daily report's ordering stable run to run.
    ranked = sorted(side.items(), key=lambda kv: (-kv[1][1], kv[0]))
    shown = ranked[:MAX_TICKERS_PER_LINE]
    parts = [
        f"{label}{f' x{count}' if count > 1 else ''} (~{fmt_dollars(dollars)})"
        for label, (count, dollars) in shown
    ]
    if len(ranked) > len(shown):
        parts.append(f"+{len(ranked) - len(shown)} more")
    return ", ".join(parts)


def build_digest(now: datetime, entries: list[dict], scope_label: str,
                 covered: int, requested: int) -> str:
    total_buy = sum(sum(d for _, d in e["summary"]["buys"].values()) for e in entries)
    total_sell = sum(sum(d for _, d in e["summary"]["sells"].values()) for e in entries)
    txn_count = sum(e["txn_count"] for e in entries)
    members = {e["member"]["member"] for e in entries}

    scope = f"top {covered} by {scope_label}"
    if covered < requested:
        # Say why rather than quietly reporting on fewer people than were asked for.
        scope += f" (of {requested} requested; only {covered} qualify)"

    lines = [
        f":bar_chart: *Daily Congress Trade Summary* -- {now:%Y-%m-%d}",
        f"_{scope} · {len(entries)} new filing(s) from {len(members)} member(s) · "
        f"{txn_count} transaction(s)_",
        f"Est. bought {fmt_dollars(total_buy)} · Est. sold {fmt_dollars(total_sell)}",
    ]

    rollup = most_traded(entries)
    if rollup:
        lines += ["", *rollup]

    for entry in sorted(entries, key=lambda e: e["member"].get("rank", 0)):
        member, summary = entry["member"], entry["summary"]
        rank = f"#{member['rank']}"
        ret = member.get("return_pct_estimate")
        ret_label = f", {ret:+.1f}%" if ret is not None else ""
        lines += ["", f"*{member['member']}* ({member['state_dist']}, {rank}{ret_label})"]
        if summary["buys"]:
            lines.append(f"  :large_green_circle: BUY  {format_side(summary['buys'])}")
        if summary["sells"]:
            lines.append(f"  :red_circle: SELL {format_side(summary['sells'])}")
        if not summary["buys"] and not summary["sells"]:
            lines.append("  _(no transactions parsed -- see PDF)_")
        lines.append(f"  {entry['pdf_link']}")

    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS].rsplit("\n", 1)[0] + "\n_(truncated)_"
    return text


def most_traded(entries: list[dict]) -> list[str]:
    """Tickers several members touched the same day -- the point of a digest over
    per-filing alerts is that clustering only shows up when you look across members."""
    buyers: dict[str, set] = defaultdict(set)
    sellers: dict[str, set] = defaultdict(set)
    for entry in entries:
        who = entry["member"]["member"]
        for label in entry["summary"]["buys"]:
            buyers[label].add(who)
        for label in entry["summary"]["sells"]:
            sellers[label].add(who)

    lines = []
    for verb, tally in (("Most bought", buyers), ("Most sold", sellers)):
        shared = sorted(((t, len(w)) for t, w in tally.items() if len(w) > 1),
                        key=lambda kv: (-kv[1], kv[0]))[:5]
        if shared:
            lines.append(f"*{verb}:* " + ", ".join(f"{t} ({n} members)" for t, n in shared))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"How many leaderboard members to cover (default {DEFAULT_TOP})")
    parser.add_argument("--rank-by", choices=("return", "volume"), default="return",
                        help="Rank the top-N by estimated return (ranked members only) "
                             "or by dollars traded (every filer)")
    parser.add_argument("--baseline-window", type=int, default=DEFAULT_BASELINE_WINDOW_DAYS,
                        help="When a member enters the top-N, still report filings from "
                             "the last N days")
    parser.add_argument("--post-empty", action="store_true",
                        help="Post the digest even when no new filings landed")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the digest without posting or recording state")
    args = parser.parse_args()

    if not LEADERBOARD_PATH.exists():
        sys.exit(f"Missing {LEADERBOARD_PATH} -- run build_leaderboard.py first")
    leaderboard = json.loads(LEADERBOARD_PATH.read_text())

    top_members = select_members(leaderboard, args.top, args.rank_by)
    if not top_members:
        sys.exit("No members to summarise -- regenerate leaderboard.json.")
    covered_by_key = {
        (m["last"], m["first"], m["state_dist"]): dict(m, rank=rank)
        for rank, m in enumerate(top_members, start=1)
    }

    webhook_url = None if args.dry_run else load_webhook_url()
    now = datetime.now(timezone.utc)

    rows = fetch_index_years(index_years(now))
    covered_rows = sorted(
        (r for r in rows
         if r.get("FilingType") == PTR_FILING_TYPE and member_key(r) in covered_by_key),
        key=filing_sort_key,
    )
    if args.verbose:
        print(f"-- {len(rows)} filings, {len(covered_rows)} PTRs from the top "
              f"{len(top_members)} --")

    first_run = not STATE_PATH.exists()
    seen, previously_covered = load_watch_state(STATE_PATH)
    covered_members = set(covered_by_key)

    # Same rule as watchlist_monitor: a member entering the top-N is baselined so the
    # digest doesn't replay their year, but their last few days still count.
    newly_covered = covered_members - previously_covered
    if newly_covered:
        stale = baseline_docids(covered_rows, newly_covered, now, args.baseline_window,
                                apply_window=not first_run)
        seen |= stale
        log(f"Baselined {len(stale)} existing PTR(s) for {len(newly_covered)} member(s) "
            f"new to the top {len(top_members)}.")
    if covered_members != previously_covered and not args.dry_run:
        save_watch_state(STATE_PATH, seen, covered_members)
    if first_run:
        return

    new_rows = [r for r in covered_rows if r["DocID"] not in seen]
    if not new_rows and not args.post_empty:
        log(f"No new filings from the top {len(top_members)}; nothing to summarise.")
        return

    def load(row: dict) -> dict:
        try:
            pdf_text = fetch_ptr_text(row["Year"], row["DocID"])
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {row['DocID']}: {exc}", file=sys.stderr)
            transactions = []
        return {
            "member": covered_by_key[member_key(row)],
            "pdf_link": PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
            "txn_count": len(transactions),
            "summary": summarise_filing(transactions),
        }

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        entries = list(pool.map(load, new_rows))

    scope_label = "est. return" if args.rank_by == "return" else "dollars traded"
    message = build_digest(now, entries, scope_label, len(top_members), args.top)
    if args.verbose or args.dry_run:
        print(message)

    if args.dry_run:
        log(f"Would post a digest covering {len(new_rows)} new filing(s).")
        return

    try:
        send_slack_message(webhook_url, message)
    except requests.RequestException as exc:
        # Leave every DocID unseen so the next run retries rather than losing the day.
        sys.exit(f"Failed to post the digest: {exc}")

    seen |= {r["DocID"] for r in new_rows}
    save_watch_state(STATE_PATH, seen, covered_members)
    log(f"Posted a digest covering {len(new_rows)} new filing(s) from the top "
        f"{len(top_members)}.")


if __name__ == "__main__":
    main()
