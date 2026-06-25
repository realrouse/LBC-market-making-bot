"""Unit tests for degraded-collection handling (collector down / partial fleet).

The collector account (index 0) is the single source of all heartbeat/inventory/deploy
data. These tests lock in that its failure produces an explicit banner and an escalated
headline — never a silently-blank, falsely-green page — and that per-account failures are
surfaced without breaking the rest of the fleet view.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_status as g  # noqa: E402

_NOW = datetime(2026, 6, 24, tzinfo=timezone.utc)


def _ok_account(services=None):
    return {"version": "10fa979", "services": services or [], "live": None,
            "grids": [], "accum": None, "heartbeats": {"rows": []}}


def _alive_hb(bot="live_bot", label="acct-2"):
    return {"account": "u", "bot_name": bot, "_label": label, "flag": "ALIVE",
            "age_s": 30, "bounds_ok": "ok", "version": "10fa979",
            "payload": {"daily_pnl": 1.0, "pnl_total": 2.0}}


class TestRenderBanners(unittest.TestCase):

    def test_collector_down_emits_bad_banner(self):
        accounts = [{"error": "unreachable", "services": []}, _ok_account()]
        out = g._render_banners(accounts, heartbeats=[])
        self.assertIn("Collector account unreachable", out)
        self.assertIn("banner bad", out)

    def test_collector_up_but_no_heartbeats_emits_warn(self):
        out = g._render_banners([_ok_account(), _ok_account()], heartbeats=[])
        self.assertIn("no heartbeats", out.lower())
        self.assertIn("banner warn", out)

    def test_other_account_failure_listed_not_as_collector(self):
        # collector (idx 0) OK, acct at idx 1 failed
        accounts = [_ok_account(), {"error": "unreachable", "services": []}]
        out = g._render_banners(accounts, heartbeats=[_alive_hb()])
        self.assertNotIn("Collector account unreachable", out)
        self.assertIn("account(s) unreachable this cycle", out)

    def test_all_ok_no_banner(self):
        out = g._render_banners([_ok_account(), _ok_account()], heartbeats=[_alive_hb()])
        self.assertEqual(out, "")


class TestRenderHtmlDegraded(unittest.TestCase):

    def _render(self, accounts, heartbeats):
        return g._render_html(heartbeats=heartbeats, accounts=accounts,
                              generated_at=_NOW, collection_s=1.0,
                              inventory=[], deploys=[], user_to_label={})

    def test_collector_down_escalates_headline_and_avoids_green_zero(self):
        accounts = [{"error": "unreachable", "services": []}]
        html = self._render(accounts, heartbeats=[])
        self.assertIn("collector unreachable", html)        # status text
        self.assertIn("dot bad", html)                       # red headline dot
        self.assertNotIn(">0/0<", html)                      # no falsely-green 0/0
        self.assertIn("Collector account unreachable", html) # banner present

    def test_healthy_fleet_has_no_banner_and_nominal(self):
        accounts = [_ok_account(), _ok_account()]
        html = self._render(accounts, heartbeats=[_alive_hb(), _alive_hb(label="acct-3")])
        self.assertIn("All systems nominal", html)
        self.assertNotIn("banner bad", html)
        self.assertNotIn("banner warn", html)


if __name__ == "__main__":
    unittest.main()
