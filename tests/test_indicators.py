"""Tests for bot/indicators.py — pure-math functions and PriceSeries."""

import sys, os, math, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
from indicators import (
    compute_sma, compute_ema, compute_rsi, compute_volatility, PriceSeries,
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


if __name__ == "__main__":
    unittest.main()
