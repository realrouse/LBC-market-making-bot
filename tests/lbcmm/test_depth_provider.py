"""Unit tests for Depth Provider pure planner."""

import unittest

from lbcmm.strategies.depth_provider import (
    contribution_usd,
    effective_levels,
    max_levels_for_budget,
    plan_depth_orders,
)


class TestPlanDepthOrders(unittest.TestCase):
    def test_mid_zero_empty(self):
        self.assertEqual(plan_depth_orders(0, usdt_budget=10, lbc_budget=1000), [])

    def test_zero_usdt_no_buys(self):
        mid = 0.002
        orders = plan_depth_orders(
            mid, usdt_budget=0, lbc_budget=10000, n_levels=10, min_notional_usdt=1.0
        )
        self.assertFalse(any(o.side == "BUY" for o in orders))
        self.assertTrue(any(o.side == "SELL" for o in orders))

    def test_dust_usdt_no_buys(self):
        orders = plan_depth_orders(
            0.002, usdt_budget=0.5, lbc_budget=0, n_levels=4, min_notional_usdt=1.0
        )
        self.assertEqual(orders, [])

    def test_buys_within_band(self):
        mid = 0.002
        orders = plan_depth_orders(
            mid,
            usdt_budget=20,
            lbc_budget=0,
            bid_depth_pct=2.0,
            ask_depth_pct=2.0,
            n_levels=4,
            min_notional_usdt=1.0,
        )
        buys = [o for o in orders if o.side == "BUY"]
        self.assertTrue(buys)
        floor = mid * 0.98
        for o in buys:
            self.assertGreaterEqual(o.price, floor - 1e-12)
            self.assertLess(o.price, mid)
            self.assertGreaterEqual(o.usdt, 1.0 - 1e-9)

    def test_auto_reduce_steps_for_small_budget(self):
        # $5 cannot fund 30 steps at $1 each → at most 5 buy orders
        orders = plan_depth_orders(
            0.002, usdt_budget=5, lbc_budget=0, n_levels=30, min_notional_usdt=1.0
        )
        buys = [o for o in orders if o.side == "BUY"]
        self.assertEqual(len(buys), 5)
        for o in buys:
            self.assertGreaterEqual(o.usdt, 1.0 - 1e-9)

    def test_sells_use_lbc_budget(self):
        mid = 0.002
        orders = plan_depth_orders(
            mid,
            usdt_budget=0,
            lbc_budget=10000,
            n_levels=4,
            min_notional_usdt=1.0,
        )
        sells = [o for o in orders if o.side == "SELL"]
        self.assertTrue(sells)
        total_coins = sum(o.qty for o in sells)
        self.assertAlmostEqual(total_coins, 10000, places=4)
        for o in sells:
            self.assertGreater(o.price, mid)
            self.assertGreaterEqual(o.usdt, 1.0 - 1e-9)

    def test_two_sided(self):
        mid = 0.0021
        orders = plan_depth_orders(
            mid, usdt_budget=40, lbc_budget=20000, n_levels=3, min_notional_usdt=1.0
        )
        self.assertTrue(any(o.side == "BUY" for o in orders))
        self.assertTrue(any(o.side == "SELL" for o in orders))

    def test_contribution_inside_2pct(self):
        mid = 0.002
        orders = plan_depth_orders(
            mid, usdt_budget=20, lbc_budget=10000, bid_depth_pct=2, ask_depth_pct=2
        )
        c = contribution_usd(orders, mid, 2.0)
        self.assertGreater(c["bid_usd"], 0)
        self.assertGreater(c["ask_usd"], 0)


class TestLevelCaps(unittest.TestCase):
    def test_max_levels(self):
        self.assertEqual(max_levels_for_budget(0, 1.0), 0)
        self.assertEqual(max_levels_for_budget(0.9, 1.0), 0)
        self.assertEqual(max_levels_for_budget(10, 1.0), 10)
        self.assertEqual(effective_levels(30, 5, 1.0), 5)
        self.assertEqual(effective_levels(3, 100, 1.0), 3)


class TestBammImport(unittest.TestCase):
    def test_build_grid(self):
        from lbcmm.strategies.bamm import build_buy_grid

        rungs = build_buy_grid(top=0.0028, floor=0.001, budget_usdt=50)
        self.assertTrue(rungs)
        self.assertAlmostEqual(sum(r.usdt for r in rungs), 50.0, places=1)


if __name__ == "__main__":
    unittest.main()
