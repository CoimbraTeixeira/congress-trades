#!/usr/bin/env python3
"""Daily digest of trades filed by the top-N members of the leaderboard.

One consolidated Discord post per run rather than one per filing: a roll-up of the most
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
    equity_transactions,
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
    add_all_assets_arg,
    baseline_docids,
    compact_dollars,
    escape_markdown,
    load_watch_state,
    load_webhook_url,
    log,
    plural,
    save_watch_state,
    send_discord_message,
    shorten,
    split_message,
)

REPO_DIR = Path(__file__).resolve().parent
LEADERBOARD_PATH = REPO_DIR / "leaderboard.json"
STATE_PATH = STATE_DIR / "summary_seen_docids.json"

DEFAULT_TOP = 50
# Discord caps a message at 2000 characters and send_discord_message() splits anything
# longer, so the old 12k character ceiling would now land as seven consecutive posts.
# Past about three the digest stops being a digest. The cap is expressed in messages
# rather than characters because line-boundary splitting leaves each chunk short of the
# limit -- a 3x2000 character budget really produces four posts, not three.
MAX_DIGEST_MESSAGES = 3
TRUNCATION_MARKER = "\n_(truncated)_"
MAX_TICKERS_PER_LINE = 8
FETCH_WORKERS = 6
# An untickered asset (a bond, a fund) is labelled by name. Short enough to sit several
# to a line, and cut on a word boundary so it reads as an abbreviation and not a bug.
MAX_LABEL_CHARS = 22


def select_members(leaderboard: dict, top: int, rank_by: str) -> list[dict]:
    """The members whose filings this digest covers.

    `alpha` ranks by estimated return less what the benchmark did over the same holding
    windows -- the default, because a raw return in a rising market ranks whoever held
    the most beta above whoever picked well. `return` ranks by the raw figure. Both are
    only defined for members who clear the leaderboard's thresholds, and there are
    usually far fewer of those than the requested top-N, because most members are
    sell-only in a given year and so have no computable return at all. `volume` ranks by
    dollars traded, which every filer has.
    """
    members = leaderboard["members"]
    if rank_by == "alpha":
        pool = [m for m in members
                if m.get("ranked") and m.get("alpha_pct_estimate") is not None]
        key = lambda m: m["alpha_pct_estimate"]  # noqa: E731
    elif rank_by == "return":
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
        label = t["ticker"] or shorten(clean_asset_name(t["asset"]), MAX_LABEL_CHARS) or "?"
        bucket[label][0] += 1
        bucket[label][1] += amount_midpoint(t["amount"]) or 0.0
    return {"buys": dict(buys), "sells": dict(sells)}


def format_side(side: dict[str, list]) -> str:
    """"NVDA ×3 (~$24k), AMAT (~$8k)" -- biggest estimated dollars first."""
    # Most trades sit in the same disclosure bucket, so dollar totals tie constantly;
    # the label breaks ties to keep a daily report's ordering stable run to run.
    ranked = sorted(side.items(), key=lambda kv: (-kv[1][1], kv[0]))
    shown = ranked[:MAX_TICKERS_PER_LINE]
    parts = [
        f"{escape_markdown(label)}{f' ×{count}' if count > 1 else ''} "
        f"(~{compact_dollars(dollars)})"
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

    scope = f"Covering the top {covered} members by {scope_label}"
    if covered < requested:
        # Say why rather than quietly reporting on fewer people than were asked for.
        scope += f" — {requested} were requested, but only {covered} qualify"

    lines = [
        f"📊 **Daily Congress Trade Summary** — {now:%Y-%m-%d}",
        f"{plural(len(entries), 'new filing')} from {plural(len(members), 'member')} · "
        f"{plural(txn_count, 'transaction')}",
        # Naming the estimate inline saves a reader wondering why the totals are round:
        # the filings disclose a bracket, never an amount.
        f"Bought ~{compact_dollars(total_buy)} · sold ~{compact_dollars(total_sell)} "
        f"(midpoints of the disclosed ranges)",
        f"_{scope}_",
    ]

    rollup = most_traded(entries, len(members))
    if rollup:
        lines += ["", *rollup]

    for entry in sorted(entries, key=lambda e: e["member"].get("rank", 0)):
        member, summary = entry["member"], entry["summary"]
        ret = member.get("return_pct_estimate")
        alpha = member.get("alpha_pct_estimate")
        ret_label = f" · {ret:+.1f}% est. return" if ret is not None else ""
        # The benchmark comparison is what makes the return legible, so it rides along
        # wherever the return is printed.
        if alpha is not None:
            ret_label += f" ({alpha:+.1f}% vs index)"
        lines += ["", f"**{escape_markdown(member['member'])}** · "
                      f"{member['state_dist']} · #{member['rank']}{ret_label}"]
        if summary["buys"]:
            lines.append(f"🟢 **BOUGHT** {format_side(summary['buys'])}")
        if summary["sells"]:
            lines.append(f"🔴 **SOLD** {format_side(summary['sells'])}")
        if not summary["buys"] and not summary["sells"]:
            lines.append("_(transactions could not be read from the PDF)_")
        lines.append(entry["pdf_link"])

    return truncate_to_messages("\n".join(lines))


def truncate_to_messages(text: str) -> str:
    """Trim a digest to at most MAX_DIGEST_MESSAGES Discord messages.

    Measured in the same chunks delivery will actually produce, so the ceiling means
    what it says. Dropping the tail line makes room for the marker inside the last
    message rather than spilling into another one."""
    chunks = split_message(text)
    if len(chunks) <= MAX_DIGEST_MESSAGES:
        return text
    kept = "\n".join(chunks[:MAX_DIGEST_MESSAGES])
    budget = max(len(kept) - len(TRUNCATION_MARKER), 0)
    return kept[:budget].rsplit("\n", 1)[0] + TRUNCATION_MARKER


def most_traded(entries: list[dict], filer_count: int) -> list[str]:
    """Tickers several members touched the same day -- the point of a digest over
    per-filing alerts is that clustering only shows up when you look across members.

    The count is reported against the number of members who filed at all, because "2
    members" is a striking number out of thirty and an unremarkable one out of two."""
    buyers: dict[str, set] = defaultdict(set)
    sellers: dict[str, set] = defaultdict(set)
    for entry in entries:
        who = entry["member"]["member"]
        for label in entry["summary"]["buys"]:
            buyers[label].add(who)
        for label in entry["summary"]["sells"]:
            sellers[label].add(who)

    lines = []
    for marker, verb, tally in (("🟢", "bought", buyers), ("🔴", "sold", sellers)):
        shared = sorted(((t, len(w)) for t, w in tally.items() if len(w) > 1),
                        key=lambda kv: (-kv[1], kv[0]))[:5]
        if shared:
            lines.append(f"{marker} **{verb.capitalize()} by more than one member:** "
                         + ", ".join(f"{escape_markdown(t)} ({n} of {filer_count})"
                                     for t, n in shared))
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=DEFAULT_TOP,
                        help=f"How many leaderboard members to cover (default {DEFAULT_TOP})")
    parser.add_argument("--rank-by", choices=("alpha", "return", "volume"),
                        default="alpha",
                        help="Rank the top-N by estimated alpha against the benchmark "
                             "(default) or raw estimated return -- both ranked members "
                             "only -- or by dollars traded (every filer)")
    parser.add_argument("--baseline-window", type=int, default=DEFAULT_BASELINE_WINDOW_DAYS,
                        help="When a member enters the top-N, still report filings from "
                             "the last N days")
    parser.add_argument("--post-empty", action="store_true",
                        help="Post the digest even when no new filings landed")
    add_all_assets_arg(parser)
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
        reported = transactions if args.all_assets else equity_transactions(transactions)
        return {
            "member": covered_by_key[member_key(row)],
            "pdf_link": PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
            # Whether the PDF read at all, kept separate from whether it held any stock
            # trades -- the digest prints "could not be read" off the first, and dropping
            # a bond-only filing must not borrow that line.
            "unreadable": not transactions,
            "txn_count": len(reported),
            "summary": summarise_filing(reported),
        }

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        entries = list(pool.map(load, new_rows))

    entries = [e for e in entries if e["txn_count"] or e["unreadable"]]
    if not entries and not args.post_empty:
        log(f"{plural(len(new_rows), 'new filing')} from the top {len(top_members)}, "
            f"none with stock transactions; nothing to summarise.")
        if not args.dry_run:
            # Still handled -- leaving them unseen would re-fetch every PDF tomorrow.
            seen |= {r["DocID"] for r in new_rows}
            save_watch_state(STATE_PATH, seen, covered_members)
        return

    scope_label = {"alpha": "est. alpha", "return": "est. return"}.get(
        args.rank_by, "dollars traded")
    message = build_digest(now, entries, scope_label, len(top_members), args.top)
    if args.verbose or args.dry_run:
        print(message)

    # Count the filings the digest actually reports on, not everything read: with the
    # stock filter on, those differ, and the digest's own header states the former.
    filtered = len(new_rows) - len(entries)
    tail = f" ({filtered} with no stock transactions)" if filtered else ""

    if args.dry_run:
        log(f"Would post a digest covering {plural(len(entries), 'new filing')}{tail}.")
        return

    try:
        send_discord_message(webhook_url, message)
    except requests.RequestException as exc:
        # Leave every DocID unseen so the next run retries rather than losing the day.
        sys.exit(f"Failed to post the digest: {exc}")

    seen |= {r["DocID"] for r in new_rows}
    save_watch_state(STATE_PATH, seen, covered_members)
    log(f"Posted a digest covering {plural(len(entries), 'new filing')}{tail} from the "
        f"top {len(top_members)}.")


if __name__ == "__main__":
    main()
