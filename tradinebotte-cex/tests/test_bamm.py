"""Unit tests for bamm.build_buy_grid — the BAMM buy-ladder planner (money-critical, pure)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategy_engines"))

import bamm  # noqa: E402


class TestGeometricPrices(unittest.TestCase):
    def test_descends_from_top_and_snaps_to_floor(self):
        px = bamm._geometric_prices(0.00278, 0.001, 5.0)
        self.assertAlmostEqual(px[0], 0.00278)
        self.assertEqual(px[-1], 0.001)                 # bottom snapped exactly to floor
        self.assertTrue(all(px[i] > px[i + 1] for i in range(len(px) - 1)))  # strictly descending

    def test_rejects_bad_bounds(self):
        with self.assertRaises(ValueError):
            bamm._geometric_prices(0.001, 0.002, 5.0)   # floor >= top
        with self.assertRaises(ValueError):
            bamm._geometric_prices(0.00278, 0.001, 0.0) # step 0


class TestBuildBuyGrid(unittest.TestCase):
    KW = dict(top=0.00278, floor=0.001, step_pct=5.0, budget_usdt=80.0, min_notional_usdt=1.1)

    def test_fully_deploys_the_budget_at_the_floor(self):
        rungs = bamm.build_buy_grid(**self.KW)
        self.assertAlmostEqual(sum(r.usdt for r in rungs), 80.0, places=2)   # deploy-at-floor
        self.assertEqual(rungs[-1].price, 0.001)                             # bottom rung is the floor

    def test_every_rung_is_placeable(self):
        rungs = bamm.build_buy_grid(**self.KW)
        self.assertTrue(all(r.usdt >= 1.1 - 1e-9 for r in rungs), [r.usdt for r in rungs])

    def test_deeper_rungs_buy_more(self):
        rungs = bamm.build_buy_grid(**self.KW)
        usd = [r.usdt for r in rungs]                    # rungs are top→floor
        self.assertEqual(usd, sorted(usd), "USDT per rung must rise toward the floor")
        coins = [r.coins for r in rungs]
        self.assertEqual(coins, sorted(coins), "coins per rung must rise toward the floor")

    def test_coins_consistent_with_price(self):
        for r in bamm.build_buy_grid(**self.KW):
            self.assertAlmostEqual(r.coins, r.usdt / r.price, places=6)

    def test_sizing_power_tilts_harder_to_the_floor(self):
        flat = bamm.build_buy_grid(**{**self.KW, "sizing_power": 1.0})
        steep = bamm.build_buy_grid(**{**self.KW, "sizing_power": 2.0})
        # steeper power puts a larger share on the floor rung
        self.assertGreater(steep[-1].usdt, flat[-1].usdt)

    def test_zero_or_negative_budget_is_empty(self):
        self.assertEqual(bamm.build_buy_grid(**{**self.KW, "budget_usdt": 0.0}), [])

    def test_tiny_budget_keeps_only_placeable_rungs_and_still_sums(self):
        rungs = bamm.build_buy_grid(**{**self.KW, "budget_usdt": 3.0})
        self.assertTrue(all(r.usdt >= 1.1 - 1e-9 for r in rungs))
        if rungs:
            self.assertAlmostEqual(sum(r.usdt for r in rungs), 3.0, places=2)

    def test_deploy_summary(self):
        s = bamm.deploy_summary(bamm.build_buy_grid(**self.KW))
        self.assertAlmostEqual(s["usdt"], 80.0, places=2)
        self.assertEqual(s["bottom"], 0.001)
        self.assertGreater(s["coins"], 0)
        self.assertAlmostEqual(s["avg_cost"], s["usdt"] / s["coins"], places=6)


class TestExtraRungs(unittest.TestCase):
    """extra_rungs: manual fixed-price/fixed-usdt rungs bolted on alongside the geometric ladder
    (2026-07-25 operator top-up — two $10 rungs at prices the 5% lattice never lands on)."""
    KW = dict(top=0.00278, floor=0.001, step_pct=5.0, budget_usdt=80.0, min_notional_usdt=1.1)

    def test_extra_rungs_are_added_at_exact_price_and_usdt(self):
        rungs = bamm.build_buy_grid(**self.KW, extra_rungs=[(0.00247, 10.0), (0.00237, 10.0)])
        extras = [r for r in rungs if r.price in (0.00247, 0.00237)]
        self.assertEqual(len(extras), 2)
        for r in extras:
            self.assertAlmostEqual(r.usdt, 10.0)
            self.assertAlmostEqual(r.coins, 10.0 / r.price, places=6)

    def test_extra_rungs_dont_change_the_geometric_ladder(self):
        base = bamm.build_buy_grid(**self.KW)
        with_extra = bamm.build_buy_grid(**self.KW, extra_rungs=[(0.00247, 10.0)])
        base_prices = {round(r.price, 9) for r in base}
        kept = [r for r in with_extra if round(r.price, 9) in base_prices]
        self.assertEqual(len(kept), len(base))
        for r in kept:
            match = next(b for b in base if round(b.price, 9) == round(r.price, 9))
            self.assertAlmostEqual(r.usdt, match.usdt, places=6)

    def test_extra_rung_below_min_notional_is_dropped(self):
        rungs = bamm.build_buy_grid(**self.KW, extra_rungs=[(0.00247, 0.5)])
        self.assertFalse(any(r.price == 0.00247 for r in rungs))

    def test_extra_rungs_alone_work_with_zero_budget(self):
        rungs = bamm.build_buy_grid(**{**self.KW, "budget_usdt": 0.0},
                                    extra_rungs=[(0.00247, 10.0), (0.00237, 10.0)])
        self.assertEqual(len(rungs), 2)
        self.assertAlmostEqual(sum(r.usdt for r in rungs), 20.0)

    def test_rungs_stay_top_to_floor_ordered_with_extras_merged_in(self):
        rungs = bamm.build_buy_grid(**self.KW, extra_rungs=[(0.00247, 10.0), (0.00237, 10.0)])
        prices = [r.price for r in rungs]
        self.assertEqual(prices, sorted(prices, reverse=True))

    def test_extra_rung_participates_in_the_normal_cycle(self):
        rungs = bamm.build_buy_grid(**self.KW, extra_rungs=[(0.00247, 10.0)])
        g = bamm.BammGrid(rungs, step_pct=5.0, stash_pct=0.10, free_usdt=100.0)
        i = next(idx for idx, r in enumerate(g.rungs) if r["price"] == 0.00247)
        coins = g.rungs[i]["loop_coins"]
        g.on_buy_fill(i, coins)
        self.assertEqual(g.rungs[i]["mode"], "ask")
        self.assertAlmostEqual(g._ask_price(i), 0.00247 * 1.05)

    def test_extra_rung_is_never_seeded_as_pre_loaded(self):
        # Regression (2026-07-25 live incident): a restart's seed_holdings() reconstruction
        # greedily claims ANY rung whose ask is above market for pre-existing holdings, in
        # ascending-ask-price order. A newly added low-ask extra rung can jump that queue ahead of
        # rungs that were genuinely already loaded — silently turning the intended NEW buy into a
        # resting sell of coins bought long ago, with the fresh capital never spent. Extra rungs
        # must be immune to this reconstruction: always a fresh empty bid after any restart.
        rungs = bamm.build_buy_grid(**self.KW, extra_rungs=[(0.00237, 10.0)])
        g = bamm.BammGrid(rungs, step_pct=5.0, stash_pct=0.10, free_usdt=100.0)
        i = next(idx for idx, r in enumerate(g.rungs) if r["price"] == 0.00237)
        g.seed_holdings(50000.0, mid=0.002)   # plenty of pre-existing holdings, market well below
        self.assertEqual(g.rungs[i]["mode"], "bid")
        self.assertAlmostEqual(g.rungs[i]["loop_coins"], 10.0 / 0.00237)

    def test_seedable_defaults_true_for_geometric_rungs(self):
        rungs = bamm.build_buy_grid(**self.KW)
        self.assertTrue(all(r.seedable for r in rungs))


class TestCycle(unittest.TestCase):
    def test_sell_after_buy_keeps_stash_and_sells_the_rest_one_rung_up(self):
        r = bamm.sell_after_buy(buy_price=0.0020, coins_bought=1000.0, step_pct=5.0, stash_pct=0.10)
        self.assertAlmostEqual(r["sell_price"], 0.0021)          # +5%
        self.assertAlmostEqual(r["sell_coins"], 900.0)           # sell 90%
        self.assertAlmostEqual(r["stash_coins"], 100.0)          # keep 10%
        self.assertAlmostEqual(r["sell_coins"] + r["stash_coins"], 1000.0)   # nothing lost

    def test_rebuy_after_sell_returns_to_the_origin_rung(self):
        r = bamm.rebuy_after_sell(sell_price=0.0021, coins_sold=900.0, step_pct=5.0)
        self.assertAlmostEqual(r["buy_price"], 0.0020)           # back down one rung
        self.assertAlmostEqual(r["buy_coins"], 900.0)            # rebuy exactly what was sold

    def test_round_trip_price_identity(self):
        # buy → sell one up → rebuy one down must land back on the original buy price
        s = bamm.sell_after_buy(buy_price=0.00151, coins_bought=500.0, step_pct=5.0)
        b = bamm.rebuy_after_sell(sell_price=s["sell_price"], coins_sold=s["sell_coins"], step_pct=5.0)
        self.assertAlmostEqual(b["buy_price"], 0.00151, places=9)

    def test_self_funding_loop_makes_usdt(self):
        # sell 90% at +5%, rebuy the same 90% one rung down → positive USDT per loop
        s = bamm.sell_after_buy(buy_price=0.0020, coins_bought=1000.0, step_pct=5.0)
        got = s["sell_coins"] * s["sell_price"]
        b = bamm.rebuy_after_sell(sell_price=s["sell_price"], coins_sold=s["sell_coins"], step_pct=5.0)
        self.assertGreater(got - b["buy_usdt"], 0.0)


class TestBammGrid(unittest.TestCase):
    def _grid(self, budget=150.0):
        rungs = bamm.build_buy_grid(top=0.00278, floor=0.001, step_pct=5.0,
                                    budget_usdt=budget, sizing_power=0.5)
        return bamm.BammGrid(rungs, step_pct=5.0, stash_pct=0.10, free_usdt=budget)

    def _held_invariant(self, g):
        """holdings must always equal the permanent stash plus coins parked in loaded rungs."""
        loaded = sum(r["loop_coins"] for r in g.rungs if r["mode"] == "ask")
        self.assertAlmostEqual(g.holdings, g.stash + loaded, places=6)

    def test_buy_banks_ten_percent_and_arms_an_ask(self):
        g = self._grid()
        i = len(g.rungs) - 1                       # floor rung
        coins = g.rungs[i]["loop_coins"]
        g.on_buy_fill(i, coins)
        self.assertAlmostEqual(g.stash, coins * 0.10)
        self.assertEqual(g.rungs[i]["mode"], "ask")
        self.assertAlmostEqual(g.rungs[i]["loop_coins"], coins * 0.90)
        self._held_invariant(g)

    def test_full_cycle_is_self_funding_and_keeps_the_stash(self):
        g = self._grid()
        i = 10
        coins = g.rungs[i]["loop_coins"]
        usdt0 = g.free_usdt
        g.on_buy_fill(i, coins)                    # buy
        g.on_sell_fill(i, g.rungs[i]["loop_coins"])  # its ask fills (0.9*coins)
        self.assertGreater(g.free_usdt, usdt0 - coins * g.rungs[i]["price"] * 0.10 + 1e-9)  # net USDT ≥ before minus the stash's cost
        self.assertGreater(g.stash, 0)             # 10% kept
        self.assertGreater(g.realized_usdt, 0)     # sold higher than bought
        self.assertEqual(g.rungs[i]["mode"], "bid")  # re-armed as a rebuy
        self._held_invariant(g)

    def test_stash_equals_sum_of_ten_percent_of_every_buy(self):
        g = self._grid()
        bought = []
        # drive a few buys and sells across rungs
        for i in (19, 18, 17, 18, 19):
            r = g.rungs[i]
            if r["mode"] == "bid":
                c = r["loop_coins"]; bought.append(c); g.on_buy_fill(i, c)
            else:
                g.on_sell_fill(i, r["loop_coins"])
        self.assertAlmostEqual(g.stash, sum(bought) * 0.10, places=6)
        self._held_invariant(g)

    def test_desired_orders_sides_and_dust(self):
        g = self._grid()
        orders = g.desired_orders(mid=0.0026)
        self.assertTrue(all(o["side"] == "buy" and o["price"] < 0.0026 for o in orders))
        self.assertTrue(all(o["coins"] * o["price"] >= g.min_notional - 1e-9 for o in orders))

    def test_seed_holdings_never_rests_a_sell_below_market(self):
        g = self._grid()
        g.seed_holdings(5000.0, mid=0.00278)
        self.assertAlmostEqual(g.holdings, 5000.0)
        # every loaded ask must be ABOVE the market — never dump the existing bag at a loss
        for i, r in enumerate(g.rungs):
            if r["mode"] == "ask":
                self.assertGreater(g._ask_price(i), 0.00278)
        self.assertGreater(g.stash, 0)             # the bulk that can't sit above market → stash
        self._held_invariant(g)

    def test_seed_all_stash_when_market_below_whole_grid(self):
        g = self._grid()
        g.seed_holdings(5000.0, mid=0.01)          # market above every rung's ask → nothing rests
        self.assertAlmostEqual(g.stash, 5000.0)
        self.assertTrue(all(r["mode"] == "bid" for r in g.rungs))


if __name__ == "__main__":
    unittest.main()
