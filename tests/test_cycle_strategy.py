"""
Tests for scripts/backtest_cycle_strategy.py

Covers:
  - TestSMA                  : sma() helper (same as analyze_cycle_volatility)
  - TestDaysSinceHalving     : halving-relative day counter
  - TestRunBacktest          : end-to-end run_backtest with synthetic daily data
  - TestPrudenceDefaults     : prudence tier default values
  - TestLoadDailyFromDailyDB : load_daily() from a `daily`-schema SQLite DB (in-memory)

All tests are offline — no real DB files required.
"""

import os, sys, sqlite3, unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backtest_cycle_strategy as bcs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def _flat_daily(n: int, price: float = 50_000.0, base_ts: int = 1_577_836_800) -> list:
    """n days at a constant price, sequential unix timestamps starting at base_ts."""
    return [
        {
            "ts":    base_ts + i * 86_400,
            "date":  datetime.fromtimestamp(base_ts + i * 86_400,
                                            tz=timezone.utc).strftime("%Y-%m-%d"),
            "high":  price,
            "low":   price,
            "close": price,
        }
        for i in range(n)
    ]


def _ramp_daily(n_low: int, price_low: float, n_high: int, price_high: float,
                base_ts: int = 1_577_836_800) -> list:
    """n_low days at price_low, then n_high days at price_high."""
    low  = _flat_daily(n_low,  price_low,  base_ts)
    high = _flat_daily(n_high, price_high, base_ts + n_low * 86_400)
    return low + high


# ---------------------------------------------------------------------------
# SMA
# ---------------------------------------------------------------------------

class TestSMA(unittest.TestCase):

    def test_window_equals_series(self):
        self.assertAlmostEqual(bcs.sma([1.0, 2.0, 3.0], 3)[2], 2.0)

    def test_prefix_filled_with_none(self):
        r = bcs.sma([1.0, 2.0, 3.0, 4.0], 3)
        self.assertIsNone(r[0])
        self.assertIsNone(r[1])

    def test_trailing_average_correct(self):
        r = bcs.sma([1.0, 2.0, 3.0, 4.0, 5.0], 3)
        self.assertAlmostEqual(r[2], 2.0)
        self.assertAlmostEqual(r[3], 3.0)
        self.assertAlmostEqual(r[4], 4.0)

    def test_window_of_one_is_identity(self):
        r = bcs.sma([5.0, 10.0, 15.0], 1)
        self.assertEqual(r, [5.0, 10.0, 15.0])

    def test_constant_series(self):
        r = bcs.sma([7.0] * 10, 5)
        for v in r[4:]:
            self.assertAlmostEqual(v, 7.0)

    def test_insufficient_data_all_none(self):
        r = bcs.sma([1.0, 2.0], 5)
        # sma always produces n-1 leading Nones; when series < n no valid values exist
        self.assertTrue(all(v is None for v in r))

    def test_single_element(self):
        self.assertAlmostEqual(bcs.sma([42.0], 1)[0], 42.0)


# ---------------------------------------------------------------------------
# days_since_last_halving
# ---------------------------------------------------------------------------

class TestDaysSinceHalving(unittest.TestCase):

    def test_day_of_h4_is_zero(self):
        self.assertEqual(bcs.days_since_last_halving(_ts("2024-04-20")), 0)

    def test_day_of_h3_is_zero(self):
        self.assertEqual(bcs.days_since_last_halving(_ts("2020-05-11")), 0)

    def test_one_year_after_h4(self):
        d = bcs.days_since_last_halving(_ts("2025-04-20"))
        self.assertIn(d, (365, 366))

    def test_before_all_halvings_is_zero(self):
        self.assertEqual(bcs.days_since_last_halving(_ts("2010-01-01")), 0)

    def test_between_h3_and_h4_references_h3(self):
        # 2022-05-11 is exactly 2 years after H3 (2020-05-11)
        d = bcs.days_since_last_halving(_ts("2022-05-11"))
        self.assertIn(d, (730, 731))

    def test_t1_threshold_400d_after_h4(self):
        ts = _ts("2024-04-20") + 400 * 86_400
        self.assertGreaterEqual(bcs.days_since_last_halving(ts), 400)

    def test_t2_threshold_480d_after_h4(self):
        ts = _ts("2024-04-20") + 480 * 86_400
        self.assertGreaterEqual(bcs.days_since_last_halving(ts), 480)

    def test_result_is_int(self):
        self.assertIsInstance(bcs.days_since_last_halving(_ts("2025-01-01")), int)


# ---------------------------------------------------------------------------
# Prudence defaults
# ---------------------------------------------------------------------------

class TestPrudenceDefaults(unittest.TestCase):

    def test_prudence_off_by_default(self):
        self.assertFalse(bcs.DEFAULTS["prudence"])

    def test_t1_days(self):
        self.assertEqual(bcs.DEFAULTS["prudence_t1_days"], 400)

    def test_t1_target(self):
        self.assertAlmostEqual(bcs.DEFAULTS["prudence_t1_target"], 0.75)

    def test_t1_rebound(self):
        self.assertAlmostEqual(bcs.DEFAULTS["prudence_t1_rebound"], 0.03)

    def test_t1_tranche(self):
        self.assertAlmostEqual(bcs.DEFAULTS["prudence_t1_tranche"], 0.15)

    def test_t2_days(self):
        self.assertEqual(bcs.DEFAULTS["prudence_t2_days"], 480)

    def test_t2_target(self):
        self.assertAlmostEqual(bcs.DEFAULTS["prudence_t2_target"], 0.50)

    def test_t2_rebound(self):
        self.assertAlmostEqual(bcs.DEFAULTS["prudence_t2_rebound"], 0.02)

    def test_t2_tranche(self):
        self.assertAlmostEqual(bcs.DEFAULTS["prudence_t2_tranche"], 0.10)

    def test_t2_days_greater_than_t1(self):
        self.assertGreater(bcs.DEFAULTS["prudence_t2_days"],
                           bcs.DEFAULTS["prudence_t1_days"])

    def test_t2_target_less_than_t1(self):
        self.assertLess(bcs.DEFAULTS["prudence_t2_target"],
                        bcs.DEFAULTS["prudence_t1_target"])


# ---------------------------------------------------------------------------
# run_backtest — synthetic daily data
# ---------------------------------------------------------------------------

class TestRunBacktest(unittest.TestCase):

    def _p(self, **overrides):
        p = dict(bcs.DEFAULTS)
        p.update(overrides)
        return p

    def test_flat_price_no_regime_change(self):
        r = bcs.run_backtest(_flat_daily(400), self._p())
        self.assertEqual(len(r["regime_history"]), 0)

    def test_flat_price_no_trades(self):
        r = bcs.run_backtest(_flat_daily(300), self._p())
        self.assertEqual(len(r["trades"]), 0)

    def test_flat_price_no_fees(self):
        r = bcs.run_backtest(_flat_daily(300), self._p())
        self.assertAlmostEqual(r["fees_paid"], 0.0, places=6)

    def test_flat_price_pv_equals_capital(self):
        p = self._p()
        r = bcs.run_backtest(_flat_daily(300), p)
        self.assertAlmostEqual(r["final_pv"], p["capital"], delta=1.0)

    def test_years_computation(self):
        r = bcs.run_backtest(_flat_daily(366), self._p())
        self.assertAlmostEqual(r["years"], 365 / 365.25, places=2)

    def test_double_price_doubles_btc_hold(self):
        daily = _ramp_daily(300, 10_000.0, 100, 20_000.0)
        p = self._p()
        r = bcs.run_backtest(daily, p)
        self.assertAlmostEqual(r["btc_hold_pv"], p["capital"] * 2.0, delta=200.0)

    def test_mayer_spike_triggers_bear(self):
        # 250 days at $10K → 200DMA ≈ $10K
        # then spike to $25K → MM = 2.5 > 2.4 threshold after min_bull_days=200
        daily = _ramp_daily(250, 10_000.0, 50, 25_000.0)
        p = self._p(min_bull_days=200, top_mm_thresh=2.4)
        r = bcs.run_backtest(daily, p)
        bear_events = [e for e in r["regime_history"] if e["regime"] == "bear"]
        self.assertGreater(len(bear_events), 0)

    def test_bear_transition_price_is_spike_price(self):
        daily = _ramp_daily(250, 10_000.0, 50, 25_000.0)
        p = self._p(min_bull_days=200, top_mm_thresh=2.4)
        r = bcs.run_backtest(daily, p)
        bear_events = [e for e in r["regime_history"] if e["regime"] == "bear"]
        if bear_events:
            self.assertAlmostEqual(bear_events[0]["price"], 25_000.0, delta=1.0)

    def test_max_drawdown_zero_with_flat_price(self):
        r = bcs.run_backtest(_flat_daily(300), self._p())
        self.assertAlmostEqual(r["max_dd"], 0.0, places=6)

    def test_start_end_dates_present(self):
        daily = _flat_daily(100)
        r = bcs.run_backtest(daily, self._p())
        self.assertEqual(r["start_date"], daily[0]["date"])
        self.assertEqual(r["end_date"],   daily[-1]["date"])

    def test_final_btc_frac_within_zero_one(self):
        r = bcs.run_backtest(_flat_daily(400), self._p())
        self.assertGreaterEqual(r["final_btc_frac"], 0.0)
        self.assertLessEqual(   r["final_btc_frac"], 1.0)

    def test_prudence_false_same_as_default_for_flat(self):
        p = self._p(prudence=False)
        r = bcs.run_backtest(_flat_daily(600), p)
        self.assertEqual(len(r["trades"]), 0)


# ---------------------------------------------------------------------------
# load_daily — in-memory SQLite with `daily` table
# ---------------------------------------------------------------------------

class TestLoadDailyFromDailyDB(unittest.TestCase):

    def _make_db(self, rows):
        conn = sqlite3.connect(":memory:")
        conn.execute("""CREATE TABLE daily (
            day_ts INTEGER PRIMARY KEY,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, source TEXT
        )""")
        conn.executemany(
            "INSERT INTO daily VALUES (?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        return conn

    def test_load_returns_list(self):
        conn = self._make_db([
            (1_577_836_800, 9_000.0, 9_500.0, 8_800.0, 9_200.0, 100.0, "test"),
            (1_577_923_200, 9_200.0, 9_800.0, 9_100.0, 9_700.0, 120.0, "test"),
        ])
        # Patch sqlite3.connect to return our in-memory conn
        import unittest.mock as mock
        with mock.patch("sqlite3.connect", return_value=conn):
            result = bcs.load_daily(":memory:")
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)

    def test_load_fields_correct(self):
        conn = self._make_db([
            (1_577_836_800, 9_000.0, 9_500.0, 8_800.0, 9_200.0, 100.0, "test"),
        ])
        import unittest.mock as mock
        with mock.patch("sqlite3.connect", return_value=conn):
            result = bcs.load_daily(":memory:")
        row = result[0]
        self.assertEqual(row["ts"],    1_577_836_800)
        self.assertAlmostEqual(row["high"],  9_500.0)
        self.assertAlmostEqual(row["low"],   8_800.0)
        self.assertAlmostEqual(row["close"], 9_200.0)
        self.assertIn("date", row)

    def test_load_sorted_by_ts(self):
        conn = self._make_db([
            (1_577_923_200, 9_200.0, 9_800.0, 9_100.0, 9_700.0, 120.0, "test"),
            (1_577_836_800, 9_000.0, 9_500.0, 8_800.0, 9_200.0, 100.0, "test"),
        ])
        import unittest.mock as mock
        with mock.patch("sqlite3.connect", return_value=conn):
            result = bcs.load_daily(":memory:")
        self.assertLess(result[0]["ts"], result[1]["ts"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
