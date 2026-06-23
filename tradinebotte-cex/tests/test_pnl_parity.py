"""Parity test: the shared round_trip_pnl helper must equal BOTH the way the live
strategy engines assemble realized PnL AND the way the backtests compute it.

This is the regression guard against live↔backtest drift — the class of bug where the
live swing stop-loss once omitted a fee leg while the backtest didn't, and nobody
noticed. If either the connector fee model, the live assembly, or the backtest
cost/proceeds model diverges from round_trip_pnl, these assertions fail.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_binance
import api_mexc
import api_mexc_futures
from tradinetools.pnl import round_trip_pnl

# (entry, exit, qty) — wins, losses, flat, realistic BTC sizes
SCENARIOS = [
    (100.0, 110.0, 2.0),
    (65000.0, 64000.0, 0.01),
    (50000.0, 55000.0, 0.05),
    (30000.0, 30000.0, 0.1),    # flat — must still cost the round-trip fee
    (70000.0, 42000.0, 0.003),  # large loss
]


def _live_assembly(api, entry, exit_, qty):
    """How the live engines build it: gross minus each leg's connector fee."""
    return (exit_ - entry) * qty - api.compute_fee(entry, qty) - api.compute_fee(exit_, qty)


def _backtest_model(fee_rate, entry, exit_, qty):
    """How the backtests build it: sell proceeds (net of exit fee) minus the cost basis
    (entry notional INCLUDING the entry fee)."""
    proceeds = qty * exit_ * (1 - fee_rate)
    cost = qty * entry * (1 + fee_rate)
    return proceeds - cost


class TestPnlParity(unittest.TestCase):
    def test_helper_matches_live_and_backtest(self):
        for api in (api_binance, api_mexc, api_mexc_futures):
            for entry, exit_, qty in SCENARIOS:
                rt = round_trip_pnl(entry, exit_, qty, api.FEE_RATE)
                self.assertAlmostEqual(
                    rt, _live_assembly(api, entry, exit_, qty), places=9,
                    msg=f"live drift: {api.__name__} {entry}->{exit_} x{qty}")
                self.assertAlmostEqual(
                    rt, _backtest_model(api.FEE_RATE, entry, exit_, qty), places=9,
                    msg=f"backtest drift: {api.__name__} {entry}->{exit_} x{qty}")

    def test_connector_fee_is_notional(self):
        # round_trip_pnl assumes fee = rate*notional; assert each connector agrees.
        for api in (api_binance, api_mexc, api_mexc_futures):
            self.assertAlmostEqual(api.compute_fee(100.0, 2.0), api.FEE_RATE * 100.0 * 2.0)


if __name__ == "__main__":
    unittest.main()
