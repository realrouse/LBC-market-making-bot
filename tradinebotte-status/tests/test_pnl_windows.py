"""Unit tests for the windowed-PnL feature (daily / weekly / monthly / alltime).

Two layers are covered:

  * ``_compute_pnl_windows`` — turns the heartbeat *history* in the shared state DB into
    per-bot window records. It is a normal module-level function (the page is DB-only, no
    SSH); the test exercises it against an in-memory heartbeat table, including the
    reset-to-zero discontinuity that a ZMQ reset produces.
  * ``_sum_windows`` / ``_win_spans`` / ``_render_bot_families`` — the pure rendering
    helpers that build the "by bot, not by account" family view and its window spans.
"""

import json
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402


def _extract_compute_pnl_windows():
    """Return the real module-level _compute_pnl_windows.

    It used to live embedded in the (now-removed) _REMOTE_COLLECT SSH string; the status
    page is DB-only now, so it is a normal importable function read straight from the
    shared state DB on the collector host.
    """
    return g._compute_pnl_windows


def _make_db(path, rows):
    """rows: list of (ts, account, bot_name, payload_dict)."""
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE heartbeats (ts INTEGER, account TEXT, bot_name TEXT,"
               " payload TEXT)")
    db.executemany("INSERT INTO heartbeats (ts, account, bot_name, payload) VALUES (?,?,?,?)",
                   [(ts, a, b, json.dumps(p)) for ts, a, b, p in rows])
    db.commit()
    db.close()


class TestComputePnlWindows(unittest.TestCase):

    def setUp(self):
        self.fn = _extract_compute_pnl_windows()
        self.path = "/tmp/_tw_test_%d.db" % os.getpid()
        if os.path.exists(self.path):
            os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_missing_db_returns_empty(self):
        self.assertEqual(self.fn("/nonexistent/path.db"), {})

    def test_window_diffs(self):
        now = int(time.time())
        day = 86400
        midnight = now - (now % day)
        mid_today = (midnight + now) // 2      # a point earlier TODAY (>= midnight, <= now)
        # A bot climbing 130 (8d ago) → 150 (5d ago) → 190 (earlier today) → 200 (now).
        rows = [
            (now - 8 * day, "c1", "live_bot", {"pnl_total": 130.0}),
            (now - 5 * day, "c1", "live_bot", {"pnl_total": 150.0}),
            (mid_today,     "c1", "live_bot", {"pnl_total": 190.0}),
            (now,           "c1", "live_bot", {"pnl_total": 200.0}),
        ]
        _make_db(self.path, rows)
        rec = self.fn(self.path)["c1|live_bot"]
        # daily = pnl_total delta since UTC midnight → first point today (190) → 200-190 = 10
        self.assertAlmostEqual(rec["daily"], 10.0)
        self.assertFalse(rec["daily_reset"])
        self.assertEqual(rec["alltime"], 200.0)             # pnl_total now
        # weekly baseline = first row >= now-7d → the 5d-ago row (150) → 200-150 = 50
        self.assertAlmostEqual(rec["weekly"], 50.0)
        self.assertFalse(rec["weekly_reset"])
        # monthly baseline predates data → earliest row (130) → 200-130 = 70
        self.assertAlmostEqual(rec["monthly"], 70.0)

    def test_reset_within_window_is_clamped_and_flagged(self):
        now = int(time.time())
        day = 86400
        # Climb to 60, a ZMQ reset zeroes it (→ 0.0), then climb to 40.
        rows = [
            (now - 6 * day, "c1", "grid_bot", {"pnl_total": 55.0}),
            (now - 4 * day, "c1", "grid_bot", {"pnl_total": 60.0}),
            (now - 3 * day, "c1", "grid_bot", {"pnl_total": 0.0}),   # reset
            (now - 1 * day, "c1", "grid_bot", {"pnl_total": 25.0}),
            (now,           "c1", "grid_bot", {"pnl_total": 40.0}),
        ]
        _make_db(self.path, rows)
        rec = self.fn(self.path)["c1|grid_bot"]
        # Baseline clamps to just-after the reset (0.0) → 40 - 0 = 40 (post-reset PnL),
        # matching alltime "since reset"; and the window is flagged.
        self.assertAlmostEqual(rec["weekly"], 40.0)
        self.assertTrue(rec["weekly_reset"])
        self.assertEqual(rec["alltime"], 40.0)

    def test_drawdown_is_not_mistaken_for_reset(self):
        now = int(time.time())
        day = 86400
        # A big drawdown 80 → 30 (not to zero) must NOT be treated as a reset.
        rows = [
            (now - 5 * day, "c1", "live_bot", {"pnl_total": 80.0}),
            (now - 2 * day, "c1", "live_bot", {"pnl_total": 30.0}),
            (now,           "c1", "live_bot", {"pnl_total": 70.0}),
        ]
        _make_db(self.path, rows)
        rec = self.fn(self.path)["c1|live_bot"]
        self.assertFalse(rec["weekly_reset"])
        self.assertAlmostEqual(rec["weekly"], 70.0 - 80.0)   # -10, honest diff

    def test_no_pnl_total_yields_none_windows(self):
        now = int(time.time())
        rows = [(now, "c1", "feed", {"ws_connected": True})]  # infra bot, no pnl_total
        _make_db(self.path, rows)
        rec = self.fn(self.path)["c1|feed"]
        self.assertIsNone(rec["alltime"])
        self.assertIsNone(rec["weekly"])


class TestWindowRenderHelpers(unittest.TestCase):

    def test_sum_windows_adds_and_ors_reset(self):
        recs = [
            {"daily": 1.0, "weekly": 10.0, "monthly": 100.0, "alltime": 100.0,
             "monthly_reset": True},
            {"daily": 2.0, "weekly": 20.0, "monthly": 200.0, "alltime": 200.0},
        ]
        agg = g._sum_windows(recs)
        self.assertEqual(agg["daily"], (3.0, False))
        self.assertEqual(agg["weekly"], (30.0, False))
        self.assertEqual(agg["monthly"], (300.0, True))     # reset OR-ed across instances

    def test_sum_windows_all_none_is_none(self):
        agg = g._sum_windows([{"weekly": None}, {}])
        self.assertEqual(agg["weekly"], (None, False))

    def test_win_spans_renders_four_windows_and_reset_mark(self):
        html = g._win_spans({"daily": 1.0, "weekly": 2.0, "monthly": 3.0,
                             "alltime": 3.0, "monthly_reset": True})
        for w in ("daily", "weekly", "monthly", "alltime"):
            self.assertIn(f"pw-{w}", html)
        self.assertIn("class='rst'", html)                  # reset marker on monthly

    def test_win_spans_none_shows_dash(self):
        html = g._win_spans({"daily": None})
        self.assertIn("no-data", html)


def _hb(bot, label, payload, flag="ALIVE"):
    return {"bot_name": bot, "account": label.replace("acct-", "c"), "_label": label,
            "flag": flag, "age_s": 30, "bounds_ok": "ok", "version": "abc1234",
            "payload": payload}


class TestRenderBotFamilies(unittest.TestCase):

    def test_groups_by_family_with_windowed_pnl(self):
        hb = [
            _hb("live_bot", "acct-2", {"capital": 1200}),
            _hb("live_bot", "acct-3", {"capital": 1000}),
            _hb("grid_bot", "acct-6", {}),
        ]
        pw = {
            "c2|live_bot": {"daily": 5.0, "weekly": 45.0, "monthly": 226.0,
                            "alltime": 226.0, "monthly_reset": True},
            "c3|live_bot": {"daily": -9.0, "weekly": 63.0, "monthly": 231.0,
                            "alltime": 231.0},
            "c6|grid_bot": {"daily": 0.0, "weekly": 8.0, "monthly": 327.0,
                            "alltime": 327.0},
        }
        html = g._render_bot_families(hb, pw, int(time.time()))
        # families present, accounts demoted to rows
        self.assertIn("live_bot", html)
        self.assertIn("grid_bot", html)
        self.assertIn("acct-2", html)
        self.assertIn("acct-3", html)
        # windowed spans + capital
        self.assertIn("pw-weekly", html)
        self.assertIn("cap $1,200", html)
        # aggregate weekly for live_bot = 45+63 = 108
        self.assertIn("$108.00", html)
        # reset marker surfaced from the flagged instance
        self.assertIn("class='rst'", html)

    def test_accumulation_is_detail_not_windowed(self):
        hb = [_hb("accumulation_bot", "acct-3",
                  {"holdings_btc": 0.0075, "free_usdt": 500, "total_realized": 0.0})]
        html = g._render_bot_families(hb, {}, int(time.time()))
        self.assertIn("accumulation_bot", html)
        self.assertIn("fam-detail", html)       # detail row, not windowed PnL
        self.assertNotIn("fam-pnl", html)       # no aggregate PnL header for accum
        self.assertIn("btc=0.0075", html)

    def test_empty_heartbeats(self):
        self.assertIn("No heartbeat data", g._render_bot_families([], {}, int(time.time())))


if __name__ == "__main__":
    unittest.main()
