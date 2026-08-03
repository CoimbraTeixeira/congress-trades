"""Regression tests for the alerting logic that decides whether a filing is seen.

Run with: python3 -m unittest discover -s tests

These cover the ways a real filing can end up silently not alerted: falling outside the
index years the monitor fetches, being baselined away when its member joins the
watchlist, or being read as "no purchases" when it was actually unreadable.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from house_disclosures import filing_sort_key, index_years  # noqa: E402
import monitor_common  # noqa: E402
from monitor_common import (  # noqa: E402
    DISCORD_MESSAGE_LIMIT,
    MAX_RETRY_SLEEP,
    baseline_docids,
    escape_markdown,
    is_within_window,
    send_discord_message,
    split_message,
)
from watchlist_monitor import build_message, collapse_buys  # noqa: E402
from daily_summary import (  # noqa: E402
    build_digest,
    format_side,
    most_traded,
    select_members,
    summarise_filing,
)


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


class BuildMessageTest(unittest.TestCase):
    MEMBER = {"member": "Hon. Test Filer", "state_dist": "XX01",
              "return_pct_estimate": 12.5, "rank": 3}
    ROW = {"Year": "2026", "DocID": "20034963", "FilingDate": "7/26/2026"}

    def test_purchase_message_lists_the_buys(self):
        msg = build_message(self.MEMBER, self.ROW, [txn()])
        self.assertIn("watchlist #3", msg)
        self.assertIn("+12.5% est. return", msg)
        self.assertIn("Acme Corp (ACME)", msg)
        self.assertIn("20034963.pdf", msg)

    def test_unparseable_filing_still_produces_an_alert(self):
        # The whole point: no purchases parsed must not read as "no purchases made".
        msg = build_message(self.MEMBER, self.ROW, [])
        self.assertIn("could not be auto-parsed", msg)
        self.assertIn("20034963.pdf", msg)

    def test_late_filings_are_flagged(self):
        msg = build_message(self.MEMBER, self.ROW, [txn(txn_date="01/02/2026")])
        self.assertIn("LATE", msg)

    def test_timely_filings_are_not_flagged(self):
        msg = build_message(self.MEMBER, self.ROW, [txn(txn_date="07/20/2026")])
        self.assertNotIn("LATE", msg)


class BaselineDocidsTest(unittest.TestCase):
    NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)
    ROWS = [
        {"DocID": "old", "FilingDate": "1/8/2026", "Last": "A", "First": "X", "StateDst": "XX01"},
        {"DocID": "new", "FilingDate": "7/30/2026", "Last": "A", "First": "X", "StateDst": "XX01"},
        {"DocID": "other", "FilingDate": "1/8/2026", "Last": "B", "First": "Y", "StateDst": "YY02"},
    ]
    MEMBER_A = {("A", "X", "XX01")}

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
    LEADERBOARD = {"members": [
        {"member": "R1", "ranked": True, "return_pct_estimate": 5.0,
         "invested_total_estimate": 100.0},
        {"member": "R2", "ranked": True, "return_pct_estimate": 9.0,
         "invested_total_estimate": 50.0},
        {"member": "U1", "ranked": False, "return_pct_estimate": 99.0,
         "invested_total_estimate": 900.0},
        {"member": "U2", "ranked": False, "invested_total_estimate": 0.0},
    ]}

    def test_return_mode_uses_ranked_members_only(self):
        # The unranked +99% member is a single lucky trade, not a top performer.
        picked = select_members(self.LEADERBOARD, 50, "return")
        self.assertEqual([m["member"] for m in picked], ["R2", "R1"])

    def test_return_mode_caps_at_the_number_that_qualify(self):
        self.assertEqual(len(select_members(self.LEADERBOARD, 50, "return")), 2)

    def test_volume_mode_includes_unranked_filers(self):
        picked = select_members(self.LEADERBOARD, 50, "volume")
        self.assertEqual([m["member"] for m in picked], ["U1", "R1", "R2"])

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
        self.assertEqual(format_side(side), "BBB x3 (~$90k), AAA (~$5k)")

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
        lines = most_traded(entries)
        self.assertEqual(len(lines), 1)
        self.assertIn("NVDA (2 members)", lines[0])
        self.assertNotIn("SOLO", lines[0])

    def test_silent_when_nothing_clusters(self):
        self.assertEqual(most_traded([self._entry("A", buys=["NVDA"])]), [])


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
        self.assertIn("top 25 by est. return", msg)
        self.assertIn("BUY", msg)
        self.assertIn("SELL", msg)
        self.assertIn("20034963.pdf", msg)

    def test_shortfall_against_the_requested_top_n_is_stated(self):
        # Asking for 50 when only 25 qualify must not silently report on 25.
        msg = build_digest(self.NOW, [self._entry()], "est. return", 25, 50)
        self.assertIn("of 50 requested; only 25 qualify", msg)

    def test_no_shortfall_note_when_the_full_set_is_covered(self):
        msg = build_digest(self.NOW, [self._entry()], "est. return", 50, 50)
        self.assertNotIn("requested", msg)

    def test_unparsed_filing_is_shown_rather_than_omitted(self):
        entry = dict(self._entry(), txn_count=0, summary=summarise_filing([]))
        msg = build_digest(self.NOW, [entry], "est. return", 25, 25)
        self.assertIn("no transactions parsed", msg)

    def test_oversized_digest_is_truncated(self):
        msg = build_digest(self.NOW, [self._entry()] * 400, "est. return", 50, 50)
        self.assertTrue(msg.endswith("_(truncated)_"))

    def test_truncated_digest_still_fits_the_message_budget(self):
        """The marker must not push the digest into an extra Discord message."""
        msg = build_digest(self.NOW, [self._entry()] * 400, "est. return", 50, 50)
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
