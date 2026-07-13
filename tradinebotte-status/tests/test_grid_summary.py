"""Unit tests for generate_status grid + accumulation card rendering.

The status page is DB-only: the grid and accumulation detail lines are derived straight
from each bot's heartbeat *payload* (in the shared state DB), not from grid_state /
live_accum.db over SSH. These tests lock in that a grid bot surfaces its lifetime PnL and
an accumulation bot surfaces its portfolio, keyed off the bot_id family tag — and that an
account with neither renders no such line. (The exact grid bounds / cycles / level fills
and the Polymarket per-trade tables are intentionally gone: they are not in the DB.)
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402


def _hb(bot, payload, display=None):
    return {"bot_name": bot, "_display": display, "_label": "acct-3", "flag": "ALIVE",
            "age_s": 30, "bounds_ok": "ok", "version": "10fa979", "payload": payload}


def _card(label, hb):
    # accounts_data dicts are now trivial: version + empty services; all detail is in hb.
    data = {"version": "10fa979", "services": [], "live": None,
            "grids": [], "accum": None, "error": None}
    return g._render_account_card(label, data, hb)


class TestGridSummary(unittest.TestCase):

    def test_grid_pnl_rendered_from_payload(self):
        hb = [_hb("polymarket-threshold-e100a8", {"capital": 1131.38, "daily_pnl": 27.28}),
              _hb("binance-grid-btcusdt-df6dd9", {"pnl_total": 140.54, "bounds_ok": True},
                  display="binance-grid-btcusdt")]
        html = _card("acct-3 [poly+grid]", hb)
        self.assertIn("binance-grid-btcusdt", html)
        self.assertIn("+$140.54", html)          # lifetime grid PnL from payload

    def test_grid_only_account_rendered(self):
        hb = [_hb("mexc_futures-grid-btc_usdt-d1e533", {"pnl_total": 336.95},
                  display="mexc_futures-grid-btcusdt")]
        html = _card("acct-6 [grid]", hb)
        self.assertIn("mexc_futures-grid-btcusdt", html)
        self.assertIn("+$336.95", html)

    def test_grid_without_pnl_renders_no_line(self):
        # A grid heartbeat that carries no pnl_total yet must not emit a broken line.
        hb = [_hb("binance-grid-btcusdt-df6dd9", {"bounds_ok": True})]
        html = _card("acct-3", hb)
        self.assertNotIn("+$", html)

    def test_no_grid_renders_nothing_extra(self):
        hb = [_hb("polymarket-threshold-00b656", {"capital": 1, "daily_pnl": 0})]
        html = _card("acct-2 [poly]", hb)
        self.assertNotIn(g.t("grid_label"), html)


class TestAccumSummary(unittest.TestCase):

    def test_accum_portfolio_rendered_from_payload(self):
        hb = [_hb("mexc-accumulation-btcusdt-4a3e3a",
                  {"holdings_btc": 0.007669, "free_usdt": 500.0, "avg_entry": 65200.93,
                   "trades_total": 3},
                  display="mexc-accumulation-btcusdt")]
        html = _card("acct-2 [poly+accum]", hb)
        self.assertIn("mexc-accumulation-btcusdt", html)
        self.assertIn("0.007669", html)          # holdings from payload
        self.assertIn("3T", html)

    def test_no_accum_renders_no_portfolio_line(self):
        hb = [_hb("binance-swing-btcusdt-062cc5", {"capital": 1, "daily_pnl": 0})]
        html = _card("acct-5 [swing]", hb)
        self.assertNotIn("holdings", html.lower())


if __name__ == "__main__":
    unittest.main()
