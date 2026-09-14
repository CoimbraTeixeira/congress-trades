#!/usr/bin/env python3
"""Build a per-member stock trading performance leaderboard from House PTR filings.

Methodology (share-based FIFO, mark-to-market):
- Scope is a trailing window (PERFORMANCE_WINDOW_YEARS, 3 by default) measured on the
  TRANSACTION date, read from every filing-year archive that can contain one. A single
  archive is not a period: it holds whatever members happened to file that year, trades
  from years earlier included, so ranking one archive ranks recent filers rather than
  good pickers over a stated span.
- Each disclosed transaction only gives a dollar RANGE, never an exact amount or share
  count. We take the midpoint of that range as the transaction's dollar value.
- That dollar value is converted to ESTIMATED SHARES at the closing price on the
  transaction date. Buys and sells of the same ticker by the same member are then
  FIFO-matched in share space, so a realized gain is
  shares_matched * (sell_price - buy_price) -- comparing like with like. (Matching in
  dollar space instead would net a buy's cost against a sale's proceeds, which are
  quoted at different prices and are not the same unit.)
- Sales exceeding matched buys (position opened before the window) have unknown cost
  basis and are excluded from gain calculations.
- Unsold shares are marked to market at the latest close for an unrealized gain.
- Every lot is also scored against SPY over its OWN holding window, dollar-weighted the
  same way, giving an alpha estimate. Members are ranked on alpha rather than raw
  return: +9% in a year the index returned +15% is underperformance, and ranking on the
  raw figure promotes whoever held the most beta rather than whoever picked well.
- The price window opens at the earliest transaction actually parsed, not January 1:
  filings routinely disclose prior-year trades, and pricing those at the first close of
  the filing year is silently wrong rather than merely missing.
- Non-equity assets (government securities, funds, crypto) are excluded from gain
  calculations but still counted as trades.
- Members below --min-invested / --min-trades are still emitted but flagged
  "ranked": false, so a single lucky $15k trade doesn't top the board.
"""

import argparse
import json
import logging
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from house_disclosures import (
    PERFORMANCE_WINDOW_YEARS,
    PTR_FILING_TYPE,
    amount_midpoint,
    fetch_index_years,
    fetch_ptr_text,
    format_filer_name,
    member_key,
    parse_date,
    parse_transactions,
    performance_index_years,
    performance_window_start,
    resolve_ticker,
    trade_transactions,
)

REPO_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_DIR / "leaderboard.json"
PRICE_BATCH_SIZE = 200
FETCH_WORKERS = 8

# The benchmark every member's return is measured against. A raw return says nothing on
# its own: +9% in a year the index returned +15% is underperformance, and ranking on the
# raw figure promotes whoever held the most beta in an up year.
BENCHMARK_TICKER = "SPY"

# Days of price history fetched before the earliest trade. PriceBook.on() rolls forward
# to the next close, so a trade dated on a weekend or a holiday needs the window to open
# strictly before it -- without the run-in the earliest trade of all would be the one
# trade that falls off the front.
PRICE_WINDOW_RUN_IN_DAYS = 7

# yfinance logs a multi-line block for every delisted/renamed ticker, which drowns the
# run's own output. Delisted symbols are expected here and handled as "unpriced".
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def price_window_start(transactions: list[dict], window_start: datetime) -> str:
    """The earliest date the price series has to cover, as an ISO date string.

    The performance window's own start, unless every trade inside it is newer -- there
    is no point downloading closes nothing prices against. Either way it is backed off
    by PRICE_WINDOW_RUN_IN_DAYS, because PriceBook.on() rolls forward to the next close:
    a trade dated on a weekend or a holiday needs the series to open strictly before it,
    and without the run-in the oldest trade of all is the one that falls off the front.

    Trades reaching here are already filtered to the window on their transaction date,
    so a misparsed ancient date cannot drag a several-hundred-ticker download back a
    decade -- it is dropped before it gets here rather than widening the fetch.
    """
    dates = [
        d for d in (parse_date(t.get("txn_date", "")) for t in transactions)
        if d is not None
    ]
    earliest = max(min(dates, default=window_start), window_start)
    return (earliest - timedelta(days=PRICE_WINDOW_RUN_IN_DAYS)).date().isoformat()


def in_performance_window(txn: dict, start: datetime, end: datetime) -> bool:
    """True if a transaction happened inside the performance window.

    Measured on the transaction date, never on the year the archive files it under. The
    45-day deadline alone pushes December trades into the next year's archive and late
    filers stretch that to months, so a 2026 archive currently reaches back to 2023 --
    scoring its contents answers "who filed lately", not "who picked well since 2023".
    An unparseable or future-dated row is outside the window by definition: there is no
    date to price it on.
    """
    date = parse_date(txn.get("txn_date", ""))
    return date is not None and start <= date <= end


class PriceBook:
    """Daily closes for every ticker we need, fetched in a couple of batch requests
    instead of one HTTP round-trip per (ticker, date) pair.

    All prices come from the same split/dividend-adjusted series, so a historical
    close and the latest close are directly comparable. Mixing an adjusted history
    with an unadjusted live quote would show a phantom gain on any dividend payer.
    """

    @staticmethod
    def yahoo_symbol(ticker: str) -> str:
        """Filings print class shares as BRK.B; Yahoo wants BRK-B."""
        return ticker.replace(".", "-")

    def __init__(self, tickers: set[str], start: str, end: str) -> None:
        self._closes: dict[str, pd.Series] = {}
        self._symbols = {t: self.yahoo_symbol(t) for t in tickers}
        tickers = sorted({self.yahoo_symbol(t) for t in tickers})
        for i in range(0, len(tickers), PRICE_BATCH_SIZE):
            batch = tickers[i:i + PRICE_BATCH_SIZE]
            try:
                data = yf.download(
                    batch, start=start, end=end, auto_adjust=True,
                    progress=False, group_by="ticker", threads=True,
                )
            except Exception as exc:
                print(f"warning: price batch failed ({exc})", file=sys.stderr)
                continue
            for ticker in batch:
                series = self._extract(data, ticker, len(batch))
                if series is not None and not series.empty:
                    self._closes[ticker] = series

    @staticmethod
    def _extract(data, ticker: str, batch_len: int):
        try:
            frame = data[ticker] if batch_len > 1 else data
            series = frame["Close"].dropna()
        except (KeyError, TypeError):
            return None
        if getattr(series.index, "tz", None) is not None:
            series.index = series.index.tz_localize(None)
        return series

    def on(self, ticker: str, date_str: str) -> float | None:
        """Close on the given date, rolling forward past weekends and holidays.

        A date *before* the series begins yields None rather than the first close we
        happen to hold. Rolling forward is right for a weekend -- the next close is
        days away -- but a trade predating the fetched window is not a weekend, and
        searchsorted would silently answer it with a close months or years later. That
        turns a price we do not have into a confident wrong one; the caller counts a
        None as an unpriced trade, which is a fact a reader can discount.
        """
        series = self._closes.get(self.yahoo_symbol(ticker))
        if series is None:
            return None
        try:
            target = pd.Timestamp(datetime.strptime(date_str, "%m/%d/%Y"))
        except ValueError:
            return None
        if target < series.index[0]:
            return None
        pos = series.index.searchsorted(target)
        if pos >= len(series):
            return None
        return float(series.iloc[pos])

    def latest(self, ticker: str) -> float | None:
        series = self._closes.get(self.yahoo_symbol(ticker))
        return float(series.iloc[-1]) if series is not None and not series.empty else None

    def __contains__(self, ticker: str) -> bool:
        return self.yahoo_symbol(ticker) in self._closes

    def __len__(self) -> int:
        return len(self._closes)


def collect_transactions(ptr_rows: list[dict], verbose: bool,
                         use_cache: bool = True) -> tuple[dict[tuple, list[dict]], int]:
    """Fetch and parse every PTR, grouped by member. PDFs are fetched concurrently --
    serially this is a few thousand round-trips over a multi-year window and dominates
    the runtime; fetch_ptr_text's on-disk text cache removes it entirely on a re-run."""
    by_member: dict[tuple, list[dict]] = defaultdict(list)

    def fetch(row):
        try:
            text = fetch_ptr_text(row["Year"], row["DocID"], use_cache=use_cache)
            return row, parse_transactions(text) if text else []
        except Exception as exc:
            return row, exc

    parsed_count = failed = 0
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for i, (row, result) in enumerate(pool.map(fetch, ptr_rows)):
            if isinstance(result, Exception):
                failed += 1
                if verbose:
                    print(f"  {row['DocID']}: fetch/parse failed: {result}", file=sys.stderr)
                continue
            parsed_count += len(result)
            for t in result:
                t["_doc_id"] = row["DocID"]
                t["_filing_date"] = row.get("FilingDate", "")
            by_member[member_key(row)].extend(result)
            if verbose and (i + 1) % 50 == 0:
                print(f"  processed {i+1}/{len(ptr_rows)} PTRs...", file=sys.stderr)

    print(f"Parsed {parsed_count} transaction line(s) from {len(ptr_rows)} PTR(s)"
          f"{f', {failed} failed' if failed else ''}.", file=sys.stderr)
    return by_member, parsed_count


def benchmark_gain(prices: PriceBook, cost: float,
                   start_date: str, end_date: str | None) -> float | None:
    """What the same cost basis would have gained in the benchmark over the same window.

    Held to the lot's own dates rather than to the calendar year, so a position opened in
    March is compared against the index from March. Comparing every member against a
    single year-to-date index figure would credit whoever simply held for longer.
    """
    start_price = prices.on(BENCHMARK_TICKER, start_date)
    end_price = (prices.on(BENCHMARK_TICKER, end_date) if end_date
                 else prices.latest(BENCHMARK_TICKER))
    if not start_price or not end_price:
        return None
    return cost * (end_price / start_price - 1)


def score_member(txns: list[dict], prices: PriceBook) -> dict:
    """FIFO-match a member's trades in share space and mark the remainder to market."""
    by_ticker = defaultdict(list)
    for t in txns:
        by_ticker[t["ticker"]].append(t)

    realized = unrealized = invested = 0.0
    # Benchmark basis is tracked separately from `invested`: a lot the benchmark could
    # not be priced over still counts as invested, and dividing its gain by the full
    # basis would understate the index rather than leave it out.
    bench_gain = bench_basis = 0.0
    priced_trades = unknown_basis = unpriced = 0
    detail = []

    for ticker, tx_list in by_ticker.items():
        tx_list.sort(key=lambda t: datetime.strptime(t["txn_date"], "%m/%d/%Y"))
        lots = []  # FIFO queue of [shares_remaining, buy_price, buy_date]
        for t in tx_list:
            dollars = amount_midpoint(t["amount"])
            price = prices.on(ticker, t["txn_date"])
            if dollars is None or not price:
                unpriced += 1
                continue
            shares = dollars / price

            if t["type"] == "Purchase":
                lots.append([shares, price, t["txn_date"]])
                continue

            remaining = shares
            while remaining > 0 and lots:
                lot = lots[0]
                matched = min(lot[0], remaining)
                cost = matched * lot[1]
                gain = matched * (price - lot[1])
                realized += gain
                invested += cost
                priced_trades += 1
                bench = benchmark_gain(prices, cost, lot[2], t["txn_date"])
                if bench is not None:
                    bench_gain += bench
                    bench_basis += cost
                detail.append({
                    "ticker": ticker, "type": "realized",
                    "buy_date": lot[2], "sell_date": t["txn_date"],
                    "amount": round(cost, 2), "gain": round(gain, 2),
                    "benchmark_gain": round(bench, 2) if bench is not None else None,
                })
                lot[0] -= matched
                remaining -= matched
                if lot[0] <= 1e-9:
                    lots.pop(0)
            if remaining > 1e-9:
                unknown_basis += 1  # sold more than we saw bought in this window

        latest = prices.latest(ticker)
        for shares, buy_price, buy_date in lots:
            if shares <= 1e-9 or not latest:
                continue
            cost = shares * buy_price
            gain = shares * (latest - buy_price)
            unrealized += gain
            invested += cost
            priced_trades += 1
            bench = benchmark_gain(prices, cost, buy_date, None)
            if bench is not None:
                bench_gain += bench
                bench_basis += cost
            detail.append({
                "ticker": ticker, "type": "unrealized",
                "buy_date": buy_date, "sell_date": None,
                "amount": round(cost, 2), "gain": round(gain, 2),
                "benchmark_gain": round(bench, 2) if bench is not None else None,
            })

    total_gain = realized + unrealized
    return_pct = total_gain / invested * 100 if invested else None
    # The index return over the *same* holding windows, dollar-weighted the same way, so
    # the difference is attributable to the picks rather than to time in the market.
    bench_pct = bench_gain / bench_basis * 100 if bench_basis else None
    alpha = (return_pct - bench_pct
             if return_pct is not None and bench_pct is not None else None)
    return {
        "total_trades": len(txns),
        "priced_trades": priced_trades,
        "unpriced_trades": unpriced,
        "skipped_unknown_basis": unknown_basis,
        "invested_total_estimate": round(invested, 2),
        "realized_gain_estimate": round(realized, 2),
        "unrealized_gain_estimate": round(unrealized, 2),
        "total_gain_estimate": round(total_gain, 2),
        "return_pct_estimate": round(return_pct, 2) if return_pct is not None else None,
        "benchmark_ticker": BENCHMARK_TICKER,
        "benchmark_gain_estimate": round(bench_gain, 2) if bench_basis else None,
        "benchmark_return_pct_estimate": round(bench_pct, 2) if bench_pct is not None else None,
        "alpha_pct_estimate": round(alpha, 2) if alpha is not None else None,
        "trade_detail": detail,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--window-years", type=int, default=PERFORMANCE_WINDOW_YEARS,
                        help=f"Trailing performance window in years, measured on the "
                             f"transaction date (default {PERFORMANCE_WINDOW_YEARS})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N PTRs (for quick testing)")
    parser.add_argument("--no-cache", action="store_true",
                        help="Re-fetch and re-extract every PDF instead of reading the "
                             "cached text under cache/ptr_text")
    parser.add_argument("--min-invested", type=float, default=25_000,
                        help="Minimum estimated cost basis to be ranked (default 25000)")
    parser.add_argument("--min-trades", type=int, default=3,
                        help="Minimum priced trades to be ranked (default 3)")
    args = parser.parse_args()
    if args.window_years < 1:
        sys.exit("--window-years must be at least 1")

    now = datetime.now(timezone.utc)
    window_start = performance_window_start(now, args.window_years)
    window_end = now.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
    index_years = performance_index_years(now, args.window_years)

    rows = fetch_index_years(index_years)
    ptr_rows = [r for r in rows if r.get("FilingType") == PTR_FILING_TYPE]
    if args.limit:
        ptr_rows = ptr_rows[:args.limit]
    print(f"{len(ptr_rows)} PTR(s) to process from the "
          f"{', '.join(str(y) for y in index_years)} archive(s), for trades between "
          f"{window_start.date()} and {window_end.date()}.", file=sys.stderr)

    # The newest archive wins a filer's display fields: a member who changed their name
    # or acquired a title mid-window should read as they file today. Index rows arrive
    # newest year first, so the first one seen wins. Identity itself is member_key(),
    # which normalizes across those spellings -- see house_disclosures.member_key.
    meta: dict[tuple, dict] = {}
    for r in ptr_rows:
        meta.setdefault(member_key(r), {
            "name": format_filer_name(r),
            "first": r.get("First", "").strip(),
            "last": r.get("Last", "").strip(),
            "state_dist": r.get("StateDst", "?"),
        })
    by_member, parsed_lines = collect_transactions(ptr_rows, args.verbose,
                                                   use_cache=not args.no_cache)

    # Resolve tickers once across all members so the name-lookup cache is shared. Only
    # in-window buys and sells get here: the archives are fetched by filing year and
    # reach further back than the window they are read for, and a row that is neither a
    # purchase nor a sale (an Exchange) has no direction to score.
    tradeable: dict[tuple, list[dict]] = defaultdict(list)
    tickers: set[str] = set()
    non_trade_lines = out_of_window_lines = 0
    for key, txns in by_member.items():
        trades = trade_transactions(txns)
        non_trade_lines += len(txns) - len(trades)
        for t in trades:
            if not in_performance_window(t, window_start, window_end):
                out_of_window_lines += 1
                continue
            t["ticker"] = resolve_ticker(t)
            if t["ticker"]:
                tickers.add(t["ticker"])
                tradeable[key].append(t)
    print(f"{sum(len(v) for v in tradeable.values())} in-window Purchase/Sale line(s) "
          f"across {len(tickers)} ticker(s); {out_of_window_lines} trade line(s) fell "
          f"outside the window and {non_trade_lines} were not buys or sells.",
          file=sys.stderr)

    # The price series opens at the window's start, or later if every trade in it is --
    # a price the series does not cover is reported as unpriced rather than silently
    # answered with a later close.
    all_txns = [t for txns in tradeable.values() for t in txns]
    start = price_window_start(all_txns, window_start)
    prices = PriceBook(tickers | {BENCHMARK_TICKER}, start=start,
                       end=(now.date().isoformat()))
    priced = sum(1 for t in tickers if t in prices)
    print(f"Priced {priced}/{len(tickers)} ticker(s) from {start}.", file=sys.stderr)
    if BENCHMARK_TICKER not in prices:
        print(f"warning: {BENCHMARK_TICKER} did not price; returns will carry no "
              f"benchmark comparison", file=sys.stderr)

    leaderboard = []
    for key, txns in tradeable.items():
        entry = {
            "member": meta[key]["name"],
            # Identity the monitors match index rows against; the name fields beside it
            # are for display and are whatever the newest archive spells them.
            "member_key": list(key),
            "first": meta[key]["first"],
            "last": meta[key]["last"],
            "state_dist": meta[key]["state_dist"],
        }
        entry.update(score_member(txns, prices))
        entry["ranked"] = (
            entry["return_pct_estimate"] is not None
            and entry["invested_total_estimate"] >= args.min_invested
            and entry["priced_trades"] >= args.min_trades
        )
        leaderboard.append(entry)

    # Ordered by alpha where there is one: a raw return ranks a bull-market beta holder
    # above a genuine stock picker. Members with no benchmarked lot fall back to the raw
    # return rather than dropping out of the file.
    leaderboard.sort(
        key=lambda m: (m["ranked"],
                       m["alpha_pct_estimate"] if m["alpha_pct_estimate"] is not None
                       else (m["return_pct_estimate"] or -1e9)),
        reverse=True,
    )

    OUTPUT_PATH.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "window_years": args.window_years,
        "window_start": window_start.date().isoformat(),
        "window_end": window_end.date().isoformat(),
        "index_years": index_years,
        "min_invested": args.min_invested,
        "min_trades": args.min_trades,
        "totals": {
            "ptr_count": len(ptr_rows),
            "transaction_lines": parsed_lines,
            "tradeable_lines": sum(len(v) for v in tradeable.values()),
            "out_of_window_lines": out_of_window_lines,
            "non_trade_lines": non_trade_lines,
            "tickers": len(tickers),
            "tickers_priced": len(prices),
        },
        "benchmark_ticker": BENCHMARK_TICKER,
        "price_window_start": start,
        "methodology": (
            f"Scope is every Purchase/Sale disclosed with a transaction date in the "
            f"{args.window_years}-year window ending {window_end.date().isoformat()}, "
            "read from each filing-year archive that can contain one. Estimated shares = "
            "disclosed range midpoint / close on the transaction date; buys and sells "
            "FIFO-matched in share space for realized gains; unsold shares marked to the "
            "latest close. Unmatched sales (positions opened before the window) and "
            "non-equity assets are excluded from gain estimates but counted as trades. "
            "Members below min_invested/min_trades are unranked. "
            f"alpha_pct_estimate is the return less what {BENCHMARK_TICKER} returned over "
            "the same holding windows, dollar-weighted the same way."
        ),
        "members": leaderboard,
    }, indent=2))
    print(f"Wrote leaderboard for {len(leaderboard)} member(s) to {OUTPUT_PATH}", file=sys.stderr)

    ranked = [m for m in leaderboard if m["ranked"]]
    print(f"\nTop 10 of {len(ranked)} ranked member(s) by estimated alpha vs "
          f"{BENCHMARK_TICKER}:")
    for m in ranked[:10]:
        alpha = m["alpha_pct_estimate"]
        alpha_label = f"{alpha:+7.2f}%" if alpha is not None else "      —"
        print(f"  {alpha_label} alpha  ({m['return_pct_estimate']:+.2f}% vs "
              f"{m['benchmark_return_pct_estimate']:+.2f}% {BENCHMARK_TICKER})  "
              f"{m['member']} ({m['state_dist']})  "
              f"gain=${m['total_gain_estimate']:,.0f} on ~${m['invested_total_estimate']:,.0f} "
              f"({m['priced_trades']} priced trades)")


if __name__ == "__main__":
    main()
