"""Unit tests for tradinetools.schemas — versioned ZMQ message dataclasses."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tradinetools.schemas import (
    BookMessage, MarketMessage, PingMessage,
    IndicatorsMessage,
)


class TestVersionField(unittest.TestCase):
    """Every schema must carry v=1 by default and serialise it."""

    def _check_v(self, cls):
        obj = cls()
        self.assertEqual(obj.v, 1)
        d = obj.to_dict()
        self.assertIn("v", d)
        self.assertEqual(d["v"], 1)

    def test_book_message_v1(self):       self._check_v(BookMessage)
    def test_market_message_v1(self):     self._check_v(MarketMessage)
    def test_ping_message_v1(self):       self._check_v(PingMessage)
    def test_indicators_message_v1(self): self._check_v(IndicatorsMessage)


class TestBookMessage(unittest.TestCase):

    def test_default_type(self):
        self.assertEqual(BookMessage().t, "book")

    def test_to_dict_contains_all_fields(self):
        m = BookMessage(token_id="abc", best_bid=0.97, best_ask=0.98,
                        spread=0.01, bid_vol=1000.0, ask_vol=500.0, obi=0.33)
        d = m.to_dict()
        self.assertEqual(d["t"],         "book")
        self.assertEqual(d["token_id"],  "abc")
        self.assertAlmostEqual(d["best_bid"], 0.97)
        self.assertAlmostEqual(d["obi"],      0.33)

    def test_roundtrip(self):
        original = BookMessage(token_id="tok1", best_bid=0.96, best_ask=0.99,
                               spread=0.03, bid_vol=200.0, ask_vol=300.0, obi=-0.2)
        restored = BookMessage.from_dict(original.to_dict())
        self.assertEqual(restored.token_id,   original.token_id)
        self.assertAlmostEqual(restored.best_bid, original.best_bid)
        self.assertAlmostEqual(restored.obi,      original.obi)
        self.assertEqual(restored.v, 1)

    def test_from_dict_ignores_unknown_fields(self):
        d = {"t": "book", "v": 1, "token_id": "x",
             "best_bid": 0.5, "best_ask": 0.6, "spread": 0.1,
             "bid_vol": 10.0, "ask_vol": 5.0, "obi": 0.0,
             "unknown_field": "dropped"}
        m = BookMessage.from_dict(d)
        self.assertEqual(m.token_id, "x")
        self.assertFalse(hasattr(m, "unknown_field"))

    def test_defaults_are_safe(self):
        m = BookMessage()
        self.assertEqual(m.token_id,  "")
        self.assertAlmostEqual(m.best_bid, 0.0)
        self.assertAlmostEqual(m.obi,      0.0)


class TestMarketMessage(unittest.TestCase):

    def test_default_type(self):
        self.assertEqual(MarketMessage().t, "market")

    def test_roundtrip(self):
        original = MarketMessage(market_id="mkt1", question="BTC up?",
                                  up_token_id="u1", dn_token_id="d1",
                                  start_ms=1000, end_ms=2000)
        restored = MarketMessage.from_dict(original.to_dict())
        self.assertEqual(restored.market_id, "mkt1")
        self.assertEqual(restored.question,  "BTC up?")
        self.assertEqual(restored.end_ms,    2000)
        self.assertEqual(restored.v, 1)

    def test_default_ms_fields(self):
        m = MarketMessage()
        self.assertEqual(m.start_ms, 0)
        self.assertEqual(m.end_ms,   0)


class TestPingMessage(unittest.TestCase):

    def test_default_type(self):
        self.assertEqual(PingMessage().t, "ping")

    def test_roundtrip(self):
        original = PingMessage(ts=1_234_567_890)
        restored = PingMessage.from_dict(original.to_dict())
        self.assertEqual(restored.ts, 1_234_567_890)
        self.assertEqual(restored.v, 1)

    def test_default_ts(self):
        self.assertEqual(PingMessage().ts, 0)


class TestIndicatorsMessage(unittest.TestCase):

    def test_default_type(self):
        self.assertEqual(IndicatorsMessage().t, "indicators")

    def test_payload_flattened_in_to_dict(self):
        m = IndicatorsMessage(stream_id="btc_4h", ts=100,
                              payload={"rsi_14": 55.0, "vol_20": 0.002})
        d = m.to_dict()
        self.assertIn("rsi_14", d)
        self.assertAlmostEqual(d["rsi_14"], 55.0)
        self.assertAlmostEqual(d["vol_20"], 0.002)
        self.assertNotIn("payload", d)

    def test_v1_present_after_payload_flatten(self):
        d = IndicatorsMessage(stream_id="x", ts=0,
                              payload={"rsi_14": 50.0}).to_dict()
        self.assertEqual(d["v"], 1)

    def test_from_dict_reconstructs_payload(self):
        wire = {"t": "indicators", "v": 1, "stream_id": "btc_4h",
                "ts": 100, "rsi_14": 55.0, "vol_20": 0.002}
        m = IndicatorsMessage.from_dict(wire)
        self.assertEqual(m.stream_id, "btc_4h")
        self.assertAlmostEqual(m.payload["rsi_14"], 55.0)
        self.assertAlmostEqual(m.payload["vol_20"], 0.002)

    def test_reserved_keys_excluded_from_payload(self):
        wire = {"t": "indicators", "v": 1, "stream_id": "s", "ts": 0,
                "extra": 42, "another": "val"}
        m = IndicatorsMessage.from_dict(wire)
        self.assertNotIn("t",         m.payload)
        self.assertNotIn("v",         m.payload)
        self.assertNotIn("stream_id", m.payload)
        self.assertNotIn("ts",        m.payload)
        self.assertIn("extra",        m.payload)
        self.assertIn("another",      m.payload)

    def test_roundtrip_preserves_payload(self):
        m = IndicatorsMessage(stream_id="btc_1d", ts=999,
                              payload={"rsi_14": 48.7, "dvol": 62.5})
        restored = IndicatorsMessage.from_dict(m.to_dict())
        self.assertEqual(restored.stream_id, "btc_1d")
        self.assertAlmostEqual(restored.payload["rsi_14"], 48.7)
        self.assertAlmostEqual(restored.payload["dvol"],   62.5)

    def test_roundtrip_empty_payload(self):
        m = IndicatorsMessage(stream_id="btc_1d", ts=999)
        restored = IndicatorsMessage.from_dict(m.to_dict())
        self.assertEqual(restored.stream_id, "btc_1d")
        self.assertEqual(restored.payload,   {})


if __name__ == "__main__":
    unittest.main()
