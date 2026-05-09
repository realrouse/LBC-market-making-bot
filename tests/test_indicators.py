"""Tests for bot/indicators.py — pure-math functions, PriceSeries, and config types."""

import sys, os, math, unittest, json, tempfile, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from indicators import (
    compute_sma, compute_ema, compute_rsi, compute_volatility, PriceSeries,
    IndicatorSpec, StreamSpec, load_config,
)


class TestSMA(unittest.TestCase):

    def test_exact_window(self):
        self.assertAlmostEqual(compute_sma([1.0, 2.0, 3.0], 3), 2.0)

    def test_longer_series_uses_tail(self):
        self.assertAlmostEqual(compute_sma([10.0, 1.0, 2.0, 3.0], 3), 2.0)

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(compute_sma([1.0, 2.0], 3))

    def test_single_price(self):
        self.assertAlmostEqual(compute_sma([5.0], 1), 5.0)

    def test_constant_series(self):
        self.assertAlmostEqual(compute_sma([0.96] * 20, 20), 0.96)


class TestEMA(unittest.TestCase):

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(compute_ema([1.0, 2.0], 3))

    def test_constant_series_equals_value(self):
        result = compute_ema([0.5] * 20, 9)
        self.assertAlmostEqual(result, 0.5, places=6)

    def test_rising_series_ema_below_last(self):
        prices = list(range(1, 21))   # 1..20
        ema = compute_ema(prices, 9)
        # EMA lags — must be below last price (20) but above SMA seed
        self.assertIsNotNone(ema)
        self.assertGreater(20.0, ema)
        self.assertGreater(ema, 5.0)

    def test_seed_only_equals_sma(self):
        prices = [2.0, 4.0, 6.0]
        ema = compute_ema(prices, 3)
        sma = compute_sma(prices, 3)
        self.assertAlmostEqual(ema, sma)

    def test_k_factor(self):
        """Single extra tick above seed: result = seed*(1-k) + tick*k."""
        prices = [1.0, 1.0, 1.0, 2.0]   # n=3 seed=1.0, one extra tick=2.0
        k = 2.0 / (3 + 1)
        expected = 1.0 * (1 - k) + 2.0 * k
        self.assertAlmostEqual(compute_ema(prices, 3), expected, places=10)


class TestRSI(unittest.TestCase):

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(compute_rsi([0.5, 0.6], 14))

    def test_all_gains_returns_100(self):
        prices = [float(i) for i in range(16)]   # always rising
        self.assertAlmostEqual(compute_rsi(prices, 14), 100.0)

    def test_all_losses_returns_0(self):
        prices = [float(15 - i) for i in range(16)]  # always falling
        self.assertAlmostEqual(compute_rsi(prices, 14), 0.0)

    def test_neutral_alternating(self):
        # Equal gains and losses → RS=1 → RSI=50
        prices = []
        v = 0.5
        for i in range(20):
            v = v + 0.01 if i % 2 == 0 else v - 0.01
            prices.append(v)
        rsi = compute_rsi(prices, 14)
        self.assertIsNotNone(rsi)
        self.assertGreater(rsi, 0.0)
        self.assertLess(rsi, 100.0)

    def test_value_in_range(self):
        import random
        random.seed(42)
        prices = [0.5 + random.gauss(0, 0.01) for _ in range(30)]
        rsi = compute_rsi(prices, 14)
        self.assertIsNotNone(rsi)
        self.assertGreaterEqual(rsi, 0.0)
        self.assertLessEqual(rsi, 100.0)

    def test_exact_window_size(self):
        prices = [1.0] * 14 + [2.0]   # one gain, n-1 flat changes = no loss delta
        rsi = compute_rsi(prices, 14)
        self.assertAlmostEqual(rsi, 100.0)


class TestVolatility(unittest.TestCase):

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(compute_volatility([0.5] * 5, 20))

    def test_constant_series_returns_zero(self):
        prices = [1.0] * 22
        vol = compute_volatility(prices, 20)
        self.assertIsNotNone(vol)
        self.assertAlmostEqual(vol, 0.0, places=10)

    def test_positive_value_on_variable_series(self):
        import random
        random.seed(7)
        prices = [0.8 + random.gauss(0, 0.02) for _ in range(25)]
        vol = compute_volatility(prices, 20)
        self.assertIsNotNone(vol)
        self.assertGreater(vol, 0.0)

    def test_zero_price_handled(self):
        prices = [0.0] * 22
        # log(0) is undefined — should return None gracefully
        result = compute_volatility(prices, 20)
        self.assertIsNone(result)

    def test_uses_only_last_n_plus_one_prices(self):
        # Build a constant tail after noisy head — vol should be ~0
        head = [1.0 + 0.1 * i for i in range(50)]
        tail = [5.0] * 22
        prices = head + tail
        vol = compute_volatility(prices, 20)
        self.assertIsNotNone(vol)
        self.assertAlmostEqual(vol, 0.0, places=10)


class TestPriceSeries(unittest.TestCase):

    def test_len_tracks_pushes(self):
        s = PriceSeries(maxlen=10)
        self.assertEqual(len(s), 0)
        s.push(0.5)
        self.assertEqual(len(s), 1)

    def test_maxlen_respected(self):
        s = PriceSeries(maxlen=5)
        for i in range(10):
            s.push(float(i))
        self.assertEqual(len(s), 5)

    def test_indicators_returns_none_when_insufficient(self):
        s = PriceSeries()
        s.push(0.96)
        ind = s.indicators(rsi_n=14, sma_n=20, ema_n=9, vol_n=20)
        self.assertTrue(all(v is None for v in ind.values()))

    def test_indicators_keys_include_periods(self):
        s = PriceSeries()
        for i in range(30):
            s.push(0.5 + i * 0.001)
        ind = s.indicators(rsi_n=14, sma_n=20, ema_n=9, vol_n=20)
        self.assertIn("rsi_14", ind)
        self.assertIn("sma_20", ind)
        self.assertIn("ema_9", ind)
        self.assertIn("vol_20", ind)

    def test_indicators_all_ready_after_enough_data(self):
        s = PriceSeries()
        for i in range(30):
            s.push(0.5 + i * 0.001)
        ind = s.indicators(rsi_n=14, sma_n=20, ema_n=9, vol_n=20)
        self.assertTrue(all(v is not None for v in ind.values()))

    def test_custom_periods(self):
        s = PriceSeries()
        for i in range(10):
            s.push(float(i + 1))
        ind = s.indicators(rsi_n=5, sma_n=5, ema_n=3, vol_n=5)
        self.assertIn("rsi_5", ind)
        self.assertIn("sma_5", ind)
        self.assertIn("ema_3", ind)
        self.assertIn("vol_5", ind)
        self.assertIsNotNone(ind["sma_5"])
        self.assertIsNotNone(ind["ema_3"])

    def test_sma_value_correct(self):
        s = PriceSeries()
        for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
            s.push(v)
        ind = s.indicators(rsi_n=3, sma_n=3, ema_n=3, vol_n=3)
        self.assertAlmostEqual(ind["sma_3"], 4.0)   # mean of [3,4,5]


class TestIndicatorSpec(unittest.TestCase):

    def test_valid_rsi(self):
        spec = IndicatorSpec.from_dict({"type": "rsi", "period": 14})
        self.assertEqual(spec.type, "rsi")
        self.assertEqual(spec.period, 14)

    def test_valid_volatility(self):
        spec = IndicatorSpec.from_dict({"type": "volatility", "period": 20})
        self.assertEqual(spec.type, "volatility")

    def test_all_valid_types(self):
        for t in ("rsi", "sma", "ema", "volatility"):
            spec = IndicatorSpec.from_dict({"type": t, "period": 5})
            self.assertEqual(spec.type, t)

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            IndicatorSpec.from_dict({"type": "macd", "period": 14})

    def test_period_too_small_raises(self):
        with self.assertRaises(ValueError):
            IndicatorSpec.from_dict({"type": "rsi", "period": 1})

    def test_case_insensitive_type(self):
        spec = IndicatorSpec.from_dict({"type": "RSI", "period": 7})
        self.assertEqual(spec.type, "rsi")


class TestStreamSpec(unittest.TestCase):

    def _valid_stream(self, **overrides):
        base = {
            "id": "btc_4h", "asset": "BTCUSDT",
            "source": "binance_ws", "timeframe": "4h",
            "indicators": [{"type": "rsi", "period": 14}],
        }
        base.update(overrides)
        return base

    def test_valid_binance_stream(self):
        spec = StreamSpec.from_dict(self._valid_stream())
        self.assertEqual(spec.id, "btc_4h")
        self.assertEqual(spec.asset, "BTCUSDT")
        self.assertEqual(spec.source, "binance_ws")
        self.assertEqual(spec.timeframe, "4h")
        self.assertEqual(len(spec.indicators), 1)
        self.assertEqual(spec.indicators[0].type, "rsi")

    def test_valid_feed_stream(self):
        spec = StreamSpec.from_dict(self._valid_stream(source="feed", timeframe="tick"))
        self.assertEqual(spec.source, "feed")

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            StreamSpec.from_dict(self._valid_stream(source="kraken_ws"))

    def test_empty_indicators_raises(self):
        with self.assertRaises(ValueError):
            StreamSpec.from_dict(self._valid_stream(indicators=[]))

    def test_multiple_indicators(self):
        spec = StreamSpec.from_dict(self._valid_stream(indicators=[
            {"type": "rsi", "period": 14},
            {"type": "volatility", "period": 20},
        ]))
        self.assertEqual(len(spec.indicators), 2)

    def test_seed_periods_default(self):
        spec = StreamSpec.from_dict(self._valid_stream())
        self.assertEqual(spec.seed_periods, 50)

    def test_seed_periods_override(self):
        spec = StreamSpec.from_dict(self._valid_stream(seed_periods=100))
        self.assertEqual(spec.seed_periods, 100)


class TestLoadConfig(unittest.TestCase):

    def _write_config(self, cfg: dict) -> str:
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        json.dump(cfg, tmp)
        tmp.close()
        return tmp.name

    def _base_cfg(self, **overrides):
        base = {
            "zmq_feed_addr": "tcp://127.0.0.1:5557",
            "zmq_out_addr":  "tcp://127.0.0.1:5559",
            "min_ticks": 25,
            "streams": [{
                "id": "btc_4h", "asset": "BTCUSDT",
                "source": "binance_ws", "timeframe": "4h",
                "indicators": [{"type": "rsi", "period": 14}],
            }],
        }
        base.update(overrides)
        return base

    def test_loads_addresses(self):
        path = self._write_config(self._base_cfg())
        feed, out, _, _ = load_config(path)
        self.assertEqual(feed, "tcp://127.0.0.1:5557")
        self.assertEqual(out,  "tcp://127.0.0.1:5559")

    def test_loads_min_ticks(self):
        path = self._write_config(self._base_cfg(min_ticks=30))
        _, _, min_ticks, _ = load_config(path)
        self.assertEqual(min_ticks, 30)

    def test_loads_streams(self):
        path = self._write_config(self._base_cfg())
        _, _, _, streams = load_config(path)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].id, "btc_4h")
        self.assertEqual(streams[0].asset, "BTCUSDT")

    def test_default_config_file(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "strategies", "indicators.json"
        )
        if not os.path.exists(config_path):
            self.skipTest("strategies/indicators.json not found")
        _, _, _, streams = load_config(config_path)
        self.assertGreater(len(streams), 0)
        # Default: RSI and volatility on BTC
        btc = streams[0]
        self.assertEqual(btc.asset, "BTCUSDT")
        types = {s.type for s in btc.indicators}
        self.assertIn("rsi", types)
        self.assertIn("volatility", types)

    def test_empty_streams_raises(self):
        path = self._write_config(self._base_cfg(streams=[]))
        with self.assertRaises(ValueError):
            load_config(path)

    def test_comment_streams_skipped(self):
        cfg = self._base_cfg()
        cfg["streams"].insert(0, {"_comment": "ignored"})
        path = self._write_config(cfg)
        _, _, _, streams = load_config(path)
        self.assertEqual(len(streams), 1)


class TestPriceSeriesComputeIndicators(unittest.TestCase):

    def _series_with(self, n: int) -> PriceSeries:
        s = PriceSeries()
        for i in range(n):
            s.push(0.5 + i * 0.001)
        return s

    def test_returns_none_when_insufficient(self):
        s = self._series_with(3)
        specs = [IndicatorSpec(type="rsi", period=14)]
        ind = s.compute_indicators(specs)
        self.assertIsNone(ind["rsi_14"])

    def test_rsi_ready_after_enough_data(self):
        s = self._series_with(30)
        specs = [IndicatorSpec(type="rsi", period=14)]
        ind = s.compute_indicators(specs)
        self.assertIsNotNone(ind["rsi_14"])

    def test_mixed_specs(self):
        s = self._series_with(30)
        specs = [
            IndicatorSpec(type="rsi",        period=14),
            IndicatorSpec(type="volatility", period=20),
        ]
        ind = s.compute_indicators(specs)
        self.assertIn("rsi_14", ind)
        self.assertIn("vol_20", ind)

    def test_key_format_uses_period(self):
        s = self._series_with(20)
        specs = [IndicatorSpec(type="sma", period=5)]
        ind = s.compute_indicators(specs)
        self.assertIn("sma_5", ind)

    def test_consistent_with_legacy_method(self):
        s = self._series_with(30)
        legacy = s.indicators(rsi_n=14, sma_n=20, ema_n=9, vol_n=20)
        specs = [
            IndicatorSpec(type="rsi",        period=14),
            IndicatorSpec(type="sma",        period=20),
            IndicatorSpec(type="ema",        period=9),
            IndicatorSpec(type="volatility", period=20),
        ]
        new = s.compute_indicators(specs)
        for key in ("rsi_14", "sma_20", "ema_9", "vol_20"):
            if legacy[key] is None:
                self.assertIsNone(new[key])
            else:
                self.assertAlmostEqual(legacy[key], new[key], places=10)


class TestMultiBotIndicatorSharing(unittest.TestCase):
    """
    Verify that two account-bots with different indicator needs (4h vs 1d) can
    share one indicators.py PUB socket.

    ZeroMQ PUB/SUB is 1→N broadcast: every subscriber receives every message.
    Filtering to "my" stream is done in application code by checking stream_id.
    This test simulates that pattern without running real Binance WebSocket tasks.
    Uses plain sync ZMQ sockets with RCVTIMEO so no async complications arise.
    """

    def setUp(self) -> None:
        import zmq
        self._ctx = zmq.Context()

    def tearDown(self) -> None:
        self._ctx.term()

    def _pub_sub_pair(self, n_subs: int = 1):
        """Return (pub, port, [sub, ...]) with RCVTIMEO=500ms on each sub."""
        import zmq
        pub = self._ctx.socket(zmq.PUB)
        port = pub.bind_to_random_port("tcp://127.0.0.1")
        subs = []
        for _ in range(n_subs):
            s = self._ctx.socket(zmq.SUB)
            s.setsockopt(zmq.SUBSCRIBE, b"")
            s.setsockopt(zmq.RCVTIMEO, 500)
            s.connect(f"tcp://127.0.0.1:{port}")
            subs.append(s)
        time.sleep(0.05)   # let TCP sockets establish before first send
        return pub, port, subs

    def test_two_subscribers_each_receive_all_messages(self) -> None:
        pub, _, (sub_a, sub_b) = self._pub_sub_pair(2)

        msg_4h = {"t": "indicators", "stream_id": "btc_4h", "timeframe": "4h",
                  "asset": "BTCUSDT", "rsi_14": 52.3, "vol_20": 0.0021, "ts": 1}
        msg_1d = {"t": "indicators", "stream_id": "btc_1d", "timeframe": "1d",
                  "asset": "BTCUSDT", "rsi_14": 48.7, "vol_20": 0.0015, "ts": 2}

        for msg in (msg_4h, msg_1d):
            pub.send_json(msg)
            time.sleep(0.01)

        received_a = [sub_a.recv_json(), sub_a.recv_json()]
        received_b = [sub_b.recv_json(), sub_b.recv_json()]

        # Both subscribers got both messages
        self.assertEqual({m["stream_id"] for m in received_a}, {"btc_4h", "btc_1d"})
        self.assertEqual({m["stream_id"] for m in received_b}, {"btc_4h", "btc_1d"})

        pub.close(); sub_a.close(); sub_b.close()

    def test_account_a_filters_to_4h_only(self) -> None:
        """account-a ignores messages not for its stream_id."""
        pub, _, (sub,) = self._pub_sub_pair(1)

        messages = [
            {"t": "indicators", "stream_id": "btc_4h", "rsi_14": 52.3, "ts": 1},
            {"t": "indicators", "stream_id": "btc_1d", "rsi_14": 48.7, "ts": 2},
            {"t": "indicators", "stream_id": "btc_4h", "rsi_14": 55.1, "ts": 3},
        ]
        for msg in messages:
            pub.send_json(msg)
            time.sleep(0.01)

        received = [sub.recv_json(), sub.recv_json(), sub.recv_json()]
        # account-a only processes messages where stream_id == "btc_4h"
        account_a_relevant = [m for m in received if m["stream_id"] == "btc_4h"]
        self.assertEqual(len(account_a_relevant), 2)
        self.assertAlmostEqual(account_a_relevant[0]["rsi_14"], 52.3)
        self.assertAlmostEqual(account_a_relevant[1]["rsi_14"], 55.1)

        pub.close(); sub.close()

    def test_account_b_filters_to_1d_only(self) -> None:
        """account-b ignores messages not for its stream_id."""
        pub, _, (sub,) = self._pub_sub_pair(1)

        messages = [
            {"t": "indicators", "stream_id": "btc_4h", "rsi_14": 52.3, "ts": 1},
            {"t": "indicators", "stream_id": "btc_1d", "rsi_14": 48.7, "ts": 2},
            {"t": "indicators", "stream_id": "btc_4h", "rsi_14": 55.1, "ts": 3},
        ]
        for msg in messages:
            pub.send_json(msg)
            time.sleep(0.01)

        received = [sub.recv_json(), sub.recv_json(), sub.recv_json()]
        account_b_relevant = [m for m in received if m["stream_id"] == "btc_1d"]
        self.assertEqual(len(account_b_relevant), 1)
        self.assertAlmostEqual(account_b_relevant[0]["rsi_14"], 48.7)

        pub.close(); sub.close()

    def test_config_loads_two_streams(self) -> None:
        """The updated indicators.json declares both btc_4h and btc_1d streams."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "strategies", "indicators.json"
        )
        if not os.path.exists(config_path):
            self.skipTest("strategies/indicators.json not found")
        _, _, _, streams = load_config(config_path)
        ids = {s.id for s in streams}
        self.assertIn("btc_4h", ids)
        self.assertIn("btc_1d", ids)
        self.assertEqual(len(streams), 2)

    def test_stream_timeframes_distinct(self) -> None:
        """btc_4h and btc_1d carry different timeframe values."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "strategies", "indicators.json"
        )
        if not os.path.exists(config_path):
            self.skipTest("strategies/indicators.json not found")
        _, _, _, streams = load_config(config_path)
        by_id = {s.id: s for s in streams}
        self.assertEqual(by_id["btc_4h"].timeframe, "4h")
        self.assertEqual(by_id["btc_1d"].timeframe, "1d")


class TestSplitConfigs(unittest.TestCase):
    """
    Verify that indicators_4h.json and indicators_1d.json are valid stand-alone
    per-account configs with distinct ports and the expected single stream each.
    """

    def _path(self, filename: str) -> str:
        return os.path.join(os.path.dirname(__file__), "..", "strategies", filename)

    def _load(self, filename: str):
        path = self._path(filename)
        if not os.path.exists(path):
            self.skipTest(f"strategies/{filename} not found")
        return load_config(path)

    # ── indicators_4h.json ────────────────────────────────────────────────────

    def test_4h_config_loads(self) -> None:
        feed, out, min_ticks, streams = self._load("indicators_4h.json")
        self.assertEqual(out, "tcp://127.0.0.1:5559")
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].id, "btc_4h")
        self.assertEqual(streams[0].timeframe, "4h")
        self.assertEqual(streams[0].asset, "BTCUSDT")
        self.assertEqual(streams[0].source, "binance_ws")

    def test_4h_has_rsi_and_volatility(self) -> None:
        _, _, _, streams = self._load("indicators_4h.json")
        types = {s.type for s in streams[0].indicators}
        self.assertIn("rsi", types)
        self.assertIn("volatility", types)

    # ── indicators_1d.json ────────────────────────────────────────────────────

    def test_1d_config_loads(self) -> None:
        feed, out, min_ticks, streams = self._load("indicators_1d.json")
        self.assertEqual(out, "tcp://127.0.0.1:5560")
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].id, "btc_1d")
        self.assertEqual(streams[0].timeframe, "1d")
        self.assertEqual(streams[0].asset, "BTCUSDT")
        self.assertEqual(streams[0].source, "binance_ws")

    def test_1d_has_rsi_and_volatility(self) -> None:
        _, _, _, streams = self._load("indicators_1d.json")
        types = {s.type for s in streams[0].indicators}
        self.assertIn("rsi", types)
        self.assertIn("volatility", types)

    # ── cross-config isolation ────────────────────────────────────────────────

    def test_ports_are_distinct(self) -> None:
        """4h and 1d configs must bind on different ports — no conflict possible."""
        _, out_4h, _, _ = self._load("indicators_4h.json")
        _, out_1d, _, _ = self._load("indicators_1d.json")
        self.assertNotEqual(out_4h, out_1d)

    def test_stream_ids_are_distinct(self) -> None:
        _, _, _, streams_4h = self._load("indicators_4h.json")
        _, _, _, streams_1d = self._load("indicators_1d.json")
        ids_4h = {s.id for s in streams_4h}
        ids_1d = {s.id for s in streams_1d}
        self.assertFalse(ids_4h & ids_1d,
                         f"stream_id collision: {ids_4h & ids_1d}")


if __name__ == "__main__":
    unittest.main()
