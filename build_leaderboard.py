#!/usr/bin/env python3
"""Build a per-member stock trading performance leaderboard from 2026 House PTR filings.

Methodology (mark-to-market, range midpoint):
- Each disclosed transaction only gives a dollar RANGE, not exact amount or share count.
  We use the midpoint of that range as the transaction's dollar value.
- Buys and sells of the same ticker by the same member are matched FIFO within the year
  to compute realized gains. Sales that exceed matched buys (i.e. the position was
  opened before 2026) have unknown cost basis and are excluded from gain calculations.
- Any unsold ("open") portion of a buy is marked to market using today's price to
  estimate unrealized gain: est. shares = midpoint $ / price at transaction date;
  current value = est. shares * price today.
- Non-equity assets (no resolvable ticker, e.g. Treasury notes, private funds) are
  excluded from gain calculations but still counted as trades.
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yfinance as yf

from house_disclosures import (
    PDF_URL_TMPL,
    PTR_FILING_TYPE,
    amount_midpoint,
    fetch_index,
    fetch_ptr_text,
    format_filer_name,
    parse_transactions,
    resolve_ticker_by_name,
)

REPO_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = REPO_DIR / "leaderboard.json"
YEAR = datetime.now(timezone.utc).year

_price_cache: dict[tuple[str, str], float | None] = {}
_current_price_cache: dict[str, float | None] = {}


def price_on(ticker: str, date_str: str) -> float | None:
    """Closing price on or shortly after the given date (weekends/holidays roll forward)."""
    key = (ticker, date_str)
    if key in _price_cache:
        return _price_cache[key]
    try:
        dt = datetime.strptime(date_str, "%m/%d/%Y")
        # Widen the window past weekends/holidays so we always land on a trading day.
        hist = yf.Ticker(ticker).history(
            start=dt.strftime("%Y-%m-%d"),
            end=(dt + timedelta(days=10)).strftime("%Y-%m-%d"),
        )
        price = float(hist["Close"].iloc[0]) if not hist.empty else None
    except Exception:
        price = None
    _price_cache[key] = price
    return price


def current_price(ticker: str) -> float | None:
    if ticker in _current_price_cache:
        return _current_price_cache[ticker]
    try:
        price = yf.Ticker(ticker).fast_info.get("lastPrice")
        price = float(price) if price is not None else None
    except Exception:
        price = None
    _current_price_cache[ticker] = price
    return price


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process the first N PTRs (for quick testing)")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    rows = fetch_index(YEAR)
    ptr_rows = [r for r in rows if r.get("FilingType") == PTR_FILING_TYPE]
    if args.limit:
        ptr_rows = ptr_rows[:args.limit]
    print(f"[{now.isoformat()}] {len(ptr_rows)} PTR(s) to process for {YEAR}.", file=sys.stderr)

    # member_key -> list of transaction dicts (+ member display name, state/district)
    member_txns: dict[str, list[dict]] = defaultdict(list)
    member_meta: dict[str, dict] = {}

    total_txn_count = 0
    ticker_txn_count = 0

    for i, row in enumerate(ptr_rows):
        doc_id = row["DocID"]
        member_key = f"{row.get('Last','')}|{row.get('First','')}|{row.get('StateDst','')}"
        member_meta.setdefault(member_key, {
            "name": format_filer_name(row),
            "state_dist": row.get("StateDst", "?"),
        })
        try:
            pdf_text = fetch_ptr_text(YEAR, doc_id)
            transactions = parse_transactions(pdf_text) if pdf_text else []
        except Exception as exc:
            if args.verbose:
                print(f"  [{i+1}/{len(ptr_rows)}] {doc_id}: fetch/parse failed: {exc}", file=sys.stderr)
            continue

        total_txn_count += len(transactions)
        for t in transactions:
            if t["type"] not in ("Purchase", "Sale"):
                continue
            if not t["ticker"]:
                t["ticker"] = resolve_ticker_by_name(t["asset"])
                if t["ticker"]:
                    t["ticker_source"] = "name-lookup"
            if t["ticker"]:
                ticker_txn_count += 1
                t["_doc_id"] = doc_id
                member_txns[member_key].append(t)

        if args.verbose and (i + 1) % 25 == 0:
            print(f"  processed {i+1}/{len(ptr_rows)} PTRs...", file=sys.stderr)

    print(f"[{now.isoformat()}] Parsed {total_txn_count} total transaction line(s), "
          f"{ticker_txn_count} with a resolvable ticker (Purchase/Sale only).", file=sys.stderr)

    leaderboard = []
    for member_key, txns in member_txns.items():
        meta = member_meta[member_key]
        by_ticker = defaultdict(list)
        for t in txns:
            by_ticker[t["ticker"]].append(t)

        realized_gain = 0.0
        unrealized_gain = 0.0
        invested_total = 0.0
        priced_trade_count = 0
        skipped_unknown_basis = 0
        trade_detail = []

        for ticker, tx_list in by_ticker.items():
            tx_list.sort(key=lambda t: datetime.strptime(t["txn_date"], "%m/%d/%Y"))
            open_lots = []  # list of [remaining_dollars, txn_date]
            for t in tx_list:
                mid = amount_midpoint(t["amount"])
                if mid is None:
                    continue
                if t["type"] == "Purchase":
                    open_lots.append([mid, t["txn_date"]])
                else:  # Sale
                    remaining_sale = mid
                    while remaining_sale > 0 and open_lots:
                        lot = open_lots[0]
                        matched = min(lot[0], remaining_sale)
                        buy_price = price_on(ticker, lot[1])
                        sell_price = price_on(ticker, t["txn_date"])
                        if buy_price and sell_price:
                            gain = matched * (sell_price / buy_price - 1)
                            realized_gain += gain
                            invested_total += matched
                            priced_trade_count += 1
                            trade_detail.append({
                                "ticker": ticker, "type": "realized",
                                "buy_date": lot[1], "sell_date": t["txn_date"],
                                "amount": matched, "gain": gain,
                            })
                        lot[0] -= matched
                        remaining_sale -= matched
                        if lot[0] <= 0:
                            open_lots.pop(0)
                    if remaining_sale > 0:
                        skipped_unknown_basis += 1  # sold more than we saw bought this year

            # Remaining open lots -> mark to market
            for remaining, txn_date in open_lots:
                if remaining <= 0:
                    continue
                buy_price = price_on(ticker, txn_date)
                now_price = current_price(ticker)
                if buy_price and now_price:
                    gain = remaining * (now_price / buy_price - 1)
                    unrealized_gain += gain
                    invested_total += remaining
                    priced_trade_count += 1
                    trade_detail.append({
                        "ticker": ticker, "type": "unrealized",
                        "buy_date": txn_date, "sell_date": None,
                        "amount": remaining, "gain": gain,
                    })

        total_gain = realized_gain + unrealized_gain
        return_pct = (total_gain / invested_total * 100) if invested_total else None

        leaderboard.append({
            "member": meta["name"],
            "state_dist": meta["state_dist"],
            "total_trades": len(txns),
            "priced_trades": priced_trade_count,
            "skipped_unknown_basis": skipped_unknown_basis,
            "invested_total_estimate": round(invested_total, 2),
            "realized_gain_estimate": round(realized_gain, 2),
            "unrealized_gain_estimate": round(unrealized_gain, 2),
            "total_gain_estimate": round(total_gain, 2),
            "return_pct_estimate": round(return_pct, 2) if return_pct is not None else None,
            "trade_detail": trade_detail,
        })

    leaderboard.sort(key=lambda m: (m["return_pct_estimate"] is not None, m["return_pct_estimate"]),
                      reverse=True)

    output = {
        "generated_at": now.isoformat(),
        "year": YEAR,
        "methodology": "Mark-to-market using disclosed range midpoint; FIFO-matched realized "
                        "gains within the year; unmatched sales (pre-existing positions) and "
                        "non-equity assets excluded from gain calc but counted as trades.",
        "members": leaderboard,
    }
    OUTPUT_PATH.write_text(json.dumps(output, indent=2))
    print(f"[{now.isoformat()}] Wrote leaderboard for {len(leaderboard)} member(s) to {OUTPUT_PATH}",
          file=sys.stderr)

    ranked = [m for m in leaderboard if m["return_pct_estimate"] is not None]
    print(f"\nTop 10 by estimated return %:")
    for m in ranked[:10]:
        print(f"  {m['return_pct_estimate']:+7.2f}%  {m['member']} ({m['state_dist']})  "
              f"gain=${m['total_gain_estimate']:,.0f} on ~${m['invested_total_estimate']:,.0f}")


if __name__ == "__main__":
    main()
