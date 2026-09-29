"""Regression tests for the alerting logic that decides whether a filing is seen.

Run with: python3 -m unittest discover -s tests

These cover the ways a real filing can end up silently not alerted: falling outside the
index years the monitor fetches, being baselined away when its member joins the
watchlist, or being read as "no purchases" when it was actually unreadable.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import congress_monitor  # noqa: E402
from house_disclosures import (  # noqa: E402
    equity_transactions,
    filing_sort_key,
    index_years,
    is_equity_transaction,
)
import monitor_common  # noqa: E402
from monitor_common import (  # noqa: E402
    DISCORD_MESSAGE_LIMIT,
    MAX_RETRY_SLEEP,
    PDF_ANNOUNCE,
    PDF_GIVE_UP,
    PDF_WAIT,
    AlertState,
    baseline_docids,
    escape_markdown,
    format_amount,
    format_asset,
    format_owner,
    format_transaction,
    is_within_window,
    load_alert_state,
    pdf_wait_action,
    plural,
    run_lock,
    save_alert_state,
    send_discord_message,
    shorten,
    split_message,
)
from watchlist_monitor import build_message, collapse_buys  # noqa: E402
from daily_summary import (  # noqa: E402
    build_digest,
    build_digest_batch,
    format_side,
    most_traded,
    select_members,
    summarise_filing,
)


class PdfWaitActionTest(unittest.TestCase):
    """An index row published ahead of its PDF used to consume the filing's only alert
    on the minutes its contents were unavailable. Hourly polling hits that gap often."""

    NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    def test_first_sighting_announces_the_filing(self):
        self.assertEqual(pdf_wait_action("20034963", {}, self.NOW), PDF_ANNOUNCE)

    def test_an_announced_filing_waits_rather_than_re_alerting(self):
        pending = {"20034963": (self.NOW - timedelta(hours=3)).isoformat()}
        self.assertEqual(pdf_wait_action("20034963", pending, self.NOW), PDF_WAIT)

    def test_the_grace_period_ends_the_wait(self):
        pending = {"20034963": (self.NOW - timedelta(hours=25)).isoformat()}
        self.assertEqual(pdf_wait_action("20034963", pending, self.NOW), PDF_GIVE_UP)

    def test_zero_grace_reports_unreadable_without_waiting(self):
        # --pending-grace 0 restores the old behaviour for anyone who wants it.
        pending = {"20034963": self.NOW.isoformat()}
        self.assertEqual(pdf_wait_action("20034963", pending, self.NOW, 0), PDF_GIVE_UP)

    def test_an_unreadable_timestamp_does_not_strand_the_filing(self):
        # A state file written by hand, or by a build that stored something else: the
        # filing must still resolve rather than being retried forever.
        pending = {"20034963": "not a timestamp"}
        self.assertEqual(pdf_wait_action("20034963", pending, self.NOW), PDF_GIVE_UP)

    def test_a_naive_timestamp_is_read_as_utc(self):
        pending = {"20034963": (self.NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()}
        self.assertEqual(pdf_wait_action("20034963", pending, self.NOW), PDF_WAIT)


class AlertStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_round_trips_seen_pending_and_membership(self):
        save_alert_state(self.path, AlertState(
            seen={"a", "b"}, pending={"c": "2026-09-14T12:00:00+00:00"},
            members={("pelosi", "CA11")}))
        state = load_alert_state(self.path)
        self.assertEqual(state.seen, {"a", "b"})
        self.assertEqual(state.pending, {"c": "2026-09-14T12:00:00+00:00"})
        self.assertEqual(state.members, {("pelosi", "CA11")})

    def test_reads_the_legacy_bare_list(self):
        # congress_monitor's state file was a plain DocID list; upgrading must not
        # re-alert on the entire back catalogue.
        self.path.write_text('["a", "b"]')
        self.assertEqual(load_alert_state(self.path).seen, {"a", "b"})

    def test_reads_a_dict_without_pending(self):
        self.path.write_text('{"seen": ["a"], "baselined_members": [["moore", "NC14"]]}')
        state = load_alert_state(self.path)
        self.assertEqual((state.seen, state.pending), ({"a"}, {}))
        self.assertEqual(state.members, {("moore", "NC14")})

    def test_missing_file_is_an_empty_state(self):
        state = load_alert_state(self.path)
        self.assertEqual((state.seen, state.pending, state.members), (set(), {}, set()))

    def test_wrong_top_level_shape_is_quarantined(self):
        self.path.write_text('"not an object"')
        with self.assertRaises(SystemExit):
            load_alert_state(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_suffix(".json.corrupt").exists())

    def test_wrong_nested_shape_is_quarantined(self):
        self.path.write_text('{"seen": "not a list"}')
        with self.assertRaises(SystemExit):
            load_alert_state(self.path)
        self.assertFalse(self.path.exists())
        self.assertTrue(self.path.with_suffix(".json.corrupt").exists())


class CongressDryRunTest(unittest.TestCase):
    def test_first_dry_run_does_not_create_baseline_state(self):
        rows = [{"DocID": "1", "FilingType": "P"}]
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "seen.json"
            args = mock.Mock(dry_run=True, verbose=False)
            with mock.patch.object(congress_monitor, "STATE_PATH", state_path), \
                    mock.patch.object(congress_monitor, "fetch_index_years", return_value=rows):
                congress_monitor.run(args)
            self.assertFalse(state_path.exists())


class RunLockTest(unittest.TestCase):
    """Hourly cron plus a slow run means two processes can overlap; both would read the
    same seen-set and post the same alert."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(monitor_common, "STATE_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_second_run_exits_instead_of_double_posting(self):
        with run_lock("congress_monitor"):
            with self.assertRaises(SystemExit) as caught:
                with run_lock("congress_monitor"):
                    self.fail("the second run entered the critical section")
        self.assertEqual(caught.exception.code, 0)  # a skipped poll is not an error

    def test_the_lock_is_released_when_the_run_finishes(self):
        with run_lock("congress_monitor"):
            pass
        with run_lock("congress_monitor"):
            pass  # reacquiring must work, including after an exception

    def test_a_crashing_run_still_releases_the_lock(self):
        with self.assertRaises(RuntimeError):
            with run_lock("congress_monitor"):
                raise RuntimeError("boom")
        with run_lock("congress_monitor"):
            pass

    def test_different_monitors_do_not_block_each_other(self):
        with run_lock("congress_monitor"), run_lock("watchlist_monitor"):
            pass


def txn(**kwargs):
    base = {
        "owner": "self", "asset": "Acme Corp (ACME)", "asset_type": "ST",
        "ticker": "ACME", "type": "Purchase", "partial": False,
        "txn_date": "07/01/2026", "notif_date": "07/02/2026",
        "amount": "$1,001 - $15,000",
    }
    base.update(kwargs)
    return base


class IndexYearsTest(unittest.TestCase):
    def test_midyear_reads_current_year_only(self):
        self.assertEqual(index_years(datetime(2026, 8, 2, tzinfo=timezone.utc)), [2026])

    def test_january_still_reads_the_previous_year(self):
        # A PTR filed on Dec 30 lives in the 2025 archive; a Jan 2 run must still see it.
        self.assertEqual(index_years(datetime(2026, 1, 2, tzinfo=timezone.utc)), [2026, 2025])

    def test_previous_year_drops_out_after_the_overlap(self):
        self.assertEqual(index_years(datetime(2026, 6, 1, tzinfo=timezone.utc)), [2026])


class FilingSortKeyTest(unittest.TestCase):
    def test_orders_oldest_first_not_archive_order(self):
        rows = [
            {"FilingDate": "7/26/2026", "DocID": "3"},
            {"FilingDate": "1/8/2026", "DocID": "1"},
            {"FilingDate": "4/30/2026", "DocID": "2"},
        ]
        self.assertEqual(
            [r["DocID"] for r in sorted(rows, key=filing_sort_key)], ["1", "2", "3"]
        )

    def test_undated_rows_sort_last_rather_than_crashing(self):
        rows = [{"FilingDate": "", "DocID": "2"}, {"FilingDate": "7/26/2026", "DocID": "1"}]
        self.assertEqual(
            [r["DocID"] for r in sorted(rows, key=filing_sort_key)], ["1", "2"]
        )


class BaselineWindowTest(unittest.TestCase):
    NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)

    def test_recent_filing_survives_promotion(self):
        self.assertTrue(is_within_window({"FilingDate": "7/30/2026"}, self.NOW, 7))

    def test_old_filing_is_baselined(self):
        self.assertFalse(is_within_window({"FilingDate": "4/30/2026"}, self.NOW, 7))

    def test_window_boundary_is_inclusive(self):
        self.assertTrue(is_within_window({"FilingDate": "7/26/2026"}, self.NOW, 7))
        self.assertFalse(is_within_window({"FilingDate": "7/25/2026"}, self.NOW, 7))

    def test_zero_window_keeps_only_todays_filings(self):
        self.assertTrue(is_within_window({"FilingDate": "8/2/2026"}, self.NOW, 0))
        self.assertFalse(is_within_window({"FilingDate": "8/1/2026"}, self.NOW, 0))

    def test_unparseable_date_is_treated_as_old(self):
        self.assertFalse(is_within_window({"FilingDate": "n/a"}, self.NOW, 7))
        self.assertFalse(is_within_window({}, self.NOW, 7))


class CollapseBuysTest(unittest.TestCase):
    def test_identical_rows_are_grouped_with_a_count(self):
        buys = [txn(), txn(), txn(ticker="OTHR", asset="Other Inc (OTHR)")]
        self.assertEqual([(t["ticker"], n) for t, n in collapse_buys(buys)],
                         [("ACME", 2), ("OTHR", 1)])

    def test_different_owners_are_not_merged(self):
        # One row per account is a real distinction, not a duplicate rendering.
        buys = [txn(owner="self"), txn(owner="SP")]
        self.assertEqual([n for _, n in collapse_buys(buys)], [1, 1])

    def test_same_ticker_different_date_is_not_merged(self):
        buys = [txn(txn_date="07/01/2026"), txn(txn_date="07/02/2026")]
        self.assertEqual(len(collapse_buys(buys)), 2)


class EquityFilterTest(unittest.TestCase):
    """The alerts are about stock picks. A member's Treasury ladder and target-date funds
    file through the same PTRs and used to alert identically."""

    def test_stocks_and_options_are_kept(self):
        self.assertTrue(is_equity_transaction(txn(asset_type="ST")))
        self.assertTrue(is_equity_transaction(txn(asset_type="OP")))

    def test_bonds_are_dropped(self):
        self.assertFalse(is_equity_transaction(
            txn(asset_type="GS", ticker=None, asset="US Treasury Note 3.5%")))
        self.assertFalse(is_equity_transaction(txn(asset_type="CS", ticker=None)))

    def test_funds_and_crypto_are_dropped(self):
        for code in ("EF", "MF", "CT", "RE", "HE", "RP"):
            self.assertFalse(is_equity_transaction(txn(asset_type=code)), code)

    def test_untyped_row_with_a_ticker_is_kept(self):
        # The asset-type code wraps onto a continuation line often enough that dropping
        # every untyped row would quietly cost real stock alerts.
        self.assertTrue(is_equity_transaction(txn(asset_type=None, ticker="ACME")))

    def test_untyped_row_without_a_ticker_is_dropped(self):
        self.assertFalse(is_equity_transaction(
            txn(asset_type=None, ticker=None, asset="US Treasury Note 3.5%")))

    def test_filter_keeps_order_and_drops_only_non_equities(self):
        rows = [txn(ticker="AAA"), txn(asset_type="GS", ticker=None), txn(ticker="BBB")]
        self.assertEqual([t["ticker"] for t in equity_transactions(rows)], ["AAA", "BBB"])

    def test_bond_only_filing_filters_to_nothing(self):
        # Distinct from an unreadable filing, which parses to [] and must still alert.
        self.assertEqual(equity_transactions([txn(asset_type="GS", ticker=None)]), [])


class FormatAmountTest(unittest.TestCase):
    def test_range_is_quoted_by_its_round_bounds(self):
        self.assertEqual(format_amount("$15,001 - $50,000"), "$15k-$50k")
        self.assertEqual(format_amount("$1,001 - $15,000"), "$1k-$15k")
        self.assertEqual(format_amount("$1,000,001 - $5,000,000"), "$1M-$5M")

    def test_open_ended_top_bracket_says_over(self):
        self.assertEqual(format_amount("Over $50,000,000"), "over $50M")

    def test_unparseable_amount_is_passed_through_not_guessed(self):
        self.assertEqual(format_amount("see attachment"), "see attachment")
        self.assertEqual(format_amount(""), "amount not disclosed")


class FormatOwnerTest(unittest.TestCase):
    def test_codes_become_words(self):
        self.assertEqual(format_owner("SP"), "spouse")
        self.assertEqual(format_owner("DC"), "child")
        self.assertEqual(format_owner("JT"), "joint")

    def test_the_filers_own_account_is_left_unsaid(self):
        self.assertEqual(format_owner("self"), "")
        self.assertEqual(format_owner(None), "")


class ShortenTest(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(shorten("Acme Corp", 20), "Acme Corp")

    def test_cut_lands_on_a_word_boundary(self):
        # "US Treasury Note 3.5% DU" read as a parser bug rather than an abbreviation.
        self.assertEqual(shorten("US Treasury Note 3.5% DUE 01/31/28", 24),
                         "US Treasury Note 3.5%…")

    def test_single_long_word_is_still_cut(self):
        self.assertEqual(shorten("x" * 30, 10), "x" * 10 + "…")


class FormatAssetTest(unittest.TestCase):
    def test_ticker_leads_and_is_not_repeated_in_the_name(self):
        self.assertEqual(format_asset(txn(asset="Acme Corp Common Stock (ACME)")),
                         "**ACME** Acme")

    def test_legal_suffix_is_dropped_when_a_ticker_identifies_the_company(self):
        self.assertEqual(
            format_asset(txn(ticker="IBM",
                             asset="International Business Machines Corporation")),
            "**IBM** International Business Machines")

    def test_untickered_asset_keeps_its_full_name(self):
        # A bond has no ticker, so the name is the only identifier the reader gets.
        self.assertIn("US Treasury Note",
                      format_asset(txn(ticker=None, asset="US Treasury Note 3.5%")))

    def test_markdown_in_a_filing_name_is_neutralised(self):
        self.assertIn(r"A\_B", format_asset(txn(ticker=None, asset="A_B")))


class FormatTransactionTest(unittest.TestCase):
    FILED = "7/26/2026"

    def test_line_leads_with_direction_asset_and_size(self):
        line = format_transaction(txn(), self.FILED)
        self.assertTrue(line.startswith("🟢 **BUY** **ACME**"))
        self.assertIn("$1k-$15k", line)
        self.assertIn("traded 07/01/2026", line)

    def test_owner_code_is_spelled_out(self):
        self.assertIn("spouse", format_transaction(txn(owner="SP"), self.FILED))
        self.assertNotIn("[SP]", format_transaction(txn(owner="SP"), self.FILED))

    def test_non_equity_assets_are_tagged(self):
        # Otherwise a Treasury note in a "buy alert" reads as a stock pick.
        line = format_transaction(txn(asset_type="GS", ticker=None), self.FILED)
        self.assertIn("[govt bond]", line)

    def test_repeated_rows_are_marked_with_a_count(self):
        self.assertIn("×3", format_transaction(txn(), self.FILED, count=3))

    def test_late_filings_show_the_days_taken(self):
        line = format_transaction(txn(txn_date="01/02/2026"), self.FILED)
        self.assertIn("⏰ 205d", line)

    def test_timely_filings_are_not_flagged(self):
        self.assertNotIn("⏰", format_transaction(txn(txn_date="07/20/2026"), self.FILED))


class PluralTest(unittest.TestCase):
    def test_singular_and_plural_instead_of_a_parenthesised_s(self):
        self.assertEqual(plural(1, "filing"), "1 filing")
        self.assertEqual(plural(2, "filing"), "2 filings")


class BuildMessageTest(unittest.TestCase):
    MEMBER = {"member": "Hon. Test Filer", "state_dist": "XX01",
              "return_pct_estimate": 12.5, "rank": 3}
    ROW = {"Year": "2026", "DocID": "20034963", "FilingDate": "7/26/2026"}

    def test_purchase_message_lists_the_buys(self):
        msg = build_message(self.MEMBER, self.ROW, [txn()], 10)
        self.assertIn("Hon. Test Filer", msg)
        self.assertIn("**ACME**", msg)
        self.assertIn("20034963.pdf", msg)

    def test_rank_states_what_it_is_out_of(self):
        # "watchlist #3" never said out of how many, nor what the ranking measured.
        msg = build_message(self.MEMBER, self.ROW, [txn()], 10)
        self.assertIn("#3 of 10 on the watchlist", msg)
        self.assertIn("+12.5% estimated return", msg)

    def test_return_is_quoted_against_the_benchmark(self):
        # A return alone cannot be read: +12.5% is a good year or a bad one depending
        # entirely on what the index did over the same period.
        member = dict(self.MEMBER, alpha_pct_estimate=4.5,
                      benchmark_return_pct_estimate=8.0, benchmark="SPY")
        msg = build_message(member, self.ROW, [txn()], 10)
        self.assertIn("+12.5% estimated return", msg)
        self.assertIn("+4.5% vs SPY (+8.0%)", msg)

    def test_member_without_an_alpha_still_states_its_return(self):
        # An unbenchmarked member drops the comparison rather than the whole line.
        msg = build_message(self.MEMBER, self.ROW, [txn()], 10)
        self.assertIn("+12.5% estimated return", msg)
        self.assertNotIn(" vs ", msg)

    def test_unparseable_filing_still_produces_an_alert(self):
        # The whole point: no purchases parsed must not read as "no purchases made".
        msg = build_message(self.MEMBER, self.ROW, [], 10)
        self.assertIn("could not be read from the PDF", msg)
        self.assertIn("20034963.pdf", msg)

    def test_late_filings_are_flagged_and_explained(self):
        msg = build_message(self.MEMBER, self.ROW, [txn(txn_date="01/02/2026")], 10)
        self.assertIn("⏰", msg)
        self.assertIn("STOCK Act deadline", msg)

    def test_timely_filings_are_not_flagged(self):
        msg = build_message(self.MEMBER, self.ROW, [txn(txn_date="07/20/2026")], 10)
        self.assertNotIn("⏰", msg)


class BaselineDocidsTest(unittest.TestCase):
    NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)
    ROWS = [
        {"DocID": "old", "FilingDate": "1/8/2026", "Last": "A", "First": "X", "StateDst": "XX01"},
        {"DocID": "new", "FilingDate": "7/30/2026", "Last": "A", "First": "X", "StateDst": "XX01"},
        {"DocID": "other", "FilingDate": "1/8/2026", "Last": "B", "First": "Y", "StateDst": "YY02"},
    ]
    MEMBER_A = {("a", "XX01")}

    def test_only_stale_filings_of_new_members_are_baselined(self):
        self.assertEqual(baseline_docids(self.ROWS, self.MEMBER_A, self.NOW, 7), {"old"})

    def test_first_run_baselines_the_whole_back_catalogue(self):
        self.assertEqual(
            baseline_docids(self.ROWS, self.MEMBER_A, self.NOW, 7, apply_window=False),
            {"old", "new"},
        )

    def test_members_outside_the_set_are_untouched(self):
        self.assertNotIn("other", baseline_docids(self.ROWS, self.MEMBER_A, self.NOW, 7))


class SelectMembersTest(unittest.TestCase):
    # R1 returned less than R2 but beat the index; R2's 9% trailed a 12% index.
    LEADERBOARD = {"members": [
        {"member": "R1", "ranked": True, "return_pct_estimate": 5.0,
         "alpha_pct_estimate": 3.0, "invested_total_estimate": 100.0,
         "traded_total_estimate": 100.0},
        {"member": "R2", "ranked": True, "return_pct_estimate": 9.0,
         "alpha_pct_estimate": -3.0, "invested_total_estimate": 50.0,
         "traded_total_estimate": 50.0},
        {"member": "U1", "ranked": False, "return_pct_estimate": 99.0,
         "alpha_pct_estimate": 90.0, "invested_total_estimate": 900.0,
         "traded_total_estimate": 900.0},
        {"member": "U2", "ranked": False, "return_pct_estimate": None,
         "invested_total_estimate": 0.0, "traded_total_estimate": 1000.0},
    ]}

    def test_alpha_mode_ranks_beating_the_index_above_a_bigger_raw_return(self):
        # The whole reason alpha is the default: R2's +9% trailed the index it was
        # earned in, and R1's +5% beat it.
        picked = select_members(self.LEADERBOARD, 50, "alpha")
        self.assertEqual([m["member"] for m in picked], ["R1", "R2"])

    def test_alpha_mode_skips_members_with_no_benchmark(self):
        board = {"members": [dict(self.LEADERBOARD["members"][0], alpha_pct_estimate=None)]}
        self.assertEqual(select_members(board, 50, "alpha"), [])

    def test_return_mode_uses_ranked_members_only(self):
        # The unranked +99% member is a single lucky trade, not a top performer.
        picked = select_members(self.LEADERBOARD, 50, "return")
        self.assertEqual([m["member"] for m in picked], ["R2", "R1"])

    def test_return_mode_caps_at_the_number_that_qualify(self):
        self.assertEqual(len(select_members(self.LEADERBOARD, 50, "return")), 2)

    def test_volume_mode_includes_sell_only_filers(self):
        picked = select_members(self.LEADERBOARD, 50, "volume")
        self.assertEqual([m["member"] for m in picked], ["U2", "U1", "R1", "R2"])

    def test_top_is_respected(self):
        self.assertEqual(len(select_members(self.LEADERBOARD, 1, "volume")), 1)


class SummariseFilingTest(unittest.TestCase):
    def test_splits_buys_from_sells_and_totals_dollars(self):
        s = summarise_filing([
            txn(ticker="NVDA", amount="$1,001 - $15,000"),
            txn(ticker="NVDA", amount="$1,001 - $15,000"),
            txn(ticker="MSFT", type="Sale", amount="$15,001 - $50,000"),
        ])
        self.assertEqual(s["buys"]["NVDA"], [2, 16001.0])
        self.assertEqual(s["sells"]["MSFT"], [1, 32500.5])

    def test_exchanges_are_not_counted_as_a_direction(self):
        s = summarise_filing([txn(type="Exchange")])
        self.assertEqual((s["buys"], s["sells"]), ({}, {}))

    def test_untickered_assets_fall_back_to_the_asset_name(self):
        s = summarise_filing([txn(ticker=None, asset="US Treasury Note")])
        self.assertIn("US Treasury Note", s["buys"])

    def test_unparseable_amount_still_counts_the_trade(self):
        s = summarise_filing([txn(amount="")])
        self.assertEqual(s["buys"]["ACME"], [1, 0.0])


class FormatSideTest(unittest.TestCase):
    def test_orders_by_dollars_and_marks_repeats(self):
        side = {"AAA": [1, 5000.0], "BBB": [3, 90000.0]}
        self.assertEqual(format_side(side), "BBB ×3 (~$90k), AAA (~$5k)")

    def test_long_lists_are_capped_with_a_remainder(self):
        side = {f"T{i}": [1, float(100 - i)] for i in range(12)}
        self.assertIn("+4 more", format_side(side))


class MostTradedTest(unittest.TestCase):
    def _entry(self, name, buys=(), sells=()):
        return {"member": {"member": name, "rank": 1},
                "summary": {"buys": {t: [1, 1.0] for t in buys},
                            "sells": {t: [1, 1.0] for t in sells}}}

    def test_reports_only_tickers_more_than_one_member_touched(self):
        entries = [self._entry("A", buys=["NVDA", "SOLO"]), self._entry("B", buys=["NVDA"])]
        lines = most_traded(entries, 2)
        self.assertEqual(len(lines), 1)
        self.assertIn("NVDA (2 of 2)", lines[0])
        self.assertNotIn("SOLO", lines[0])

    def test_count_is_reported_against_the_number_who_filed(self):
        # "2 members" is striking out of thirty and unremarkable out of two.
        entries = [self._entry("A", buys=["NVDA"]), self._entry("B", buys=["NVDA"])]
        self.assertIn("NVDA (2 of 30)", most_traded(entries, 30)[0])

    def test_silent_when_nothing_clusters(self):
        self.assertEqual(most_traded([self._entry("A", buys=["NVDA"])], 1), [])


class BuildDigestTest(unittest.TestCase):
    NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)

    def _entry(self):
        return {
            "member": {"member": "Hon. Test Filer", "state_dist": "XX01", "rank": 3,
                       "return_pct_estimate": 12.5},
            "pdf_link": "https://example.invalid/20034963.pdf",
            "txn_count": 2,
            "summary": summarise_filing([txn(ticker="NVDA"), txn(ticker="MSFT", type="Sale")]),
        }

    def test_digest_reports_scope_and_both_directions(self):
        msg = build_digest(self.NOW, [self._entry()], "est. return", 25, 25)
        self.assertIn("top 25 members by est. return", msg)
        self.assertIn("BOUGHT", msg)
        self.assertIn("SOLD", msg)
        self.assertIn("20034963.pdf", msg)

    def test_totals_say_they_are_range_midpoints(self):
        # Every figure here is an estimate; the message should not imply otherwise.
        self.assertIn("midpoints of the disclosed ranges",
                      build_digest(self.NOW, [self._entry()], "est. return", 25, 25))

    def test_shortfall_against_the_requested_top_n_is_stated(self):
        # Asking for 50 when only 25 qualify must not silently report on 25.
        msg = build_digest(self.NOW, [self._entry()], "est. return", 25, 50)
        self.assertIn("50 were requested, but only 25 qualify", msg)

    def test_no_shortfall_note_when_the_full_set_is_covered(self):
        msg = build_digest(self.NOW, [self._entry()], "est. return", 50, 50)
        self.assertNotIn("requested", msg)

    def test_unparsed_filing_is_shown_rather_than_omitted(self):
        entry = dict(self._entry(), txn_count=0, summary=summarise_filing([]))
        msg = build_digest(self.NOW, [entry], "est. return", 25, 25)
        self.assertIn("could not be read from the PDF", msg)

    def test_oversized_digest_defers_whole_filings(self):
        msg, included = build_digest_batch(
            self.NOW, [self._entry()] * 400, "est. return", 50, 50
        )
        self.assertGreater(included, 0)
        self.assertLess(included, 400)
        self.assertFalse(msg.endswith("_(truncated)_"))
        self.assertLessEqual(len(split_message(msg)), 3)


class FakeResponse:
    def __init__(self, status_code=204, payload=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class EscapeMarkdownTest(unittest.TestCase):
    def test_escapes_discord_markdown(self):
        self.assertEqual(escape_markdown("*Hon* Filer"), r"\*Hon\* Filer")
        self.assertEqual(escape_markdown("A_B"), r"A\_B")

    def test_escapes_backslash_first(self):
        self.assertEqual(escape_markdown(r"a\*b"), r"a\\\*b")

    def test_leaves_ampersand_alone(self):
        """Discord renders &amp; literally, so entities must not be escaped."""
        self.assertEqual(escape_markdown("AT&T Inc."), "AT&T Inc.")


class SplitMessageTest(unittest.TestCase):
    def test_short_message_is_one_chunk(self):
        self.assertEqual(split_message("a\nb"), ["a\nb"])

    def test_empty_message_still_yields_a_chunk(self):
        self.assertEqual(split_message(""), [""])

    def test_chunks_respect_the_limit_and_lose_nothing(self):
        text = "\n".join(f"- filing {i} " + "x" * 90 for i in range(200))
        chunks = split_message(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("\n".join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), DISCORD_MESSAGE_LIMIT)

    def test_line_longer_than_the_limit_is_hard_split(self):
        chunks = split_message("z" * (DISCORD_MESSAGE_LIMIT * 2 + 1))
        self.assertEqual(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), DISCORD_MESSAGE_LIMIT)


class SendDiscordMessageTest(unittest.TestCase):
    """A PTR disclosing enough transactions clears Discord's 2000-char limit, which it
    rejects outright rather than truncating -- so an unsplit alert is a lost alert."""

    def _capture(self, responses=None):
        posted = []
        seq = list(responses or [])

        def fake_post(url, json=None, timeout=None):
            posted.append(json)
            return seq.pop(0) if seq else FakeResponse()

        return posted, fake_post

    def test_uses_discord_content_key(self):
        posted, fake_post = self._capture()
        with mock.patch.object(monitor_common.requests, "post", fake_post):
            send_discord_message("https://hook", "hello")
        self.assertEqual(posted, [{"content": "hello"}])

    def test_oversized_message_is_split_across_posts(self):
        posted, fake_post = self._capture()
        text = "\n".join(f"- line {i} " + "y" * 90 for i in range(200))
        with mock.patch.object(monitor_common.requests, "post", fake_post):
            send_discord_message("https://hook", text)
        self.assertGreater(len(posted), 1)
        for payload in posted:
            self.assertLessEqual(len(payload["content"]), DISCORD_MESSAGE_LIMIT)
        self.assertEqual("\n".join(p["content"] for p in posted), text)

    def test_retries_after_rate_limit(self):
        posted, fake_post = self._capture(
            [FakeResponse(429, {"retry_after": 0.01}), FakeResponse()])
        with mock.patch.object(monitor_common.requests, "post", fake_post), \
                mock.patch.object(monitor_common.time, "sleep") as slept:
            send_discord_message("https://hook", "hi")
        self.assertEqual(len(posted), 2)
        slept.assert_called_once()

    def test_rate_limit_wait_is_capped(self):
        _, fake_post = self._capture([FakeResponse(429, {"retry_after": 99999})] * 9)
        with mock.patch.object(monitor_common.requests, "post", fake_post), \
                mock.patch.object(monitor_common.time, "sleep") as slept:
            with self.assertRaises(requests.HTTPError):
                send_discord_message("https://hook", "hi")
        for call in slept.call_args_list:
            self.assertLessEqual(call[0][0], MAX_RETRY_SLEEP)


if __name__ == "__main__":
    unittest.main()
