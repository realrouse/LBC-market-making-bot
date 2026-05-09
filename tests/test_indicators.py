"""Tests for bot/indicators.py — pure-math functions, PriceSeries, and config types."""

import sys, os, math, unittest, json, tempfile, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from indicators import (
    compute_sma, compute_ema, compute_rsi, compute_volatility, PriceSeries,
    IndicatorSpec, StreamSpec, IndicatorsConfig, load_config,
    derive_stream_id, parse_subscribe_request, _handle_subscribe,
    _SOURCES_WITHOUT_INDICATORS, _DEFAULT_POLL_INTERVALS, _VALID_SOURCES,
    _shift_addr, _PORT_SHIFT,
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
        cfg = load_config(path)
        self.assertEqual(cfg.feed_addr, "tcp://127.0.0.1:5557")
        self.assertEqual(cfg.out_addr,  "tcp://127.0.0.1:5559")

    def test_loads_reg_addr_default(self):
        path = self._write_config(self._base_cfg())
        cfg = load_config(path)
        self.assertEqual(cfg.reg_addr, "tcp://127.0.0.1:5561")

    def test_loads_reg_addr_override(self):
        path = self._write_config(self._base_cfg(zmq_reg_addr="tcp://127.0.0.1:5599"))
        cfg = load_config(path)
        self.assertEqual(cfg.reg_addr, "tcp://127.0.0.1:5599")

    def test_loads_min_ticks(self):
        path = self._write_config(self._base_cfg(min_ticks=30))
        cfg = load_config(path)
        self.assertEqual(cfg.min_ticks, 30)

    def test_loads_streams(self):
        path = self._write_config(self._base_cfg())
        cfg = load_config(path)
        self.assertEqual(len(cfg.streams), 1)
        self.assertEqual(cfg.streams[0].id, "btc_4h")
        self.assertEqual(cfg.streams[0].asset, "BTCUSDT")

    def test_returns_indicators_config_namedtuple(self):
        path = self._write_config(self._base_cfg())
        cfg = load_config(path)
        self.assertIsInstance(cfg, IndicatorsConfig)

    def test_default_config_file(self):
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "strategies", "indicators.json"
        )
        if not os.path.exists(config_path):
            self.skipTest("strategies/indicators.json not found")
        cfg = load_config(config_path)
        self.assertGreater(len(cfg.streams), 0)
        btc = cfg.streams[0]
        self.assertEqual(btc.asset, "BTCUSDT")
        types = {s.type for s in btc.indicators}
        self.assertIn("rsi", types)
        self.assertIn("volatility", types)

    def test_empty_streams_raises(self):
        path = self._write_config(self._base_cfg(streams=[]))
        with self.assertRaises(ValueError):
            load_config(path)

    def test_comment_streams_skipped(self):
        cfg_dict = self._base_cfg()
        cfg_dict["streams"].insert(0, {"_comment": "ignored"})
        path = self._write_config(cfg_dict)
        cfg = load_config(path)
        self.assertEqual(len(cfg.streams), 1)


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
        cfg = load_config(config_path)
        ids = {s.id for s in cfg.streams}
        self.assertIn("btc_4h", ids)
        self.assertIn("btc_1d", ids)
        self.assertEqual(len(cfg.streams), 2)

    def test_stream_timeframes_distinct(self) -> None:
        """btc_4h and btc_1d carry different timeframe values."""
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "strategies", "indicators.json"
        )
        if not os.path.exists(config_path):
            self.skipTest("strategies/indicators.json not found")
        cfg = load_config(config_path)
        by_id = {s.id: s for s in cfg.streams}
        self.assertEqual(by_id["btc_4h"].timeframe, "4h")
        self.assertEqual(by_id["btc_1d"].timeframe, "1d")


class TestSplitConfigs(unittest.TestCase):
    """
    Verify that indicators_4h_bitcoin.json and indicators_1d_bitcoin.json are valid stand-alone
    per-account configs with distinct ports and the expected single stream each.
    """

    def _path(self, filename: str) -> str:
        return os.path.join(os.path.dirname(__file__), "..", "strategies", filename)

    def _load(self, filename: str) -> IndicatorsConfig:
        path = self._path(filename)
        if not os.path.exists(path):
            self.skipTest(f"strategies/{filename} not found")
        return load_config(path)

    # ── indicators_4h_bitcoin.json ────────────────────────────────────────────────────

    def test_4h_config_loads(self) -> None:
        cfg = self._load("indicators_4h_bitcoin.json")
        self.assertEqual(cfg.out_addr, "tcp://127.0.0.1:5559")
        self.assertEqual(cfg.reg_addr, "tcp://127.0.0.1:5561")
        self.assertEqual(len(cfg.streams), 1)
        self.assertEqual(cfg.streams[0].id, "btc_4h")
        self.assertEqual(cfg.streams[0].timeframe, "4h")
        self.assertEqual(cfg.streams[0].asset, "BTCUSDT")
        self.assertEqual(cfg.streams[0].source, "binance_ws")

    def test_4h_has_rsi_and_volatility(self) -> None:
        cfg = self._load("indicators_4h_bitcoin.json")
        types = {s.type for s in cfg.streams[0].indicators}
        self.assertIn("rsi", types)
        self.assertIn("volatility", types)

    # ── indicators_1d_bitcoin.json ────────────────────────────────────────────────────

    def test_1d_config_loads(self) -> None:
        cfg = self._load("indicators_1d_bitcoin.json")
        self.assertEqual(cfg.out_addr, "tcp://127.0.0.1:5560")
        self.assertEqual(cfg.reg_addr, "tcp://127.0.0.1:5562")
        self.assertEqual(len(cfg.streams), 1)
        self.assertEqual(cfg.streams[0].id, "btc_1d")
        self.assertEqual(cfg.streams[0].timeframe, "1d")
        self.assertEqual(cfg.streams[0].asset, "BTCUSDT")
        self.assertEqual(cfg.streams[0].source, "binance_ws")

    def test_1d_has_rsi_and_volatility(self) -> None:
        cfg = self._load("indicators_1d_bitcoin.json")
        types = {s.type for s in cfg.streams[0].indicators}
        self.assertIn("rsi", types)
        self.assertIn("volatility", types)

    # ── cross-config isolation ────────────────────────────────────────────────

    def test_pub_ports_are_distinct(self) -> None:
        """4h and 1d configs must bind on different PUB ports."""
        cfg_4h = self._load("indicators_4h_bitcoin.json")
        cfg_1d = self._load("indicators_1d_bitcoin.json")
        self.assertNotEqual(cfg_4h.out_addr, cfg_1d.out_addr)

    def test_reg_ports_are_distinct(self) -> None:
        """4h and 1d configs must bind on different REP registration ports."""
        cfg_4h = self._load("indicators_4h_bitcoin.json")
        cfg_1d = self._load("indicators_1d_bitcoin.json")
        self.assertNotEqual(cfg_4h.reg_addr, cfg_1d.reg_addr)

    def test_stream_ids_are_distinct(self) -> None:
        cfg_4h = self._load("indicators_4h_bitcoin.json")
        cfg_1d = self._load("indicators_1d_bitcoin.json")
        ids_4h = {s.id for s in cfg_4h.streams}
        ids_1d = {s.id for s in cfg_1d.streams}
        self.assertFalse(ids_4h & ids_1d,
                         f"stream_id collision: {ids_4h & ids_1d}")


class TestDeriveStreamId(unittest.TestCase):

    def test_strips_usdt(self):
        self.assertEqual(derive_stream_id("BTCUSDT", "4h"), "btc_4h")

    def test_strips_usdc(self):
        self.assertEqual(derive_stream_id("ETHUSDC", "1d"), "eth_1d")

    def test_strips_busd(self):
        self.assertEqual(derive_stream_id("BNBBUSD", "1h"), "bnb_1h")

    def test_no_known_suffix(self):
        self.assertEqual(derive_stream_id("BTCEUR", "1h"), "btceur_1h")

    def test_lowercase_input(self):
        self.assertEqual(derive_stream_id("btcusdt", "4h"), "btc_4h")


class TestParseSubscribeRequest(unittest.TestCase):

    def _req(self, **overrides):
        base = {
            "asset":      "BTCUSDT",
            "timeframe":  "4h",
            "source":     "binance_ws",
            "indicators": [{"type": "rsi", "period": 14}],
        }
        base.update(overrides)
        return base

    def test_minimal_request(self):
        stream_id, spec = parse_subscribe_request(self._req())
        self.assertEqual(stream_id, "btc_4h")
        self.assertEqual(spec.asset, "BTCUSDT")
        self.assertEqual(spec.timeframe, "4h")
        self.assertEqual(spec.source, "binance_ws")
        self.assertEqual(spec.indicators[0].type, "rsi")

    def test_explicit_stream_id(self):
        stream_id, spec = parse_subscribe_request(
            self._req(stream_id="my_custom_stream")
        )
        self.assertEqual(stream_id, "my_custom_stream")
        self.assertEqual(spec.id, "my_custom_stream")

    def test_stream_id_derived_when_absent(self):
        stream_id, _ = parse_subscribe_request(self._req(asset="ETHUSDT", timeframe="1d"))
        self.assertEqual(stream_id, "eth_1d")

    def test_seed_periods_forwarded(self):
        _, spec = parse_subscribe_request(self._req(seed_periods=100))
        self.assertEqual(spec.seed_periods, 100)

    def test_missing_asset_raises(self):
        with self.assertRaises(ValueError):
            parse_subscribe_request(self._req(asset=""))

    def test_missing_timeframe_raises(self):
        with self.assertRaises(ValueError):
            parse_subscribe_request(self._req(timeframe=""))

    def test_invalid_indicator_type_raises(self):
        with self.assertRaises(ValueError):
            parse_subscribe_request(self._req(indicators=[{"type": "macd", "period": 14}]))

    def test_invalid_source_raises(self):
        with self.assertRaises(ValueError):
            parse_subscribe_request(self._req(source="kraken_ws"))

    def test_empty_indicators_raises(self):
        with self.assertRaises(ValueError):
            parse_subscribe_request(self._req(indicators=[]))

    def test_asset_uppercased(self):
        _, spec = parse_subscribe_request(self._req(asset="btcusdt"))
        self.assertEqual(spec.asset, "BTCUSDT")


class TestHandleSubscribe(unittest.IsolatedAsyncioTestCase):
    """Integration tests for _handle_subscribe using real async ZMQ sockets."""

    async def test_subscribe_returns_ok(self):
        import zmq
        import zmq.asyncio as azmq
        from unittest.mock import AsyncMock, patch

        ctx = azmq.Context()
        pub = ctx.socket(zmq.PUB)
        pub.bind_to_random_port("tcp://127.0.0.1")
        active = {}

        with patch("indicators._start_binance_stream", new=AsyncMock()):
            resp = await _handle_subscribe(
                {"asset": "BTCUSDT", "timeframe": "4h", "source": "binance_ws",
                 "indicators": [{"type": "rsi", "period": 14}]},
                pub, 25, active,
            )

        self.assertEqual(resp["status"], "ok")
        self.assertEqual(resp["stream_id"], "btc_4h")
        pub.close(); ctx.term()

    async def test_invalid_request_returns_error(self):
        import zmq
        import zmq.asyncio as azmq

        ctx = azmq.Context()
        pub = ctx.socket(zmq.PUB)
        pub.bind_to_random_port("tcp://127.0.0.1")

        resp = await _handle_subscribe(
            {"asset": "", "timeframe": "4h",
             "indicators": [{"type": "rsi", "period": 14}]},
            pub, 25, {},
        )

        self.assertEqual(resp["status"], "error")
        self.assertIn("asset", resp["message"])
        pub.close(); ctx.term()

    async def test_duplicate_subscribe_returns_ok(self):
        import zmq
        import zmq.asyncio as azmq
        from unittest.mock import AsyncMock, patch

        ctx = azmq.Context()
        pub = ctx.socket(zmq.PUB)
        pub.bind_to_random_port("tcp://127.0.0.1")

        req = {"asset": "BTCUSDT", "timeframe": "4h", "source": "binance_ws",
               "indicators": [{"type": "rsi", "period": 14}]}
        active = {}

        with patch("indicators._start_binance_stream", new=AsyncMock()):
            resp1 = await _handle_subscribe(req, pub, 25, active)
            resp2 = await _handle_subscribe(req, pub, 25, active)

        self.assertEqual(resp1["status"], "ok")
        self.assertEqual(resp2["status"], "ok")
        self.assertEqual(resp1["stream_id"], resp2["stream_id"])
        pub.close(); ctx.term()

    async def test_feed_source_not_in_active_returns_error(self):
        import zmq
        import zmq.asyncio as azmq

        ctx = azmq.Context()
        pub = ctx.socket(zmq.PUB)
        pub.bind_to_random_port("tcp://127.0.0.1")

        resp = await _handle_subscribe(
            {"asset": "BTCUSDT", "timeframe": "tick", "source": "feed",
             "indicators": [{"type": "rsi", "period": 14}]},
            pub, 25, {},   # empty active — feed stream not registered
        )

        self.assertEqual(resp["status"], "error")
        pub.close(); ctx.term()


class TestNewSources(unittest.TestCase):
    """Tests for the three poll-based sources: binance_funding, deribit_iv, fear_greed."""

    # ── module-level constants ────────────────────────────────────────────────

    def test_sources_without_indicators_contains_all_three(self):
        self.assertIn("binance_funding", _SOURCES_WITHOUT_INDICATORS)
        self.assertIn("deribit_iv",      _SOURCES_WITHOUT_INDICATORS)
        self.assertIn("fear_greed",      _SOURCES_WITHOUT_INDICATORS)

    def test_default_poll_intervals(self):
        self.assertEqual(_DEFAULT_POLL_INTERVALS["binance_funding"], 900)
        self.assertEqual(_DEFAULT_POLL_INTERVALS["deribit_iv"],      300)
        self.assertEqual(_DEFAULT_POLL_INTERVALS["fear_greed"],      3600)

    # ── StreamSpec.from_dict ──────────────────────────────────────────────────

    def test_poll_source_allows_empty_indicators(self):
        for source in ("binance_funding", "deribit_iv", "fear_greed"):
            spec = StreamSpec.from_dict({
                "id": source, "asset": "", "source": source,
                "timeframe": "n/a", "indicators": [],
            })
            self.assertEqual(spec.source, source)
            self.assertEqual(spec.indicators, [])

    def test_poll_source_poll_interval_default_zero(self):
        spec = StreamSpec.from_dict({
            "id": "btc_funding", "asset": "BTCUSDT",
            "source": "binance_funding", "timeframe": "n/a", "indicators": [],
        })
        self.assertEqual(spec.poll_interval_s, 0)

    def test_poll_source_poll_interval_override(self):
        spec = StreamSpec.from_dict({
            "id": "btc_funding", "asset": "BTCUSDT",
            "source": "binance_funding", "timeframe": "n/a", "indicators": [],
            "poll_interval_s": 1800,
        })
        self.assertEqual(spec.poll_interval_s, 1800)

    def test_binance_ws_still_requires_indicators(self):
        with self.assertRaises(ValueError):
            StreamSpec.from_dict({
                "id": "btc_4h", "asset": "BTCUSDT",
                "source": "binance_ws", "timeframe": "4h", "indicators": [],
            })

    # ── parse_subscribe_request for poll sources ──────────────────────────────

    def test_fear_greed_subscribe_no_asset_required(self):
        stream_id, spec = parse_subscribe_request({
            "source": "fear_greed",
        })
        self.assertEqual(stream_id, "fear_greed")
        self.assertEqual(spec.source, "fear_greed")
        self.assertEqual(spec.indicators, [])

    def test_binance_funding_subscribe_with_asset(self):
        stream_id, spec = parse_subscribe_request({
            "source": "binance_funding",
            "asset":  "BTCUSDT",
        })
        self.assertEqual(stream_id, "binance_funding")
        self.assertEqual(spec.asset, "BTCUSDT")

    def test_poll_subscribe_poll_interval_forwarded(self):
        _, spec = parse_subscribe_request({
            "source":          "deribit_iv",
            "poll_interval_s": 600,
        })
        self.assertEqual(spec.poll_interval_s, 600)

    # ── JSON config files ─────────────────────────────────────────────────────

    def _load_config(self, filename: str) -> IndicatorsConfig:
        path = os.path.join(os.path.dirname(__file__), "..", "strategies", filename)
        if not os.path.exists(path):
            self.skipTest(f"strategies/{filename} not found")
        return load_config(path)

    def test_funding_config_loads(self):
        cfg = self._load_config("indicators_funding_bitcoin.json")
        self.assertEqual(len(cfg.streams), 1)
        s = cfg.streams[0]
        self.assertEqual(s.id,     "btc_funding")
        self.assertEqual(s.source, "binance_funding")
        self.assertEqual(s.asset,  "BTCUSDT")
        self.assertEqual(s.poll_interval_s, 900)

    def test_deribit_iv_config_loads(self):
        cfg = self._load_config("indicators_deribit_iv_bitcoin.json")
        self.assertEqual(len(cfg.streams), 1)
        s = cfg.streams[0]
        self.assertEqual(s.id,     "btc_dvol")
        self.assertEqual(s.source, "deribit_iv")
        self.assertEqual(s.poll_interval_s, 300)

    def test_fear_greed_config_loads(self):
        cfg = self._load_config("indicators_fear_greed.json")
        self.assertEqual(len(cfg.streams), 1)
        s = cfg.streams[0]
        self.assertEqual(s.id,     "fear_greed")
        self.assertEqual(s.source, "fear_greed")
        self.assertEqual(s.poll_interval_s, 3600)

    def test_all_three_configs_have_distinct_pub_ports(self):
        cfg_f  = self._load_config("indicators_funding_bitcoin.json")
        cfg_d  = self._load_config("indicators_deribit_iv_bitcoin.json")
        cfg_fg = self._load_config("indicators_fear_greed.json")
        ports = {cfg_f.out_addr, cfg_d.out_addr, cfg_fg.out_addr}
        self.assertEqual(len(ports), 3, f"PUB port collision: {ports}")


class TestPortBase(unittest.TestCase):
    """Tests for _shift_addr() and port-base configuration."""

    def test_shift_addr_zero_is_noop(self):
        addr = "tcp://127.0.0.1:5559"
        # When _PORT_SHIFT is 0 (default PORT_BASE=5557), _shift_addr is a no-op.
        if _PORT_SHIFT == 0:
            self.assertEqual(_shift_addr(addr), addr)

    def test_shift_addr_positive(self):
        # Simulate PORT_BASE=6557 → shift=+1000
        from indicators import _shift_addr as sa
        import indicators as ind_mod
        orig = ind_mod._PORT_SHIFT  # pylint: disable=protected-access
        ind_mod._PORT_SHIFT = 1000
        try:
            self.assertEqual(sa("tcp://127.0.0.1:5559"), "tcp://127.0.0.1:6559")
            self.assertEqual(sa("tcp://127.0.0.1:5561"), "tcp://127.0.0.1:6561")
        finally:
            ind_mod._PORT_SHIFT = orig

    def test_shift_addr_non_tcp_unchanged(self):
        import indicators as ind_mod
        orig = ind_mod._PORT_SHIFT
        ind_mod._PORT_SHIFT = 1000
        try:
            self.assertEqual(_shift_addr("ipc:///tmp/feed.ipc"), "ipc:///tmp/feed.ipc")
        finally:
            ind_mod._PORT_SHIFT = orig

    def test_shift_addr_preserves_host(self):
        import indicators as ind_mod
        orig = ind_mod._PORT_SHIFT
        ind_mod._PORT_SHIFT = 500
        try:
            result = _shift_addr("tcp://0.0.0.0:5559")
            self.assertTrue(result.startswith("tcp://0.0.0.0:"))
            self.assertEqual(result, "tcp://0.0.0.0:6059")
        finally:
            ind_mod._PORT_SHIFT = orig

    def test_load_config_shifts_json_addresses(self):
        """When PORT_SHIFT is non-zero, JSON-declared addresses are shifted."""
        import indicators as ind_mod
        orig_shift = ind_mod._PORT_SHIFT
        orig_ind   = ind_mod._IND_ADDR
        orig_reg   = ind_mod._REG_ADDR
        orig_feed  = ind_mod._FEED_ADDR
        ind_mod._PORT_SHIFT = 1000
        ind_mod._IND_ADDR   = "tcp://127.0.0.1:6559"
        ind_mod._REG_ADDR   = "tcp://127.0.0.1:6561"
        ind_mod._FEED_ADDR  = "tcp://127.0.0.1:6557"
        try:
            cfg_dict = {
                "zmq_feed_addr": "tcp://127.0.0.1:5557",
                "zmq_out_addr":  "tcp://127.0.0.1:5559",
                "zmq_reg_addr":  "tcp://127.0.0.1:5561",
                "min_ticks": 25,
                "streams": [{
                    "id": "btc_4h", "asset": "BTCUSDT",
                    "source": "binance_ws", "timeframe": "4h",
                    "indicators": [{"type": "rsi", "period": 14}],
                }],
            }
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
            json.dump(cfg_dict, tmp); tmp.close()
            cfg = load_config(tmp.name)
            self.assertEqual(cfg.feed_addr, "tcp://127.0.0.1:6557")
            self.assertEqual(cfg.out_addr,  "tcp://127.0.0.1:6559")
            self.assertEqual(cfg.reg_addr,  "tcp://127.0.0.1:6561")
        finally:
            ind_mod._PORT_SHIFT = orig_shift
            ind_mod._IND_ADDR   = orig_ind
            ind_mod._REG_ADDR   = orig_reg
            ind_mod._FEED_ADDR  = orig_feed


class TestBinanceMarketStructure(unittest.TestCase):
    """Tests for binance_oi, binance_ls_ratio, and binance_liquidations sources."""

    # ── module-level constants ────────────────────────────────────────────────

    def test_three_new_sources_in_valid_sources(self):
        for src in ("binance_oi", "binance_ls_ratio", "binance_liquidations"):
            self.assertIn(src, _VALID_SOURCES)

    def test_three_new_sources_without_indicators(self):
        for src in ("binance_oi", "binance_ls_ratio", "binance_liquidations"):
            self.assertIn(src, _SOURCES_WITHOUT_INDICATORS)

    def test_default_poll_intervals_are_300s(self):
        for src in ("binance_oi", "binance_ls_ratio", "binance_liquidations"):
            self.assertEqual(_DEFAULT_POLL_INTERVALS[src], 300)

    # ── StreamSpec.from_dict ──────────────────────────────────────────────────

    def test_binance_oi_spec_loads(self):
        spec = StreamSpec.from_dict({
            "id": "btc_oi", "asset": "BTCUSDT",
            "source": "binance_oi", "timeframe": "n/a",
            "indicators": [], "poll_interval_s": 300,
        })
        self.assertEqual(spec.source, "binance_oi")
        self.assertEqual(spec.poll_interval_s, 300)

    def test_binance_ls_ratio_spec_loads(self):
        spec = StreamSpec.from_dict({
            "id": "btc_ls_ratio", "asset": "BTCUSDT",
            "source": "binance_ls_ratio", "timeframe": "n/a",
            "indicators": [],
        })
        self.assertEqual(spec.source, "binance_ls_ratio")

    def test_binance_liquidations_spec_loads(self):
        spec = StreamSpec.from_dict({
            "id": "btc_liquidations", "asset": "BTCUSDT",
            "source": "binance_liquidations", "timeframe": "n/a",
            "indicators": [],
        })
        self.assertEqual(spec.source, "binance_liquidations")

    # ── parse_subscribe_request ───────────────────────────────────────────────

    def test_binance_oi_subscribe(self):
        stream_id, spec = parse_subscribe_request({
            "source": "binance_oi", "asset": "BTCUSDT",
        })
        self.assertEqual(stream_id, "binance_oi")
        self.assertEqual(spec.source, "binance_oi")

    def test_binance_ls_ratio_subscribe_no_asset(self):
        stream_id, spec = parse_subscribe_request({"source": "binance_ls_ratio"})
        self.assertEqual(stream_id, "binance_ls_ratio")

    def test_binance_liquidations_subscribe_poll_interval(self):
        _, spec = parse_subscribe_request({
            "source": "binance_liquidations", "poll_interval_s": 600,
        })
        self.assertEqual(spec.poll_interval_s, 600)

    # ── JSON config files ─────────────────────────────────────────────────────

    def _load(self, filename: str) -> IndicatorsConfig:
        path = os.path.join(os.path.dirname(__file__), "..", "strategies", filename)
        if not os.path.exists(path):
            self.skipTest(f"strategies/{filename} not found")
        return load_config(path)

    def test_oi_config_loads(self):
        cfg = self._load("indicators_oi_bitcoin.json")
        self.assertEqual(len(cfg.streams), 1)
        s = cfg.streams[0]
        self.assertEqual(s.id,     "btc_oi")
        self.assertEqual(s.source, "binance_oi")
        self.assertEqual(s.asset,  "BTCUSDT")
        self.assertEqual(s.poll_interval_s, 300)

    def test_ls_ratio_config_loads(self):
        cfg = self._load("indicators_ls_ratio_bitcoin.json")
        self.assertEqual(len(cfg.streams), 1)
        s = cfg.streams[0]
        self.assertEqual(s.id,     "btc_ls_ratio")
        self.assertEqual(s.source, "binance_ls_ratio")
        self.assertEqual(s.poll_interval_s, 300)

    def test_liquidations_config_loads(self):
        cfg = self._load("indicators_liquidations_bitcoin.json")
        self.assertEqual(len(cfg.streams), 1)
        s = cfg.streams[0]
        self.assertEqual(s.id,     "btc_liquidations")
        self.assertEqual(s.source, "binance_liquidations")
        self.assertEqual(s.poll_interval_s, 300)

    def test_all_three_configs_have_distinct_pub_ports(self):
        cfg_oi  = self._load("indicators_oi_bitcoin.json")
        cfg_ls  = self._load("indicators_ls_ratio_bitcoin.json")
        cfg_liq = self._load("indicators_liquidations_bitcoin.json")
        ports = {cfg_oi.out_addr, cfg_ls.out_addr, cfg_liq.out_addr}
        self.assertEqual(len(ports), 3, f"PUB port collision: {ports}")


if __name__ == "__main__":
    unittest.main()
