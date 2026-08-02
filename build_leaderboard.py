#!/usr/bin/env python3
"""Build a per-member stock trading performance leaderboard from House PTR filings.

Methodology (share-based FIFO, mark-to-market):
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
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

from house_disclosures import (
    PTR_FILING_TYPE,
    amount_midpoint,
    fetch_index,
    fetch_ptr_text,
    format_filer_name,
    member_key,
    parse_transactions,
    resolve_ticker,
)

REPO_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_DIR / "leaderboard.json"
YEAR = datetime.now(timezone.utc).year
PRICE_BATCH_SIZE = 200
FETCH_WORKERS = 8

# yfinance logs a multi-line block for every delisted/renamed ticker, which drowns the
# run's own output. Delisted symbols are expected here and handled as "unpriced".
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


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
        """Close on the given date, rolling forward past weekends and holidays."""
        series = self._closes.get(self.yahoo_symbol(ticker))
        if series is None:
            return None
        try:
            target = pd.Timestamp(datetime.strptime(date_str, "%m/%d/%Y"))
        except ValueError:
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


def collect_transactions(ptr_rows: list[dict], verbose: bool) -> tuple[dict[tuple, list[dict]], int]:
    """Fetch and parse every PTR, grouped by member. PDFs are fetched concurrently --
    serially this is several hundred round-trips and dominates the runtime."""
    by_member: dict[tuple, list[dict]] = defaultdict(list)

    def fetch(row):
        try:
            text = fetch_ptr_text(row["Year"], row["DocID"])
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


def score_member(txns: list[dict], prices: PriceBook) -> dict:
    """FIFO-match a member's trades in share space and mark the remainder to market."""
    by_ticker = defaultdict(list)
    for t in txns:
        by_ticker[t["ticker"]].append(t)

    realized = unrealized = invested = 0.0
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
                detail.append({
                    "ticker": ticker, "type": "realized",
                    "buy_date": lot[2], "sell_date": t["txn_date"],
                    "amount": round(cost, 2), "gain": round(gain, 2),
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
            detail.append({
                "ticker": ticker, "type": "unrealized",
                "buy_date": buy_date, "sell_date": None,
                "amount": round(cost, 2), "gain": round(gain, 2),
            })

    total_gain = realized + unrealized
    return {
        "total_trades": len(txns),
        "priced_trades": priced_trades,
        "unpriced_trades": unpriced,
        "skipped_unknown_basis": unknown_basis,
        "invested_total_estimate": round(invested, 2),
        "realized_gain_estimate": round(realized, 2),
        "unrealized_gain_estimate": round(unrealized, 2),
        "total_gain_estimate": round(total_gain, 2),
        "return_pct_estimate": round(total_gain / invested * 100, 2) if invested else None,
        "trade_detail": detail,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--year", type=int, default=YEAR, help=f"Filing year (default {YEAR})")
    parser.add_argument("--limit", type=int, default=None,
                        help="Only process the first N PTRs (for quick testing)")
    parser.add_argument("--min-invested", type=float, default=25_000,
                        help="Minimum estimated cost basis to be ranked (default 25000)")
    parser.add_argument("--min-trades", type=int, default=3,
                        help="Minimum priced trades to be ranked (default 3)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    rows = fetch_index(args.year)
    ptr_rows = [r for r in rows if r.get("FilingType") == PTR_FILING_TYPE]
    if args.limit:
        ptr_rows = ptr_rows[:args.limit]
    print(f"{len(ptr_rows)} PTR(s) to process for {args.year}.", file=sys.stderr)

    meta = {member_key(r): {"name": format_filer_name(r), "state_dist": r.get("StateDst", "?")}
            for r in ptr_rows}
    by_member, parsed_lines = collect_transactions(ptr_rows, args.verbose)

    # Resolve tickers once across all members so the name-lookup cache is shared.
    tradeable: dict[tuple, list[dict]] = defaultdict(list)
    tickers: set[str] = set()
    for key, txns in by_member.items():
        for t in txns:
            if t["type"] not in ("Purchase", "Sale"):
                continue
            t["ticker"] = resolve_ticker(t)
            if t["ticker"]:
                tickers.add(t["ticker"])
                tradeable[key].append(t)
    print(f"{sum(len(v) for v in tradeable.values())} Purchase/Sale line(s) across "
          f"{len(tickers)} ticker(s).", file=sys.stderr)

    prices = PriceBook(
        tickers,
        start=f"{args.year}-01-01",
        end=(now.date().isoformat()),
    )
    print(f"Priced {len(prices)}/{len(tickers)} ticker(s).", file=sys.stderr)

    leaderboard = []
    for key, txns in tradeable.items():
        entry = {
            "member": meta[key]["name"],
            "last": key[0], "first": key[1],
            "state_dist": meta[key]["state_dist"],
        }
        entry.update(score_member(txns, prices))
        entry["ranked"] = (
            entry["return_pct_estimate"] is not None
            and entry["invested_total_estimate"] >= args.min_invested
            and entry["priced_trades"] >= args.min_trades
        )
        leaderboard.append(entry)

    leaderboard.sort(key=lambda m: (m["ranked"], m["return_pct_estimate"] or -1e9), reverse=True)

    OUTPUT_PATH.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "year": args.year,
        "min_invested": args.min_invested,
        "min_trades": args.min_trades,
        "totals": {
            "ptr_count": len(ptr_rows),
            "transaction_lines": parsed_lines,
            "tradeable_lines": sum(len(v) for v in tradeable.values()),
            "tickers": len(tickers),
            "tickers_priced": len(prices),
        },
        "methodology": (
            "Estimated shares = disclosed range midpoint / close on the transaction date; "
            "buys and sells FIFO-matched in share space for realized gains; unsold shares "
            "marked to the latest close. Unmatched sales (positions opened before the "
            "window) and non-equity assets are excluded from gain estimates but counted "
            "as trades. Members below min_invested/min_trades are unranked."
        ),
        "members": leaderboard,
    }, indent=2))
    print(f"Wrote leaderboard for {len(leaderboard)} member(s) to {OUTPUT_PATH}", file=sys.stderr)

    ranked = [m for m in leaderboard if m["ranked"]]
    print(f"\nTop 10 of {len(ranked)} ranked member(s) by estimated return %:")
    for m in ranked[:10]:
        print(f"  {m['return_pct_estimate']:+7.2f}%  {m['member']} ({m['state_dist']})  "
              f"gain=${m['total_gain_estimate']:,.0f} on ~${m['invested_total_estimate']:,.0f} "
              f"({m['priced_trades']} priced trades)")


if __name__ == "__main__":
    main()
