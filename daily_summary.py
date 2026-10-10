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
    scanned_page_count,
    trade_transactions,
)
from monitor_common import (
    DEFAULT_BASELINE_WINDOW_DAYS,
    LEADERBOARD_PATH,
    PDF_GIVE_UP,
    STATE_DIR,
    add_all_assets_arg,
    add_pending_grace_arg,
    baseline_docids,
    compact_dollars,
    escape_markdown,
    load_alert_state,
    load_webhook_url,
    log,
    pdf_wait_action,
    plural,
    run_lock,
    save_alert_state,
    scanned_note,
    select_members,
    send_discord_message,
    shorten,
    split_message,
    top_performer_scope,
    top_performers_by_key,
)
from search_common import ThemeIndex

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
                 covered: int, requested: int, index=None) -> str:
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
    rollup += traded_themes(entries, index, len(members))
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
        top = member.get("top")
        lines += ["", f"{'🏆 ' if top else ''}**{escape_markdown(member['member'])}** · "
                      f"{member['state_dist']} · #{member['rank']}{ret_label}",
                  entry["pdf_link"]]
        if summary["buys"]:
            lines.append(f"🟢 **BOUGHT** {format_side(summary['buys'])}")
        if summary["sells"]:
            lines.append(f"🔴 **SOLD** {format_side(summary['sells'])}")
        if not summary["buys"] and not summary["sells"]:
            lines.append(f"_({scanned_note(entry['scanned_pages'])})_"
                         if entry.get("scanned_pages")
                         else "_(transactions could not be read from the PDF)_")

    tops = [e["member"]["top"] for e in entries if e["member"].get("top")]
    if tops:
        lines += ["", f"_🏆 = {top_performer_scope(tops[0])}_"]
    return "\n".join(lines)


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


def build_digest_batch(now: datetime, entries: list[dict], scope_label: str,
                       covered: int, requested: int, index=None) -> tuple[str, int]:
    """Build the largest oldest-first batch that fits the Discord post budget.

    The count tells the caller exactly which reportable filings may be marked seen.
    Overflow remains unseen for the next digest instead of disappearing behind a
    truncation marker.
    """
    if not entries:
        return build_digest(now, entries, scope_label, covered, requested, index), 0

    low, high = 1, len(entries)
    best_count = 0
    best_message = ""
    while low <= high:
        count = (low + high) // 2
        candidate = build_digest(
            now, entries[:count], scope_label, covered, requested, index
        )
        if len(split_message(candidate)) <= MAX_DIGEST_MESSAGES:
            best_count, best_message = count, candidate
            low = count + 1
        else:
            high = count - 1

    if best_count:
        return best_message, best_count

    # A pathological single filing can still carry very long third-party text. Its PDF
    # link is placed before details, so this fallback still identifies what was handled.
    return (
        truncate_to_messages(
            build_digest(now, entries[:1], scope_label, covered, requested, index)
        ),
        1,
    )


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


def traded_themes(entries: list[dict], index, filer_count: int,
                  max_themes: int = 3) -> list[str]:
    """Groups of *different* tickers that mean the same thing, traded by several members.

    most_traded() catches the same symbol filed by several people. This catches the case
    it structurally cannot: six members buying NVDA, AMD, AVGO and TSM on the same day
    share a theme and share no ticker, so an equality-based rollup reports nothing at all.

    A theme has to span more than one member *and* more than one ticker, otherwise it is
    either a repeat of the line above or a single position wearing a sector name.
    """
    if index is None or not index.available:
        return []

    lines = []
    for marker, verb, side in (("🟢", "Bought", "buys"), ("🔴", "Sold", "sells")):
        who: dict[str, set] = defaultdict(set)
        dollars: dict[str, float] = defaultdict(float)
        for entry in entries:
            for label, (_, amount) in entry["summary"][side].items():
                who[label].add(entry["member"]["member"])
                dollars[label] += amount

        themes = []
        for name, tickers in index.themes(who.keys()):
            members = set().union(*(who[t] for t in tickers))
            if len(members) > 1 and len(tickers) > 1:
                themes.append((name, tickers, len(members),
                               sum(dollars[t] for t in tickers)))
        themes.sort(key=lambda t: (-t[2], -t[3], t[0]))

        for name, tickers, n, amount in themes[:max_themes]:
            shown = ", ".join(escape_markdown(t) for t in tickers[:4])
            more = f" +{len(tickers) - 4}" if len(tickers) > 4 else ""
            lines.append(f"{marker} **{verb} across {escape_markdown(name)}:** "
                         f"{n} of {filer_count} members, ~{compact_dollars(amount)} "
                         f"({shown}{more})")
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
    add_pending_grace_arg(parser)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the digest without posting or recording state")
    args = parser.parse_args()

    # The digest is daily, but it shares the seen-set discipline of the hourly monitors:
    # a manual run alongside the scheduled one would otherwise post twice.
    with run_lock("daily_summary"):
        run(args)


def run(args) -> None:
    if not LEADERBOARD_PATH.exists():
        sys.exit(f"Missing {LEADERBOARD_PATH} -- run build_leaderboard.py first")
    leaderboard = json.loads(LEADERBOARD_PATH.read_text())

    top_members = select_members(leaderboard, args.top, args.rank_by)
    if not top_members:
        sys.exit("No members to summarise -- regenerate leaderboard.json.")
    if any("member_key" not in m for m in top_members):
        sys.exit(f"{LEADERBOARD_PATH} predates normalized member identity -- rerun "
                 f"build_leaderboard.py.")
    # Matched on the leaderboard's normalized (surname, district) key, not the printed
    # name: the index spells the same filer differently from one archive to the next.
    # The 🏆 marks the same top-by-alpha set the trade alerts flag, whatever --rank-by
    # chose the digest's coverage, so the mark means one thing everywhere.
    top_performers = top_performers_by_key(leaderboard)
    covered_by_key = {
        tuple(m["member_key"]): dict(m, rank=rank,
                                     top=top_performers.get(tuple(m["member_key"])))
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
    state = load_alert_state(STATE_PATH)
    covered_members = set(covered_by_key)

    # Same rule as watchlist_monitor: a member entering the top-N is baselined so the
    # digest doesn't replay their year, but their last few days still count.
    newly_covered = covered_members - state.members
    if newly_covered:
        stale = baseline_docids(covered_rows, newly_covered, now, args.baseline_window,
                                apply_window=not first_run)
        state.seen |= stale
        log(f"Baselined {len(stale)} existing PTR(s) for {len(newly_covered)} member(s) "
            f"new to the top {len(top_members)}.")
    if covered_members != state.members:
        state.members = covered_members
        if not args.dry_run:
            save_alert_state(STATE_PATH, state)
    if first_run:
        return

    new_rows = [r for r in covered_rows if r["DocID"] not in state.seen]
    if not new_rows and not args.post_empty:
        log(f"No new filings from the top {len(top_members)}; nothing to summarise.")
        return

    def load(row: dict) -> dict:
        pdf_text = None
        try:
            pdf_text = fetch_ptr_text(row["Year"], row["DocID"])
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {row['DocID']}: {exc}", file=sys.stderr)
            transactions = []
        scanned_pages = scanned_page_count(pdf_text)
        # Buys and sells only, and stocks only unless --all-assets: an Exchange is a
        # share-class swap or a merger conversion, so it has no direction to report and
        # a filing holding nothing else drops out here rather than reaching the digest
        # as a member line with neither a BOUGHT nor a SOLD row under it.
        reported = trade_transactions(
            transactions if args.all_assets else equity_transactions(transactions))
        return {
            "member": covered_by_key[member_key(row)],
            "doc_id": row["DocID"],
            "pdf_link": PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
            # Whether the PDF read at all, kept separate from whether it held any stock
            # trades -- the digest prints "could not be read" off the first, and dropping
            # a bond-only filing must not borrow that line. A scan is neither: it is
            # complete and published, and is listed with a link at once.
            "unreadable": not transactions and not scanned_pages,
            "scanned_pages": scanned_pages,
            "txn_count": len(reported),
            "summary": summarise_filing(reported),
        }

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        all_entries = list(pool.map(load, new_rows))

    entries = [e for e in all_entries
               if e["txn_count"] or e["unreadable"] or e["scanned_pages"]]
    # A filing whose PDF has not published yet is held over instead of being reported as
    # unreadable: the Clerk indexes a row before the document is reachable, and tomorrow's
    # digest will have the real contents. Only after the grace period does it go in as
    # unreadable, so a genuinely broken filing is still reported once.
    deferred = set()
    for row, entry in zip(new_rows, all_entries):
        if not entry["unreadable"]:
            state.pending.pop(row["DocID"], None)
            continue
        if pdf_wait_action(row["DocID"], state.pending, now, args.pending_grace) != PDF_GIVE_UP:
            state.pending.setdefault(row["DocID"], now.isoformat())
            deferred.add(row["DocID"])
    if deferred:
        log(f"Holding {len(deferred)} filing(s) whose PDF has not published yet for a "
            f"later digest.")
    entries = [e for e in entries if e["doc_id"] not in deferred]
    reportable_ids = {e["doc_id"] for e in entries}
    silent_handled = {
        r["DocID"] for r in new_rows
        if r["DocID"] not in deferred and r["DocID"] not in reportable_ids
    }
    if not entries and not args.post_empty:
        log(f"{plural(len(new_rows), 'new filing')} from the top {len(top_members)}, "
            f"none with reportable stock transactions; nothing to summarise.")
        if not args.dry_run:
            # Still handled -- leaving them unseen would re-fetch every PDF tomorrow.
            state.seen |= silent_handled
            save_alert_state(STATE_PATH, state)
        return

    scope_label = {"alpha": "est. alpha", "return": "est. return"}.get(
        args.rank_by, "dollars traded")
    # Optional: the digest is a report, not a search tool, so a missing or unbuilt index
    # costs it the theme lines and nothing else.
    message, included_count = build_digest_batch(
        now, entries, scope_label, len(top_members), args.top, ThemeIndex()
    )
    included_entries = entries[:included_count]
    included_ids = {e["doc_id"] for e in included_entries}
    size_deferred = len(entries) - included_count
    if args.verbose or args.dry_run:
        print(message)

    filtered = len(silent_handled)
    tail = f" ({filtered} with no stock transactions)" if filtered else ""
    size_tail = f" {size_deferred} deferred by the message limit." if size_deferred else ""

    if args.dry_run:
        log(f"Would post a digest covering "
            f"{plural(included_count, 'new filing')}{tail}.{size_tail}")
        return

    try:
        send_discord_message(webhook_url, message)
    except requests.RequestException as exc:
        # Leave every DocID unseen so the next run retries rather than losing the day.
        sys.exit(f"Failed to post the digest: {exc}")

    state.seen |= silent_handled | included_ids
    save_alert_state(STATE_PATH, state)
    log(f"Posted a digest covering {plural(included_count, 'new filing')}{tail} from the "
        f"top {len(top_members)}.{size_tail}")


if __name__ == "__main__":
    main()
