"""
Regression tests — two concerns:

1. Performance regression (TestBacktestPerformance)
   Run the backtest engine on data/paper3.db with default Params and assert
   that win rate, PnL, and drawdown stay within known-good bounds.
   If a code change silently degrades strategy performance, this test fails.
   Skipped automatically when paper3.db is absent (CI without data/).

2. Backtest ↔ bot live consistency (TestParamConsistency)
   The critical trading parameters are defined in two places:
     - bot/live_bot.py       (module-level constants)
     - analysis/backtest.py   (Params dataclass defaults)
   If they diverge, backtests no longer predict live performance.
   These tests enforce that both files always agree on every shared parameter.
"""

import os, sqlite3, sys, unittest

# ── path setup ────────────────────────────────────────────────────────────────
_ROOT      = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_BOT_DIR   = os.path.join(_ROOT, "tradinebotte-polymarket")
_SCR_DIR   = os.path.join(_ROOT, "analysis")
_DATA_DIR  = os.path.join(_ROOT, "data")

sys.path.insert(0, _BOT_DIR)
sys.path.insert(0, _SCR_DIR)

os.environ.setdefault("TRADINEBOTTE_DIR",
                      os.path.join(os.path.expanduser("~"), "tmp", "tradinebotte-test"))

import live_bot  as bot
import backtest  as bt
import api_polymarket as api_poly

_PAPER3_DB = os.path.join(_DATA_DIR, "paper3.db")


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_rows(db_path: str):
    """Load snapshots from a DB file in the order run_backtest expects."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ts_ms, market_id, direction, secs_remaining, "
        "best_bid, best_ask, ask_vol, obi "
        "FROM snapshots ORDER BY ts_ms"
    ).fetchall()
    conn.close()
    return rows


# ── 1. Performance regression ─────────────────────────────────────────────────

@unittest.skipUnless(os.path.exists(_PAPER3_DB), "data/paper3.db absent — skipped")
class TestBacktestPerformance(unittest.TestCase):
    """
    Lock known-good results on paper3.db (764 399 snapshots, default Params).
    Thresholds are set with ~1% slack so intentional minor param changes don't
    trip the gate, but real regressions do.

    Reference run (2026-05-06):
      trades=2856  wins=2808  losses=30  open=18
      win_rate=98.9%  pnl=+$89.13  max_dd=$80.69  capital=$189.13
    """

    @classmethod
    def setUpClass(cls):
        rows = _load_rows(_PAPER3_DB)
        cls.params = bt.Params()
        cls.trades, cls.capital = bt.run_backtest(rows, cls.params)
        cls.stats = bt.summarize(cls.trades, cls.params, cls.capital)

    # ── snapshot count sanity ─────────────────────────────────────────────────

    def test_snapshot_count_plausible(self):
        conn = sqlite3.connect(_PAPER3_DB)
        n = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        conn.close()
        self.assertGreater(n, 700_000, "paper3.db snapshot count dropped unexpectedly")

    # ── trade counts ──────────────────────────────────────────────────────────

    def test_win_rate_above_98_percent(self):
        self.assertGreaterEqual(
            self.stats["win_rate"], 98.0,
            f"Win rate regression: {self.stats['win_rate']:.2f}% < 98.0%",
        )

    def test_total_pnl_above_80(self):
        self.assertGreaterEqual(
            self.stats["total_pnl"], 80.0,
            f"PnL regression: ${self.stats['total_pnl']:.2f} < $80.00",
        )

    def test_losses_below_50(self):
        self.assertLessEqual(
            self.stats["losses"], 50,
            f"Loss count regression: {self.stats['losses']} > 50",
        )

    def test_wins_above_2700(self):
        self.assertGreaterEqual(
            self.stats["wins"], 2700,
            f"Win count regression: {self.stats['wins']} < 2700",
        )

    def test_capital_above_180(self):
        self.assertGreaterEqual(
            self.stats["capital_final"], 180.0,
            f"Capital regression: ${self.stats['capital_final']:.2f} < $180.00",
        )

    # ── drawdown guard ────────────────────────────────────────────────────────

    def test_max_drawdown_below_100(self):
        self.assertLessEqual(
            self.stats["max_drawdown"], 100.0,
            f"Drawdown regression: ${self.stats['max_drawdown']:.2f} > $100.00",
        )

    # ── internal consistency ──────────────────────────────────────────────────

    def test_wins_plus_losses_equals_resolved(self):
        resolved = self.stats["wins"] + self.stats["losses"]
        self.assertEqual(self.stats["total"] - self.stats["open"], resolved)

    def test_capital_equals_start_plus_pnl(self):
        # capital_final ≈ capital_start + total_pnl (losses also deduct gas fee,
        # so allow $5 tolerance for rounding across many trades)
        expected = self.params.capital_start + self.stats["total_pnl"]
        self.assertAlmostEqual(self.stats["capital_final"], expected, delta=5.0)

    def test_all_resolved_trades_have_pnl(self):
        for t in self.trades:
            if t.outcome in ("WIN", "LOSS"):
                self.assertIsNotNone(t.pnl_net, f"trade {t.market_id} resolved but pnl_net is None")

    def test_win_pnl_better_than_full_loss(self):
        # A WIN never loses the full stake — even at ENTRY_MAX the gas fee
        # only costs ~$0.03, so pnl_net > -STAKE always holds.
        bad = [t for t in self.trades
               if t.outcome == "WIN" and t.pnl_net is not None
               and t.pnl_net <= -self.params.stake]
        self.assertEqual(len(bad), 0, f"{len(bad)} WIN trades lost the full stake")

    def test_loss_pnl_near_negative_stake(self):
        # A LOSS costs roughly -STAKE (plus fee + gas). Allow $1 tolerance.
        bad = [t for t in self.trades
               if t.outcome == "LOSS" and t.pnl_net is not None
               and t.pnl_net > -self.params.stake + 1.0]
        self.assertEqual(len(bad), 0, f"{len(bad)} LOSS trades did not deduct stake")

    def test_loss_pnl_negative(self):
        bad = [t for t in self.trades if t.outcome == "LOSS" and t.pnl_net is not None and t.pnl_net >= 0]
        self.assertEqual(len(bad), 0, f"{len(bad)} LOSS trades have non-negative PnL")


# ── 2. Backtest ↔ bot live consistency ───────────────────────────────────────

class TestParamConsistency(unittest.TestCase):
    """
    Every parameter that appears in both live_bot.py and backtest.Params must
    have the same default value in both files.  A mismatch means the backtest
    no longer predicts live bot behaviour — a silent, hard-to-debug bug.
    """

    def _check(self, bot_attr, params_attr, label):
        bot_val    = getattr(bot, bot_attr)
        params_val = getattr(bt.Params(), params_attr)
        self.assertAlmostEqual(
            bot_val, params_val,
            msg=f"{label}: live_bot.{bot_attr}={bot_val} "
                f"≠ backtest.Params.{params_attr}={params_val}",
        )

    def test_signal_threshold(self):
        self._check("SIGNAL_THRESHOLD", "signal_threshold", "signal threshold")

    def test_min_secs_remaining(self):
        self._check("MIN_SECS_REMAINING", "min_secs_remaining", "min secs remaining")

    def test_win_threshold(self):
        self._check("WIN_THRESHOLD", "win_threshold", "win threshold")

    def test_loss_threshold(self):
        self._check("LOSS_THRESHOLD", "loss_threshold", "loss threshold")

    def test_obi_reject_thresh(self):
        self._check("OBI_REJECT_THRESH", "obi_reject_thresh", "OBI reject threshold")

    def test_stake(self):
        self._check("STAKE", "stake", "stake")

    def test_daily_stop_loss(self):
        self._check("DAILY_STOP_LOSS", "daily_stop_loss", "daily stop loss")

    def test_min_ask_vol(self):
        self._check("MIN_ASK_VOL", "min_ask_vol", "min ask volume")

    def test_entry_max(self):
        self._check("ENTRY_MAX", "entry_max", "entry max")

    def test_capital_start(self):
        self._check("CAPITAL_START", "capital_start", "capital start")

    def test_fee_rate_matches_api(self):
        # backtest.FEE_RATE must match the exchange adapter in use
        self.assertAlmostEqual(
            bt.FEE_RATE, api_poly.FEE_RATE,
            msg=f"backtest.FEE_RATE={bt.FEE_RATE} ≠ api_polymarket.FEE_RATE={api_poly.FEE_RATE}",
        )

    def test_gas_fee_matches(self):
        self.assertAlmostEqual(
            bot.GAS_FEE_USD, bt.GAS_FEE_USD,
            msg=f"live_bot.GAS_FEE_USD={bot.GAS_FEE_USD} ≠ backtest.GAS_FEE_USD={bt.GAS_FEE_USD}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
