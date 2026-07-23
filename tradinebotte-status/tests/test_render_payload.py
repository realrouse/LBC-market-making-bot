"""Unit tests for generate_status._render_payload_summary — per-bot heartbeat summary.

Covers the cumulative-PnL (pnl_total) reporting and, critically, the fallback path
for heartbeats that predate pnl_total — the current production state during a deploy.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402

_NOW = 9_999_999_999


class TestRenderPayloadSummary(unittest.TestCase):

    def test_old_heartbeat_without_pnl_total_shows_daily_as_pnl(self):
        """Old bots emit daily_pnl but no pnl_total — must not crash, daily → pnl=."""
        out = g._render_payload_summary(
            "swing_bot", {"daily_pnl": -5.0, "capital": 100}, _NOW)
        self.assertIn("pnl=$-5.00", out)
        self.assertNotIn("day=", out)
        self.assertNotIn("None", out)

    def test_new_heartbeat_shows_cumulative_as_headline_and_daily_separately(self):
        out = g._render_payload_summary(
            "swing_bot",
            {"daily_pnl": -5.0, "pnl_total": 57.17, "trades_total": 10, "capital": 100},
            _NOW)
        self.assertIn("pnl=$+57.17", out)   # cumulative is the headline
        self.assertIn("day=$-5.00", out)    # daily shown alongside
        self.assertIn("trades=10", out)

    def test_grid_zero_cumulative_still_renders(self):
        out = g._render_payload_summary(
            "grid_bot",
            {"daily_pnl": 0.0, "pnl_total": 0.0, "trades_total": 0, "capital": 2000},
            _NOW)
        self.assertIn("pnl=$+0.00", out)
        self.assertNotIn("None", out)


class TestAccountBotRendering(unittest.TestCase):
    """account_bot (Polymarket feed consumer) shares live_bot's payload shape, so its
    tooltip labels must mean the same thing: pnl=/day=/trades= are cumulative/daily/total,
    NOT daily/open (the pre-fix branch mislabelled them, contradicting its windowed PnL)."""

    _PAYLOAD = {"pnl_total": 64.77, "daily_pnl": 0.79, "trades_total": 513,
                "capital": 1064.77, "open_trades": 2,
                "last_feed_msg_ts": _NOW - 30}

    def test_pnl_is_cumulative_not_daily(self):
        out = g._render_payload_summary("account_bot", self._PAYLOAD, _NOW)
        self.assertIn("pnl=$+64.77", out)   # cumulative headline, matches alltime span
        self.assertIn("day=$+0.79", out)    # daily shown separately
        self.assertNotIn("pnl=$+0.79", out)  # regression: daily must NOT be labelled pnl=

    def test_trades_is_total_not_open(self):
        out = g._render_payload_summary("account_bot", self._PAYLOAD, _NOW)
        self.assertIn("trades=513", out)    # total, not open_trades (2)
        self.assertIn("open=2", out)
        self.assertIn("cap=$1065", out)
        self.assertIn("feed=", out)
        self.assertNotIn("None", out)

    def test_old_heartbeat_without_pnl_total_falls_back_to_daily(self):
        out = g._render_payload_summary(
            "account_bot", {"daily_pnl": 1.5, "open_trades": 0}, _NOW)
        self.assertIn("pnl=$+1.50", out)    # no pnl_total → daily shown as pnl=
        self.assertNotIn("day=", out)
        self.assertNotIn("None", out)


class TestDataFreshness(unittest.TestCase):
    """Regression guard for the 2026-06-16 silent-recording bug: a bot can heartbeat
    fine while its data table stops growing. last_write_ts drives the ⚠data flag."""

    def test_data_flag_stale_when_last_write_old(self):
        stale = {"last_write_ts": _NOW - (g._DATA_STALE_AFTER + 100)}
        self.assertEqual(g._data_flag(stale, _NOW), "STALE")

    def test_data_flag_ok_when_last_write_recent(self):
        fresh = {"last_write_ts": _NOW - 30}
        self.assertEqual(g._data_flag(fresh, _NOW), "")

    def test_data_flag_absent_field_never_alarms(self):
        # infra services / --no-snapshots bots OMIT last_write_ts → no false alarm
        self.assertEqual(g._data_flag({}, _NOW), "")

    def test_data_flag_zero_means_booted_but_never_wrote_alarms(self):
        # present-but-0.0 = bot claims to record but has written nothing (post-restart
        # repro of the 06-16 bug). Must alarm — this is the case the monitor exists for.
        self.assertEqual(g._data_flag({"last_write_ts": 0}, _NOW), "STALE")

    def test_summary_marks_stale_data_for_recording_bots(self):
        out = g._render_payload_summary(
            "swing_bot",
            {"pnl_total": 1.0, "last_book_ts": _NOW - 5,
             "last_write_ts": _NOW - (g._DATA_STALE_AFTER + 100)},
            _NOW)
        self.assertIn("data=", out)
        self.assertIn("⚠", out)          # stale marker present
        self.assertIn("book=", out)      # book (received) still shown separately

    def test_summary_fresh_data_has_no_warning(self):
        out = g._render_payload_summary(
            "grid_bot",
            {"pnl_total": 1.0, "last_write_ts": _NOW - 30}, _NOW)
        self.assertIn("data=", out)
        self.assertNotIn("⚠", out)


if __name__ == "__main__":
    unittest.main()
