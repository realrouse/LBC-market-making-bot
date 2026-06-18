"""Unit tests for tradinetools.build_heartbeat — the self-reported `mode` field."""

import importlib.util
import os
import unittest

# `tradinetools` is a namespace package at the repo level; build_heartbeat lives in the
# inner package __init__ (only stdlib imports at module scope), so load it by file path
# to sidestep the namespace-vs-package import ambiguity in the test runner.
_INIT = os.path.join(os.path.dirname(__file__), "..", "tradinetools", "__init__.py")
_spec = importlib.util.spec_from_file_location("_tbnt_heartbeat", _INIT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
build_heartbeat = _mod.build_heartbeat


class TestHeartbeatMode(unittest.TestCase):
    def test_mode_omitted_when_none(self):
        """Infra services pass mode=None → the field must be absent, not 'live'."""
        p = build_heartbeat("feed", None, {})
        self.assertNotIn("mode", p)

    def test_mode_sim(self):
        p = build_heartbeat("grid_bot", None, {}, mode="sim")
        self.assertEqual(p["mode"], "sim")

    def test_mode_live(self):
        p = build_heartbeat("live_bot", None, {}, mode="live")
        self.assertEqual(p["mode"], "live")

    def test_base_fields_present(self):
        p = build_heartbeat("live_bot", None, {"bounds_ok": True}, mode="sim")
        for k in ("ts", "bot_name", "account", "version", "status", "mode", "bounds_ok"):
            self.assertIn(k, p)
        self.assertEqual(p["bot_name"], "live_bot")

    def test_extra_cannot_be_shadowed_by_mode(self):
        """mode is set before extra merges, so extra still wins on a collision."""
        p = build_heartbeat("x", None, {"mode": "live"}, mode="sim")
        self.assertEqual(p["mode"], "live")


if __name__ == "__main__":
    unittest.main()
