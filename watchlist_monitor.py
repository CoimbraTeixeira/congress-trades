#!/usr/bin/env python3
"""Daily check: did anyone on the top-10 watchlist (watchlist.json) file a new
PTR containing a Purchase? Alerts fast on buys specifically, separate from the
general congress_monitor.py (which alerts on every new PTR, any type)."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from house_disclosures import (
    PDF_URL_TMPL,
    PTR_FILING_TYPE,
    equity_transactions,
    fetch_index_years,
    fetch_ptr_text,
    filing_sort_key,
    index_years,
    member_key,
    parse_transactions,
    scanned_page_count,
)
from monitor_common import (
    DEFAULT_BASELINE_WINDOW_DAYS,
    LATE_LEGEND,
    PDF_ANNOUNCE,
    PDF_GIVE_UP,
    PDF_WAIT,
    STATE_DIR,
    add_common_args,
    any_late,
    baseline_docids,
    display_name,
    escape_markdown,
    format_transaction,
    load_alert_state,
    load_webhook_url,
    log,
    pdf_wait_action,
    plural,
    run_lock,
    save_alert_state,
    scanned_note,
    send_discord_message,
)
from search_common import THEME_SIMILARITY, ThemeIndex

REPO_DIR = Path(__file__).resolve().parent
WATCHLIST_PATH = REPO_DIR / "watchlist.json"
STATE_PATH = STATE_DIR / "watchlist_seen_docids.json"


def collapse_buys(buys: list[dict]) -> list[tuple[dict, int]]:
    """Group identical purchase rows into (transaction, count) pairs. A filing routinely
    discloses the same ticker, date and amount range several times over (one row per
    account), which renders as a wall of duplicate lines otherwise."""
    grouped: dict[tuple, list] = {}
    for t in buys:
        key = (t["owner"], t["asset"], t["txn_date"], t["amount"], t["partial"])
        grouped.setdefault(key, [t, 0])[1] += 1
    return [(t, n) for t, n in grouped.values()]


def theme_note(index, member: dict, buys: list[dict]) -> str | None:
    """One line placing this buy against what the member already owns.

    The alert says who bought what; it never said whether that is more of the same or a
    direction they have not traded before. An exact-ticker lookup cannot answer it -- it
    has no way to know ENPH relates to FSLR -- so the comparison runs over the indexed
    business-description vectors. Returns None whenever the index cannot speak to it,
    because a missing context line is fine and a missing alert is not.
    """
    if index is None or not index.available:
        return None
    bought = index.known(t["ticker"] for t in buys if t.get("ticker"))
    if not bought:
        return None
    held = [t for t in index.holdings(display_name(member)) if t not in bought]
    if not held:
        return None

    nearest = [(tk, index.closest(tk, held)) for tk in bought]
    ranked = sorted(((tk, m, s) for tk, ms in nearest if ms for m, s in [ms]),
                    key=lambda x: -x[2])
    if not ranked:
        return None
    ticker, like, score = ranked[0]
    if score < THEME_SIMILARITY:
        return (f"🧭 New direction — nothing in this member's {len(held)} existing "
                f"position(s) resembles {escape_markdown(ticker)}")
    theme = index.label([ticker, like])
    return (f"🧭 More {escape_markdown(theme)} — {escape_markdown(ticker)} sits "
            f"alongside their existing {escape_markdown(like)}")


def build_message(member: dict, row: dict, buys: list[dict], watchlist_size: int,
                  index=None) -> str:
    filing_date = row.get("FilingDate", "?")
    pdf_link = PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"])
    name = escape_markdown(member["member"])
    collapsed = collapse_buys(buys)

    # Rank alone ("watchlist #3") never said out of how many, or what the ranking
    # measures; both fit in the line that establishes why this member is watched.
    # The raw return leads with the benchmark beside it, because a return quoted alone
    # cannot be read: +12% is a good year or a bad one depending on what the index did.
    standing = f"{member['return_pct_estimate']:+.1f}% estimated return"
    alpha = member.get("alpha_pct_estimate")
    bench_pct = member.get("benchmark_return_pct_estimate")
    if alpha is not None and bench_pct is not None:
        standing += (f" · {alpha:+.1f}% vs {member.get('benchmark', 'the benchmark')} "
                     f"({bench_pct:+.1f}%)")
    lines = [
        f"🚨 **{name}** ({member['state_dist']}) — watchlist buy",
        f"#{member['rank']} of {watchlist_size} on the watchlist · {standing}",
    ]
    if not buys:
        # Silence here would read as "this filing had no purchases", which is the one
        # thing it does not mean. Only sent once the grace period for the PDF to publish
        # has run out -- before that the filing is announced instead, and the buys follow.
        lines += [
            f"Filed {filing_date} · transactions could not be read from the PDF, so "
            f"this may or may not contain a buy",
            pdf_link,
        ]
        return "\n".join(lines)

    # Count the disclosed rows, not the collapsed lines -- the ×N on a line is what
    # reconciles the two, so a header saying "3 purchases" above a "×2" would not add up.
    lines += [f"Filed {filing_date} · {plural(len(buys), 'purchase')}", ""]
    lines += [format_transaction(t, filing_date, show_direction=False, count=n)
              for t, n in collapsed]
    note = theme_note(index, member, buys)
    if note:
        lines.append(note)
    lines.append("")
    if any_late(buys, filing_date):
        lines.append(LATE_LEGEND)
    lines.append(f"Full PTR: {pdf_link}")
    return "\n".join(lines)


def build_landed_message(member: dict, row: dict, watchlist_size: int) -> str:
    """A watched member's filing is in the index but its PDF is not published yet.

    Worth its own alert here for the same reason as the general monitor, and more so:
    the point of this monitor is speed on a watched member's buys, and the index row is
    the earliest evidence that exists.
    """
    return "\n".join([
        f"🚨 **{escape_markdown(member['member'])}** ({member['state_dist']}) — "
        f"watchlist filing landed",
        f"#{member['rank']} of {watchlist_size} on the watchlist",
        f"Filed {row.get('FilingDate', '?')} · contents not published yet; any buys "
        f"will follow when the PDF appears",
        PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
    ])


def build_scanned_message(member: dict, row: dict, pages: int, watchlist_size: int) -> str:
    """A watched member filed on paper. The scan may hold a buy, and this monitor exists
    for speed on exactly that, so it is said on the first sighting rather than after a
    grace period spent waiting for text a scan will never have."""
    return "\n".join([
        f"🚨 **{escape_markdown(member['member'])}** ({member['state_dist']}) — "
        f"watchlist filing",
        f"#{member['rank']} of {watchlist_size} on the watchlist",
        f"Filed {row.get('FilingDate', '?')} · {scanned_note(pages)}; it may or may not "
        f"contain a buy",
        PDF_URL_TMPL.format(year=row["Year"], doc_id=row["DocID"]),
    ])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_args(parser)
    parser.add_argument(
        "--baseline-window", type=int, default=DEFAULT_BASELINE_WINDOW_DAYS,
        help=f"When a member joins the watchlist, still alert on filings from the last "
             f"N days (default {DEFAULT_BASELINE_WINDOW_DAYS}); 0 keeps only same-day ones")
    args = parser.parse_args()

    # Hourly polling can overlap a slow run, and both would post the same buy alert.
    with run_lock("watchlist_monitor"):
        run(args)


def run(args) -> None:
    if not WATCHLIST_PATH.exists():
        sys.exit(f"Missing {WATCHLIST_PATH} -- run build_watchlist.py first")
    watchlist = json.loads(WATCHLIST_PATH.read_text())
    benchmark = watchlist.get("benchmark_ticker") or "the benchmark"
    if any("member_key" not in m for m in watchlist["members"]):
        sys.exit(f"{WATCHLIST_PATH} predates normalized member identity -- rerun "
                 f"build_leaderboard.py and build_watchlist.py.")
    # The key is the leaderboard's own normalized (surname, district): the index spells
    # the same filer's name differently from archive to archive, so matching on the
    # printed name silently drops that member's filings.
    watched_by_key = {
        tuple(m["member_key"]): dict(m, rank=rank, benchmark=benchmark)
        for rank, m in enumerate(watchlist["members"], start=1)
    }

    webhook_url = None if args.dry_run else load_webhook_url()
    now = datetime.now(timezone.utc)

    rows = fetch_index_years(index_years(now))
    watched_rows = sorted(
        (r for r in rows
         if r.get("FilingType") == PTR_FILING_TYPE and member_key(r) in watched_by_key),
        key=filing_sort_key,
    )
    if args.verbose:
        print(f"-- {len(rows)} filings, {len(watched_rows)} PTRs from watchlist members --")

    first_run = not STATE_PATH.exists()
    state = load_alert_state(STATE_PATH)
    watched_members = set(watched_by_key)

    def persist() -> None:
        if not args.dry_run:
            save_alert_state(STATE_PATH, state)

    # Baseline any member who wasn't on the watchlist last run -- on the very first run
    # that's everyone, and later it's whoever the latest build_watchlist.py promoted.
    # Without this, every rotation replays the new member's back catalogue as alerts.
    # Their last few days stay alertable though: a member is promoted precisely because
    # they trade well, and suppressing the trade that earned them the slot is backwards.
    #
    # State holds last run's membership rather than every member ever seen, so someone
    # who drops off and is promoted again months later is re-baselined instead of
    # replaying their whole absence. build_watchlist.py runs daily, so that happens.
    newly_watched = watched_members - state.members
    if newly_watched:
        stale = baseline_docids(watched_rows, newly_watched, now, args.baseline_window,
                                apply_window=not first_run)
        state.seen |= stale
        label = "watchlist member(s)" if first_run else "newly promoted member(s)"
        log(f"Baselined {len(stale)} existing PTR(s) for {len(newly_watched)} "
            f"{label}, no alerts sent for those.")
    if watched_members != state.members:
        state.members = watched_members
        persist()
    if first_run:
        return

    # A filing announced ahead of its PDF stays out of `seen`, so it returns here every
    # run until the buys can be read or the grace period expires.
    new_rows = [r for r in watched_rows if r["DocID"] not in state.seen]
    backlog = max(0, len(new_rows) - args.max_alerts)
    if backlog:
        log(f"{len(new_rows)} new watchlist PTR(s) exceeds --max-alerts={args.max_alerts}; "
            f"handling {args.max_alerts} now, leaving {backlog} for the next run.")
        new_rows = new_rows[: args.max_alerts]
    if args.verbose:
        print(f"-- {len(new_rows)} new watchlist PTR(s), {len(state.pending)} awaiting "
              f"a PDF --")

    # Built once per run, not per alert: it reads the whole ticker collection, and the
    # vectors it needs are already in the store, so no embedding model is loaded here.
    index = ThemeIndex()
    sent = announced = waiting = scanned = 0
    for row in new_rows:
        doc_id = row["DocID"]
        member = watched_by_key[member_key(row)]
        pdf_text = None
        try:
            pdf_text = fetch_ptr_text(row["Year"], doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            print(f"Failed to fetch/parse PDF {doc_id}: {exc}", file=sys.stderr)
            transactions = []

        pages = scanned_page_count(pdf_text)
        if pages:
            message = build_scanned_message(member, row, pages, len(watched_by_key))
            if not deliver(message, doc_id, webhook_url, args):
                continue
            scanned += 1
            state.pending.pop(doc_id, None)
            state.seen.add(doc_id)
            persist()
            continue

        # A filing we couldn't read is not a filing without purchases, and staying silent
        # on it is indistinguishable from "nothing happened". But an unpublished PDF is
        # not an unreadable one either: announce the filing, then alert the buys when the
        # contents appear, and only call it unreadable once the grace period is spent.
        if not transactions:
            action = pdf_wait_action(doc_id, state.pending, now, args.pending_grace)
            if action == PDF_WAIT:
                waiting += 1
                if args.verbose:
                    print(f"{doc_id}: PDF still unreadable, announced "
                          f"{state.pending[doc_id]} — waiting")
                continue
            message = (build_landed_message(member, row, len(watched_by_key))
                       if action == PDF_ANNOUNCE
                       else build_message(member, row, [], len(watched_by_key), index))
            if not deliver(message, doc_id, webhook_url, args):
                continue
            if action == PDF_ANNOUNCE:
                announced += 1
                state.pending[doc_id] = now.isoformat()
            elif action == PDF_GIVE_UP:
                sent += 1
                state.pending.pop(doc_id, None)
                state.seen.add(doc_id)
            persist()
            continue

        state.pending.pop(doc_id, None)
        purchases = [t for t in transactions if t["type"] == "Purchase"]
        # A watchlist member buying a Treasury note is not the signal this monitor exists
        # for -- they are ranked on equity returns, so a bond purchase says nothing about
        # why they are watched.
        buys = purchases if args.all_assets else equity_transactions(purchases)
        if buys:
            message = build_message(member, row, buys, len(watched_by_key), index)
            if not deliver(message, doc_id, webhook_url, args):
                continue
            sent += 1
        state.seen.add(doc_id)
        persist()

    verb = "Would send" if args.dry_run else "Sent"
    log(f"{verb} {sent} buy alert(s) out of {len(new_rows)} new watchlist filing(s)."
        + (f" {announced} filing(s) announced ahead of their PDF." if announced else "")
        + (f" {waiting} still awaiting a PDF." if waiting else "")
        + (f" {scanned} scanned paper filing(s) linked without contents." if scanned else "")
        + (f" {backlog} deferred." if backlog else ""))


def deliver(message: str, doc_id: str, webhook_url: str | None, args) -> bool:
    """Post one alert. False means the caller must not record the filing as handled."""
    if args.verbose or args.dry_run:
        print(message)
        print("---")
    if args.dry_run:
        return True
    try:
        send_discord_message(webhook_url, message)
    except requests.RequestException as exc:
        print(f"Failed to send Discord message for {doc_id}: {exc}", file=sys.stderr)
        return False
    return True


if __name__ == "__main__":
    main()
