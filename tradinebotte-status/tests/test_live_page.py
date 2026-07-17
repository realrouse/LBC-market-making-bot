"""Unit tests for the real-money (live) status page: the per-trade section renderer,
the trade loader's grouping/filtering, and the overview-vs-live scope split in _render_html.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402

_LIVE_BOT = "mexc-accumulation-lbcusdt-955a99"


def _live_hb(mode="live"):
    return [{"account": "acct-a", "bot_name": _LIVE_BOT, "_display": "accum-lbc",
             "_label": "acct-8", "flag": "ALIVE", "age_s": 10, "bounds_ok": "ok",
             "version": "6b6b33d",
             "payload": {"mode": mode, "holdings_btc": 2949.27, "avg_entry": 0.00239,
                         "free_usdt": 93.4, "pnl_total": 0.53}}]


def _trades():
    return {("acct-a", _LIVE_BOT): [
        {"account": "acct-a", "bot_name": _LIVE_BOT, "ts_ms": 1784313056189, "side": "sell",
         "reason": "ladder+10.0%", "price": 0.002657, "qty": 884.78, "quote": 2.35,
         "fee": 0.0, "order_id": "C02__SELL1", "maker": 1},
        {"account": "acct-a", "bot_name": _LIVE_BOT, "ts_ms": 1784289808196, "side": "buy",
         "reason": "live-fill", "price": 0.002386, "qty": 886.95, "quote": 2.116,
         "fee": 0.0, "order_id": "C02__BUY1", "maker": 1},
    ]}


class TestRenderLiveTrades(unittest.TestCase):
    def test_full_detail_and_stats(self):
        html = g._render_live_trades(_live_hb(), _trades())
        self.assertIn("accum-lbc", html)
        self.assertIn("C02__SELL1", html)          # order IDs shown (full detail)
        self.assertIn("C02__BUY1", html)
        self.assertIn("ladder+10.0%", html)
        self.assertIn("LIVE", html)                 # mode badge
        self.assertIn("2949", html)                 # holdings

    def test_no_trades_yet(self):
        html = g._render_live_trades(_live_hb(), {})
        self.assertIn(g.t("lt_no_trades"), html)

    def test_no_live_bots(self):
        self.assertIn(g.t("lt_none"), g._render_live_trades([], {}))


class TestScopeSplit(unittest.TestCase):
    def _render(self, scope, hb, trades):
        return g._render_html(
            heartbeats=hb, accounts=[{"error": None, "services": []}],
            generated_at=datetime.datetime.now(datetime.timezone.utc),
            collection_s=0.1, inventory=[], deploys=[], user_to_label={},
            pnl_windows={}, scope=scope, trades_by_bot=trades,
            nav_href="tradinebottestatus.html")

    def test_live_scope_shows_trades(self):
        html = self._render("live", _live_hb(), _trades())
        self.assertIn("real money", html.lower())
        self.assertIn("C02__SELL1", html)
        self.assertIn("nav-link", html)

    def test_overview_scope_has_no_trade_section(self):
        # An overview page (sim bots) must not carry the live trade section.
        html = self._render("overview", _live_hb("sim"), {})
        self.assertNotIn("C02__SELL1", html)
        self.assertIn("simulation", html.lower())


class TestLoadTradesFilter(unittest.TestCase):
    def test_keys_filter(self):
        import sqlite3
        import tempfile
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tradinetools"))
        from tradinetools.db import open_db, store_trade
        d = tempfile.mkdtemp()
        db = open_db(os.path.join(d, "s.db"))
        store_trade(db, dict(account="acct-a", bot_name=_LIVE_BOT, ts_ms=1, side="buy",
                             price=1.0, qty=1.0))
        store_trade(db, dict(account="acct-b", bot_name="other-bot", ts_ms=2, side="buy",
                             price=1.0, qty=1.0))
        db.close()
        only_live = g._load_trades(os.path.join(d, "s.db"), {("acct-a", _LIVE_BOT)})
        self.assertIn(("acct-a", _LIVE_BOT), only_live)
        self.assertNotIn(("acct-b", "other-bot"), only_live)


if __name__ == "__main__":
    unittest.main()
