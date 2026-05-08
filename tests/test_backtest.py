"""
Tests for scripts/backtest.py

Builds synthetic snapshot rows in memory to exercise the replay engine
without requiring a real live.db.
"""

import os, sys, sqlite3, time, unittest
from unittest.mock import patch
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import backtest as bt


# ─── Snapshot row builder ─────────────────────────────────────────────────────

def snap(
    market_id="mkt1",
    direction="UP",
    secs_remaining=60.0,
    best_bid=0.50,
    best_ask=0.51,
    ask_vol=50.0,
    obi=0.0,
    ts_ms=None,
):
    """Return a single snapshot row tuple in the format load_rows() produces."""
    if ts_ms is None:
        ts_ms = int(time.time() * 1000)
    return (ts_ms, market_id, direction, secs_remaining, best_bid, best_ask, ask_vol, obi)


def seq(market_id="mkt1", direction="UP", n=10, start_bid=0.50,
        start_secs=300, ts_start=None):
    """
    Build a sequence of n snapshot rows for a single token, advancing time by
    5 seconds per row and secs_remaining by -5.
    """
    rows = []
    base = ts_start or int(time.time() * 1000)
    for i in range(n):
        rows.append(snap(
            market_id=market_id, direction=direction,
            secs_remaining=max(0, start_secs - i * 5),
            best_bid=start_bid,
            best_ask=start_bid + 0.01,
            ts_ms=base + i * 5000,
        ))
    return rows


# ─── compute_fee ─────────────────────────────────────────────────────────────

class TestFeeHelper(unittest.TestCase):

    def test_known_value(self):
        # 0.02 × min(0.96, 0.04) × 10 = 0.008
        self.assertAlmostEqual(bt._fee(0.96, 10), 0.008)

    def test_symmetric(self):
        self.assertAlmostEqual(bt._fee(0.30, 100), bt._fee(0.70, 100))


# ─── run_backtest — basic cases ───────────────────────────────────────────────

class TestRunBacktestBasic(unittest.TestCase):

    def _params(self, **kw):
        return bt.Params(**kw)

    def test_no_rows_returns_empty(self):
        trades, capital = bt.run_backtest([], bt.Params())
        self.assertEqual(trades, [])
        self.assertAlmostEqual(capital, 100.0)

    def test_signal_fires_and_trade_recorded(self):
        # Bid spikes to 0.97 (above threshold 0.96), then resolves WIN at 0.99
        rows = [
            snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=1000),
            snap(best_bid=0.99, best_ask=0.995, secs_remaining=55, ts_ms=6000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].outcome, "WIN")

    def test_loss_detected(self):
        rows = [
            snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=1000),
            snap(best_bid=0.01, best_ask=0.02,  secs_remaining=50, ts_ms=6000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].outcome, "LOSS")

    def test_unresolved_trade_marked_open(self):
        # Signal fires but never resolves before data ends
        rows = seq(start_bid=0.97, n=5)
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].outcome, "OPEN")

    def test_no_signal_below_threshold(self):
        rows = seq(start_bid=0.94, n=10)  # below SIGNAL_THRESHOLD=0.96
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 0)

    def test_no_signal_ask_at_settlement(self):
        rows = [snap(best_bid=0.97, best_ask=1.0, secs_remaining=60)]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 0)

    def test_no_signal_insufficient_secs(self):
        rows = [snap(best_bid=0.97, best_ask=0.975, secs_remaining=29)]
        # MIN_SECS_REMAINING=30 by default
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 0)

    def test_no_signal_thin_ask_volume(self):
        rows = [snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, ask_vol=5.0)]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 0)

    def test_no_signal_obi_too_negative(self):
        rows = [snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, obi=-0.8)]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 0)

    def test_no_duplicate_entry_same_market(self):
        # Signal fires twice for the same market — only one trade should be entered
        rows = [
            snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=1000),
            snap(best_bid=0.97, best_ask=0.975, secs_remaining=55, ts_ms=6000),
            snap(best_bid=0.99, best_ask=0.995, secs_remaining=50, ts_ms=11000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 1)

    def test_win_increases_capital(self):
        rows = [
            snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=1000),
            snap(best_bid=0.99, best_ask=0.995, secs_remaining=55, ts_ms=6000),
        ]
        _, capital = bt.run_backtest(rows, bt.Params(capital_start=100.0))
        self.assertGreater(capital, 100.0)

    def test_loss_decreases_capital(self):
        rows = [
            snap(best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=1000),
            snap(best_bid=0.01, best_ask=0.02,  secs_remaining=55, ts_ms=6000),
        ]
        _, capital = bt.run_backtest(rows, bt.Params(capital_start=100.0))
        self.assertLess(capital, 100.0)


# ─── run_backtest — multi-market ─────────────────────────────────────────────

class TestRunBacktestMultiMarket(unittest.TestCase):

    def test_independent_markets_both_entered(self):
        # Two markets signalling at the same time → two trades
        now = int(time.time() * 1000)
        rows = [
            snap("mkt1", "UP",   best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now),
            snap("mkt2", "DOWN", best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now + 1),
            snap("mkt1", "UP",   best_bid=0.99, secs_remaining=55, ts_ms=now + 5000),
            snap("mkt2", "DOWN", best_bid=0.99, secs_remaining=55, ts_ms=now + 5001),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 2)
        self.assertTrue(all(t.outcome == "WIN" for t in trades))

    def test_opposite_direction_does_not_resolve_trade(self):
        # We enter UP, but only DOWN token data arrives after entry.
        # Trade should remain OPEN (unresolved).
        now = int(time.time() * 1000)
        rows = [
            snap("mkt1", "UP",   best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now),
            snap("mkt1", "DOWN", best_bid=0.99, secs_remaining=55, ts_ms=now + 5000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].outcome, "OPEN")  # DOWN price can't resolve UP trade

    def test_expiry_win_above_half(self):
        now = int(time.time() * 1000)
        rows = [
            snap("mkt1", "UP", best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now),
            snap("mkt1", "UP", best_bid=0.60, secs_remaining=0, ts_ms=now + 5000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(trades[0].outcome, "WIN")

    def test_expiry_loss_below_half(self):
        now = int(time.time() * 1000)
        rows = [
            snap("mkt1", "UP", best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now),
            snap("mkt1", "UP", best_bid=0.40, secs_remaining=0, ts_ms=now + 5000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params())
        self.assertEqual(trades[0].outcome, "LOSS")


# ─── run_backtest — daily stop-loss ──────────────────────────────────────────

class TestRunBacktestDailyStopLoss(unittest.TestCase):

    def test_blocked_after_daily_stop_loss(self):
        # Build rows across a real UTC day so the date grouping works
        today_start = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
        # First trade: fires and loses
        rows = [
            snap("mkt1", "UP", best_bid=0.97, best_ask=0.975, secs_remaining=60,
                 ts_ms=today_start + 1000),
            snap("mkt1", "UP", best_bid=0.01, secs_remaining=55,
                 ts_ms=today_start + 6000),
            # Second trade same day: daily PnL now < -stop_loss → must be blocked
            snap("mkt2", "UP", best_bid=0.97, best_ask=0.975, secs_remaining=60,
                 ts_ms=today_start + 10000),
        ]
        p = bt.Params(daily_stop_loss=5.0)  # tiny stop so one loss triggers it
        trades, _ = bt.run_backtest(rows, p)
        # Only the first (losing) trade should exist
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].outcome, "LOSS")


# ─── run_backtest — parameter sensitivity ────────────────────────────────────

class TestRunBacktestParams(unittest.TestCase):

    def _win_rows(self, market_id="mkt1", bid=0.97, ts_base=None):
        now = ts_base or int(time.time() * 1000)
        return [
            snap(market_id, "UP", best_bid=bid, best_ask=bid + 0.005,
                 secs_remaining=60, ts_ms=now),
            snap(market_id, "UP", best_bid=0.99, best_ask=0.995,
                 secs_remaining=55, ts_ms=now + 5000),
        ]

    def test_higher_threshold_fewer_trades(self):
        rows = self._win_rows(bid=0.96) + self._win_rows("mkt2", bid=0.96,
                                                         ts_base=int(time.time()*1000)+100)
        t_low,  _ = bt.run_backtest(rows, bt.Params(signal_threshold=0.95))
        t_high, _ = bt.run_backtest(rows, bt.Params(signal_threshold=0.97))
        self.assertGreaterEqual(len(t_low), len(t_high))

    def test_custom_win_threshold(self):
        # With win_threshold=0.995, a bid of 0.99 should NOT resolve as WIN
        now = int(time.time() * 1000)
        rows = [
            snap("mkt1", "UP", best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now),
            snap("mkt1", "UP", best_bid=0.99, secs_remaining=55, ts_ms=now + 5000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params(win_threshold=0.995))
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].outcome, "OPEN")  # 0.99 < 0.995

    def test_custom_loss_threshold(self):
        # With loss_threshold=0.05, a bid of 0.04 should resolve as LOSS
        now = int(time.time() * 1000)
        rows = [
            snap("mkt1", "UP", best_bid=0.97, best_ask=0.975, secs_remaining=60, ts_ms=now),
            snap("mkt1", "UP", best_bid=0.04, secs_remaining=55, ts_ms=now + 5000),
        ]
        trades, _ = bt.run_backtest(rows, bt.Params(loss_threshold=0.05))
        self.assertEqual(trades[0].outcome, "LOSS")


# ─── summarize ────────────────────────────────────────────────────────────────

class TestSummarize(unittest.TestCase):

    def _trade(self, outcome, pnl):
        t = bt.SimTrade("m", "UP", 0, 0.975, 10, 0.006, 0.97, 60)
        t.outcome, t.pnl_net = outcome, pnl
        return t

    def test_empty(self):
        s = bt.summarize([], bt.Params(), 100.0)
        self.assertEqual(s["total"], 0)
        self.assertAlmostEqual(s["win_rate"], 0.0)

    def test_all_wins(self):
        trades = [self._trade("WIN", 0.23) for _ in range(5)]
        s = bt.summarize(trades, bt.Params(), 101.15)
        self.assertEqual(s["wins"], 5)
        self.assertEqual(s["losses"], 0)
        self.assertAlmostEqual(s["win_rate"], 100.0)
        self.assertAlmostEqual(s["total_pnl"], 5 * 0.23)

    def test_mixed(self):
        trades = [self._trade("WIN", 0.23)] * 3 + [self._trade("LOSS", -10.03)]
        s = bt.summarize(trades, bt.Params(), 90.0)
        self.assertEqual(s["wins"], 3)
        self.assertEqual(s["losses"], 1)
        self.assertAlmostEqual(s["win_rate"], 75.0)

    def test_max_drawdown_zero_when_always_profitable(self):
        trades = [self._trade("WIN", 1.0) for _ in range(5)]
        s = bt.summarize(trades, bt.Params(), 105.0)
        self.assertAlmostEqual(s["max_drawdown"], 0.0)

    def test_max_drawdown_non_zero_after_loss(self):
        # WIN +1, LOSS -5, WIN +1 → drawdown of 5 after the loss
        trades = [
            self._trade("WIN",  1.0),
            self._trade("LOSS", -5.0),
            self._trade("WIN",  1.0),
        ]
        s = bt.summarize(trades, bt.Params(), 97.0)
        self.assertAlmostEqual(s["max_drawdown"], 5.0)

    def test_open_trades_counted_separately(self):
        t_open = bt.SimTrade("m", "UP", 0, 0.975, 10, 0.006, 0.97, 60)
        t_open.outcome = "OPEN"
        s = bt.summarize([t_open], bt.Params(), 100.0)
        self.assertEqual(s["open"], 1)
        self.assertEqual(s["total"], 1)
        self.assertEqual(s["wins"], 0)


class TestRatio(unittest.TestCase):
    """bt._ratio — PnL/MaxDD risk-adjusted ratio."""

    def test_positive_pnl_and_dd(self):
        self.assertAlmostEqual(bt._ratio(10.0, 4.0), 2.5)

    def test_zero_dd_positive_pnl_returns_inf(self):
        self.assertEqual(bt._ratio(5.0, 0.0), float("inf"))

    def test_zero_dd_zero_pnl_returns_zero(self):
        self.assertEqual(bt._ratio(0.0, 0.0), 0.0)

    def test_negative_pnl_positive_dd(self):
        self.assertAlmostEqual(bt._ratio(-3.0, 6.0), -0.5)

    def test_negative_dd_treated_as_zero(self):
        self.assertEqual(bt._ratio(1.0, -1.0), float("inf"))


def _make_snapshots_conn():
    """In-memory DB with a minimal snapshots table for _percentile tests."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE snapshots (val REAL)"
    )
    for v in [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]:
        conn.execute("INSERT INTO snapshots VALUES (?)", (v,))
    conn.commit()
    return conn


def _make_trades_conn(rows=None):
    """
    In-memory DB with a minimal trades table.
    rows: list of dicts with keys stake, resolved, outcome, pnl_net,
          signal_best_bid, signal_secs_remaining, capital_before, signal_ts_ms.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE trades (
            stake REAL, resolved INTEGER, outcome TEXT,
            pnl_net REAL, signal_best_bid REAL,
            signal_secs_remaining REAL, capital_before REAL,
            signal_ts_ms INTEGER
        )
    """)
    for r in (rows or []):
        conn.execute(
            "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?)",
            (r.get("stake", 10.0), r.get("resolved", 1), r.get("outcome", "WIN"),
             r.get("pnl_net", 0.23), r.get("signal_best_bid", 0.97),
             r.get("signal_secs_remaining", 60.0), r.get("capital_before", 100.0),
             r.get("signal_ts_ms", 1_700_000_000_000)),
        )
    conn.commit()
    return conn


class TestPercentile(unittest.TestCase):
    """bt._percentile — ORDER BY + OFFSET percentile over a numeric column."""

    def setUp(self):
        self.conn = _make_snapshots_conn()

    def tearDown(self):
        self.conn.close()

    def test_median_of_ten_values(self):
        # values 1..10, median (0.5) offset = 5 → value 6
        result = bt._percentile(self.conn, "val", "snapshots", "1=1", 0.5)
        self.assertEqual(result, 6)

    def test_fifth_percentile(self):
        # offset = int(0.05 * 10) = 0 → smallest value = 1
        result = bt._percentile(self.conn, "val", "snapshots", "1=1", 0.05)
        self.assertEqual(result, 1)

    def test_high_percentile_returns_large_value(self):
        # 0.9 → offset = int(0.9 * 10) = 9 → last value = 10
        result = bt._percentile(self.conn, "val", "snapshots", "1=1", 0.9)
        self.assertEqual(result, 10)

    def test_empty_table_returns_none(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t (x REAL)")
        conn.commit()
        self.assertIsNone(bt._percentile(conn, "x", "t", "1=1", 0.5))
        conn.close()

    def test_where_clause_filters_rows(self):
        result = bt._percentile(self.conn, "val", "snapshots", "val > 5", 0.0)
        self.assertGreater(result, 5)


class TestDetectActualParams(unittest.TestCase):
    """bt.detect_actual_params — infers bot runtime config from trades table."""

    def test_returns_none_when_no_trades_table(self):
        conn = sqlite3.connect(":memory:")
        self.assertIsNone(bt.detect_actual_params(conn))
        conn.close()

    def test_returns_none_when_empty(self):
        conn = _make_trades_conn([])
        self.assertIsNone(bt.detect_actual_params(conn))
        conn.close()

    def test_detects_modal_stake(self):
        rows = [{"stake": 10.0}] * 8 + [{"stake": 150.0}] * 2
        conn = _make_trades_conn(rows)
        p = bt.detect_actual_params(conn)
        self.assertIsNotNone(p)
        self.assertAlmostEqual(p["stake"], 10.0)
        conn.close()

    def test_threshold_rounded_down_to_0_01(self):
        # All bids at 0.975 → 5th pct = 0.975 → floor to 0.97
        rows = [{"signal_best_bid": 0.975}] * 20
        conn = _make_trades_conn(rows)
        p = bt.detect_actual_params(conn)
        self.assertIsNotNone(p)
        self.assertAlmostEqual(p["threshold"], 0.97)
        conn.close()

    def test_stops_and_ghosts_counted(self):
        rows = (
            [{"outcome": "WIN"}] * 5
            + [{"outcome": "STOP"}] * 2
            + [{"outcome": "GHOST"}] * 1
        )
        conn = _make_trades_conn(rows)
        p = bt.detect_actual_params(conn)
        self.assertEqual(p["stops"], 2)
        self.assertEqual(p["ghosts"], 1)
        conn.close()

    def test_capital_start_is_minimum_capital_before(self):
        rows = [
            {"capital_before": 100.0},
            {"capital_before": 883.0},
            {"capital_before": 500.0},
        ]
        conn = _make_trades_conn(rows)
        p = bt.detect_actual_params(conn)
        self.assertAlmostEqual(p["capital_start"], 100.0)
        conn.close()


class TestActualStats(unittest.TestCase):
    """bt._actual_stats — aggregate WIN/LOSS/STOP/GHOST from trades table."""

    def test_returns_none_when_no_trades_table(self):
        conn = sqlite3.connect(":memory:")
        self.assertIsNone(bt._actual_stats(conn))
        conn.close()

    def test_counts_outcomes(self):
        rows = (
            [{"outcome": "WIN",  "pnl_net":  0.5}] * 6
            + [{"outcome": "LOSS", "pnl_net": -5.0}] * 2
            + [{"outcome": "STOP", "pnl_net": -3.0}] * 1
            + [{"outcome": "GHOST","pnl_net":  0.0}] * 1
        )
        conn = _make_trades_conn(rows)
        s = bt._actual_stats(conn)
        self.assertIsNotNone(s)
        self.assertEqual(s["wins"],   6)
        self.assertEqual(s["losses"], 2)
        self.assertEqual(s["stops"],  1)
        self.assertEqual(s["ghosts"], 1)
        self.assertEqual(s["total"], 10)
        conn.close()

    def test_pnl_net_summed(self):
        rows = [{"outcome": "WIN", "pnl_net": 1.0}] * 3
        conn = _make_trades_conn(rows)
        s = bt._actual_stats(conn)
        self.assertAlmostEqual(s["total_pnl"], 3.0)
        conn.close()

    def test_empty_table_returns_none(self):
        conn = _make_trades_conn([])
        self.assertIsNone(bt._actual_stats(conn))
        conn.close()


class TestCollectDbs(unittest.TestCase):
    """bt._collect_dbs — resolves the ordered list of DB files to replay."""

    def test_explicit_db_args_returned_as_is(self):
        paths = ["/tmp/a.db", "/tmp/b.db"]
        result = bt._collect_dbs(paths, scan_all=False)
        self.assertEqual(result, paths)

    def test_explicit_args_ignores_scan_all(self):
        # scan_all is False; explicit args take priority
        paths = ["/tmp/explicit.db"]
        result = bt._collect_dbs(paths, scan_all=False)
        self.assertEqual(result, ["/tmp/explicit.db"])

    def test_scan_all_with_nonexistent_data_dir_returns_empty_or_live(self):
        # When data/ doesn't exist and live.db is absent we get sample or [].
        with patch.object(bt, "_data_dir", "/nonexistent_dir_xyz"):
            with patch.object(bt, "_live_db", "/nonexistent_live.db"):
                result = bt._collect_dbs(None, scan_all=True)
        # Result should be a list (may be empty or fallback to sample).
        self.assertIsInstance(result, list)

    def test_empty_db_args_falls_back_to_sample_when_no_defaults(self):
        with patch.object(bt, "_live_db", "/nonexistent_live.db"):
            with patch.object(bt, "_paper3_db", "/nonexistent_paper3.db"):
                result = bt._collect_dbs(None, scan_all=False)
        # Falls back to the bundled sample DB.
        self.assertTrue(len(result) >= 1)
        self.assertTrue(result[-1].endswith(".db"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
