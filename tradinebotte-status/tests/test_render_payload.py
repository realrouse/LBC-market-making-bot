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


class TestOrderbookBotRendering(unittest.TestCase):
    """orderbook_bot has its own payload shape (total_pnl / open_positions / last_price)."""

    def test_payload_summary_shows_pnl_positions_price(self):
        out = g._render_payload_summary(
            "orderbook_bot",
            {"total_pnl": -2.5, "open_positions": 3, "last_price": 64200.0, "bounds_ok": True},
            _NOW)
        self.assertIn("pnl=$-2.50", out)
        self.assertIn("pos=3", out)
        self.assertIn("px=$64,200", out)
        self.assertNotIn("None", out)

    def test_key_metric_uses_total_pnl_with_sign(self):
        self.assertEqual(g._key_metric("orderbook_bot", {"total_pnl": 12.3}), "+$12.30")
        self.assertEqual(g._key_metric("orderbook_bot", {"total_pnl": -4.0}), "-$4.00")
        self.assertEqual(g._key_metric("orderbook_bot", {}), "")


if __name__ == "__main__":
    unittest.main()
