"""Unit tests for the shared realized-PnL math (tradinetools.pnl)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tradinetools.pnl import round_trip_pnl, trade_fee


class TestTradeFee(unittest.TestCase):
    def test_notional(self):
        self.assertAlmostEqual(trade_fee(100.0, 2.0, 0.001), 0.2)

    def test_zero_rate(self):
        self.assertEqual(trade_fee(100.0, 2.0, 0.0), 0.0)


class TestRoundTripPnl(unittest.TestCase):
    def test_win_nets_both_fees(self):
        # (110-100)*2 - 0.001*100*2 - 0.001*110*2 = 20 - 0.20 - 0.22 = 19.58
        self.assertAlmostEqual(round_trip_pnl(100.0, 110.0, 2.0, 0.001), 19.58)

    def test_loss_nets_both_fees(self):
        # (90-100)*1 - 0.001*100 - 0.001*90 = -10 - 0.19 = -10.19
        self.assertAlmostEqual(round_trip_pnl(100.0, 90.0, 1.0, 0.001), -10.19)

    def test_flat_move_still_costs_both_legs(self):
        # No price move must still cost the round-trip fee (the bug this guards against:
        # a path that forgets a fee leg would return 0 here).
        self.assertAlmostEqual(round_trip_pnl(100.0, 100.0, 1.0, 0.001), -0.2)

    def test_zero_fee(self):
        self.assertAlmostEqual(round_trip_pnl(100.0, 110.0, 2.0, 0.0), 20.0)

    def test_maker_taker_asymmetry(self):
        # maker entry (0%) + taker exit (0.2%) on a flat move → only the exit fee
        self.assertAlmostEqual(
            round_trip_pnl(100.0, 100.0, 1.0, 0.001, entry_fee_rate=0.0, exit_fee_rate=0.002),
            -0.2)


if __name__ == "__main__":
    unittest.main()
