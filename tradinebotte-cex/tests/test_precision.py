# pylint: disable=protected-access
"""Generic per-pair price/quantity precision (api_common helpers + grid level math).

Guards the ".2f floors a sub-cent pair to 0.00" regression: formatting and grid
levels must adapt to any pair's exchange precision (BTC 2dp, LBC 6dp), never a
hardcoded 2dp.
"""

import asyncio
import os
import sys
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

# Fake connectors module before importing the grid engine. Reuse an already-registered
# one if present (import-order-independent under `discover`), else register ours; either
# way it must be a valid async connector for both suites (the precision tests also set
# g._api explicitly per-test). See the mirror block in test_strategy_engines.
_existing = sys.modules.get("connectors")
if _existing is not None and hasattr(_existing, "load"):
    _fake_api = _existing.load.return_value
else:
    _fake_api = MagicMock()
    _connectors_mod = types.ModuleType("connectors")
    _connectors_mod.load = MagicMock(return_value=_fake_api)
    sys.modules["connectors"] = _connectors_mod
_fake_api.FEE_RATE = 0.0
_fake_api.compute_fee = MagicMock(return_value=0.0)
_fake_api.post_order = AsyncMock(return_value="sim_001")
_fake_api.get_open_orders = AsyncMock(return_value=[])
_fake_api.cancel_order = AsyncMock(return_value=None)
_fake_api.post_market_order = AsyncMock(return_value="sim_mkt_001")
_fake_api.get_symbol_precision = AsyncMock(return_value=(2, 6))  # BTC-scale default; precision tests override per-test

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api_common import (decimals_of, decimals_for_price, fmt_price, fmt_qty,  # noqa: E402
                        warm_symbol_precision)
from strategy_engines.grid import GridStrategy  # noqa: E402
from strategy_engines.swing import SwingStrategy                # noqa: E402


class _GridCfg:
    def __init__(self, lower, upper, levels, size, symbol):
        self.grid_lower = lower
        self.grid_upper = upper
        self.grid_levels = levels
        self.grid_order_size_usdt = size
        self.grid_symbol = symbol
        self.connector = "mexc"


class TestDecimalsOf(unittest.TestCase):
    def test_step_sizes(self):
        self.assertEqual(decimals_of("0.001"), 3)      # LBC baseSizePrecision
        self.assertEqual(decimals_of("0.000001"), 6)   # BTC baseSizePrecision
        self.assertEqual(decimals_of("0.0001"), 4)     # ETH baseSizePrecision
        self.assertEqual(decimals_of("1e-6"), 6)       # scientific notation
        self.assertEqual(decimals_of("0.01"), 2)       # BTC tickSize
        self.assertEqual(decimals_of("1"), 0)
        self.assertEqual(decimals_of("100"), 0)
        self.assertEqual(decimals_of(0.001), 3)        # float input


class TestDecimalsForPrice(unittest.TestCase):
    def test_magnitude_based(self):
        self.assertEqual(decimals_for_price(64203.0), 2)   # BTC
        self.assertEqual(decimals_for_price(1799.0), 2)    # ETH
        self.assertEqual(decimals_for_price(0.0), 2)       # guard
        self.assertGreaterEqual(decimals_for_price(0.00211), 5)   # LBC — not collapsed
        self.assertLessEqual(decimals_for_price(0.00211), 8)      # capped


class TestFormatters(unittest.TestCase):
    def test_price_sub_cent_not_collapsed(self):
        # The core regression: LBC price must NOT become "0.00".
        self.assertEqual(fmt_price(0.00211, 6), "0.002110")
        self.assertEqual(fmt_price(0.0021103, 6), "0.002110")   # rounds down to tick
        self.assertEqual(fmt_price(0.0021108, 6), "0.002111")   # rounds up to nearest tick

    def test_price_btc_unchanged(self):
        self.assertEqual(fmt_price(64203.446, 2), "64203.45")
        self.assertEqual(fmt_price(64203.0, 2), "64203.00")

    def test_qty_floored_never_up(self):
        # 100 USDT / 0.00211 = 47393.36...; floored to LBC's 0.001 step.
        self.assertEqual(fmt_qty(47393.3649, 3), "47393.364")
        self.assertEqual(fmt_qty(0.123999, 3), "0.123")        # floor, not round up
        self.assertEqual(fmt_qty(1.9, 0), "1")                 # 0dp floors down


class TestGridLevelsSubCent(unittest.TestCase):
    def test_lbc_levels_distinct_and_not_zero(self):
        cfg = _GridCfg(0.001, 0.003, 20, 5.0, "LBCUSDT")
        g = GridStrategy(cfg)
        prices = [lvl.price for lvl in g.grid.levels]
        self.assertEqual(len(prices), 20)
        self.assertEqual(len(set(prices)), 20, "grid levels collapsed / not distinct")
        self.assertTrue(all(p > 0 for p in prices), "a level rounded to 0.00")
        self.assertAlmostEqual(prices[0], 0.001, places=6)
        self.assertAlmostEqual(prices[-1], 0.003, places=6)
        self.assertGreaterEqual(g._price_dec, 4)   # derived precision sufficient for LBC

    def test_btc_derived_precision_is_2(self):
        cfg = _GridCfg(49000.0, 73500.0, 30, 50.0, "BTCUSDT")
        g = GridStrategy(cfg)
        self.assertEqual(g._price_dec, 2)          # BTC behavior unchanged (cents)

    def test_refresh_precision_applies_exchange_tick(self):
        cfg = _GridCfg(0.001, 0.003, 20, 5.0, "LBCUSDT")
        g = GridStrategy(cfg)
        g._api = MagicMock()
        g._api.get_symbol_precision = AsyncMock(return_value=(6, 3))
        state = MagicMock()
        asyncio.run(g._refresh_precision(state))
        self.assertEqual(g._price_dec, 6)
        for lvl in g.grid.levels:
            # each level representable in <=6dp and non-zero
            self.assertEqual(round(lvl.price, 6), lvl.price)
            self.assertGreater(lvl.price, 0)

    def test_refresh_precision_failclosed_keeps_derived(self):
        cfg = _GridCfg(0.001, 0.003, 20, 5.0, "LBCUSDT")
        g = GridStrategy(cfg)
        derived = g._price_dec
        g._api = MagicMock()
        g._api.get_symbol_precision = AsyncMock(return_value=None)  # fetch failed
        asyncio.run(g._refresh_precision(MagicMock()))
        self.assertEqual(g._price_dec, derived)    # falls back, non-fatal
        self.assertTrue(all(lvl.price > 0 for lvl in g.grid.levels))


class TestWarmPrecision(unittest.TestCase):
    def test_warm_returns_precision(self):
        api = MagicMock()
        api.get_symbol_precision = AsyncMock(return_value=(6, 3))
        got = asyncio.run(warm_symbol_precision(api, MagicMock(), "LBCUSDT:SELL"))
        self.assertEqual(got, (6, 3))
        api.get_symbol_precision.assert_awaited_once()
        self.assertEqual(api.get_symbol_precision.await_args.args[1], "LBCUSDT")  # suffix stripped

    def test_warm_noop_without_capability(self):
        # sim-only / connector-free path: no get_symbol_precision → no-op, no raise.
        api = object()
        self.assertIsNone(asyncio.run(warm_symbol_precision(api, MagicMock(), "BTCUSDT")))

    def test_warm_swallows_fetch_error(self):
        api = MagicMock()
        api.get_symbol_precision = AsyncMock(side_effect=RuntimeError("net down"))
        self.assertIsNone(asyncio.run(warm_symbol_precision(api, MagicMock(), "BTCUSDT")))

    def test_grid_warm_precision_sets_price_dec(self):
        cfg = _GridCfg(0.001, 0.003, 20, 5.0, "LBCUSDT")
        g = GridStrategy(cfg)
        g._api = MagicMock()
        g._api.get_symbol_precision = AsyncMock(return_value=(6, 3))
        asyncio.run(g.warm_precision(MagicMock()))
        self.assertEqual(g._price_dec, 6)


class _SwingCfg:
    def __init__(self, symbol, support, resistance):
        self.strategy_cfg = {
            "symbol": symbol, "support_levels": support, "resistance_levels": resistance,
            "order_size_usdt": 20.0, "max_positions": 3, "sl_pct": 0.05,
            "trend_filter_enabled": False, "ema200_filter_enabled": False,
        }
        self.connector = "mexc"
        self.indicators_addr = "tcp://127.0.0.1:5559"


class TestSwingStopLossSubCent(unittest.TestCase):
    def test_sl_not_collapsed_for_sub_cent_pair(self):
        s = SwingStrategy(_SwingCfg("LBCUSDT", [0.0015, 0.0018], [0.0025, 0.0028]))
        self.assertGreaterEqual(s._price_dec, 4)
        sl = s._compute_sl(0.0018)                 # 5% fallback SL (no ATR)
        self.assertGreater(sl, 0.0, "sub-cent stop-loss rounded to 0.00 → never triggers")
        self.assertAlmostEqual(sl, 0.0018 * 0.95, places=6)

    def test_sl_btc_precision_is_2(self):
        s = SwingStrategy(_SwingCfg("BTCUSDT", [70000.0, 72500.0], [78000.0, 80000.0]))
        self.assertEqual(s._price_dec, 2)          # BTC unchanged

    def test_fallback_tp_not_collapsed_sub_cent(self):
        # resistance all BELOW entry → _find_tp uses the fallback (the site the audit
        # caught: swing.py:241 was round(..., 2) → 0.00 for a sub-cent pair).
        s = SwingStrategy(_SwingCfg("LBCUSDT", [0.0015], [0.0016]))
        tp = s._find_tp(0.0020)
        self.assertGreater(tp, 0.0020, "fallback TP not above entry / collapsed to 0.00")
        self.assertLess(tp, 0.0030)


if __name__ == "__main__":
    unittest.main()
