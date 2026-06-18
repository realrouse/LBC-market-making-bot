"""Unit tests for generate_status._render_expected_actual — the expected-vs-actual section."""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g


def _hb(account, bot, flag="ALIVE", mode=None):
    """A classified heartbeat row as _classify_heartbeats would produce."""
    age = {"ALIVE": 60, "STALE": 9000, "DEAD": 99999}[flag]
    return {
        "account": account, "bot_name": bot, "flag": flag,
        "age_s": age, "last_ts": int(time.time()) - age,
        "payload": {"mode": mode} if mode else {},
    }


class TestExpectedActual(unittest.TestCase):
    U2L = {"u1": "acct-1", "u2": "acct-2"}

    def test_empty_inventory_renders_nothing(self):
        self.assertEqual(g._render_expected_actual([], [], [], self.U2L), "")

    def test_missing_bot_flagged(self):
        inv = [{"account": "u2", "bot_name": "live_bot", "kind": "bot", "is_live": False}]
        html = g._render_expected_actual(inv, [], [], self.U2L)
        self.assertIn("MISSING", html)
        self.assertIn("1 expected but silent", html)
        self.assertIn("acct-2", html)        # mapped label, not the raw username
        self.assertNotIn("u2</td>", html)

    def test_service_without_heartbeat_is_na_not_missing(self):
        inv = [{"account": "u1", "bot_name": "status_collector", "kind": "service"}]
        html = g._render_expected_actual(inv, [], [], self.U2L)
        self.assertIn("n/a", html)
        self.assertNotIn("MISSING", html)
        self.assertIn("all expected bots present", html)

    def test_alive_bot_present(self):
        inv = [{"account": "u2", "bot_name": "grid_bot", "kind": "bot", "is_live": False}]
        hb = [_hb("u2", "grid_bot", "ALIVE", mode="sim")]
        html = g._render_expected_actual(inv, [], hb, self.U2L)
        self.assertIn("ALIVE", html)
        self.assertIn("all expected bots present", html)

    def test_mode_mismatch_flagged(self):
        inv = [{"account": "u2", "bot_name": "live_bot", "kind": "bot", "is_live": False}]
        hb = [_hb("u2", "live_bot", "ALIVE", mode="live")]   # declared sim, reports live
        html = g._render_expected_actual(inv, [], hb, self.U2L)
        self.assertIn("mode mismatch", html)
        self.assertIn("sim≠live", html)

    def test_latest_deploy_rendered(self):
        inv = [{"account": "u2", "bot_name": "grid_bot", "kind": "bot", "is_live": False}]
        hb = [_hb("u2", "grid_bot", "ALIVE", mode="sim")]
        dep = [{"account": "u2", "bot_name": "grid_bot", "last_ts": int(time.time()) - 120,
                "git_hash": "abc1234def", "result": "OK"}]
        html = g._render_expected_actual(inv, dep, hb, self.U2L)
        self.assertIn("abc1234", html)      # short hash
        self.assertIn("OK", html)


if __name__ == "__main__":
    unittest.main()
