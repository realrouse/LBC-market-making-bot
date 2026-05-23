"""
Tests for scripts/backtest_scalping.py

Covers:
  TestIndicators        — sma, ema, atr_series, bollinger, vwap_rolling, vol_zscore
  TestRunBacktestFlat   — flat-price edge cases (no trades, capital preserved)
  TestCandleMomentum    — signal fires and does not fire, TP/SL/timeout exits
  TestMeanRev           — entry below BB+VWAP, SL from ATR, VWAP TP
  TestBreakout          — breakout above range high, ATR filter, TP/SL
  TestMetrics           — win rate, avg hold, fees, max drawdown
  TestLoadKlines        — schema validation (mocked sqlite3)

All tests are offline — no real DB files required.
"""

import math
import os
import sys
import sqlite3
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "analysis"))
import backtest_scalping as bs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flat_klines(n: int, price: float = 50_000.0, volume: float = 10.0,
                 base_ms: int = 1_577_836_800_000) -> list:
    return [
        {
            "ts_ms":  base_ms + i * 60_000,
            "open":   price,
            "high":   price,
            "low":    price,
            "close":  price,
            "volume": volume,
        }
        for i in range(n)
    ]


def _spike_klines(n_flat: int, price: float, n_spike: int, spike_price: float,
                  volume_spike: float = 50.0, base_ms: int = 1_577_836_800_000) -> list:
    flat  = _flat_klines(n_flat, price, base_ms=base_ms)
    spike = [
        {
            "ts_ms":  base_ms + (n_flat + i) * 60_000,
            "open":   price,
            "high":   spike_price,
            "low":    price,
            "close":  spike_price,
            "volume": volume_spike,
        }
        for i in range(n_spike)
    ]
    return flat + spike


def _default_params(**overrides):
    p = dict(bs.DEFAULTS)
    p.update(overrides)
    return p


# ---------------------------------------------------------------------------
# Indicator tests
# ---------------------------------------------------------------------------

class TestIndicators(unittest.TestCase):

    def test_sma_prefix_none(self):
        r = bs.sma([1.0, 2.0, 3.0, 4.0], 3)
        self.assertIsNone(r[0])
        self.assertIsNone(r[1])

    def test_sma_trailing_correct(self):
        r = bs.sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertAlmostEqual(r[2], 2.0)
        self.assertAlmostEqual(r[4], 4.0)

    def test_sma_window_one_is_identity(self):
        self.assertEqual(bs.sma([5.0, 10.0], 1), [5.0, 10.0])

    def test_ema_prefix_none(self):
        r = bs.ema([1.0] * 10, 5)
        for v in r[:4]:
            self.assertIsNone(v)

    def test_ema_constant_series_equals_value(self):
        r = bs.ema([7.0] * 20, 5)
        for v in r[4:]:
            self.assertAlmostEqual(v, 7.0, places=6)

    def test_atr_positive_for_volatile_series(self):
        highs  = [100.0 + i for i in range(20)]
        lows   = [90.0  + i for i in range(20)]
        closes = [95.0  + i for i in range(20)]
        r = bs.atr_series(highs, lows, closes, 5)
        for v in r[4:]:
            self.assertGreater(v, 0)

    def test_atr_zero_for_flat_series(self):
        r = bs.atr_series([10.0]*20, [10.0]*20, [10.0]*20, 5)
        for v in r[4:]:
            self.assertAlmostEqual(v, 0.0, places=8)

    def test_bollinger_mid_equals_sma(self):
        closes = [float(i) for i in range(30)]
        _, mid, _ = bs.bollinger(closes, 20, 2.0)
        sma_ref = bs.sma(closes, 20)
        for m, s in zip(mid[19:], sma_ref[19:]):
            self.assertAlmostEqual(m, s, places=8)

    def test_bollinger_upper_above_mid(self):
        closes = [float(i % 10) for i in range(30)]
        upper, mid, lower = bs.bollinger(closes, 10, 2.0)
        for u, m, lo in zip(upper[9:], mid[9:], lower[9:]):
            if u is not None:
                self.assertGreaterEqual(u, m)
                self.assertLessEqual(lo, m)

    def test_bollinger_flat_series_upper_equals_mid(self):
        closes = [50_000.0] * 30
        upper, mid, lower = bs.bollinger(closes, 20, 2.0)
        for u, m in zip(upper[19:], mid[19:]):
            self.assertAlmostEqual(u, m, places=4)

    def test_vwap_rolling_constant_equals_price(self):
        closes  = [100.0] * 40
        volumes = [5.0]   * 40
        r = bs.vwap_rolling(closes, volumes, 10)
        for v in r[9:]:
            self.assertAlmostEqual(v, 100.0, places=6)

    def test_vwap_rolling_prefix_none(self):
        r = bs.vwap_rolling([1.0]*20, [1.0]*20, 10)
        for v in r[:9]:
            self.assertIsNone(v)

    def test_vol_zscore_zero_for_constant_volume(self):
        r = bs.vol_zscore([5.0] * 30, 10)
        for v in r[9:]:
            self.assertAlmostEqual(v, 0.0, places=6)

    def test_vol_zscore_positive_for_spike(self):
        vols = [1.0] * 29 + [100.0]
        r = bs.vol_zscore(vols, 10)
        self.assertGreater(r[-1], 0)

    def test_vol_zscore_prefix_none(self):
        r = bs.vol_zscore([1.0] * 20, 10)
        for v in r[:9]:
            self.assertIsNone(v)

    def test_rolling_max_prefix_none(self):
        r = bs.rolling_max([1.0, 2.0, 3.0, 4.0], 3)
        self.assertIsNone(r[0])
        self.assertIsNone(r[1])

    def test_rolling_max_correct_values(self):
        r = bs.rolling_max([3.0, 1.0, 4.0, 1.0, 5.0], 3)
        self.assertAlmostEqual(r[2], 4.0)
        self.assertAlmostEqual(r[3], 4.0)
        self.assertAlmostEqual(r[4], 5.0)

    def test_rolling_max_window_one_is_identity(self):
        self.assertEqual(bs.rolling_max([5.0, 2.0, 8.0], 1), [5.0, 2.0, 8.0])


# ---------------------------------------------------------------------------
# Flat price — base cases
# ---------------------------------------------------------------------------

class TestRunBacktestFlat(unittest.TestCase):

    def _run(self, stype, n=200, **overrides):
        p = _default_params(strategy_type=stype, **overrides)
        return bs.run_backtest(_flat_klines(n), p)

    def test_candle_momentum_flat_no_trades(self):
        # Flat candles have body_ratio=0 → no signal
        r = self._run("candle_momentum")
        self.assertEqual(len(r["trades"]), 0)

    def test_meanrev_flat_no_trades(self):
        # Flat price never goes below BB lower (std=0 → lower=mid=close)
        r = self._run("meanrev")
        self.assertEqual(len(r["trades"]), 0)

    def test_breakout_flat_no_trades(self):
        # Flat close never exceeds SMA of highs when all highs are equal
        r = self._run("breakout", bo_min_atr_pct=0.0001)
        self.assertEqual(len(r["trades"]), 0)

    def test_flat_capital_preserved_no_trades(self):
        for stype in ("candle_momentum", "meanrev", "breakout"):
            r = self._run(stype)
            self.assertAlmostEqual(r["final_capital"], 10_000.0, places=4,
                                   msg=f"capital drift in {stype}")

    def test_flat_max_dd_zero(self):
        for stype in ("candle_momentum", "meanrev", "breakout"):
            r = self._run(stype)
            self.assertAlmostEqual(r["max_dd"], 0.0, places=6)

    def test_start_end_dates_present(self):
        r = self._run("candle_momentum")
        self.assertIn("start_date", r)
        self.assertIn("end_date", r)

    def test_years_correct_for_1440_candles(self):
        # 1440 minutes = 1 day
        r = self._run("candle_momentum", n=1440)
        self.assertAlmostEqual(r["years"], 1 / 365.25, places=3)


# ---------------------------------------------------------------------------
# Candle momentum signal
# ---------------------------------------------------------------------------

class TestCandleMomentum(unittest.TestCase):

    def _p(self, **overrides):
        p = _default_params(strategy_type="candle_momentum")
        p.update(overrides)
        return p

    def _klines_bullish(self, n_pre=30, n_signal=5):
        base = _flat_klines(n_pre, 50_000.0)
        signal = [
            {
                "ts_ms":  1_577_836_800_000 + (n_pre + i) * 60_000,
                "open":   50_000.0,
                "high":   50_500.0,
                "low":    49_800.0,
                "close":  50_400.0,   # body_ratio = 400/700 ≈ 0.57 — just below threshold
                "volume": 100.0,      # volume spike
            }
            for i in range(n_signal)
        ]
        return base + signal

    def test_no_signal_below_body_ratio(self):
        # body_ratio ≈ 0.57 with threshold 0.60 → no entry
        klines = self._klines_bullish()
        r = bs.run_backtest(klines, self._p(cm_vol_z_thresh=0.5))
        self.assertEqual(len(r["trades"]), 0)

    def test_signal_fires_with_strong_body(self):
        # body_ratio = 900/1000 = 0.9, volume spike → should trade
        n_pre = 50
        base  = _flat_klines(n_pre, 50_000.0, volume=1.0)
        candle = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  49_000.0,
            "high":  50_000.0,
            "low":   49_000.0,
            "close": 49_900.0,    # body = 900, range = 1000 → ratio = 0.9
            "volume": 50.0,
        }
        more_flat = _flat_klines(20, 50_000.0, base_ms=1_577_836_800_000 + (n_pre + 1) * 60_000)
        klines = base + [candle] + more_flat
        r = bs.run_backtest(klines, self._p(
            cm_body_ratio_thresh=0.80,
            cm_vol_z_thresh=1.0,
            cm_min_range_pct=0.0001,
        ))
        self.assertGreater(len(r["trades"]), 0)

    def test_tp_exit_wins_trade(self):
        # Construct klines that trigger entry then immediately hit TP
        n_pre = 50
        base  = _flat_klines(n_pre, 50_000.0, volume=1.0)
        entry_candle = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  49_000.0, "high": 50_000.0,
            "low":   49_000.0, "close": 49_900.0,
            "volume": 80.0,
        }
        # Next candle spikes to TP level
        tp_price = 49_900.0 * 1.006  # cm_take_profit_pct default
        tp_candle = {
            "ts_ms": 1_577_836_800_000 + (n_pre + 1) * 60_000,
            "open":  49_900.0, "high": tp_price + 100,
            "low":   49_900.0, "close": tp_price,
            "volume": 10.0,
        }
        klines = base + [entry_candle, tp_candle]
        r = bs.run_backtest(klines, self._p(
            cm_body_ratio_thresh=0.80,
            cm_vol_z_thresh=0.5,
            cm_min_range_pct=0.0001,
        ))
        tp_trades = [t for t in r["trades"] if t["reason"] == "take_profit"]
        self.assertGreater(len(tp_trades), 0)
        self.assertGreater(tp_trades[0]["pnl"], 0)

    def test_sl_exit_loses_trade(self):
        n_pre = 50
        base  = _flat_klines(n_pre, 50_000.0, volume=1.0)
        entry_candle = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  49_000.0, "high": 50_000.0,
            "low":   49_000.0, "close": 49_900.0,
            "volume": 80.0,
        }
        sl_price = 49_900.0 * (1 - 0.003)
        sl_candle = {
            "ts_ms": 1_577_836_800_000 + (n_pre + 1) * 60_000,
            "open":  49_900.0, "high": 49_900.0,
            "low":   sl_price - 100, "close": sl_price,
            "volume": 10.0,
        }
        klines = base + [entry_candle, sl_candle]
        r = bs.run_backtest(klines, self._p(
            cm_body_ratio_thresh=0.80,
            cm_vol_z_thresh=0.5,
            cm_min_range_pct=0.0001,
        ))
        sl_trades = [t for t in r["trades"] if t["reason"] == "stop_loss"]
        self.assertGreater(len(sl_trades), 0)
        self.assertLess(sl_trades[0]["pnl"], 0)

    def test_timeout_exit_after_max_hold(self):
        n_pre  = 50
        base   = _flat_klines(n_pre, 50_000.0, volume=1.0)
        entry  = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  49_000.0, "high": 50_000.0,
            "low":   49_000.0, "close": 49_900.0,
            "volume": 80.0,
        }
        # Follow with flat candles that don't reach TP or SL
        post   = _flat_klines(15, 49_900.0, volume=1.0,
                              base_ms=1_577_836_800_000 + (n_pre + 1) * 60_000)
        klines = base + [entry] + post
        r = bs.run_backtest(klines, self._p(
            cm_body_ratio_thresh=0.80,
            cm_vol_z_thresh=0.5,
            cm_min_range_pct=0.0001,
            cm_max_hold_minutes=5,
            cm_take_profit_pct=0.10,   # very far TP — won't be hit
            cm_stop_loss_pct=0.10,     # very far SL — won't be hit
        ))
        timeout_trades = [t for t in r["trades"] if t["reason"] == "timeout"]
        self.assertGreater(len(timeout_trades), 0)

    def test_fees_deducted_from_winning_trade(self):
        n_pre = 50
        base  = _flat_klines(n_pre, 50_000.0, volume=1.0)
        entry = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  49_000.0, "high": 50_000.0,
            "low":   49_000.0, "close": 49_900.0,
            "volume": 80.0,
        }
        tp_px  = 49_900.0 * 1.006
        tp_c   = {
            "ts_ms": 1_577_836_800_000 + (n_pre + 1) * 60_000,
            "open":  49_900.0, "high": tp_px + 200,
            "low":   49_900.0, "close": tp_px,
            "volume": 10.0,
        }
        klines = base + [entry, tp_c]
        r = bs.run_backtest(klines, self._p(
            cm_body_ratio_thresh=0.80,
            cm_vol_z_thresh=0.5,
            cm_min_range_pct=0.0001,
        ))
        self.assertGreater(r["fees_paid"], 0)


# ---------------------------------------------------------------------------
# Mean-reversion signal
# ---------------------------------------------------------------------------

class TestMeanRev(unittest.TestCase):

    def _p(self, **overrides):
        p = _default_params(strategy_type="meanrev")
        p.update(overrides)
        return p

    def test_flat_series_no_entry(self):
        r = bs.run_backtest(_flat_klines(500, 50_000.0), self._p())
        self.assertEqual(len(r["trades"]), 0)

    def test_entry_below_bb_and_vwap(self):
        # 400 flat candles, then a sharp dip candle well below mean
        n_pre = 400
        base  = _flat_klines(n_pre, 50_000.0, volume=5.0)
        # A candle 1% below mean — should trigger entry if BB std allows
        dip_px = 49_400.0
        dip = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  50_000.0, "high": 50_000.0,
            "low":   dip_px, "close": dip_px,
            "volume": 5.0,
        }
        recovery = _flat_klines(60, 50_000.0, volume=5.0,
                                base_ms=1_577_836_800_000 + (n_pre + 1) * 60_000)
        klines = base + [dip] + recovery
        r = bs.run_backtest(klines, self._p(
            mr_bb_std_mult=1.0,       # tighter bands → easier to breach
            mr_vwap_dev_thresh=0.005,
            mr_sl_atr_mult=5.0,       # wide SL so it isn't hit
        ))
        self.assertGreater(len(r["trades"]), 0)

    def test_stop_loss_triggered(self):
        n_pre = 400
        base  = _flat_klines(n_pre, 50_000.0, volume=5.0)
        dip_px = 49_400.0
        entry_c = {
            "ts_ms": 1_577_836_800_000 + n_pre * 60_000,
            "open":  50_000.0, "high": 50_000.0,
            "low":   dip_px, "close": dip_px,
            "volume": 5.0,
        }
        # A candle that drops further — far below entry
        crash_px = dip_px * 0.97
        crash_c  = {
            "ts_ms": 1_577_836_800_000 + (n_pre + 1) * 60_000,
            "open":  dip_px, "high": dip_px,
            "low":   crash_px, "close": crash_px,
            "volume": 5.0,
        }
        klines = base + [entry_c, crash_c]
        r = bs.run_backtest(klines, self._p(
            mr_bb_std_mult=1.0,
            mr_vwap_dev_thresh=0.005,
            mr_sl_atr_mult=0.5,   # very tight stop
        ))
        sl_trades = [t for t in r["trades"] if t["reason"] == "stop_loss"]
        self.assertGreater(len(sl_trades), 0)

    def test_win_rate_within_bounds(self):
        r = bs.run_backtest(_flat_klines(500, 50_000.0), self._p())
        self.assertGreaterEqual(r["win_rate"], 0.0)
        self.assertLessEqual(r["win_rate"], 1.0)


# ---------------------------------------------------------------------------
# Breakout signal
# ---------------------------------------------------------------------------

class TestBreakout(unittest.TestCase):

    def _p(self, **overrides):
        p = _default_params(strategy_type="breakout")
        p.update(overrides)
        return p

    def test_flat_no_trades(self):
        r = bs.run_backtest(_flat_klines(200, 50_000.0), self._p())
        self.assertEqual(len(r["trades"]), 0)

    def test_spike_above_range_triggers_entry(self):
        n_pre   = 40
        base    = _flat_klines(n_pre, 50_000.0)
        # A series with volatile candles so ATR is non-zero
        volatile = [
            {
                "ts_ms": 1_577_836_800_000 + (n_pre + i) * 60_000,
                "open":  50_000.0,
                "high":  50_000.0 + 200 * (i % 2 + 1),
                "low":   49_800.0,
                "close": 50_100.0,
                "volume": 5.0,
            }
            for i in range(20)
        ]
        # Then a breakout candle above SMA(highs, 20)
        breakout_c = {
            "ts_ms": 1_577_836_800_000 + (n_pre + 20) * 60_000,
            "open":  50_100.0,
            "high":  52_000.0,
            "low":   50_100.0,
            "close": 51_500.0,   # above SMA of highs
            "volume": 10.0,
        }
        post = _flat_klines(150, 51_500.0, volume=5.0,
                            base_ms=1_577_836_800_000 + (n_pre + 21) * 60_000)
        klines = base + volatile + [breakout_c] + post
        r = bs.run_backtest(klines, self._p(bo_min_atr_pct=0.0001))
        self.assertGreater(len(r["trades"]), 0)

    def test_atr_filter_blocks_flat_market(self):
        # Very high atr_pct threshold means flat markets never trigger
        r = bs.run_backtest(_flat_klines(200, 50_000.0),
                            self._p(bo_min_atr_pct=99.0))
        self.assertEqual(len(r["trades"]), 0)

    def test_tp_2x_sl_ratio(self):
        # Verify config defaults express 2:1 R:R
        p = self._p()
        self.assertAlmostEqual(p["bo_tp_atr_mult"] / p["bo_sl_atr_mult"], 2.0)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class TestMetrics(unittest.TestCase):

    def _p(self, stype="candle_momentum", **overrides):
        p = _default_params(strategy_type=stype)
        p.update(overrides)
        return p

    def test_win_rate_between_0_and_1(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertGreaterEqual(r["win_rate"], 0.0)
        self.assertLessEqual(r["win_rate"], 1.0)

    def test_max_dd_between_0_and_1(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertGreaterEqual(r["max_dd"], 0.0)
        self.assertLessEqual(r["max_dd"], 1.0)

    def test_fees_non_negative(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertGreaterEqual(r["fees_paid"], 0.0)

    def test_final_capital_non_negative(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertGreater(r["final_capital"], 0.0)

    def test_wins_plus_losses_equals_trades(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertEqual(r["wins"] + r["losses"], len(r["trades"]))

    def test_avg_hold_non_negative(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertGreaterEqual(r["avg_hold_min"], 0.0)

    def test_empty_klines_returns_empty_dict(self):
        r = bs.run_backtest([], self._p())
        self.assertEqual(r, {})

    def test_too_few_klines_returns_empty_dict(self):
        r = bs.run_backtest(_flat_klines(10), self._p())
        self.assertEqual(r, {})

    def test_total_ret_at_least_positive_no_trades(self):
        r = bs.run_backtest(_flat_klines(200), self._p())
        self.assertGreater(r["total_ret"], 0)


# ---------------------------------------------------------------------------
# load_klines (mocked sqlite3)
# ---------------------------------------------------------------------------

class TestLoadKlines(unittest.TestCase):

    def _make_conn(self, rows, interval_ms=60_000):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE klines "
            "(ts_ms INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        conn.executemany("INSERT INTO klines VALUES (?,?,?,?,?,?)", rows)
        conn.commit()
        return conn

    def test_load_returns_list_of_dicts(self):
        rows = [
            (1_577_836_800_000, 9000, 9500, 8800, 9200, 10),
            (1_577_836_860_000, 9200, 9800, 9100, 9700, 12),
        ]
        conn = self._make_conn(rows)
        with patch("sqlite3.connect", return_value=conn):
            result = bs.load_klines(":memory:")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_load_fields_present(self):
        rows = [(1_577_836_800_000, 9000, 9500, 8800, 9200, 10)]
        conn = self._make_conn(rows)
        with patch("sqlite3.connect", return_value=conn):
            result = bs.load_klines(":memory:")
        row = result[0]
        for field in ("ts_ms", "open", "high", "low", "close", "volume"):
            self.assertIn(field, row)

    def test_load_sorted_by_ts(self):
        rows = [
            (1_577_836_860_000, 9200, 9800, 9100, 9700, 12),
            (1_577_836_800_000, 9000, 9500, 8800, 9200, 10),
        ]
        conn = self._make_conn(rows)
        with patch("sqlite3.connect", return_value=conn):
            result = bs.load_klines(":memory:")
        self.assertLess(result[0]["ts_ms"], result[1]["ts_ms"])

    def test_non_1m_interval_warns(self):
        rows = [
            (1_577_836_800_000, 9000, 9500, 8800, 9200, 10),
            (1_577_836_800_000 + 300_000, 9200, 9800, 9100, 9700, 12),  # 5m interval
        ]
        conn = self._make_conn(rows, interval_ms=300_000)
        import io
        with patch("sqlite3.connect", return_value=conn):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
                bs.load_klines(":memory:")
                output = mock_err.getvalue()
        self.assertIn("WARNING", output)


if __name__ == "__main__":
    unittest.main(verbosity=2)
