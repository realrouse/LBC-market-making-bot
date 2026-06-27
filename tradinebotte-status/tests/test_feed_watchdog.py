"""Unit tests for feed_watchdog.decide — the pure restart-decision for a stuck feed.

Solution C: a feed can be 'active' (heartbeating) yet not publishing (get_markets masks
errors as [] → stuck in 'No markets', 0 books / 0 pings). The watchdog restarts it on its
own heartbeat signals (last_book_ts / ws_connected), failure-mode-agnostic.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import feed_watchdog as fw  # noqa: E402

_NOW = 1_000_000  # arbitrary epoch


def _hb(age_s, *, ws=True, book_age_s=5):
    """A heartbeat tuple (ts, payload) `age_s` seconds old, with ws_connected and a
    last_book_ts that is `book_age_s` seconds old."""
    return (_NOW - age_s, {"ws_connected": ws, "last_book_ts": _NOW - book_age_s})


class TestDecide(unittest.TestCase):

    def test_no_rows_is_no_data(self):
        self.assertEqual(fw.decide([], _NOW), "no-data")

    def test_healthy_feed_is_ok(self):
        rows = [_hb(10, ws=True, book_age_s=8), _hb(130, ws=True, book_age_s=8)]
        self.assertEqual(fw.decide(rows, _NOW), "ok")

    def test_stale_heartbeat_is_not_a_feed_trigger(self):
        # latest heartbeat itself too old → feed/collector down, not "alive but silent"
        rows = [_hb(400, ws=False, book_age_s=999)]
        self.assertEqual(fw.decide(rows, _NOW, hb_dead_s=300), "stale-heartbeat")

    def test_alive_but_book_stale_restarts(self):
        # heartbeating recently, but no book published in > STALE_BOOK_S → stuck
        rows = [_hb(10, ws=False, book_age_s=600), _hb(130, ws=False, book_age_s=520)]
        self.assertEqual(fw.decide(rows, _NOW, stale_book_s=300), "restart")

    def test_sustained_disconnect_restarts_before_book_threshold(self):
        # ws_connected False across the last 2 heartbeats, book not yet past threshold
        rows = [_hb(10, ws=False, book_age_s=120), _hb(130, ws=False, book_age_s=120)]
        self.assertEqual(fw.decide(rows, _NOW, stale_book_s=300), "restart")

    def test_single_disconnect_sample_waits(self):
        # only the latest is disconnected (a transient reconnect) → don't restart yet
        rows = [_hb(10, ws=False, book_age_s=120), _hb(130, ws=True, book_age_s=120)]
        self.assertEqual(fw.decide(rows, _NOW, stale_book_s=300), "ok")

    def test_missing_last_book_ts_treated_as_zero_restarts(self):
        # a feed stuck since boot never set last_book_ts → 0 → very stale → restart
        rows = [(_NOW - 10, {"ws_connected": False})]
        # only 1 sample, so sustained-disconnect path needs 2; but book_ts=0 is ancient
        self.assertEqual(fw.decide(rows, _NOW, stale_book_s=300), "restart")


if __name__ == "__main__":
    unittest.main()
