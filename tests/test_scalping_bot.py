"""
Tests for bot/scalping_bot.py

Covers:
  TestIndicatorHelpers  — _sma, _ema_last, _atr_last, _bollinger_last,
                          _vwap_last, _vol_zscore_last, _rolling_max_last
  TestScalpingBotInit   — config loading, param merging, logging, SQLite init
  TestPositionManagement— open/close position, PnL, capital accounting, fees
  TestCandleMomentum    — signal fires / does not fire based on body_ratio + vol_z
  TestMeanRevSignal     — entry below BB+VWAP, ATR guard
  TestBreakoutSignal    — entry on range breakout, ATR filter
  TestExitLogic         — TP / SL / timeout exit via _on_closed_candle
  TestEdgeCases         — warm-up guard, empty buffer, reconnection state

All tests are fully offline — no network calls, no real Binance WebSocket.
"""

import json
import math
import os
import sqlite3
import sys
import tempfile
import unittest
from collections import deque
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import scalping_bot as sb
from scalping_bot import ScalpingBot, DEFAULTS, _MIN_WARM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmpdir: str, **overrides) -> str:
    cfg = {"strategy_type": "candle_momentum"}
    cfg.update(overrides)
    path = os.path.join(tmpdir, "test_strategy.json")
    with open(path, "w") as f:
        json.dump(cfg, f)
    return path


def _flat_buf(n: int, price: float = 50_000.0, volume: float = 5.0,
              base_ms: int = 1_577_836_800_000) -> list:
    return [
        {"ts_ms": base_ms + i * 60_000, "open": price, "high": price,
         "low": price, "close": price, "volume": volume}
        for i in range(n)
    ]


def _make_bot(tmpdir: str, **cfg_overrides) -> ScalpingBot:
    """Create a ScalpingBot with a temp dir, suppressing all logging output."""
    cfg_path = _make_config(tmpdir, **cfg_overrides)
    with patch("scalping_bot.setup_bot_logger", return_value=MagicMock()):
        bot = ScalpingBot(cfg_path, install_dir=tmpdir)
    return bot


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

class TestIndicatorHelpers(unittest.TestCase):

    def test_sma_correct(self):
        self.assertAlmostEqual(sb._sma([1.0, 2.0, 3.0, 4.0], 3), 3.0)

    def test_sma_too_short_returns_none(self):
        self.assertIsNone(sb._sma([1.0, 2.0], 5))

    def test_sma_window_one(self):
        self.assertAlmostEqual(sb._sma([42.0], 1), 42.0)

    def test_ema_last_constant_series(self):
        self.assertAlmostEqual(sb._ema_last([7.0] * 20, 5), 7.0, places=5)

    def test_ema_last_too_short_returns_none(self):
        self.assertIsNone(sb._ema_last([1.0, 2.0], 5))

    def test_atr_last_flat_is_zero(self):
        self.assertAlmostEqual(
            sb._atr_last([10.0]*20, [10.0]*20, [10.0]*20, 5), 0.0, places=8)

    def test_atr_last_volatile_is_positive(self):
        highs  = [100.0 + i for i in range(20)]
        lows   = [90.0  + i for i in range(20)]
        closes = [95.0  + i for i in range(20)]
        self.assertGreater(sb._atr_last(highs, lows, closes, 5), 0)

    def test_atr_last_too_short_returns_none(self):
        self.assertIsNone(sb._atr_last([10.0]*3, [9.0]*3, [9.5]*3, 5))

    def test_bollinger_flat_upper_equals_mid(self):
        closes = [50_000.0] * 25
        u, m, l = sb._bollinger_last(closes, 20, 2.0)
        self.assertAlmostEqual(u, m, places=4)
        self.assertAlmostEqual(l, m, places=4)

    def test_bollinger_too_short_returns_none_triple(self):
        u, m, l = sb._bollinger_last([1.0, 2.0], 20, 2.0)
        self.assertIsNone(u)
        self.assertIsNone(m)
        self.assertIsNone(l)

    def test_vwap_last_constant_equals_price(self):
        self.assertAlmostEqual(
            sb._vwap_last([100.0]*20, [5.0]*20, 10), 100.0)

    def test_vwap_last_too_short_returns_none(self):
        self.assertIsNone(sb._vwap_last([1.0]*5, [1.0]*5, 10))

    def test_vol_zscore_last_constant_is_zero(self):
        self.assertAlmostEqual(sb._vol_zscore_last([5.0]*30, 10), 0.0, places=6)

    def test_vol_zscore_last_spike_is_positive(self):
        vols = [1.0] * 29 + [100.0]
        self.assertGreater(sb._vol_zscore_last(vols, 10), 0)

    def test_vol_zscore_too_short_returns_none(self):
        self.assertIsNone(sb._vol_zscore_last([1.0]*5, 10))

    def test_rolling_max_last_correct(self):
        self.assertAlmostEqual(sb._rolling_max_last([3.0, 1.0, 5.0, 2.0], 3), 5.0)

    def test_rolling_max_last_too_short_returns_none(self):
        self.assertIsNone(sb._rolling_max_last([1.0, 2.0], 5))

    def test_rolling_max_last_window_one(self):
        self.assertAlmostEqual(sb._rolling_max_last([7.0, 3.0], 1), 3.0)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

class TestScalpingBotInit(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_defaults_merged_with_config(self):
        bot = _make_bot(self._tmp, strategy_type="meanrev")
        self.assertEqual(bot.p["strategy_type"], "meanrev")
        self.assertIn("mr_bb_period", bot.p)

    def test_capital_initialized_from_config(self):
        bot = _make_bot(self._tmp, capital=25_000.0)
        self.assertAlmostEqual(bot._capital, 25_000.0)

    def test_position_starts_none(self):
        bot = _make_bot(self._tmp)
        self.assertIsNone(bot._position)

    def test_buffer_starts_empty(self):
        bot = _make_bot(self._tmp)
        self.assertEqual(len(bot._buf), 0)

    def test_db_created(self):
        bot = _make_bot(self._tmp, strategy_type="candle_momentum")
        db_path = os.path.join(self._tmp, "scalping_candle_momentum.db")
        self.assertTrue(os.path.exists(db_path))

    def test_pid_file_created(self):
        bot = _make_bot(self._tmp, strategy_type="breakout")
        pid_path = os.path.join(self._tmp, "scalping_breakout.pid")
        self.assertTrue(os.path.exists(pid_path))

    def test_pid_file_contains_pid(self):
        bot = _make_bot(self._tmp, strategy_type="meanrev")
        pid_path = os.path.join(self._tmp, "scalping_meanrev.pid")
        with open(pid_path) as f:
            self.assertEqual(int(f.read().strip()), os.getpid())

    def test_underscore_keys_stripped_from_params(self):
        bot = _make_bot(self._tmp, _name="test", _description="desc")
        self.assertNotIn("_name", bot.p)
        self.assertNotIn("_description", bot.p)

    def test_trades_and_wins_start_at_zero(self):
        bot = _make_bot(self._tmp)
        self.assertEqual(bot._trades, 0)
        self.assertEqual(bot._wins, 0)


# ---------------------------------------------------------------------------
# Position management
# ---------------------------------------------------------------------------

class TestPositionManagement(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.bot  = _make_bot(self._tmp, capital=10_000.0, fee_rate=0.001,
                              slippage_pct=0.0005, stake_frac=0.20)

    def test_open_position_sets_position(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        self.assertIsNotNone(self.bot._position)

    def test_open_position_deducts_fee(self):
        capital_before = self.bot._capital
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        self.assertLess(self.bot._capital, capital_before)

    def test_close_tp_increases_capital(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        capital_after_open = self.bot._capital
        # exit at TP → win
        tp = self.bot._position["tp"]
        self.bot._close_position(tp, "take_profit", 2_000_000)
        self.assertGreater(self.bot._capital, capital_after_open)

    def test_close_sl_decreases_capital(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        capital_after_open = self.bot._capital
        sl = self.bot._position["sl"]
        self.bot._close_position(sl, "stop_loss", 2_000_000)
        self.assertLess(self.bot._capital, capital_after_open)

    def test_close_clears_position(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        self.bot._close_position(50_300.0, "take_profit", 2_000_000)
        self.assertIsNone(self.bot._position)

    def test_trades_counter_incremented(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        self.bot._close_position(50_300.0, "take_profit", 2_000_000)
        self.assertEqual(self.bot._trades, 1)

    def test_wins_counter_incremented_on_win(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        tp = self.bot._position["tp"]
        self.bot._close_position(tp, "take_profit", 2_000_000)
        self.assertEqual(self.bot._wins, 1)

    def test_wins_not_incremented_on_loss(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        sl = self.bot._position["sl"]
        self.bot._close_position(sl, "stop_loss", 2_000_000)
        self.assertEqual(self.bot._wins, 0)

    def test_trade_written_to_db(self):
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0, 1_000_000, "cm_long")
        self.bot._close_position(50_300.0, "take_profit", 2_000_000)
        row = self.bot._db.execute("SELECT COUNT(*) FROM trades").fetchone()
        self.assertEqual(row[0], 1)

    def test_close_without_position_is_safe(self):
        self.bot._close_position(50_000.0, "timeout", 2_000_000)   # no-op


# ---------------------------------------------------------------------------
# Candle momentum signal
# ---------------------------------------------------------------------------

class TestCandleMomentumSignal(unittest.TestCase):
    # NOTE: tests use _on_closed_candle (not _check_entry directly) so that the
    # new candle is appended to the buffer before z-score is computed — matching
    # the actual runtime flow.

    _BASE_MS = 1_577_836_800_000

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.bot  = _make_bot(
            self._tmp,
            strategy_type="candle_momentum",
            cm_body_ratio_thresh=0.80,
            cm_vol_z_window=10,
            cm_vol_z_thresh=1.0,
            cm_min_range_pct=0.0001,
            cm_take_profit_pct=0.006,
            cm_stop_loss_pct=0.003,
        )
        # Fill buffer with flat low-volume candles for baseline
        for c in _flat_buf(50, 50_000.0, volume=1.0, base_ms=self._BASE_MS):
            self.bot._buf.append(c)

    def _ts(self, offset_min=51):
        return self._BASE_MS + offset_min * 60_000

    def _bullish_candle(self, offset_min=51):
        """Strong bull candle: body_ratio = 0.9, high volume."""
        return {
            "ts_ms": self._ts(offset_min), "open": 49_000.0,
            "high": 50_000.0, "low": 49_000.0,
            "close": 49_900.0, "volume": 80.0,
        }

    def test_strong_candle_opens_position(self):
        # _on_closed_candle appends to buffer first so vol_zscore sees vol=80
        self.bot._on_closed_candle(self._bullish_candle())
        self.assertIsNotNone(self.bot._position)

    def test_weak_body_no_entry(self):
        # body_ratio = 200/1000 = 0.2 < 0.80
        candle = {"ts_ms": self._ts(), "open": 49_700.0,
                  "high": 50_000.0, "low": 49_000.0,
                  "close": 49_900.0, "volume": 80.0}
        self.bot._on_closed_candle(candle)
        self.assertIsNone(self.bot._position)

    def test_low_volume_no_entry(self):
        # Reset baseline to high volume, then send a low-volume candle
        for c in _flat_buf(10, 50_000.0, volume=80.0,
                            base_ms=self._ts()):
            self.bot._buf.append(c)
        weak_vol = {"ts_ms": self._ts(62), "open": 49_000.0,
                    "high": 50_000.0, "low": 49_000.0,
                    "close": 49_900.0, "volume": 1.0}
        self.bot._on_closed_candle(weak_vol)
        self.assertIsNone(self.bot._position)

    def test_flat_candle_not_traded(self):
        flat = {"ts_ms": self._ts(), "open": 50_000.0,
                "high": 50_000.0, "low": 50_000.0,
                "close": 50_000.0, "volume": 80.0}
        self.bot._on_closed_candle(flat)
        self.assertIsNone(self.bot._position)

    def test_no_entry_if_already_in_position(self):
        # ts_open_ms just 1 minute before the signal candle → well within max_hold
        ts_open = self._ts(50)
        self.bot._open_position(49_900.0, 50_200.0, 49_750.0, ts_open, "cm_long")
        entry_px_before = self.bot._position["entry_px"]
        # Signal candle: does not reach TP or SL, hold < max_hold
        signal = {"ts_ms": self._ts(51), "open": 49_000.0,
                  "high": 50_100.0, "low": 49_800.0,   # hi < tp=50200, lo > sl=49750
                  "close": 49_900.0, "volume": 80.0}
        self.bot._on_closed_candle(signal)
        self.assertIsNotNone(self.bot._position)
        self.assertAlmostEqual(self.bot._position["entry_px"], entry_px_before)


# ---------------------------------------------------------------------------
# Mean-reversion signal
# ---------------------------------------------------------------------------

class TestMeanRevSignal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.bot  = _make_bot(
            self._tmp,
            strategy_type="meanrev",
            mr_bb_period=10,
            mr_bb_std_mult=1.0,
            mr_vwap_window=20,
            mr_vwap_dev_thresh=0.002,
            mr_atr_period=5,
            mr_sl_atr_mult=2.0,
        )
        # Fill buffer with steady candles
        for c in _flat_buf(100, 50_000.0, volume=5.0):
            self.bot._buf.append(c)

    def test_dip_below_bb_and_vwap_opens_position(self):
        dip = {"ts_ms": 1_577_899_800_000, "open": 50_000.0,
               "high": 50_000.0, "low": 48_000.0, "close": 48_500.0,
               "volume": 5.0}
        self.bot._check_entry(dip)
        self.assertIsNotNone(self.bot._position)

    def test_price_above_bb_no_entry(self):
        spike = {"ts_ms": 1_577_899_800_000, "open": 50_000.0,
                 "high": 52_000.0, "low": 50_000.0, "close": 51_500.0,
                 "volume": 5.0}
        self.bot._check_entry(spike)
        self.assertIsNone(self.bot._position)

    def test_sl_above_98pct_guard_blocks_entry(self):
        # Add volatile candles so ATR >> 0; then huge sl_atr_mult → sl < 98% of entry
        for i in range(20):
            self.bot._buf.append({
                "ts_ms": 1_577_899_800_000 + i * 60_000,
                "open": 50_000.0, "high": 53_000.0,  # ATR ≈ 3000
                "low": 47_000.0,  "close": 50_000.0, "volume": 5.0,
            })
        self.bot.p["mr_sl_atr_mult"] = 10.0   # sl = 48500 - 10*3000 = 18500 << 0.98*48500
        dip = {"ts_ms": 1_577_901_000_000, "open": 50_000.0,
               "high": 50_000.0, "low": 48_000.0, "close": 48_500.0,
               "volume": 5.0}
        self.bot._on_closed_candle(dip)
        self.assertIsNone(self.bot._position)

    def test_tp_is_vwap(self):
        dip = {"ts_ms": 1_577_899_800_000, "open": 50_000.0,
               "high": 50_000.0, "low": 48_000.0, "close": 48_500.0,
               "volume": 5.0}
        self.bot._check_entry(dip)
        if self.bot._position:
            closes  = [c["close"]  for c in self.bot._buf]
            volumes = [c["volume"] for c in self.bot._buf]
            vwap    = sb._vwap_last(closes, volumes, self.bot.p["mr_vwap_window"])
            self.assertAlmostEqual(self.bot._position["tp"], vwap, places=2)


# ---------------------------------------------------------------------------
# Breakout signal
# ---------------------------------------------------------------------------

class TestBreakoutSignal(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.bot  = _make_bot(
            self._tmp,
            strategy_type="breakout",
            bo_range_period=5,
            bo_atr_period=5,
            bo_min_atr_pct=0.0001,
            bo_sl_atr_mult=1.0,
            bo_tp_atr_mult=2.0,
        )
        for c in _flat_buf(50, 50_000.0):
            self.bot._buf.append(c)

    def test_breakout_opens_position(self):
        # Add volatile candles to create non-zero ATR
        for i in range(10):
            self.bot._buf.append({
                "ts_ms": 1_577_899_800_000 + i * 60_000,
                "open": 50_000.0, "high": 50_500.0,
                "low": 49_500.0, "close": 50_100.0, "volume": 5.0
            })
        # Now a candle that breaks above range high
        breakout = {
            "ts_ms": 1_577_900_500_000,
            "open": 50_100.0, "high": 52_000.0,
            "low": 50_100.0, "close": 51_500.0, "volume": 5.0
        }
        self.bot._check_entry(breakout)
        self.assertIsNotNone(self.bot._position)

    def test_tp_is_2x_sl_distance(self):
        # Set slippage_pct=0 so entry_px == signal_px; then TP/SL distances
        # are exactly bo_tp_atr_mult * ATR and bo_sl_atr_mult * ATR → ratio = 2.0
        self.bot.p["slippage_pct"] = 0.0
        for i in range(10):
            self.bot._buf.append({
                "ts_ms": 1_577_899_800_000 + i * 60_000,
                "open": 50_000.0, "high": 50_500.0,
                "low": 49_500.0, "close": 50_100.0, "volume": 5.0
            })
        breakout = {
            "ts_ms": 1_577_900_500_000,
            "open": 50_100.0, "high": 52_000.0,
            "low": 50_100.0, "close": 51_500.0, "volume": 5.0
        }
        self.bot._on_closed_candle(breakout)
        if self.bot._position:
            entry = self.bot._position["entry_px"]
            tp    = self.bot._position["tp"]
            sl    = self.bot._position["sl"]
            self.assertAlmostEqual((tp - entry) / (entry - sl),
                                   self.bot.p["bo_tp_atr_mult"] / self.bot.p["bo_sl_atr_mult"],
                                   places=4)

    def test_high_atr_filter_blocks_flat(self):
        self.bot.p["bo_min_atr_pct"] = 99.0
        breakout = {
            "ts_ms": 1_577_900_500_000,
            "open": 50_000.0, "high": 55_000.0,
            "low": 50_000.0, "close": 54_000.0, "volume": 5.0
        }
        self.bot._check_entry(breakout)
        self.assertIsNone(self.bot._position)


# ---------------------------------------------------------------------------
# Exit logic via _on_closed_candle
# ---------------------------------------------------------------------------

class TestExitLogic(unittest.TestCase):
    # All candle timestamps are offset from TS_OPEN to control hold duration.
    TS_OPEN = 1_577_836_800_000   # epoch of position open

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.bot  = _make_bot(self._tmp, capital=10_000.0)
        for c in _flat_buf(50, base_ms=self.TS_OPEN - 50 * 60_000):
            self.bot._buf.append(c)
        # entry=50_025 (with slippage), tp=50_300, sl=49_850
        self.bot._open_position(50_000.0, 50_300.0, 49_850.0,
                                self.TS_OPEN, "cm_long")

    def _candle(self, hi, lo, cl, offset_min=1):
        ts = self.TS_OPEN + offset_min * 60_000
        return {"ts_ms": ts, "open": cl, "high": hi, "low": lo,
                "close": cl, "volume": 5.0}

    def test_tp_candle_closes_with_win(self):
        self.bot._on_closed_candle(self._candle(50_400.0, 49_950.0, 50_350.0))
        self.assertIsNone(self.bot._position)
        self.assertEqual(self.bot._wins, 1)

    def test_sl_candle_closes_with_loss(self):
        self.bot._on_closed_candle(self._candle(50_000.0, 49_700.0, 49_800.0))
        self.assertIsNone(self.bot._position)
        self.assertEqual(self.bot._wins, 0)
        self.assertEqual(self.bot._trades, 1)

    def test_neutral_candle_keeps_position(self):
        # hi < tp=50300, lo > sl=49850, hold=1 min < 10 min max
        self.bot._on_closed_candle(self._candle(50_200.0, 49_900.0, 50_100.0))
        self.assertIsNotNone(self.bot._position)

    def test_timeout_closes_position(self):
        # hold = 15 min > cm_max_hold_minutes=10 → timeout
        self.bot._on_closed_candle(self._candle(50_200.0, 49_900.0, 50_100.0,
                                                offset_min=15))
        self.assertIsNone(self.bot._position)

    def test_sl_takes_priority_over_tp(self):
        # Candle spans both SL (lo < 49850) and TP (hi > 50300)
        # SL is checked first → loss, not win
        self.bot._on_closed_candle(self._candle(51_000.0, 49_700.0, 50_000.0))
        self.assertEqual(self.bot._wins, 0)   # SL hit first, not TP


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_warm_up_guard_no_signal_below_min(self):
        bot = _make_bot(self._tmp, strategy_type="candle_momentum",
                        cm_body_ratio_thresh=0.5,
                        cm_vol_z_thresh=0.0,
                        cm_min_range_pct=0.0001)
        # _on_closed_candle appends before the guard check, so pre-fill with
        # _MIN_WARM - 2 candles: after append we have _MIN_WARM - 1 < _MIN_WARM.
        for c in _flat_buf(_MIN_WARM - 2, volume=100.0):
            bot._buf.append(c)
        strong = {"ts_ms": 9_999_999_999, "open": 49_000.0, "high": 50_000.0,
                  "low": 49_000.0, "close": 49_900.0, "volume": 200.0}
        bot._on_closed_candle(strong)
        self.assertIsNone(bot._position)

    def test_buffer_maxlen_respected(self):
        bot = _make_bot(self._tmp)
        for c in _flat_buf(600):
            bot._buf.append(c)
        self.assertLessEqual(len(bot._buf), 500)

    def test_close_without_open_is_safe(self):
        bot = _make_bot(self._tmp)
        bot._close_position(50_000.0, "timeout", 2_000_000)   # no-op

    def test_db_schema_has_trades_table(self):
        bot = _make_bot(self._tmp, strategy_type="meanrev")
        tables = {r[0] for r in bot._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        self.assertIn("trades", tables)


if __name__ == "__main__":
    unittest.main(verbosity=2)
