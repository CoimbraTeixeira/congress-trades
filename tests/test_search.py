"""Tests for the semantic-search data prep -- the pure transforms that decide what gets
embedded and how the CLI facets become a Milvus filter. No model, Milvus, or network here;
the embedding/query round-trip is covered by running the scripts, not by unit tests.

Run with: python3 -m unittest discover -s tests
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_search_index import build_rows, clean_name, iso_date  # noqa: E402
from search_trades import build_filter  # noqa: E402

TMETA = {"NVDA": {"name": "NVIDIA Corporation", "sector": "Technology",
                  "industry": "Semiconductors"}}


def member(name, state, trades):
    return {"member": name, "state_dist": state, "trade_detail": trades}


def trade(ticker, kind, date, amount, gain):
    return {"ticker": ticker, "type": kind, "buy_date": date,
            "amount": amount, "gain": gain}


class CleanName(unittest.TestCase):
    def test_strips_leading_and_interior_titles(self):
        self.assertEqual(clean_name("Hon. Richard Dean Dr McCormick"),
                         "Richard Dean McCormick")

    def test_keeps_initials_and_ordinary_names(self):
        self.assertEqual(clean_name("Hon. Robert E. Latta"), "Robert E. Latta")


class IsoDate(unittest.TestCase):
    def test_parses_us_format(self):
        self.assertEqual(iso_date("01/20/2026"), "2026-01-20")

    def test_unparseable_is_none(self):
        self.assertIsNone(iso_date("not-a-date"))
        self.assertIsNone(iso_date(""))


class BuildRows(unittest.TestCase):
    def setUp(self):
        members = [member("Hon. Jane A. Doe", "CA01", [
            trade("NVDA", "unrealized", "01/10/2026", 1000.0, 500.0),
            trade("NVDA", "realized", "03/15/2026", 2000.0, -100.0),
            trade("ZZZZ", "unrealized", "bad-date", 50.0, 0.0),
        ])]
        self.rows = {r["ticker"]: r for r in build_rows(members, TMETA)}

    def test_repeat_buys_collapse_into_one_position(self):
        self.assertEqual(set(self.rows), {"NVDA", "ZZZZ"})
        nvda = self.rows["NVDA"]
        self.assertEqual(nvda["buys"], 2)
        self.assertEqual(nvda["amount"], 3000.0)
        self.assertEqual(nvda["gain"], 400.0)

    def test_realized_when_any_leg_is_realized(self):
        self.assertTrue(self.rows["NVDA"]["realized"])
        self.assertFalse(self.rows["ZZZZ"]["realized"])

    def test_name_cleaned_and_enrichment_applied(self):
        nvda = self.rows["NVDA"]
        self.assertEqual(nvda["member"], "Jane A. Doe")
        self.assertEqual(nvda["company"], "NVIDIA Corporation")
        self.assertEqual(nvda["sector"], "Technology")
        # the embedded text must carry the words a text model can match on
        self.assertIn("Technology", nvda["text"])
        self.assertIn("NVDA", nvda["text"])

    def test_last_date_is_latest_parseable_or_empty(self):
        self.assertEqual(self.rows["NVDA"]["last_date"], "2026-03-15")
        self.assertEqual(self.rows["ZZZZ"]["last_date"], "")

    def test_unknown_ticker_degrades_gracefully(self):
        zzzz = self.rows["ZZZZ"]
        self.assertEqual(zzzz["company"], "ZZZZ")
        self.assertEqual(zzzz["sector"], "Unknown")


class BuildFilter(unittest.TestCase):
    def test_empty_when_no_facets(self):
        self.assertEqual(build_filter(), "")

    def test_single_clauses(self):
        self.assertEqual(build_filter(sector="Energy"), 'sector == "Energy"')
        self.assertEqual(build_filter(min_gain=1000), "gain >= 1000")
        self.assertEqual(build_filter(max_gain=5000), "gain <= 5000")
        self.assertEqual(build_filter(member="Pelosi"), 'member like "%Pelosi%"')

    def test_held_toggles_realized(self):
        self.assertEqual(build_filter(held=True), "realized == False")
        self.assertEqual(build_filter(held=False), "realized == True")

    def test_clauses_join_with_and(self):
        self.assertEqual(build_filter(sector="Technology", min_gain=5000),
                         'sector == "Technology" && gain >= 5000')


if __name__ == "__main__":
    unittest.main()
