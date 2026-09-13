"""Regression tests for the scoring engine -- the only code here that produces numbers.

Run with: python3 -m unittest discover -s tests

The parser has had tests since the beginning; the FIFO matcher and the price lookup that
turn its output into a ranking had none, which is how a pre-window trade came to be
priced at the first close of the filing year for 23% of matched lots. These cover the
lookup's boundaries, the matching arithmetic, and the benchmark comparison.
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_leaderboard import (  # noqa: E402
    BENCHMARK_TICKER,
    MAX_PRICE_LOOKBACK_YEARS,
    PRICE_WINDOW_RUN_IN_DAYS,
    PriceBook,
    price_window_start,
    score_member,
)


def series(points: dict[str, float]) -> pd.Series:
    """A close series from {"YYYY-MM-DD": close}."""
    index = pd.to_datetime(sorted(points))
    return pd.Series([points[d.strftime("%Y-%m-%d")] for d in index], index=index)


def price_book(**series_by_ticker: pd.Series) -> PriceBook:
    """A PriceBook over canned series, bypassing the network fetch in __init__."""
    book = PriceBook.__new__(PriceBook)
    book._closes = {PriceBook.yahoo_symbol(t): s for t, s in series_by_ticker.items()}
    return book


def txn(ticker: str, kind: str, date: str, midpoint: int) -> dict:
    """A parsed transaction. The disclosed amount is a range whose midpoint is the
    figure the scorer uses, so it is stated here as the range a filing would print."""
    low, high = midpoint // 2, midpoint + midpoint // 2
    return {
        "ticker": ticker,
        "type": kind,
        "txn_date": date,
        "amount": f"${low:,} - ${high:,}",
    }


class PriceLookup(unittest.TestCase):
    def setUp(self):
        self.prices = price_book(AAA=series({
            "2026-01-02": 100.0,
            "2026-01-05": 110.0,
            "2026-01-06": 120.0,
        }))

    def test_close_on_a_trading_day(self):
        self.assertEqual(self.prices.on("AAA", "01/05/2026"), 110.0)

    def test_non_trading_day_rolls_forward_to_the_next_close(self):
        # 01/03 and 01/04 are a weekend; the next close is the Monday.
        self.assertEqual(self.prices.on("AAA", "01/03/2026"), 110.0)

    def test_date_before_the_window_is_unpriced_rather_than_clamped(self):
        """The regression this suite exists for.

        searchsorted answers a pre-window date with position 0, so a 2025 trade used to
        come back priced at the first close of 2026 -- silently wrong rather than
        missing, and wrong in a consistent direction."""
        self.assertIsNone(self.prices.on("AAA", "02/07/2025"))
        self.assertNotEqual(self.prices.on("AAA", "02/07/2025"), 100.0)

    def test_date_after_the_window_is_unpriced(self):
        self.assertIsNone(self.prices.on("AAA", "01/07/2027"))

    def test_unknown_ticker_is_unpriced(self):
        self.assertIsNone(self.prices.on("ZZZ", "01/05/2026"))

    def test_unparseable_date_is_unpriced(self):
        self.assertIsNone(self.prices.on("AAA", "not a date"))

    def test_class_shares_use_the_yahoo_symbol(self):
        prices = price_book(**{"BRK-B": series({"2026-01-02": 400.0})})
        self.assertEqual(prices.on("BRK.B", "01/02/2026"), 400.0)
        self.assertIn("BRK.B", prices)

    def test_latest_is_the_final_close(self):
        self.assertEqual(self.prices.latest("AAA"), 120.0)


class PriceWindow(unittest.TestCase):
    def test_opens_before_the_earliest_trade_not_at_january(self):
        start = price_window_start([txn("AAA", "Purchase", "02/07/2025", 10_000)], 2026)
        self.assertLess(datetime.fromisoformat(start), datetime(2025, 2, 7))

    def test_run_in_covers_a_trade_dated_on_a_non_trading_day(self):
        """on() rolls forward, so the window has to open strictly before the oldest
        trade -- otherwise a Saturday-dated trade is the one trade that falls off."""
        saturday = "05/17/2025"
        start = datetime.fromisoformat(
            price_window_start([txn("AAA", "Purchase", saturday, 10_000)], 2026))
        self.assertLess(start, datetime(2025, 5, 17))
        self.assertLessEqual((datetime(2025, 5, 17) - start).days,
                             PRICE_WINDOW_RUN_IN_DAYS + 1)

    def test_never_opens_later_than_the_filing_year(self):
        """All trades late in the year still get a full-year window; unsold lots are
        marked across it and the year is the reporting period."""
        start = price_window_start([txn("AAA", "Purchase", "11/03/2026", 10_000)], 2026)
        self.assertLess(datetime.fromisoformat(start), datetime(2026, 1, 1))

    def test_a_misparsed_ancient_date_does_not_widen_the_window(self):
        txns = [txn("AAA", "Purchase", "01/01/1900", 10_000),
                txn("AAA", "Purchase", "03/04/2026", 10_000)]
        start = datetime.fromisoformat(price_window_start(txns, 2026))
        self.assertGreater(start, datetime(2026 - MAX_PRICE_LOOKBACK_YEARS - 1, 1, 1))

    def test_no_parseable_dates_falls_back_to_the_filing_year(self):
        start = price_window_start([txn("AAA", "Purchase", "garbage", 10_000)], 2026)
        self.assertLess(datetime.fromisoformat(start), datetime(2026, 1, 1))

    def test_no_transactions_at_all(self):
        self.assertTrue(price_window_start([], 2026).startswith("2025-12"))


class Scoring(unittest.TestCase):
    """AAA doubles from 100 to 200 over the window; the benchmark rises 10%."""

    def setUp(self):
        self.prices = price_book(
            AAA=series({
                "2026-01-05": 100.0,
                "2026-03-02": 150.0,
                "2026-06-01": 200.0,
            }),
            **{BENCHMARK_TICKER: series({
                "2026-01-05": 400.0,
                "2026-03-02": 440.0,
                "2026-06-01": 480.0,
            })},
        )

    def test_realized_gain_is_matched_in_share_space(self):
        # $10k at 100 = 100 shares; sold for $15k at 150 = the same 100 shares.
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Sale", "03/02/2026", 15_000),
        ], self.prices)
        self.assertAlmostEqual(result["realized_gain_estimate"], 5_000.0)
        self.assertAlmostEqual(result["unrealized_gain_estimate"], 0.0)
        self.assertAlmostEqual(result["invested_total_estimate"], 10_000.0)
        self.assertAlmostEqual(result["return_pct_estimate"], 50.0)

    def test_unsold_shares_are_marked_to_the_latest_close(self):
        result = score_member(
            [txn("AAA", "Purchase", "01/05/2026", 10_000)], self.prices)
        self.assertAlmostEqual(result["unrealized_gain_estimate"], 10_000.0)
        self.assertAlmostEqual(result["realized_gain_estimate"], 0.0)

    def test_partial_sale_leaves_the_remainder_marked_to_market(self):
        # Buy 100 shares at 100; sell 50 of them at 150 ($7.5k), keep 50 to 200.
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Sale", "03/02/2026", 7_500),
        ], self.prices)
        self.assertAlmostEqual(result["realized_gain_estimate"], 2_500.0)
        self.assertAlmostEqual(result["unrealized_gain_estimate"], 5_000.0)
        self.assertEqual(result["priced_trades"], 2)

    def test_fifo_consumes_the_oldest_lot_first(self):
        # 100 shares at 100, then 100 at 150. Selling 100 shares at 200 must realize
        # against the 100 lot (+$10k), not the 150 lot (+$5k).
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Purchase", "03/02/2026", 15_000),
            txn("AAA", "Sale", "06/01/2026", 20_000),
        ], self.prices)
        self.assertAlmostEqual(result["realized_gain_estimate"], 10_000.0)

    def test_sale_exceeding_matched_buys_is_flagged_not_counted(self):
        """A position opened before the window has no cost basis we can see, so it must
        not become a gain of zero."""
        result = score_member(
            [txn("AAA", "Sale", "03/02/2026", 15_000)], self.prices)
        self.assertEqual(result["skipped_unknown_basis"], 1)
        self.assertAlmostEqual(result["realized_gain_estimate"], 0.0)
        self.assertAlmostEqual(result["invested_total_estimate"], 0.0)
        self.assertIsNone(result["return_pct_estimate"])

    def test_trade_before_the_price_window_is_unpriced_not_mispriced(self):
        """End to end: the pre-window buy must not open a lot at the window's first
        close, which is what produced a whole year of understated gains."""
        result = score_member([
            txn("AAA", "Purchase", "02/07/2025", 10_000),
            txn("AAA", "Purchase", "01/05/2026", 10_000),
        ], self.prices)
        self.assertEqual(result["unpriced_trades"], 1)
        self.assertEqual(result["priced_trades"], 1)
        self.assertAlmostEqual(result["invested_total_estimate"], 10_000.0)

    def test_delisted_ticker_is_unpriced_rather_than_dropped_silently(self):
        result = score_member(
            [txn("GONE", "Purchase", "01/05/2026", 10_000)], self.prices)
        self.assertEqual(result["unpriced_trades"], 1)
        self.assertEqual(result["total_trades"], 1)
        self.assertIsNone(result["return_pct_estimate"])


class Benchmark(unittest.TestCase):
    def setUp(self):
        self.prices = price_book(
            AAA=series({"2026-01-05": 100.0, "2026-03-02": 150.0, "2026-06-01": 200.0}),
            **{BENCHMARK_TICKER: series({
                "2026-01-05": 400.0,
                "2026-03-02": 440.0,   # +10% over the holding window
                "2026-06-01": 480.0,
            })},
        )

    def test_alpha_is_the_return_less_the_benchmark_over_the_same_window(self):
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Sale", "03/02/2026", 15_000),
        ], self.prices)
        self.assertAlmostEqual(result["return_pct_estimate"], 50.0)
        self.assertAlmostEqual(result["benchmark_return_pct_estimate"], 10.0)
        self.assertAlmostEqual(result["benchmark_gain_estimate"], 1_000.0)
        self.assertAlmostEqual(result["alpha_pct_estimate"], 40.0)

    def test_benchmark_runs_to_the_latest_close_for_an_unsold_lot(self):
        result = score_member(
            [txn("AAA", "Purchase", "01/05/2026", 10_000)], self.prices)
        # SPY 400 -> 480 is +20% against the position's +100%.
        self.assertAlmostEqual(result["benchmark_return_pct_estimate"], 20.0)
        self.assertAlmostEqual(result["alpha_pct_estimate"], 80.0)

    def test_matching_the_index_is_zero_alpha_not_a_win(self):
        """The point of the whole comparison: a member who tracked the index ranks at
        zero, however large the raw return."""
        prices = price_book(
            AAA=series({"2026-01-05": 100.0, "2026-03-02": 110.0}),
            **{BENCHMARK_TICKER: series({"2026-01-05": 400.0, "2026-03-02": 440.0})},
        )
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Sale", "03/02/2026", 11_000),
        ], prices)
        self.assertAlmostEqual(result["return_pct_estimate"], 10.0)
        self.assertAlmostEqual(result["alpha_pct_estimate"], 0.0)

    def test_alpha_is_absent_when_the_benchmark_did_not_price(self):
        prices = price_book(AAA=series({"2026-01-05": 100.0, "2026-03-02": 150.0}))
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Sale", "03/02/2026", 15_000),
        ], prices)
        self.assertAlmostEqual(result["return_pct_estimate"], 50.0)
        self.assertIsNone(result["benchmark_return_pct_estimate"])
        self.assertIsNone(result["alpha_pct_estimate"])

    def test_benchmark_basis_excludes_lots_it_could_not_price(self):
        """A lot outside the benchmark's own series must be left out of the index
        figure rather than diluting it towards zero."""
        prices = price_book(
            AAA=series({"2026-01-05": 100.0, "2026-03-02": 150.0, "2026-06-01": 200.0}),
            # Benchmark series starts late, so the January lot cannot be benchmarked.
            **{BENCHMARK_TICKER: series({"2026-03-02": 440.0, "2026-06-01": 484.0})},
        )
        result = score_member([
            txn("AAA", "Purchase", "01/05/2026", 10_000),
            txn("AAA", "Purchase", "03/02/2026", 15_000),
        ], prices)
        # Only the March lot ($15k) is benchmarked: SPY 440 -> 484 is +10%.
        self.assertAlmostEqual(result["benchmark_gain_estimate"], 1_500.0)
        self.assertAlmostEqual(result["benchmark_return_pct_estimate"], 10.0)
        self.assertAlmostEqual(result["invested_total_estimate"], 25_000.0)


if __name__ == "__main__":
    unittest.main()
