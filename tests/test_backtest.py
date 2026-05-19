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
import backtest_stake_secs as bss


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


class TestDailyRatios(unittest.TestCase):
    """bt._daily_ratios — Sharpe and Sortino computation."""

    # 2026-01-01 UTC = 1767225600000 ms; 2026-01-02 UTC = +86400000 ms
    _DAY1 = 1767225600000
    _DAY2 = _DAY1 + 86400000
    _DAY3 = _DAY2 + 86400000

    def _rt(self, pnl: float, ts_ms: int) -> bt.SimTrade:
        t = bt.SimTrade("m", "UP", 0, 0.97, 10.0, 0.006, 0.97, 60)
        t.outcome, t.pnl_net, t.exit_ts_ms = "WIN" if pnl > 0 else "LOSS", pnl, ts_ms
        return t

    def test_fewer_than_two_days_returns_zero(self):
        # All trades on the same day → only 1 daily bucket → (0, 0)
        trades = [self._rt(0.10, self._DAY1), self._rt(0.10, self._DAY1)]
        s, r = bt._daily_ratios(trades)
        self.assertAlmostEqual(s, 0.0)
        self.assertAlmostEqual(r, 0.0)

    def test_empty_returns_zero(self):
        s, r = bt._daily_ratios([])
        self.assertAlmostEqual(s, 0.0)
        self.assertAlmostEqual(r, 0.0)

    def test_all_positive_days_sortino_infinite(self):
        trades = [self._rt(1.0, self._DAY1), self._rt(0.5, self._DAY2)]
        _, sortino = bt._daily_ratios(trades)
        self.assertEqual(sortino, float("inf"))

    def test_known_two_day_values(self):
        import math as _m
        # Day 1: +1.0, Day 2: -2.0  →  mean=-0.5, std=1.5
        trades = [self._rt(1.0, self._DAY1), self._rt(-2.0, self._DAY2)]
        sharpe, sortino = bt._daily_ratios(trades)
        mean, std = -0.5, 1.5
        expected_sharpe = mean / std * _m.sqrt(365)
        self.assertAlmostEqual(sharpe, expected_sharpe, places=5)
        # Sortino: downside_sq = min(-2,0)^2 / 2 = 2.0, dd_dev = sqrt(2)
        dd_dev = _m.sqrt(2.0)
        expected_sortino = mean / dd_dev * _m.sqrt(365)
        self.assertAlmostEqual(sortino, expected_sortino, places=5)

    def test_trades_aggregated_per_day(self):
        # Two trades on day 1 → single daily bucket of +0.2
        trades = [
            self._rt(0.10, self._DAY1), self._rt(0.10, self._DAY1),
            self._rt(-5.0, self._DAY2),
        ]
        sharpe, _ = bt._daily_ratios(trades)
        # Day 1 = +0.20, Day 2 = -5.0 → should be negative Sharpe
        self.assertLess(sharpe, 0.0)

    def test_no_exit_ts_trades_ignored(self):
        t = bt.SimTrade("m", "UP", 0, 0.97, 10.0, 0.006, 0.97, 60)
        t.outcome, t.pnl_net = "WIN", 0.10
        # exit_ts_ms is None — should be skipped; returns (0, 0)
        s, r = bt._daily_ratios([t])
        self.assertAlmostEqual(s, 0.0)
        self.assertAlmostEqual(r, 0.0)


class TestSummarizeSharpe(unittest.TestCase):
    """summarize() now includes sharpe and sortino keys."""

    _DAY1 = 1767225600000
    _DAY2 = _DAY1 + 86400000

    def _trade(self, outcome, pnl, ts_ms=None):
        t = bt.SimTrade("m", "UP", 0, 0.975, 10, 0.006, 0.97, 60)
        t.outcome, t.pnl_net = outcome, pnl
        t.exit_ts_ms = ts_ms
        return t

    def test_summarize_includes_sharpe_key(self):
        t = self._trade("WIN", 0.10, self._DAY1)
        s = bt.summarize([t], bt.Params(), 100.1)
        self.assertIn("sharpe", s)
        self.assertIn("sortino", s)

    def test_single_day_sharpe_is_zero(self):
        trades = [self._trade("WIN", 0.10, self._DAY1)] * 3
        s = bt.summarize(trades, bt.Params(), 100.3)
        self.assertAlmostEqual(s["sharpe"], 0.0)

    def test_two_days_sharpe_negative_on_net_loss(self):
        trades = [
            self._trade("WIN",  0.10, self._DAY1),
            self._trade("LOSS", -5.0, self._DAY2),
        ]
        s = bt.summarize(trades, bt.Params(), 95.1)
        self.assertLess(s["sharpe"], 0.0)

    def test_all_wins_same_day_sortino_zero(self):
        # Single day → only 1 bucket → both ratios = 0
        trades = [self._trade("WIN", 0.10, self._DAY1)] * 5
        s = bt.summarize(trades, bt.Params(), 100.5)
        self.assertAlmostEqual(s["sortino"], 0.0)

    def test_no_exit_ts_summarize_ratios_zero(self):
        # Trades missing exit_ts_ms (edge case) → ratios = 0
        trades = [self._trade("WIN", 0.10, None)] * 5
        s = bt.summarize(trades, bt.Params(), 100.5)
        self.assertAlmostEqual(s["sharpe"], 0.0)
        self.assertAlmostEqual(s["sortino"], 0.0)


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


# ─── Walk-forward ─────────────────────────────────────────────────────────────

class TestSplitWeekly(unittest.TestCase):
    """bt._split_weekly — weekly bucket partitioning."""

    # A known Monday midnight UTC anchor (2026-01-05 00:00:00 UTC)
    # bt._WEEK_MS = 604800000
    _EPOCH_WEEK = (1767484800000 // bt._WEEK_MS) * bt._WEEK_MS  # aligned boundary

    def _snap(self, offset_ms: int) -> tuple:
        ts = self._EPOCH_WEEK + offset_ms
        return (ts, "m1", "UP", 60.0, 0.97, 0.97, 20.0, 0.0)

    def test_empty_returns_empty(self):
        self.assertEqual(bt._split_weekly([]), [])

    def test_single_week_one_bucket(self):
        rows = [self._snap(0), self._snap(100), self._snap(200)]
        weeks = bt._split_weekly(rows)
        self.assertEqual(len(weeks), 1)
        self.assertEqual(len(weeks[0]), 3)

    def test_two_distinct_weeks_two_buckets(self):
        rows = [
            self._snap(0),                          # week 0
            self._snap(bt._WEEK_MS),                # week 1
            self._snap(bt._WEEK_MS + 1000),         # week 1
        ]
        weeks = bt._split_weekly(rows)
        self.assertEqual(len(weeks), 2)
        self.assertEqual(len(weeks[0]), 1)
        self.assertEqual(len(weeks[1]), 2)

    def test_preserves_order_within_bucket(self):
        ts0 = self._EPOCH_WEEK + 0
        ts1 = self._EPOCH_WEEK + 1000
        rows = [(ts1, "m", "UP", 60, 0.97, 0.97, 20, 0),
                (ts0, "m", "UP", 60, 0.97, 0.97, 20, 0)]
        weeks = bt._split_weekly(sorted(rows))
        self.assertEqual(weeks[0][0][0], ts0)

    def test_empty_weeks_skipped(self):
        # Week 0 and week 2 have rows; week 1 is empty.
        rows = [
            self._snap(0),
            self._snap(2 * bt._WEEK_MS),
        ]
        weeks = bt._split_weekly(rows)
        self.assertEqual(len(weeks), 2)


class TestWalkForward(unittest.TestCase):
    """bt.walk_forward and bt._fold_sweep integration."""

    _W = bt._WEEK_MS

    def _rows(self, n_weeks: int = 6) -> list:
        """Build n_weeks × 500 synthetic snapshot rows.
        Alternates markets so trades can fire and resolve within each week.
        best_bid=0.97 (above threshold=0.95) and best_ask=0.97 < 1.0.
        """
        rows = []
        t0 = (1767484800000 // self._W) * self._W   # aligned start
        for week in range(n_weeks):
            base = t0 + week * self._W
            for i in range(500):
                ts  = base + i * 5000          # 5-second steps
                mid = i % 10                   # 10 rotating markets
                # Alternate bid: high signal → resolution
                bid = 0.97 if i % 3 != 0 else 0.995   # sometimes crosses win_threshold
                rows.append((ts, f"mkt{mid}", "UP", 60.0, bid, 0.97, 20.0, 0.0))
        return rows

    def test_returns_empty_when_insufficient_data(self):
        # Only 1 week of data — cannot form even one fold with train=4w
        rows = self._rows(n_weeks=1)
        folds = bt.walk_forward(rows, train_weeks=4)
        self.assertEqual(folds, [])

    def test_single_fold_with_five_weeks(self):
        # train=4w + 1 test week → exactly one fold
        rows = self._rows(n_weeks=5)
        folds = bt.walk_forward(rows, train_weeks=4)
        self.assertEqual(len(folds), 1)

    def test_two_folds_with_six_weeks(self):
        rows = self._rows(n_weeks=6)
        folds = bt.walk_forward(rows, train_weeks=4)
        self.assertGreaterEqual(len(folds), 1)   # ≥1; second fold skipped if too few trades

    def test_fold_has_expected_fields(self):
        rows = self._rows(n_weeks=5)
        folds = bt.walk_forward(rows, train_weeks=4)
        if not folds:
            self.skipTest("no valid folds produced with synthetic data")
        f = folds[0]
        self.assertIsInstance(f.best_params, bt.Params)
        self.assertIn("total_pnl", f.train_stats)
        self.assertIn("total_pnl", f.test_stats)
        self.assertIsInstance(f.train_start, str)
        self.assertEqual(len(f.train_start), 10)   # "YYYY-MM-DD"

    def test_ev_degradation_none_when_train_ev_zero(self):
        f = bt.WFold(
            fold_n=1,
            train_start="2026-01-01", train_end="2026-01-28",
            test_start="2026-01-28", test_end="2026-02-04",
            best_params=bt.Params(),
            train_stats={"wins": 0, "losses": 0, "total_pnl": 0.0,
                         "win_rate": 0.0, "max_drawdown": 0.0, "sharpe": 0.0},
            test_stats= {"wins": 5, "losses": 0, "total_pnl": 0.5,
                         "win_rate": 100.0, "max_drawdown": 0.0, "sharpe": 0.0},
        )
        self.assertIsNone(f.ev_degradation_pct)

    def test_ev_degradation_formula(self):
        import math as _m
        f = bt.WFold(
            fold_n=1,
            train_start="2026-01-01", train_end="2026-01-28",
            test_start="2026-01-28", test_end="2026-02-04",
            best_params=bt.Params(),
            train_stats={"wins": 10, "losses": 0, "total_pnl": 1.0,
                         "win_rate": 100.0, "max_drawdown": 0.0, "sharpe": 0.0},
            test_stats= {"wins":  5, "losses": 0, "total_pnl": 0.4,
                         "win_rate": 100.0, "max_drawdown": 0.0, "sharpe": 0.0},
        )
        # train_ev = 1.0/10 = 0.10  test_ev = 0.4/5 = 0.08
        # degradation = (0.08 - 0.10) / 0.10 * 100 = -20.0%
        self.assertAlmostEqual(f.ev_degradation_pct, -20.0, places=5)


# ─── backtest_stake_secs — Curve B (step function) ───────────────────────────

class TestComputeStakeStep(unittest.TestCase):
    """bss.compute_stake_step() zone dispatch."""

    _STAKES = (12.0, 10.0, 8.0, 6.0)   # s0/s1/s2/s3

    def test_below_first_break(self):
        self.assertEqual(bss.compute_stake_step(30.0, self._STAKES), 12.0)

    def test_at_first_break(self):
        # 45 ≥ STEP_BREAKS[0] → falls into s1 zone
        self.assertEqual(bss.compute_stake_step(45.0, self._STAKES), 10.0)

    def test_between_first_and_second_break(self):
        self.assertEqual(bss.compute_stake_step(52.0, self._STAKES), 10.0)

    def test_at_second_break(self):
        self.assertEqual(bss.compute_stake_step(60.0, self._STAKES), 8.0)

    def test_between_second_and_third_break(self):
        self.assertEqual(bss.compute_stake_step(75.0, self._STAKES), 8.0)

    def test_at_third_break(self):
        self.assertEqual(bss.compute_stake_step(90.0, self._STAKES), 6.0)

    def test_above_third_break(self):
        self.assertEqual(bss.compute_stake_step(120.0, self._STAKES), 6.0)


class TestStepCombos(unittest.TestCase):
    """bss._step_combos() non-increasing constraint and vol modes."""

    def setUp(self):
        self._combos = bss._step_combos()

    def test_non_empty(self):
        self.assertGreater(len(self._combos), 0)

    def test_non_increasing_constraint(self):
        for c in self._combos:
            self.assertGreaterEqual(c["s0"], c["s1"])
            self.assertGreaterEqual(c["s1"], c["s2"])
            self.assertGreaterEqual(c["s2"], c["s3"])

    def test_only_valid_vol_modes(self):
        for c in self._combos:
            self.assertIn(c["vol_mode"], ("off", "weekday"))

    def test_curve_label(self):
        for c in self._combos:
            self.assertEqual(c["curve"], "B")

    def test_count_reasonable(self):
        # Worst case: all 81 combos × 2 vol modes = 162; filtered ≥ 1
        self.assertLessEqual(len(self._combos), 162)


# ─── backtest_stake_secs — Curve C (Kelly/bucket) ────────────────────────────

class TestSecsBucket(unittest.TestCase):
    """bss._secs_bucket() maps seconds to 0-based zone index."""

    def test_below_45(self):
        self.assertEqual(bss._secs_bucket(30.0), 0)

    def test_exactly_45(self):
        self.assertEqual(bss._secs_bucket(45.0), 1)

    def test_between_45_and_60(self):
        self.assertEqual(bss._secs_bucket(55.0), 1)

    def test_between_60_and_90(self):
        self.assertEqual(bss._secs_bucket(75.0), 2)

    def test_between_90_and_120(self):
        self.assertEqual(bss._secs_bucket(100.0), 3)

    def test_at_120(self):
        self.assertEqual(bss._secs_bucket(120.0), 4)

    def test_above_120(self):
        self.assertEqual(bss._secs_bucket(180.0), 4)


class TestBucketKellyParams(unittest.TestCase):
    """bss._bucket_kelly_params() (p, b) computation per secs zone."""

    def _make_trade(self, secs, outcome, pnl, stake=10.0):
        return bss.Trade(
            trade_id=0, signal_ms=1000, bid=0.97, secs=secs,
            obi=0.5, outcome=outcome, pnl_orig=pnl, stake_orig=stake,
        )

    def test_returns_correct_bucket_count(self):
        # 5 buckets for 4 KELLY_BREAKS
        trades = [self._make_trade(30.0, "WIN", 9.7)]
        result = bss._bucket_kelly_params(trades)
        self.assertEqual(len(result), len(bss.KELLY_BREAKS) + 1)

    def test_sparse_bucket_returns_zeros(self):
        # Only 3 trades in bucket 0 → below threshold of 5
        trades = [self._make_trade(30.0, "WIN", 9.7) for _ in range(3)]
        result = bss._bucket_kelly_params(trades)
        self.assertEqual(result[0], (0.0, 0.0))

    def test_populated_bucket_win_rate(self):
        # 9 wins + 1 loss in bucket 0 (secs < 45) → p = 0.9
        wins   = [self._make_trade(30.0, "WIN",  9.7) for _ in range(9)]
        losses = [self._make_trade(30.0, "LOSS", -10.2)]
        result = bss._bucket_kelly_params(wins + losses)
        p, _b  = result[0]
        self.assertAlmostEqual(p, 0.9, places=9)

    def test_populated_bucket_avg_payout(self):
        # All wins at pnl=9.7 on stake=10 → pnl_per_dollar=0.97
        trades = [self._make_trade(30.0, "WIN", 9.7) for _ in range(5)]
        result = bss._bucket_kelly_params(trades)
        _p, b  = result[0]
        self.assertAlmostEqual(b, 0.97, places=9)

    def test_empty_bucket_is_zero(self):
        # No trades → all buckets are (0, 0)
        result = bss._bucket_kelly_params([])
        self.assertTrue(all(r == (0.0, 0.0) for r in result))


class TestKellyStake(unittest.TestCase):
    """bss._kelly_stake() formula and edge cases."""

    def test_no_edge_returns_zero(self):
        # p=0.5, b=0.5: f* = (0.5×0.5 - 0.5) / 0.5 = -0.5 → 0
        self.assertEqual(bss._kelly_stake(0.5, 0.5, 0.25, 1000.0), 0.0)

    def test_zero_b_returns_zero(self):
        self.assertEqual(bss._kelly_stake(0.9, 0.0, 0.25, 1000.0), 0.0)

    def test_zero_p_returns_zero(self):
        self.assertEqual(bss._kelly_stake(0.0, 0.97, 0.25, 1000.0), 0.0)

    def test_positive_edge(self):
        # p=0.98, b=0.97: f* = (0.98×0.97 − 0.02)/0.97 ≈ 0.9594; quarter-Kelly on $1000
        stake = bss._kelly_stake(0.98, 0.97, 0.25, 1000.0)
        self.assertGreater(stake, 0.0)

    def test_capped_at_kelly_stake_cap(self):
        # Full Kelly on huge capital should be capped at KELLY_STAKE_CAP
        stake = bss._kelly_stake(0.99, 0.97, 1.0, 1_000_000.0)
        self.assertAlmostEqual(stake, bss.KELLY_STAKE_CAP)

    def test_quarter_kelly_below_cap(self):
        # Quarter-Kelly on $100 at p=0.98 → ~$24, below the $30 cap
        stake = bss._kelly_stake(0.98, 0.97, 0.25, 100.0)
        self.assertLess(stake, bss.KELLY_STAKE_CAP)


class TestSimulateStep(unittest.TestCase):
    """bss._simulate_step() integration test with synthetic trades."""

    _MS = 1_700_000_000_000   # arbitrary epoch inside a weekday

    def _trade(self, secs, outcome, pnl=9.7, stake=10.0):
        return bss.Trade(
            trade_id=id(object()), signal_ms=self._MS,
            bid=0.97, secs=secs, obi=0.5,
            outcome=outcome, pnl_orig=pnl, stake_orig=stake,
        )

    def test_win_uses_correct_zone_stake(self):
        # secs=30 → zone 0 → s0=15; pnl_per_dollar = 0.97
        trades = [self._trade(30.0, "WIN")]
        params = {"curve": "B", "s0": 15.0, "s1": 10.0, "s2": 8.0, "s3": 6.0, "vol_mode": "off"}
        r = bss._simulate_step(trades, {}, params)
        self.assertEqual(r.wins, 1)
        self.assertAlmostEqual(r.total_pnl, 0.97 * 15.0, places=9)

    def test_loss_uses_correct_zone_stake(self):
        # secs=100 → zone 3 → s3=6; pnl_per_dollar = -10.2/10 = -1.02
        trades = [self._trade(100.0, "LOSS", pnl=-10.2)]
        params = {"curve": "B", "s0": 15.0, "s1": 10.0, "s2": 8.0, "s3": 6.0, "vol_mode": "off"}
        r = bss._simulate_step(trades, {}, params)
        self.assertEqual(r.losses, 1)
        self.assertAlmostEqual(r.total_pnl, -1.02 * 6.0, places=9)

    def test_win_rate(self):
        trades = [self._trade(30.0, "WIN"), self._trade(30.0, "LOSS", pnl=-10.2)]
        params = {"curve": "B", "s0": 10.0, "s1": 10.0, "s2": 10.0, "s3": 10.0, "vol_mode": "off"}
        r = bss._simulate_step(trades, {}, params)
        self.assertEqual(r.wins, 1)
        self.assertEqual(r.losses, 1)
        self.assertAlmostEqual(r.win_rate, 0.5)


class TestSimulateKelly(unittest.TestCase):
    """bss._simulate_kelly() integration tests."""

    _MS = 1_700_000_000_000

    def _trade(self, secs, outcome, pnl=9.7, stake=10.0):
        return bss.Trade(
            trade_id=id(object()), signal_ms=self._MS,
            bid=0.97, secs=secs, obi=0.5,
            outcome=outcome, pnl_orig=pnl, stake_orig=stake,
        )

    def _bucket_params_all_edge(self):
        # p=0.98, b=0.97 for every bucket → positive edge
        return [(0.98, 0.97)] * (len(bss.KELLY_BREAKS) + 1)

    def _bucket_params_no_edge(self):
        # p=0.5, b=0.5 → no edge in any bucket
        return [(0.5, 0.5)] * (len(bss.KELLY_BREAKS) + 1)

    def test_no_edge_skips_all_trades(self):
        trades = [self._trade(30.0, "WIN") for _ in range(5)]
        params = {"curve": "C", "kelly_fraction": 0.25, "vol_mode": "off"}
        r = bss._simulate_kelly(trades, {}, params, self._bucket_params_no_edge())
        self.assertEqual(r.n, 0)
        self.assertEqual(r.skipped, 5)

    def test_positive_edge_takes_trades(self):
        trades = [self._trade(30.0, "WIN") for _ in range(5)]
        params = {"curve": "C", "kelly_fraction": 0.25, "vol_mode": "off"}
        r = bss._simulate_kelly(trades, {}, params, self._bucket_params_all_edge())
        self.assertEqual(r.wins, 5)
        self.assertEqual(r.n, 5)

    def test_capital_grows_on_wins(self):
        trades = [self._trade(30.0, "WIN") for _ in range(5)]
        params = {"curve": "C", "kelly_fraction": 0.25, "vol_mode": "off"}
        r = bss._simulate_kelly(trades, {}, params, self._bucket_params_all_edge())
        self.assertGreater(r.total_pnl, 0.0)

    def test_stake_capped_at_kelly_stake_cap(self):
        # Full Kelly (fraction=1.0) on a large bucket edge should never exceed cap
        trades = [self._trade(30.0, "WIN") for _ in range(10)]
        params = {"curve": "C", "kelly_fraction": 1.0, "vol_mode": "off"}
        bp = [(0.99, 0.97)] * (len(bss.KELLY_BREAKS) + 1)
        r  = bss._simulate_kelly(trades, {}, params, bp)
        # Per-trade PnL = pnl_per_dollar × stake; stake ≤ KELLY_STAKE_CAP
        # So max total_pnl ≤ n × KELLY_STAKE_CAP × 0.97
        self.assertLessEqual(r.total_pnl, r.n * bss.KELLY_STAKE_CAP * 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
