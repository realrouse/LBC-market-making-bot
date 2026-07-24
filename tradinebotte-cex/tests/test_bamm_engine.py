"""BAMM shadow integration into AccumulationStrategy — the routing + isolation guarantees.

The load-bearing assertions are the SAFETY ones: in shadow, BAMM must never mutate the engine's
real holdings/free, so switching a real-money bot to bamm can only ever stop it and start a log."""

import asyncio
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategy_engines"))

from accumulation import AccumulationStrategy  # noqa: E402


def _eng(**over):
    cfg = {"strategy_type": "bamm", "symbol": "LBCUSDT", "live_execution": True, "shadow": True,
           "capital_usdt": 150.0, "grid_top": 0.00278, "grid_floor": 0.001, "grid_step_pct": 5.0,
           "grid_budget_usdt": 150.0, "grid_sizing_power": 0.5, "grid_stash_pct": 0.10,
           "sell_min_notional_usdt": 1.1, "snapshot_every_n": 1}
    cfg.update(over)
    return AccumulationStrategy(types.SimpleNamespace(connector="mexc", strategy_cfg=cfg))


class TestBammEngineShadow(unittest.TestCase):
    def _run(self, eng, prices):
        state = types.SimpleNamespace(conn=None)
        for px in prices:
            ts = types.SimpleNamespace(mid=px, obi_ema=0.0, spread_bps=10.0, ts_ms=1)
            asyncio.run(eng.on_book_update(state, ts))

    def test_bamm_detected_and_shadow_forced(self):
        eng = _eng(shadow=False)              # bamm live is not wired → must force shadow
        self.assertTrue(eng.bamm)
        self.assertTrue(eng.shadow)

    def test_grid_inits_and_sim_buys_on_a_dip(self):
        eng = _eng()
        eng.acc.holdings_btc = 0.0
        real_free = eng.acc.free_usdt
        self._run(eng, [0.00278, 0.00250, 0.00220])
        self.assertIsNotNone(eng._bamm)
        self.assertGreater(eng._bamm.n_buys, 0)           # bought into the dip
        self.assertGreater(eng._bamm.stash, 0)            # kept 10% of each buy
        # SAFETY: real accounting untouched by the shadow sim
        self.assertEqual(eng.acc.holdings_btc, 0.0)
        self.assertEqual(eng.acc.free_usdt, real_free)

    def test_seed_position_sold_up_but_real_holdings_untouched(self):
        eng = _eng()
        eng.acc.holdings_btc = 3765.0                     # the current live position
        self._run(eng, [0.00278, 0.00300, 0.00320])       # rip up → sim sells the seeded asks
        self.assertGreater(eng._bamm.n_sells, 0)
        self.assertEqual(eng.acc.holdings_btc, 3765.0)    # SAFETY: real position not moved


if __name__ == "__main__":
    unittest.main()
