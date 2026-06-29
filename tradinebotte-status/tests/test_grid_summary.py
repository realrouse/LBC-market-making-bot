"""Unit tests for generate_status grid-aggregate rendering.

Grid bots keep aggregate state (bounds / cycles / level fills / halted) in grid_state +
grid_levels rather than a per-trade log, so the Polymarket trade queries return nothing
for them. These tests lock in that the grid aggregate is surfaced in the account card —
for a grid at the standard path (account runs only a grid) and at an alternate data dir
(TRADINEBOTTE_DIR=~/tradinebotte-grid) — and that the halted flag raises a badge.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402


def _hb(bot, payload):
    return {"bot_name": bot, "_label": "acct-3", "flag": "ALIVE", "age_s": 30,
            "bounds_ok": "ok", "version": "10fa979", "payload": payload}


def _grid(dirname="tradinebotte-grid", halted=0, buy=16, sell=14):
    return {
        "dir": dirname,
        "state": [{"symbol": "BTCUSDT", "grid_lower": 49000.0, "grid_upper": 73500.0,
                   "grid_step": 844.83, "order_size_usdt": 50.0, "total_cycles": 206,
                   "total_profit_usd": 124.41, "halted": halted}],
        "levels": [{"status": "buy_placed", "n": buy}, {"status": "sell_placed", "n": sell}],
    }


class TestGridSummary(unittest.TestCase):

    def test_alt_dir_grid_aggregate_rendered(self):
        hb = [_hb("live_bot", {"capital": 1131.38, "daily_pnl": 27.28}),
              _hb("grid_bot", {"daily_pnl": 8.4})]
        data = {"version": "10fa979", "services": [], "live": None,
                "grids": [_grid()], "accum": None}
        html = g._render_account_card("acct-3 [poly+grid]", data, hb)
        self.assertIn("grid", html)
        self.assertIn("$49.0k–$73.5k", html)   # bounds
        self.assertIn("14/30 holding", html)    # sell_placed / total
        self.assertIn("206 cycles", html)
        self.assertIn("+$124.41", html)         # total_profit_usd via _fmt_pnl

    def test_standard_path_grid_also_rendered(self):
        """An account running only a grid keeps grid_state at the standard ~/tradinebotte path."""
        data = {"version": "x", "services": [], "live": None,
                "grids": [_grid(dirname="tradinebotte", buy=9, sell=12)], "accum": None}
        html = g._render_account_card("acct-6 [grid]", data, [_hb("grid_bot", {"daily_pnl": 0})])
        self.assertIn("12/21 holding", html)

    def test_halted_grid_shows_badge(self):
        data = {"version": "x", "services": [], "live": None,
                "grids": [_grid(halted=1)], "accum": None}
        html = g._render_account_card("acct-3", data, [_hb("grid_bot", {"daily_pnl": 0})])
        self.assertIn("HALTED", html)

    def test_no_grids_renders_nothing_extra(self):
        data = {"version": "x", "services": [], "live": None, "accum": None}  # no 'grids' key
        html = g._render_account_card("acct-2 [poly]", data, [_hb("live_bot", {"capital": 1, "daily_pnl": 0})])
        self.assertNotIn("holding", html)
        self.assertNotIn("HALTED", html)


if __name__ == "__main__":
    unittest.main()
