"""Parity guard: the engine-driven harness must agree with the re-implemented backtest
on the path where they *provably* coincide.

Rationale (docs/backtest-fidelity.md §5): for swing, the harness driving the real engine and
`backtest_swing_dca.run_swing` diverge ONLY on the stop-loss path (the engine never re-arms a
support after an SL; the backtest re-arms on price recovery). On any window where **no SL fires**
— every exit is a take-profit — the two are identical (the real bullrun window gave Δ=0). This
test pins that with a tiny synthetic no-SL fixture, so a future change to either the engine's
fill/PnL accounting or the backtest silently breaking that agreement is caught in CI.

No data/*.db dependency (those aren't in git): the fixture is built in-memory.
"""

import os
import sys
import asyncio
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "analysis"))

import backtest_engine as be   # noqa: E402  (self-inserts cex path, forces sim mode)
import backtest_swing_dca as bsd  # noqa: E402


def _c(ts, o, h, l, cl):
    return (ts, float(o), float(h), float(l), float(cl), 1.0)   # 6-tuple incl. volume


# One clean support→TP cycle, no stop-loss anywhere:
#   c0: price 99–100, arms a LIMIT BUY at support 97 (init price 99 > 97).
#   c1: dips to 96.9 → BUY fills at 97 (SL 95.06 is never touched: low 96.9 > 95.06).
#   c2: rises to 104 → TP SELL fills at resistance 103.
# Both engine and backtest must book exactly one winning round-trip 97→103.
_SUPPORT = [97.0]
_RESISTANCE = [103.0]
_ROWS = [
    _c(1_000, 100, 100, 99.0, 99.0),
    _c(61_000, 99, 99.0, 96.9, 96.9),
    _c(121_000, 97, 104.0, 97.0, 104.0),
]


def _run_engine():
    cfg = {"symbol": "BTCUSDT", "support_levels": _SUPPORT, "resistance_levels": _RESISTANCE,
           "order_size_usdt": 200.0, "max_positions": 1, "sl_pct": 0.02,
           "tp_pct_fallback": 0.04, "trend_filter_enabled": False,
           "ema200_filter_enabled": False, "poll_interval": 0.0}
    s = be.SwingStrategy(types.SimpleNamespace(connector="binance", strategy_cfg=cfg))
    asyncio.run(be._drive(s, _ROWS, ensure_schema=be.SwingStrategy.ensure_schema))
    return s.sw.total_trades, s.sw.total_pnl


def _run_backtest():
    p = bsd.SwingParams(support_levels=_SUPPORT, resistance_levels=_RESISTANCE,
                        order_size_usdt=200.0, max_positions=1, sl_pct=0.02,
                        tp_pct_fallback=0.04)
    r = bsd.run_swing(_ROWS, p)
    return r.n_trades, r.realized_pnl


class TestSwingHarnessParity(unittest.TestCase):

    def test_no_sl_path_engine_equals_backtest(self):
        e_trades, e_pnl = _run_engine()
        b_trades, b_pnl = _run_backtest()

        # Sanity: the fixture must actually trade (not a vacuous 0 == 0).
        self.assertEqual(e_trades, 1, "fixture should book exactly one round-trip")
        self.assertGreater(e_pnl, 0.0, "the 97→103 cycle must be a net win")

        # Parity: on the no-SL path the two implementations are identical.
        self.assertEqual(e_trades, b_trades)
        self.assertAlmostEqual(e_pnl, b_pnl, places=6)


if __name__ == "__main__":
    unittest.main()
