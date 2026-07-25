"""Unit tests for the real-money (live) status page: the per-trade section renderer,
the trade loader's grouping/filtering, and the overview-vs-live scope split in _render_html.
"""

import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402

_LIVE_BOT = "mexc-bamm-lbcusdt-d83114"


def _live_hb(mode="live", open_orders="__unset__", flag="ALIVE"):
    payload = {"mode": mode, "holdings_btc": 2949.27, "avg_entry": 0.00239,
               "free_usdt": 93.4, "pnl_total": 0.53}
    if open_orders != "__unset__":
        payload["open_orders"] = open_orders
    return [{"account": "acct-a", "bot_name": _LIVE_BOT, "_display": "accum-lbc",
             "_label": "acct-8", "flag": flag, "age_s": 10, "last_ts": 1784313000,
             "bounds_ok": "ok", "version": "6b6b33d", "payload": payload}]


def _open_orders():
    return [{"side": "buy", "price": 0.0009, "qty": 500.0, "notional": 0.45,
             "order_id": "OO__BUY9", "placed_ts": 1784310000},
            {"side": "sell", "price": 0.0015, "qty": 300.0, "notional": 0.45,
             "order_id": "OO__SELL5", "placed_ts": 1784311000}]


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


class TestOpenOrdersTable(unittest.TestCase):
    """The "orders in flight" table is separate from the executed-fill log, and its truth
    semantics (a decaying snapshot) drive the None-vs-[] handling + the stale treatment."""

    def test_open_orders_render_as_own_table(self):
        html = g._render_live_trades(_live_hb(open_orders=_open_orders()), {})
        self.assertIn(g.t("oo_title"), html)             # open-orders section caption
        self.assertIn(g.t("lt_executed_title"), html)    # executed section caption
        self.assertIn("OO__BUY9", html)                  # resting order ids shown
        self.assertIn("OO__SELL5", html)
        self.assertIn("0.000900", html)                  # resting price formatted

    def test_absent_key_is_unknown_not_zero(self):
        # Old bot / pre-deploy heartbeat: no open_orders key → "awaiting", NOT "no orders".
        html = g._render_live_trades(_live_hb(), {})
        self.assertIn(g.t("oo_unknown"), html)
        self.assertNotIn(g.t("oo_none"), html)

    def test_empty_list_is_zero_orders(self):
        html = g._render_live_trades(_live_hb(open_orders=[]), {})
        self.assertIn(g.t("oo_none"), html)
        self.assertNotIn(g.t("oo_unknown"), html)

    def test_stale_heartbeat_dims_open_orders(self):
        # A DEAD heartbeat's resting book may be out of date → dimmed + flag badge.
        html = g._render_live_trades(_live_hb(open_orders=_open_orders(), flag="DEAD"), {})
        self.assertIn("stale-tbl", html)
        # ALIVE heartbeat is NOT dimmed.
        self.assertNotIn("stale-tbl", g._render_live_trades(
            _live_hb(open_orders=_open_orders()), {}))


class TestLiveTablesToggle(unittest.TestCase):
    """The open-orders / executed-trades selector (same mechanism as the chart PnL/Assets toggle):
    one region, one table shown at a time via data-tab; open orders uncapped, executed capped."""

    def _many_trades(self, n):
        # _load_trades delivers newest-first (ORDER BY ts_ms DESC); mirror that: OID0 = newest.
        return {("acct-a", _LIVE_BOT): [
            {"account": "acct-a", "bot_name": _LIVE_BOT, "ts_ms": 1_700_000_000_000 - i,
             "side": "buy", "reason": "bamm", "price": 0.001 + i * 1e-6, "qty": 100.0,
             "quote": 0.1, "fee": 0.0, "order_id": f"OID{i}"} for i in range(n)]}

    def test_toggle_markup_present_default_open(self):
        html = g._render_live_trades(_live_hb(open_orders=_open_orders()), _trades())
        self.assertIn("ltTab(this,'open')", html)
        self.assertIn("ltTab(this,'exec')", html)
        self.assertIn("data-tab='open'", html)          # open orders shown by default
        self.assertIn("lt-pane-open", html)
        self.assertIn("lt-pane-exec", html)

    def test_executed_capped_at_20(self):
        html = g._render_live_trades(_live_hb(open_orders=[]), self._many_trades(30))
        self.assertIn("OID0", html)                     # newest 20 kept
        self.assertIn("OID19", html)
        self.assertNotIn("OID20", html)                 # 21st-newest dropped by the 20-cap
        self.assertIn(g.t("lt_capped", n=20, total=30), html)

    def test_open_orders_not_capped(self):
        many_oo = [{"side": "buy", "price": 0.001 + i * 1e-6, "qty": 100.0, "notional": 0.1,
                    "order_id": f"OO{i}", "placed_ts": 1_700_000_000 + i} for i in range(25)]
        html = g._render_live_trades(_live_hb(open_orders=many_oo), {})
        for i in range(25):
            self.assertIn(f"OO{i}", html)               # every resting order shown, no cap


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


class TestPnlSeries(unittest.TestCase):
    """Evolution-chart data: _downsample, _load_pnl_series (equity-or-capital, key filter),
    _fleet_series (forward-filled sum), and chart presence in the rendered page."""

    def _db_with_history(self):
        import json
        import tempfile
        import time
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tradinetools"))
        from tradinetools.db import open_db
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.db")
        db = open_db(p)
        base = int(time.time()) - 3600   # recent, inside the 30d history horizon
        # accum bot: equity present, no capital. grid bot: capital present, no equity.
        for i in range(10):
            db.execute("INSERT INTO heartbeats(ts,account,bot_name,version,status,bounds_ok,payload)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (base + i * 120, "acct-a", _LIVE_BOT, "v", "running", 1,
                        json.dumps({"pnl_total": i * 0.1, "equity": 100 + i})))
            db.execute("INSERT INTO heartbeats(ts,account,bot_name,version,status,bounds_ok,payload)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (base + i * 120, "acct-b", "grid-x", "v", "running", 1,
                        json.dumps({"pnl_total": i * 2.0, "capital": 1000 + i * 2})))
        db.commit()
        db.close()
        return p

    def test_downsample_caps_and_keeps_last(self):
        rows = [(t, t) for t in range(1000)]
        out = g._downsample(rows, 50)
        self.assertLessEqual(len(out), 51)
        self.assertEqual(out[-1], rows[-1])          # last point preserved

    def test_series_equity_or_capital(self):
        p = self._db_with_history()
        s = g._load_pnl_series(p)
        accum = s[f"acct-a|{_LIVE_BOT}"]
        grid = s["acct-b|grid-x"]
        self.assertEqual(accum["asset"][-1], 109.0)   # equity
        self.assertEqual(grid["asset"][-1], 1018.0)   # capital fallback
        self.assertAlmostEqual(accum["pnl"][-1], 0.9)

    def test_series_key_filter(self):
        p = self._db_with_history()
        only = g._load_pnl_series(p, {("acct-a", _LIVE_BOT)})
        self.assertIn(f"acct-a|{_LIVE_BOT}", only)
        self.assertNotIn("acct-b|grid-x", only)

    def test_fleet_sum(self):
        p = self._db_with_history()
        s = g._load_pnl_series(p)
        fl = g._fleet_series(s)
        # both bots' last equity summed (109 + 1018); pnl last (0.9 + 18.0)
        self.assertAlmostEqual(fl["asset"][-1], 1127.0, places=1)
        self.assertAlmostEqual(fl["pnl"][-1], 18.9, places=1)

    def test_empty_series_no_crash(self):
        self.assertEqual(g._fleet_series({}), {"ts": [], "pnl": [], "asset": []})

    def test_chart_present_in_rendered_page(self):
        html = g._render_html(
            heartbeats=_live_hb(), accounts=[{"error": None, "services": []}],
            generated_at=datetime.datetime.now(datetime.timezone.utc), collection_s=0.1,
            inventory=[], deploys=[], user_to_label={}, pnl_windows={}, scope="live",
            trades_by_bot=_trades(), nav_href="x.html",
            series={f"acct-a|{_LIVE_BOT}": {"ts": [1, 2], "pnl": [0.1, 0.2], "asset": [100, 101]},
                    "__fleet__": {"ts": [1, 2], "pnl": [0.1, 0.2], "asset": [100, 101]}})
        self.assertIn("tbchart", html)
        self.assertIn("window.TBSERIES=", html)
        self.assertIn("function tbRender", html)
        self.assertIn("__fleet__", html)


if __name__ == "__main__":
    unittest.main()
