"""Tests for the indicators service's pure request/parsing/depth helpers.

test_indicators.py already covers the indicator math (SMA/EMA/RSI/volatility/PriceSeries);
this closes the gaps around the non-math logic: stream-id derivation, subscribe-request
validation, the order-book depth-diff application, and the port-shift address rewrite.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import indicators as ind  # noqa: E402


class TestDeriveStreamId(unittest.TestCase):

    def test_strips_quote_suffix_and_lowercases(self):
        self.assertEqual(ind.derive_stream_id("BTCUSDT", "4h"), "btc_4h")
        self.assertEqual(ind.derive_stream_id("ETHUSDC", "1d"), "eth_1d")
        self.assertEqual(ind.derive_stream_id("SOLBUSD", "1h"), "sol_1h")

    def test_non_quote_suffix_kept_whole(self):
        self.assertEqual(ind.derive_stream_id("BTCEUR", "1h"), "btceur_1h")

    def test_only_first_matching_suffix_stripped(self):
        # "usdt" stripped once, not recursively.
        self.assertEqual(ind.derive_stream_id("USDTUSDT", "5m"), "usdt_5m")


class TestParseSubscribeRequest(unittest.TestCase):

    _IND = [{"type": "sma", "period": 20}]   # a valid indicator (non-poll sources need ≥1)

    def test_valid_request_derives_stream_id(self):
        sid, spec = ind.parse_subscribe_request(
            {"asset": "btcusdt", "timeframe": "4h", "indicators": self._IND})
        self.assertEqual(sid, "btc_4h")
        self.assertEqual(spec.source, "binance_ws")        # default source
        self.assertEqual(spec.asset, "BTCUSDT")            # upper-cased

    def test_explicit_stream_id_overrides_derivation(self):
        sid, _ = ind.parse_subscribe_request(
            {"asset": "BTCUSDT", "timeframe": "4h", "stream_id": "custom_x",
             "indicators": self._IND})
        self.assertEqual(sid, "custom_x")

    def test_missing_asset_raises(self):
        with self.assertRaises(ValueError):
            ind.parse_subscribe_request({"timeframe": "4h"})

    def test_missing_timeframe_raises(self):
        with self.assertRaises(ValueError):
            ind.parse_subscribe_request({"asset": "BTCUSDT"})

    def test_poll_source_needs_no_asset_or_timeframe(self):
        # A source in _SOURCES_WITHOUT_INDICATORS (e.g. fear_greed) is exempt; stream_id
        # falls back to the source name.
        sid, spec = ind.parse_subscribe_request({"source": "fear_greed"})
        self.assertEqual(sid, "fear_greed")
        self.assertEqual(spec.source, "fear_greed")

    def test_optional_fields_passthrough(self):
        _, spec = ind.parse_subscribe_request(
            {"asset": "BTCUSDT", "timeframe": "1h", "indicators": self._IND,
             "seed_periods": "50", "params": {"depth": 5}})
        self.assertEqual(spec.seed_periods, 50)            # coerced to int


class TestApplyDepthEvent(unittest.TestCase):

    def test_insert_and_update_levels(self):
        bids, asks = {}, {}
        ind._apply_depth_event(bids, asks,
                               {"b": [["100.0", "2.0"]], "a": [["101.0", "3.0"]]})
        self.assertEqual(bids, {100.0: 2.0})
        self.assertEqual(asks, {101.0: 3.0})
        # update the same price level
        ind._apply_depth_event(bids, asks, {"b": [["100.0", "5.0"]], "a": []})
        self.assertEqual(bids[100.0], 5.0)

    def test_zero_quantity_deletes_level(self):
        bids, asks = {100.0: 2.0}, {101.0: 3.0}
        ind._apply_depth_event(bids, asks,
                               {"b": [["100.0", "0"]], "a": [["101.0", "0.0"]]})
        self.assertEqual(bids, {})
        self.assertEqual(asks, {})

    def test_delete_missing_level_is_noop(self):
        bids = {}
        ind._apply_depth_event(bids, {}, {"b": [["999.0", "0"]]})
        self.assertEqual(bids, {})

    def test_empty_event_leaves_book_unchanged(self):
        bids, asks = {100.0: 1.0}, {101.0: 1.0}
        ind._apply_depth_event(bids, asks, {})
        self.assertEqual((bids, asks), ({100.0: 1.0}, {101.0: 1.0}))


class TestShiftAddr(unittest.TestCase):

    def setUp(self):
        self._saved = ind._PORT_SHIFT

    def tearDown(self):
        ind._PORT_SHIFT = self._saved

    def test_no_shift_is_identity(self):
        ind._PORT_SHIFT = 0
        self.assertEqual(ind._shift_addr("tcp://127.0.0.1:5557"), "tcp://127.0.0.1:5557")

    def test_non_tcp_passthrough_even_with_shift(self):
        ind._PORT_SHIFT = 3
        self.assertEqual(ind._shift_addr("ipc:///run/x.sock"), "ipc:///run/x.sock")

    def test_shifts_port(self):
        ind._PORT_SHIFT = 3
        self.assertEqual(ind._shift_addr("tcp://127.0.0.1:5557"), "tcp://127.0.0.1:5560")


if __name__ == "__main__":
    unittest.main()
