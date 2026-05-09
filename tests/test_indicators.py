"""Tests for bot/indicators.py — pure-math functions, PriceSeries, and config types."""

import sys, os, math, unittest, json, tempfile

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


if __name__ == "__main__":
    unittest.main()
