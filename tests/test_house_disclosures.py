"""Regression tests for PTR parsing.

Run with: python3 -m unittest discover -s tests

The fixture is real `pdftotext -layout` output stitched from three 2026 filings; each
block in it previously broke the line-oriented parser in a different way.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from house_disclosures import (  # noqa: E402
    amount_midpoint,
    clean_asset_name,
    filing_delay_days,
    format_filer_name,
    is_late_filing,
    member_key,
    parse_transactions,
    resolve_ticker,
)

FIXTURE = Path(__file__).parent / "fixtures" / "ptr_sample.txt"


class ParseTransactionsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.txns = parse_transactions(FIXTURE.read_text())
        cls.by_ticker = {t["ticker"]: t for t in cls.txns if t["ticker"]}

    def test_finds_every_transaction(self):
        self.assertEqual(len(self.txns), 6)

    def test_wrapped_asset_name_keeps_ticker_and_upper_bound(self):
        """The name wraps, carrying the ticker, [ST] code and the range's upper bound
        onto the continuation line."""
        awk = self.by_ticker["AWK"]
        self.assertEqual(awk["amount"], "$50,001 - $100,000")
        self.assertEqual(awk["asset_type"], "ST")
        self.assertEqual(awk["owner"], "SP")
        self.assertEqual(awk["type"], "Sale")
        self.assertIn("American Water Works", awk["asset"])

    def test_partial_sale_is_not_dropped(self):
        """'S (partial)' in the type column used to make the whole row unmatchable."""
        amzn = self.by_ticker["AMZN"]
        self.assertEqual(amzn["type"], "Sale")
        self.assertTrue(amzn["partial"])
        self.assertEqual(amzn["amount"], "$1,001 - $15,000")

    def test_transaction_split_across_page_break(self):
        """The repeated table header must not truncate a record."""
        ibm = self.by_ticker["IBM"]
        self.assertEqual(ibm["amount"], "$15,001 - $50,000")
        self.assertEqual(ibm["asset_type"], "ST")
        self.assertIn("Corporation", ibm["asset"])

    def test_full_range_on_one_line(self):
        self.assertEqual(self.by_ticker["SPGI"]["amount"], "$15,001 - $50,000")
        self.assertEqual(self.by_ticker["SPGI"]["type"], "Purchase")

    def test_page_furniture_never_becomes_an_asset(self):
        for t in self.txns:
            self.assertNotIn("Notification", t["asset"])
            self.assertNotIn("$200", t["asset"])
            self.assertNotIn("[", t["asset"])

    def test_amounts_all_resolved(self):
        for t in self.txns:
            self.assertTrue(t["amount"], f"no amount for {t['asset']!r}")
            self.assertIsNotNone(amount_midpoint(t["amount"]))

    def test_government_security_gets_no_ticker(self):
        """A treasury note has no ticker and must not be sent to the name lookup --
        Yahoo would happily return an unrelated equity."""
        note = next(t for t in self.txns if t["asset_type"] == "GS")
        self.assertIsNone(note["ticker"])
        self.assertIsNone(resolve_ticker(note))  # no network call: gated on asset type

    def test_empty_input(self):
        self.assertEqual(parse_transactions(""), [])
        self.assertEqual(parse_transactions("nothing to see here\n"), [])


class AmountMidpointTest(unittest.TestCase):
    def test_range(self):
        self.assertEqual(amount_midpoint("$15,001 - $50,000"), 32500.5)

    def test_single_value(self):
        self.assertEqual(amount_midpoint("$1,001"), 1001.0)

    def test_open_ended_top_bucket_is_conservative(self):
        self.assertEqual(amount_midpoint("$1,000,001 +"), 1000001.0)

    def test_unparseable(self):
        self.assertIsNone(amount_midpoint(""))
        self.assertIsNone(amount_midpoint("N/A"))


class LateFilingTest(unittest.TestCase):
    def test_within_deadline(self):
        self.assertFalse(is_late_filing("01/14/2026", "02/17/2026"))  # 34 days

    def test_past_deadline(self):
        self.assertTrue(is_late_filing("01/14/2026", "06/04/2026"))

    def test_boundary_is_45_days(self):
        self.assertFalse(is_late_filing("01/01/2026", "02/15/2026"))  # exactly 45
        self.assertTrue(is_late_filing("01/01/2026", "02/16/2026"))  # 46

    def test_measures_filing_delay_not_broker_notification(self):
        """The notification date is when the broker told the filer; it says nothing
        about how long the filer then sat on it."""
        self.assertEqual(filing_delay_days("01/14/2026", "02/17/2026"), 34)

    def test_unpadded_dates_from_the_index(self):
        self.assertEqual(filing_delay_days("1/14/2026", "2/17/2026"), 34)

    def test_garbage_dates(self):
        self.assertIsNone(filing_delay_days("", "02/17/2026"))
        self.assertFalse(is_late_filing("not a date", "also not"))


class HelpersTest(unittest.TestCase):
    def test_clean_asset_name_strips_boilerplate_and_ticker(self):
        self.assertEqual(
            clean_asset_name("Paychex, Inc. - Common Stock (PAYX)"), "Paychex, Inc")

    def test_format_filer_name(self):
        row = {"Prefix": "Hon.", "First": "Jane", "Last": "Doe", "Suffix": ""}
        self.assertEqual(format_filer_name(row), "Hon. Jane Doe")

    def test_member_key_uses_raw_fields(self):
        row = {"Prefix": "Hon.", "First": "Jane", "Last": "Doe", "StateDst": "CA01"}
        self.assertEqual(member_key(row), ("Doe", "Jane", "CA01"))

    def test_member_key_tolerates_missing_fields(self):
        self.assertEqual(member_key({}), ("", "", ""))


if __name__ == "__main__":
    unittest.main()
