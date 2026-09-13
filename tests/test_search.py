"""Tests for the semantic-search data prep -- the pure transforms that decide what gets
embedded, how a day's tickers become themes, and how the CLI facets become a Milvus filter.
No model, Milvus, or network here; the embedding/query round-trip is covered by running the
scripts, not by unit tests.

Run with: python3 -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build_search_index import (build_position_rows, build_ticker_rows,  # noqa: E402
                                embed_text, iso_date, parse_date_stamp)
from monitor_common import display_name  # noqa: E402
from search_common import cluster_vectors, truncate_bytes  # noqa: E402
from search_trades import build_filter  # noqa: E402

TMETA = {"NVDA": {"name": "NVIDIA Corporation", "sector": "Technology",
                  "industry": "Semiconductors",
                  "summary": "NVIDIA designs graphics processing units."}}


def member(name, state, trades, first="", last=""):
    return {"member": name, "state_dist": state, "trade_detail": trades,
            "first": first, "last": last}


def trade(ticker, kind, date, amount, gain):
    return {"ticker": ticker, "type": kind, "buy_date": date,
            "amount": amount, "gain": gain}


class DisplayName(unittest.TestCase):
    def test_built_from_identity_fields_not_the_composed_string(self):
        # The composed name carries honorifics and a Suffix-column stray ('Dr'); the
        # first/last fields are already clean, so the display name must come from them.
        row = {"member": "Hon. Richard Dean Dr McCormick",
               "first": "Richard", "last": "McCormick"}
        self.assertEqual(display_name(row), "McCormick, Richard")

    def test_falls_back_to_whatever_identity_exists(self):
        self.assertEqual(display_name({"last": "Pelosi", "first": ""}), "Pelosi")
        self.assertEqual(display_name({"member": "Only Composed"}), "Only Composed")


class IsoDate(unittest.TestCase):
    def test_parses_us_format(self):
        self.assertEqual(iso_date("01/20/2026"), "2026-01-20")

    def test_unparseable_is_none(self):
        self.assertIsNone(iso_date("bad-date"))
        self.assertIsNone(iso_date(""))


class ParseDateStamp(unittest.TestCase):
    def test_naive_iso_is_read_as_utc(self):
        self.assertEqual(parse_date_stamp("2026-01-01T00:00:00"), 1767225600)

    def test_missing_or_broken_stamp_is_zero(self):
        # A zero stamp is what makes the CLI omit the freshness note rather than print
        # "indexed 1970-01-01".
        self.assertEqual(parse_date_stamp(None), 0)
        self.assertEqual(parse_date_stamp("not-a-date"), 0)


class EmbedText(unittest.TestCase):
    def test_business_description_is_what_gets_embedded(self):
        text = embed_text("NVDA", TMETA["NVDA"])
        self.assertIn("graphics processing units", text)
        self.assertIn("NVDA", text)

    def test_no_description_degrades_to_sector_words(self):
        bare = {"name": "Zeta", "sector": "Energy", "industry": "Solar", "summary": ""}
        text = embed_text("ZZZZ", bare)
        self.assertIn("Energy", text)
        self.assertIn("Solar", text)

    def test_dollar_figures_never_reach_the_embedding(self):
        """Numbers are scalar columns. Embedding them only adds noise a text model
        cannot represent, which is the bug this schema exists to fix."""
        self.assertNotIn("$", embed_text("NVDA", TMETA["NVDA"]))


class BuildTickerRows(unittest.TestCase):
    def test_one_row_per_symbol_regardless_of_how_many_members_hold_it(self):
        rows = build_ticker_rows(["NVDA"], TMETA, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "NVDA")

    def test_unknown_ticker_degrades_gracefully(self):
        rows = build_ticker_rows(["ZZZZ"], TMETA, 0)
        self.assertEqual(rows[0]["name"], "ZZZZ")
        self.assertEqual(rows[0]["sector"], "Unknown")


class BuildPositionRows(unittest.TestCase):
    def setUp(self):
        members = [member("Hon. Jane A. Doe", "CA01", [
            trade("NVDA", "unrealized", "01/10/2026", 1000.0, 500.0),
            trade("NVDA", "realized", "03/15/2026", 2000.0, -100.0),
            trade("ZZZZ", "unrealized", "bad-date", 50.0, 0.0),
        ], first="Jane", last="Doe")]
        self.rows = {r["ticker"]: r for r in build_position_rows(members, TMETA, 99)}

    def test_repeat_buys_collapse_into_one_position(self):
        self.assertEqual(set(self.rows), {"NVDA", "ZZZZ"})
        nvda = self.rows["NVDA"]
        self.assertEqual(nvda["buys"], 2)
        self.assertEqual(nvda["amount"], 3000.0)
        self.assertEqual(nvda["gain"], 400.0)

    def test_realized_when_any_leg_is_realized(self):
        self.assertTrue(self.rows["NVDA"]["realized"])
        self.assertFalse(self.rows["ZZZZ"]["realized"])

    def test_member_matches_the_display_spelling_used_for_lookups(self):
        # watchlist_monitor finds a member's holdings by this exact string; if the two
        # disagree the theme note silently reports "no existing positions" for everyone.
        self.assertEqual(self.rows["NVDA"]["member"], "Doe, Jane")

    def test_last_date_is_latest_parseable_or_empty(self):
        self.assertEqual(self.rows["NVDA"]["last_date"], "2026-03-15")
        self.assertEqual(self.rows["ZZZZ"]["last_date"], "")

    def test_freshness_stamp_rides_on_every_row(self):
        self.assertEqual(self.rows["NVDA"]["generated_at"], 99)


class TruncateBytes(unittest.TestCase):
    def test_multibyte_character_is_never_split(self):
        # Milvus counts max_length in bytes; slicing in characters is what let a single
        # non-ASCII company name abort a whole insert. Cutting at 6 bytes lands inside
        # the two-byte 'é', which must be dropped whole rather than emitted half.
        text = "Nestlé" * 40
        cut = truncate_bytes(text, 6)
        self.assertEqual(cut, "Nestl")
        self.assertLessEqual(len(cut.encode("utf-8")), 6)

    def test_exact_byte_fit_keeps_the_whole_character(self):
        self.assertEqual(truncate_bytes("Nestlé", 7), "Nestlé")

    def test_short_text_is_returned_unchanged(self):
        self.assertEqual(truncate_bytes("NVDA", 32), "NVDA")


class ClusterVectors(unittest.TestCase):
    """Two tight pairs on orthogonal axes, plus an outlier that belongs to neither."""

    VECTORS = [
        [1.0, 0.0, 0.0],
        [0.98, 0.2, 0.0],
        [0.0, 0.0, 1.0],
        [0.05, 0.0, 0.998],
        [0.0, 1.0, 0.0],
    ]

    def test_similar_rows_group_and_dissimilar_ones_do_not(self):
        groups = cluster_vectors(self.VECTORS, 0.9)
        self.assertIn([0, 1], groups)
        self.assertIn([2, 3], groups)
        self.assertIn([4], groups)

    def test_every_row_lands_in_exactly_one_group(self):
        groups = cluster_vectors(self.VECTORS, 0.9)
        assigned = sorted(i for g in groups for i in g)
        self.assertEqual(assigned, list(range(len(self.VECTORS))))

    def test_raising_the_threshold_splits_a_group(self):
        self.assertIn([0, 1], cluster_vectors(self.VECTORS, 0.9))
        self.assertNotIn([0, 1], cluster_vectors(self.VECTORS, 0.999))

    def test_densest_group_is_reported_first(self):
        groups = cluster_vectors([[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [0.0, 1.0]], 0.9)
        self.assertEqual(groups[0], [0, 1, 2])

    def test_empty_input_is_not_an_error(self):
        self.assertEqual(cluster_vectors([]), [])


class BuildFilter(unittest.TestCase):
    def test_empty_when_no_facets(self):
        self.assertEqual(build_filter(), "")

    def test_held_toggles_realized(self):
        self.assertEqual(build_filter(held=True), "realized == False")
        self.assertEqual(build_filter(held=False), "realized == True")

    def test_every_facet_contributes_a_clause(self):
        flt = build_filter(sector="Technology", min_gain=5000, max_gain=9000,
                           member="Pelosi")
        self.assertEqual(flt.count("&&"), 3)
        for fragment in ("Technology", "5000", "9000", "Pelosi"):
            self.assertIn(fragment, flt)

    def test_quotes_in_a_facet_cannot_escape_the_literal(self):
        """`--member '%" or gain > 0 or member like "%'` must stay one predicate rather
        than rewriting the filter into "everything profitable"."""
        flt = build_filter(member='%" or gain > 0 or member like "%')
        literal = flt[len("member like "):]
        self.assertTrue(literal.startswith('"') and literal.endswith('"'))
        # Drop every escaped pair; a surviving quote would have closed the literal early
        # and let the rest of the value be parsed as expression syntax.
        self.assertNotIn('"', re.sub(r"\\.", "", literal[1:-1]))
        self.assertEqual(flt.count("&&"), 0)

    def test_backslash_cannot_escape_the_closing_quote(self):
        flt = build_filter(sector="Tech\\")
        self.assertTrue(flt.endswith('"'))
        self.assertIn("\\\\", flt)


if __name__ == "__main__":
    unittest.main()
