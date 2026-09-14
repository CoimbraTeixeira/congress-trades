"""Regression tests for PTR parsing.

Run with: python3 -m unittest discover -s tests

The fixture is real `pdftotext -layout` output stitched from three 2026 filings; each
block in it previously broke the line-oriented parser in a different way.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import house_disclosures  # noqa: E402
from house_disclosures import (  # noqa: E402
    amount_midpoint,
    clean_asset_name,
    fetch_ptr_text,
    filing_delay_days,
    format_filer_name,
    is_late_filing,
    member_key,
    parse_transactions,
    resolve_ticker,
    trade_transactions,
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


class MemberIdentityTest(unittest.TestCase):
    """Real pairs from the 2023-2026 archives that used to score as two members each,
    splitting one record in half and spending two watchlist slots on one person."""

    def test_a_title_added_mid_window_is_the_same_member(self):
        self.assertEqual(
            member_key({"Last": "Greene", "First": "Marjorie Taylor", "StateDst": "GA14"}),
            member_key({"Last": "Greene", "First": "Marjorie Taylor Mrs", "StateDst": "GA14"}))

    def test_an_irreconcilable_first_name_is_the_same_member(self):
        self.assertEqual(
            member_key({"Last": "Franklin", "First": "C. Scott", "StateDst": "FL18"}),
            member_key({"Last": "Franklin", "First": "Scott Scott", "StateDst": "FL18"}))

    def test_post_nominals_migrating_between_fields_is_the_same_member(self):
        self.assertEqual(
            member_key({"Last": "Dunn, MD, FACS", "First": "Neal Patrick", "StateDst": "FL02"}),
            member_key({"Last": "Dunn", "First": "Neal Patrick MD, Facs", "StateDst": "FL02"}))

    def test_same_surname_in_different_districts_stays_separate(self):
        self.assertNotEqual(
            member_key({"Last": "Moore", "First": "Tim", "StateDst": "NC14"}),
            member_key({"Last": "Moore", "First": "Blake", "StateDst": "UT01"}))

    def test_a_compound_surname_survives_normalization(self):
        self.assertEqual(
            member_key({"Last": "Van Drew", "First": "Jefferson", "StateDst": "NJ02"}),
            ("van drew", "NJ02"))

    def test_tolerates_missing_fields(self):
        self.assertEqual(member_key({}), ("", ""))


def _txn(kind: str, ticker: str = "AAA") -> dict:
    return {"type": kind, "ticker": ticker, "txn_date": "01/05/2026",
            "amount": "$1,001 - $15,000"}


class TradeFilterTest(unittest.TestCase):
    """Only buys and sells say anything about a pick, and all three readers -- the
    leaderboard, the digest and the buy alerts -- have to agree on that."""

    def test_purchases_and_sales_are_kept_in_order(self):
        rows = [_txn("Purchase", "AAA"), _txn("Exchange", "BBB"), _txn("Sale", "CCC")]
        self.assertEqual([t["ticker"] for t in trade_transactions(rows)], ["AAA", "CCC"])

    def test_exchange_only_filing_filters_to_nothing(self):
        # A share-class swap or a merger conversion: no direction, nothing to report.
        self.assertEqual(trade_transactions([_txn("Exchange")]), [])

    def test_a_row_with_no_type_at_all_is_dropped(self):
        self.assertEqual(trade_transactions([{"ticker": "AAA"}]), [])


class PtrTextCacheTest(unittest.TestCase):
    """A published filing is immutable, and the 3-year window re-reads a few thousand of
    them every rebuild. The cache is what keeps that off the Clerk's site -- but it must
    never make a transient failure permanent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            mock.patch.object(house_disclosures, "PTR_TEXT_CACHE_DIR",
                              Path(self.tmp.name)),
            mock.patch.object(house_disclosures.shutil, "which",
                              return_value="/usr/bin/pdftotext"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _session(status: int = 200, content: bytes = b"%PDF-1.4"):
        resp = mock.Mock(status_code=status, content=content)
        return mock.Mock(return_value=mock.Mock(get=mock.Mock(return_value=resp)))

    def _extract(self, stdout: str, returncode: int = 0):
        return mock.patch.object(
            house_disclosures.subprocess, "run",
            return_value=subprocess.CompletedProcess([], returncode, stdout, ""))

    def test_extracted_text_is_reused_without_refetching(self):
        session = self._session()
        with mock.patch.object(house_disclosures, "_session", session), \
                self._extract("TRANSACTION TABLE"):
            self.assertEqual(fetch_ptr_text(2026, "20033910"), "TRANSACTION TABLE")
        with mock.patch.object(house_disclosures, "_session", session):
            self.assertEqual(fetch_ptr_text(2026, "20033910"), "TRANSACTION TABLE")
        self.assertEqual(session.return_value.get.call_count, 1)

    def test_a_missing_pdf_is_not_cached_as_an_empty_filing(self):
        with mock.patch.object(house_disclosures, "_session", self._session(status=404)):
            self.assertIsNone(fetch_ptr_text(2026, "20033911"))
        with mock.patch.object(house_disclosures, "_session", self._session()), \
                self._extract("LATER TEXT"):
            self.assertEqual(fetch_ptr_text(2026, "20033911"), "LATER TEXT")

    def test_an_unreadable_pdf_is_not_cached(self):
        with mock.patch.object(house_disclosures, "_session", self._session()), \
                self._extract("", returncode=1):
            self.assertIsNone(fetch_ptr_text(2026, "20033912"))
        self.assertEqual(list(Path(self.tmp.name).rglob("*.txt")), [])

    def test_no_cache_refetches(self):
        with mock.patch.object(house_disclosures, "_session", self._session()), \
                self._extract("FIRST"):
            fetch_ptr_text(2026, "20033913")
        session = self._session()
        with mock.patch.object(house_disclosures, "_session", session), \
                self._extract("SECOND"):
            self.assertEqual(fetch_ptr_text(2026, "20033913", use_cache=False), "SECOND")
        self.assertEqual(session.return_value.get.call_count, 1)

    def test_a_doc_id_that_is_not_a_filename_is_never_written(self):
        # The DocID comes off a network-fetched index; it is not our string.
        with mock.patch.object(house_disclosures, "_session", self._session()), \
                self._extract("TEXT"):
            self.assertEqual(fetch_ptr_text(2026, "../../etc/passwd"), "TEXT")
        self.assertEqual(list(Path(self.tmp.name).rglob("*")), [])


if __name__ == "__main__":
    unittest.main()
