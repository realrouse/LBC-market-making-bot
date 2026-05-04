"""
Tests for scripts/backtest.py

Builds synthetic snapshot rows in memory to exercise the replay engine
without requiring a real live.db.
"""

import os, sys, time, unittest
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
