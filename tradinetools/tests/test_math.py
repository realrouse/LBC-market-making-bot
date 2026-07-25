"""Unit tests for tradinetools.math — scalar indicator functions."""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tradinetools.math import (
    sma_last, ema_last, atr_last, bollinger_last,
    vwap_last, vol_zscore_last, rolling_max_last, ichimoku_last,
)


class TestSmaLast(unittest.TestCase):

    def test_exact_window(self):
        self.assertAlmostEqual(sma_last([1.0, 2.0, 3.0], 3), 2.0)

    def test_uses_tail(self):
        self.assertAlmostEqual(sma_last([10.0, 1.0, 2.0, 3.0], 3), 2.0)

    def test_short_series_returns_none(self):
        self.assertIsNone(sma_last([1.0, 2.0], 3))

    def test_single_element(self):
        self.assertAlmostEqual(sma_last([5.0], 1), 5.0)

    def test_constant_series(self):
        self.assertAlmostEqual(sma_last([0.96] * 20, 20), 0.96)

    def test_n_equals_one_returns_last(self):
        self.assertAlmostEqual(sma_last([1.0, 2.0, 7.0], 1), 7.0)


class TestEmaLast(unittest.TestCase):

    def test_short_series_returns_none(self):
        self.assertIsNone(ema_last([1.0, 2.0], 3))

    def test_constant_series_equals_constant(self):
        self.assertAlmostEqual(ema_last([0.5] * 20, 9), 0.5, places=6)

    def test_seed_only_equals_sma(self):
        prices = [2.0, 4.0, 6.0]
        self.assertAlmostEqual(ema_last(prices, 3), sma_last(prices, 3))

    def test_k_factor_one_extra_tick(self):
        # seed=[1,1,1] (n=3), extra tick=2.0
        # k=2/(3+1)=0.5 → result = 1.0*(1-k) + 2.0*k
        k = 2.0 / (3 + 1)
        expected = 1.0 * (1 - k) + 2.0 * k
        self.assertAlmostEqual(ema_last([1.0, 1.0, 1.0, 2.0], 3), expected, places=10)

    def test_rising_series_ema_lags_last(self):
        prices = list(range(1, 21))
        ema = ema_last(prices, 9)
        self.assertIsNotNone(ema)
        self.assertLess(ema, 20.0)
        self.assertGreater(ema, 5.0)

    def test_n_equals_series_length_equals_sma(self):
        prices = [1.0, 3.0, 5.0, 7.0]
        self.assertAlmostEqual(ema_last(prices, 4), sma_last(prices, 4))


class TestAtrLast(unittest.TestCase):

    def test_short_series_returns_none(self):
        self.assertIsNone(atr_last([1.0] * 5, [0.9] * 5, [1.0] * 5, 14))

    def test_flat_series_zero_atr(self):
        n = 20
        h = [100.0] * n
        l = [100.0] * n
        c = [100.0] * n
        self.assertAlmostEqual(atr_last(h, l, c, 14), 0.0, places=6)

    def test_positive_on_volatile_series(self):
        n = 20
        h = [100.0 + i * 2 for i in range(n)]
        l = [100.0 + i * 2 - 5 for i in range(n)]
        c = [100.0 + i * 2 - 1 for i in range(n)]
        atr = atr_last(h, l, c, 14)
        self.assertIsNotNone(atr)
        self.assertGreater(atr, 0)

    def test_gap_dominates_true_range(self):
        # Two bars: prev_close=95. Bar high=115, low=105.
        # TR candidates: HL=10, H-PC=20, L-PC=10 → TR = 20
        h = [100.0, 115.0]
        l = [90.0,  105.0]
        c = [95.0,  110.0]
        atr = atr_last(h, l, c, 1)
        self.assertAlmostEqual(atr, 20.0)

    def test_requires_n_plus_one_bars(self):
        # n=5 requires 6 bars minimum; 5 bars → None
        n = 5
        h = [10.0] * n
        l = [9.0]  * n
        c = [9.5]  * n
        self.assertIsNone(atr_last(h, l, c, n))
        h2 = h + [10.0]; l2 = l + [9.0]; c2 = c + [9.5]
        self.assertIsNotNone(atr_last(h2, l2, c2, n))


class TestBollingerLast(unittest.TestCase):

    def test_short_series_all_none(self):
        upper, mid, lower = bollinger_last([1.0, 2.0], 5, 2.0)
        self.assertIsNone(upper)
        self.assertIsNone(mid)
        self.assertIsNone(lower)

    def test_ordering_upper_gt_mid_gt_lower(self):
        import random
        random.seed(42)
        closes = [50_000 + random.gauss(0, 500) for _ in range(25)]
        upper, mid, lower = bollinger_last(closes, 20, 2.0)
        self.assertGreater(upper, mid)
        self.assertGreater(mid, lower)

    def test_flat_series_zero_width(self):
        upper, mid, lower = bollinger_last([100.0] * 25, 20, 2.0)
        self.assertAlmostEqual(upper, lower, places=6)
        self.assertAlmostEqual(mid, 100.0)

    def test_mid_equals_sma(self):
        closes = [float(i) for i in range(1, 26)]
        _, mid, _ = bollinger_last(closes, 20, 2.0)
        self.assertAlmostEqual(mid, sma_last(closes, 20))

    def test_k_zero_gives_upper_eq_lower(self):
        import random
        random.seed(7)
        closes = [50_000 + random.gauss(0, 100) for _ in range(25)]
        upper, mid, lower = bollinger_last(closes, 20, 0.0)
        self.assertAlmostEqual(upper, lower, places=10)
        self.assertAlmostEqual(upper, mid,   places=10)


class TestVwapLast(unittest.TestCase):

    def test_short_series_returns_none(self):
        self.assertIsNone(vwap_last([1.0, 2.0], [1.0, 1.0], 5))

    def test_equal_volumes_equals_close_sma(self):
        closes = [10.0, 20.0, 30.0, 40.0, 50.0]
        vols   = [1.0] * 5
        self.assertAlmostEqual(vwap_last(closes, vols, 5), 30.0)

    def test_zero_volume_returns_last_close(self):
        closes = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertAlmostEqual(vwap_last(closes, [0.0] * 5, 5), closes[-1])

    def test_weighted_average(self):
        # close=[1,2], vol=[1,3] → (1*1 + 2*3)/(1+3) = 7/4 = 1.75
        self.assertAlmostEqual(vwap_last([1.0, 2.0], [1.0, 3.0], 2), 1.75)

    def test_uses_tail_only(self):
        # Long series; window=2 should use only [40,50] with vol [1,1]
        closes = [10.0, 20.0, 30.0, 40.0, 50.0]
        vols   = [100.0, 100.0, 100.0, 1.0, 1.0]
        self.assertAlmostEqual(vwap_last(closes, vols, 2), 45.0)


class TestVolZscoreLast(unittest.TestCase):

    def test_short_series_returns_none(self):
        self.assertIsNone(vol_zscore_last([1.0, 2.0], 5))

    def test_flat_volumes_returns_zero(self):
        self.assertAlmostEqual(vol_zscore_last([5.0] * 20, 20), 0.0)

    def test_spike_returns_large_positive(self):
        # 19 bars at 1.0, then spike at 100.0
        vols = [1.0] * 19 + [100.0]
        self.assertGreater(vol_zscore_last(vols, 20), 4.0)

    def test_low_last_returns_negative(self):
        vols = [100.0] * 19 + [1.0]
        self.assertLess(vol_zscore_last(vols, 20), 0.0)

    def test_uses_tail_window(self):
        # Head is huge; window=5 only sees [1,1,1,1,100]
        head = [1000.0] * 20
        tail = [1.0, 1.0, 1.0, 1.0, 100.0]
        z = vol_zscore_last(head + tail, 5)
        self.assertGreater(z, 1.0)


class TestRollingMaxLast(unittest.TestCase):

    def test_short_series_returns_none(self):
        self.assertIsNone(rolling_max_last([1.0, 2.0], 5))

    def test_returns_maximum(self):
        self.assertAlmostEqual(rolling_max_last([1.0, 5.0, 3.0, 2.0, 4.0], 5), 5.0)

    def test_uses_tail_window(self):
        # first element is 99 but window=3 only sees [3,2,4]
        self.assertAlmostEqual(rolling_max_last([99.0, 3.0, 2.0, 4.0], 3), 4.0)

    def test_single_element_window(self):
        self.assertAlmostEqual(rolling_max_last([7.0, 3.0, 5.0], 1), 5.0)

    def test_exact_window(self):
        series = [float(i) for i in range(10)]
        self.assertAlmostEqual(rolling_max_last(series, 10), 9.0)


class TestIchimokuLast(unittest.TestCase):

    # Small custom periods make every line hand-verifiable.
    #  idx:      0    1    2    3    4    5
    H = [30.0, 12.0, 11.0, 15.0, 14.0, 13.0]
    L = [25.0,  9.0,  7.0, 11.0, 10.0,  9.0]
    C = [28.0, 11.0, 10.0, 13.0, 12.0, 11.0]
    KW = dict(tenkan_n=2, kijun_n=3, senkou_b_n=4, displacement=2)

    def test_tenkan_kijun_current(self):
        r = ichimoku_last(self.H, self.L, self.C, **self.KW)
        # tenkan(2): H[4:6]=[14,13]→14, L[4:6]=[10,9]→9 → (14+9)/2
        self.assertAlmostEqual(r["tenkan"], 11.5)
        # kijun(3): H[3:6]=[15,14,13]→15, L[3:6]=[11,10,9]→9 → (15+9)/2
        self.assertAlmostEqual(r["kijun"], 12.0)

    def test_cloud_uses_displaced_window_not_current(self):
        # The crux: cloud must be the spans computed `displacement`(2) bars ago.
        r = ichimoku_last(self.H, self.L, self.C, **self.KW)
        # span_b(4, back=2): H[0:4]=[30,12,11,15]→30, L[0:4]=[25,9,7,11]→7 → 18.5
        # tenkan_past(2,back2)=11, kijun_past(3,back2)=11 → span_a=11.0
        self.assertAlmostEqual(r["cloud_top"], 18.5)     # max(18.5, 11.0)
        self.assertAlmostEqual(r["cloud_bottom"], 11.0)  # min(18.5, 11.0)
        # Sanity: cloud differs from the current tenkan/kijun (no look-at-now leak).
        self.assertNotAlmostEqual(r["cloud_top"], r["kijun"])

    def test_chikou_is_close_displacement_bars_ago(self):
        r = ichimoku_last(self.H, self.L, self.C, **self.KW)
        # chikou = C[n-1-displacement] = C[6-1-2] = C[3] = 13.0
        self.assertAlmostEqual(r["chikou"], 13.0)

    def test_cloud_none_when_insufficient_history(self):
        # 5 bars < senkou_b_n(4)+displacement(2)=6 → cloud None, lines still present.
        r = ichimoku_last(self.H[:5], self.L[:5], self.C[:5], **self.KW)
        self.assertIsNone(r["cloud_top"])
        self.assertIsNone(r["cloud_bottom"])
        self.assertIsNotNone(r["tenkan"])
        self.assertIsNotNone(r["kijun"])
        self.assertAlmostEqual(r["chikou"], 10.0)  # C[5-1-2]=C[2]

    def test_standard_periods_cloud_needs_78_bars(self):
        # 60 bars: kijun/chikou defined, but cloud needs 52+26=78 → None.
        h = [100.0] * 60; l = [99.0] * 60; c = [99.5] * 60
        r = ichimoku_last(h, l, c)
        self.assertIsNone(r["cloud_top"])
        self.assertIsNone(r["cloud_bottom"])
        self.assertIsNotNone(r["kijun"])
        self.assertIsNotNone(r["chikou"])   # 60 > displacement(26)

    def test_standard_periods_full_series(self):
        # 80 flat-ish bars → every line defined; flat series → all lines equal price.
        n = 80
        h = [100.0] * n; l = [100.0] * n; c = [100.0] * n
        r = ichimoku_last(h, l, c)
        for k in ("tenkan", "kijun", "cloud_top", "cloud_bottom", "chikou"):
            self.assertAlmostEqual(r[k], 100.0, msg=f"{k} should be 100.0 on flat series")


if __name__ == "__main__":
    unittest.main()
