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

    def test_bamm_detected_and_shadow_respected(self):
        eng = _eng(shadow=True)
        self.assertTrue(eng.bamm)
        self.assertTrue(eng.shadow)                   # configured shadow stays shadow

    def test_dispatcher_knows_bamm(self):
        # live_bot.py loads the engine via strategy_engines.load(config.strategy_type) — the
        # dispatcher MUST map "bamm" (it crash-looped the real bot when it didn't). Direct
        # construction in the other tests bypasses this path, so pin it here.
        from strategy_engines import load, available
        self.assertIn("bamm", available())
        cfg = {"strategy_type": "bamm", "symbol": "LBCUSDT", "shadow": True,
               "grid_top": 0.0028, "grid_budget_usdt": 150.0}
        strat = load("bamm", types.SimpleNamespace(connector="mexc", strategy_cfg=cfg, bot_id="x"))
        self.assertEqual(type(strat).__name__, "AccumulationStrategy")
        self.assertTrue(strat.bamm)

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
        # first mid is BELOW grid_top so the seed rests asks ABOVE market; then price rises → sells
        self._run(eng, [0.00255, 0.00290, 0.00300])
        self.assertGreater(eng._bamm.n_sells, 0)          # seeded asks sold on the way up
        self.assertEqual(eng.acc.holdings_btc, 3765.0)    # SAFETY: real position not moved

    def test_seed_below_market_never_dumps(self):
        eng = _eng()
        eng.acc.holdings_btc = 3765.0
        self._run(eng, [0.00278])                          # mid == grid_top → no ask above market
        # nothing should be offered for sale below the market — all seed is held as stash
        self.assertEqual(eng._bamm.n_sells, 0)
        self.assertAlmostEqual(eng._bamm.stash, 3765.0)


class _MockEx:
    """Minimal MEXC-shaped exchange: records maker orders, lets a test fill them, and answers
    get_order / get_open_orders / cancel_order the way reconcile_order expects."""
    def __init__(self):
        self.orders = {}
        self.n = 0

    async def post_order(self, _session, _symbol, price, usdt=None, *, quantity=None,
                         side="BUY", order_type=None):
        self.n += 1
        oid = f"o{self.n}"
        qty = float(quantity) if quantity is not None else float(usdt) / price
        self.orders[oid] = {"order_id": oid, "status": "NEW", "executed_qty": 0.0,
                            "cummulative_quote_qty": 0.0, "price": price, "qty": qty, "side": side}
        return oid

    async def get_order(self, _session, _symbol, oid):
        return self.orders.get(oid)

    async def get_open_orders(self, _session, _symbol):
        return [o for o in self.orders.values() if o["status"] in ("NEW", "PARTIALLY_FILLED")]

    async def cancel_order(self, _session, _symbol, oid):
        if oid in self.orders:
            self.orders[oid]["status"] = "CANCELED"

    def fill(self, oid):
        o = self.orders[oid]
        o["status"] = "FILLED"
        o["executed_qty"] = o["qty"]
        o["cummulative_quote_qty"] = o["qty"] * o["price"]


class TestBammLiveExec(unittest.TestCase):
    """The real-money path against a mock exchange: places a ladder, and a fill spawns the paired
    order while the REAL wallet is credited from the actual fill deltas."""

    def _live_eng(self):
        eng = _eng(shadow=True)          # constructed keyless → live cleared → shadow
        eng._api = _MockEx()             # stub the connector, force live like the accum tests do
        eng.live = True
        eng.shadow = False
        eng.acc.holdings_btc = 0.0
        eng.acc.free_usdt = 150.0
        return eng

    def _tick(self, eng, px):
        state = types.SimpleNamespace(conn=None, session=None)
        # _record_accum_trade needs a conn; give it a throwaway in-memory sqlite with the schema
        import sqlite3
        conn = sqlite3.connect(":memory:")
        eng.ensure_schema(conn)
        state.conn = conn
        eng._check_drift = _noop_async          # skip the balance-drift probe (no real balances)
        ts = types.SimpleNamespace(mid=px, obi_ema=0.0, spread_bps=10.0, ts_ms=1)
        asyncio.run(eng.on_book_update(state, ts))

    def test_places_ladder_then_a_fill_spawns_the_paired_sell(self):
        eng = self._live_eng()
        self._tick(eng, 0.00250)                       # price below top → bids rest on rungs
        self.assertTrue(eng.acc.open_buys, "should rest a buy ladder")
        self.assertEqual(eng.acc.holdings_btc, 0.0)    # nothing filled yet
        # fill one resting buy, tick again → it should credit + rest the paired sell
        oid, rec = next(iter(eng.acc.open_buys.items()))
        coins = rec["orig_qty"]
        eng._api.fill(oid)
        self._tick(eng, 0.00250)
        self.assertAlmostEqual(eng.acc.holdings_btc, coins, places=3)   # real credit
        self.assertLess(eng.acc.free_usdt, 150.0)                       # real USDT spent
        self.assertTrue(eng.acc.open_sells, "a filled buy must rest its paired sell")
        self.assertNotIn(oid, eng.acc.open_buys)

    def test_sell_fill_books_realized_and_rebuys(self):
        eng = self._live_eng()
        self._tick(eng, 0.00250)
        boid, brec = next(iter(eng.acc.open_buys.items()))
        eng._api.fill(boid); self._tick(eng, 0.00250)          # buy fills → sell rests
        soid, srec = next(iter(eng.acc.open_sells.items()))
        eng._api.fill(soid); self._tick(eng, 0.00250)          # sell fills → rebuy rests
        self.assertGreater(eng.acc.total_realized, 0.0)        # grid profit booked
        self.assertNotIn(soid, eng.acc.open_sells)


async def _noop_async(*_a, **_k):
    return None


if __name__ == "__main__":
    unittest.main()
