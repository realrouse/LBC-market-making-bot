# pylint: disable=protected-access
"""Unit tests for tradinebotte-cex strategy engines: SwingStrategy, DCAStrategy, SwingHoldStrategy."""

import sys
import os
import time
import sqlite3
import types
import unittest
from unittest.mock import AsyncMock, MagicMock

# ── Inject fake connectors module BEFORE importing strategy modules ─────────
_fake_api = MagicMock()
_fake_api.compute_fee = MagicMock(return_value=0.0)
_fake_api.FEE_RATE = 0.0   # engines now compute round-trip PnL via FEE_RATE (round_trip_pnl)
_fake_api.post_order = AsyncMock(return_value="sim_001")
_fake_api.get_open_orders = AsyncMock(return_value=[])
_fake_api.cancel_order = AsyncMock(return_value=None)
_fake_api.post_market_order = AsyncMock(return_value="sim_mkt_001")

_connectors_mod = types.ModuleType("connectors")
_connectors_mod.load = MagicMock(return_value=_fake_api)
sys.modules.setdefault("connectors", _connectors_mod)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy_engines.swing     import SwingStrategy      # pylint: disable=wrong-import-position
from strategy_engines.dca       import DCAStrategy        # pylint: disable=wrong-import-position
from strategy_engines.swinghold import SwingHoldStrategy  # pylint: disable=wrong-import-position
from strategy_engines.grid      import GridStrategy, GridLevel  # pylint: disable=wrong-import-position


# ── Minimal config fixtures ──────────────────────────────────────────────────

class _SwingCfg:
    strategy_cfg = {
        "symbol":               "BTCUSDT",
        "support_levels":       [70000.0, 72500.0, 75000.0],
        "resistance_levels":    [78000.0, 80000.0, 82500.0],
        "order_size_usdt":      200.0,
        "max_positions":        3,
        "sl_pct":               0.02,
        "tp_pct_fallback":      0.04,
        "trend_filter_enabled": False,
        "ema200_filter_enabled": False,
    }
    connector     = "binance"
    indicators_addr = "tcp://127.0.0.1:5559"


class _DcaCfg:
    symbol          = "BTCUSDT"
    dca_interval_h  = 4.0
    dca_amount_usdt = 100.0
    max_positions   = 5
    tp_pct          = 0.03
    sl_pct          = 0.0
    poll_interval   = 2.0
    connector       = "binance"


class _ShCfg:
    strategy_cfg = {
        "symbol":            "BTCUSDT",
        "support_levels":    [70000.0, 72500.0, 75000.0],
        "resistance_levels": [78000.0, 80000.0, 82500.0, 85000.0],
        "order_size_usdt":   200.0,
        "max_positions":     3,
        "sell_fraction":     0.30,
        "sl_pct":            0.02,
        "tp_pct_fallback":   0.04,
    }
    connector = "binance"


class _GridCfg:
    grid_symbol          = "BTCUSDT"
    grid_lower           = 100.0
    grid_upper           = 104.0
    grid_levels          = 5        # step = (104-100)/(5-1) = 1.0
    grid_order_size_usdt = 50.0
    connector            = "binance"
    grid_trail_mode      = "static"


def _swing():
    return SwingStrategy(_SwingCfg())

def _dca():
    return DCAStrategy(_DcaCfg())

def _sh():
    return SwingHoldStrategy(_ShCfg())

def _grid():
    return GridStrategy(_GridCfg())

def _state():
    """Minimal state-like object backed by an in-memory SQLite."""
    conn  = sqlite3.connect(":memory:")
    state = MagicMock()
    state.conn    = conn
    state.session = MagicMock()
    return state

def _swing_cfg_override(**kw):
    """Return a SwingStrategy config with one or more strategy_cfg fields overridden."""
    new_cfg = {**_SwingCfg.strategy_cfg, **kw}
    return type("C", (), {"strategy_cfg": new_cfg,
                          "connector": "binance",
                          "indicators_addr": ""})()

def _sh_cfg_override(**kw):
    new_cfg = {**_ShCfg.strategy_cfg, **kw}
    return type("C", (), {"strategy_cfg": new_cfg, "connector": "binance"})()


# ════════════════════════════════════════════════════════════════════════════
# SwingStrategy — init validation
# ════════════════════════════════════════════════════════════════════════════

class TestSwingStrategyInit(unittest.TestCase):

    def test_valid_config(self):
        s = _swing()
        self.assertEqual(s.sw.symbol, "BTCUSDT")
        self.assertEqual(len(s.sw.support), 3)
        self.assertEqual(len(s.sw.resistance), 3)

    def test_strategy_type(self):
        self.assertEqual(SwingStrategy.STRATEGY_TYPE, "swing")

    def test_support_empty_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(support_levels=[]))

    def test_resistance_empty_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(resistance_levels=[]))

    def test_order_size_zero_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(order_size_usdt=0))

    def test_order_size_negative_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(order_size_usdt=-50))

    def test_max_positions_zero_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(max_positions=0))

    def test_sl_pct_ge_one_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(sl_pct=1.0))

    def test_sl_pct_negative_raises(self):
        with self.assertRaises(ValueError):
            SwingStrategy(_swing_cfg_override(sl_pct=-0.01))

    def test_levels_sorted_ascending(self):
        s = _swing()
        sup_prices = [lvl.price for lvl in s.sw.support]
        res_prices = [lvl.price for lvl in s.sw.resistance]
        self.assertEqual(sup_prices, sorted(sup_prices))
        self.assertEqual(res_prices, sorted(res_prices))


# ════════════════════════════════════════════════════════════════════════════
# SwingStrategy — _find_tp
# ════════════════════════════════════════════════════════════════════════════

class TestSwingFindTp(unittest.TestCase):

    def setUp(self):
        self.s = _swing()   # resistance = [78000, 80000, 82500]

    def test_returns_lowest_resistance_above_entry(self):
        self.assertAlmostEqual(self.s._find_tp(75000.0), 78000.0)

    def test_returns_second_level_when_entry_above_first(self):
        self.assertAlmostEqual(self.s._find_tp(79000.0), 80000.0)

    def test_fallback_when_no_resistance_above(self):
        expected = round(83000.0 * 1.04, 2)
        self.assertAlmostEqual(self.s._find_tp(83000.0), expected)

    def test_exact_match_excluded(self):
        # entry == 78000 → 78000 is NOT strictly above, so next level is used
        tp = self.s._find_tp(78000.0)
        self.assertGreater(tp, 78000.0)

    def test_fallback_tp_pct_respected(self):
        # tp_pct_fallback=0.04; entry way above all resistance
        self.s.sw.tp_pct_fallback = 0.05
        tp = self.s._find_tp(90000.0)
        self.assertAlmostEqual(tp, round(90000.0 * 1.05, 2))


# ════════════════════════════════════════════════════════════════════════════
# SwingStrategy — _trend_ok
# ════════════════════════════════════════════════════════════════════════════

class TestSwingTrendOk(unittest.TestCase):

    def setUp(self):
        self.s = _swing()
        self.s._trend_filter   = True
        self.s._ema200_filter  = True
        self.s._rsi_stale_secs = 3600.0
        self.s._rsi_buy_max    = 52.0

    def test_no_indicators_bypasses_filter(self):
        # Default state: last_rsi=None, last_ind_ts=0 → stale → bypass → True
        self.assertTrue(self.s._trend_ok(75000.0))

    def test_rsi_within_limit_allows_entry(self):
        self.s.sw.last_rsi    = 48.0
        self.s.sw.last_ind_ts = time.time()
        self.assertTrue(self.s._trend_ok(75000.0))

    def test_rsi_overbought_blocks_entry(self):
        self.s.sw.last_rsi    = 55.0
        self.s.sw.last_ind_ts = time.time()
        self.assertFalse(self.s._trend_ok(75000.0))

    def test_price_above_ema200_allows_entry(self):
        self.s.sw.last_ema200 = 70000.0
        self.s.sw.last_rsi    = 48.0
        self.s.sw.last_ind_ts = time.time()
        self.assertTrue(self.s._trend_ok(75000.0))

    def test_price_below_ema200_blocks_entry(self):
        self.s.sw.last_ema200 = 80000.0
        self.s.sw.last_rsi    = 48.0
        self.s.sw.last_ind_ts = time.time()
        self.assertFalse(self.s._trend_ok(75000.0))  # price=75000 < ema200=80000

    def test_stale_indicators_bypass_both_filters(self):
        self.s.sw.last_rsi    = 99.0     # would block if fresh
        self.s.sw.last_ema200 = 99999.0  # would block if fresh
        self.s.sw.last_ind_ts = 0.0      # stale → bypass
        self.assertTrue(self.s._trend_ok(75000.0))

    def test_trend_filter_disabled_ignores_rsi(self):
        self.s._trend_filter  = False
        self.s._ema200_filter = False
        self.s.sw.last_rsi    = 99.0
        self.s.sw.last_ind_ts = time.time()
        self.assertTrue(self.s._trend_ok(75000.0))


# ════════════════════════════════════════════════════════════════════════════
# SwingStrategy — _compute_sl
# ════════════════════════════════════════════════════════════════════════════

class TestSwingComputeSl(unittest.TestCase):

    def setUp(self):
        self.s = _swing()
        self.s._atr_sl_mult = 1.5

    def test_atr_based_sl(self):
        self.s.sw.last_atr = 1000.0
        sl = self.s._compute_sl(80000.0)
        self.assertAlmostEqual(sl, round(80000.0 - 1000.0 * 1.5, 2))

    def test_fallback_sl_pct_when_no_atr(self):
        self.s.sw.last_atr = None
        sl = self.s._compute_sl(80000.0)
        self.assertAlmostEqual(sl, round(80000.0 * (1 - self.s.sw.sl_pct), 2))

    def test_atr_zero_falls_back_to_pct(self):
        self.s.sw.last_atr = 0.0   # condition is `last_atr > 0`
        sl = self.s._compute_sl(80000.0)
        self.assertAlmostEqual(sl, round(80000.0 * 0.98, 2))

    def test_sl_strictly_below_entry(self):
        self.s.sw.last_atr = 500.0
        sl = self.s._compute_sl(80000.0)
        self.assertLess(sl, 80000.0)


# ════════════════════════════════════════════════════════════════════════════
# DCAStrategy — init validation
# ════════════════════════════════════════════════════════════════════════════

def _dca_cfg(**kw):
    attrs = {k: getattr(_DcaCfg, k) for k in dir(_DcaCfg) if not k.startswith("_")}
    attrs.update(kw)
    return type("C", (), attrs)()


class TestDCAStrategyInit(unittest.TestCase):

    def test_valid_config(self):
        d = _dca()
        self.assertEqual(d.dca.symbol, "BTCUSDT")

    def test_strategy_type(self):
        self.assertEqual(DCAStrategy.STRATEGY_TYPE, "dca")

    def test_interval_zero_raises(self):
        with self.assertRaises(ValueError):
            DCAStrategy(_dca_cfg(dca_interval_h=0))

    def test_interval_negative_raises(self):
        with self.assertRaises(ValueError):
            DCAStrategy(_dca_cfg(dca_interval_h=-1))

    def test_amount_zero_raises(self):
        with self.assertRaises(ValueError):
            DCAStrategy(_dca_cfg(dca_amount_usdt=0))

    def test_max_positions_zero_raises(self):
        with self.assertRaises(ValueError):
            DCAStrategy(_dca_cfg(max_positions=0))

    def test_tp_pct_negative_raises(self):
        with self.assertRaises(ValueError):
            DCAStrategy(_dca_cfg(tp_pct=-0.01))

    def test_interval_h_converted_to_seconds(self):
        d = _dca()
        self.assertAlmostEqual(d.dca.interval_s, 4 * 3600)

    def test_sl_disabled_by_default(self):
        d = _dca()
        self.assertAlmostEqual(d.dca.sl_pct, 0.0)


# ════════════════════════════════════════════════════════════════════════════
# DCAStrategy — price calculations via _place_buy
# ════════════════════════════════════════════════════════════════════════════

class TestDCAPriceCalc(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _fake_api.post_order = AsyncMock(return_value="sim_buy_001")
        _fake_api.compute_fee = MagicMock(return_value=0.0)
        _fake_api.FEE_RATE = 0.0
        self.d = _dca()
        self.state = _state()
        self.d.ensure_schema(self.state.conn)

    async def test_tp_price_calculation(self):
        await self.d._place_buy(self.state, 50000.0)
        pos = self.d.dca.positions[-1]
        self.assertAlmostEqual(pos.tp_price, round(50000.0 * 1.03, 2))

    async def test_qty_calculation(self):
        await self.d._place_buy(self.state, 50000.0)
        pos = self.d.dca.positions[-1]
        self.assertAlmostEqual(pos.qty, 100.0 / 50000.0)

    async def test_sl_none_when_sl_pct_zero(self):
        await self.d._place_buy(self.state, 50000.0)
        pos = self.d.dca.positions[-1]
        self.assertIsNone(pos.sl_price)

    async def test_sl_price_when_sl_enabled(self):
        d2 = DCAStrategy(_dca_cfg(sl_pct=0.02))
        d2.ensure_schema(self.state.conn)
        await d2._place_buy(self.state, 50000.0)
        pos = d2.dca.positions[-1]
        self.assertAlmostEqual(pos.sl_price, round(50000.0 * 0.98, 2))

    async def test_position_stored_in_db(self):
        await self.d._place_buy(self.state, 50000.0)
        row = self.state.conn.execute(
            "SELECT entry_price FROM dca_positions WHERE status='buy_placed'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row[0], 50000.0)


# ════════════════════════════════════════════════════════════════════════════
# DCAStrategy — simulation fill detection
# ════════════════════════════════════════════════════════════════════════════

class TestDCASimFills(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _fake_api.post_order = AsyncMock(return_value="sim_001")
        _fake_api.compute_fee = MagicMock(return_value=0.0)
        _fake_api.FEE_RATE = 0.0
        self.d = _dca()
        self.state = _state()
        self.d.ensure_schema(self.state.conn)

    @staticmethod
    def _ts(bid, ask):
        ts = MagicMock()
        ts.best_bid = bid
        ts.best_ask = ask
        return ts

    async def test_buy_fills_when_ask_lte_entry(self):
        await self.d._place_buy(self.state, 50000.0)
        pos = self.d.dca.positions[-1]
        await self.d._check_sim_fills(self.state, self._ts(bid=50100.0, ask=49900.0))
        self.assertEqual(pos.status, "long")

    async def test_buy_does_not_fill_when_ask_above_entry(self):
        await self.d._place_buy(self.state, 50000.0)
        pos = self.d.dca.positions[-1]
        await self.d._check_sim_fills(self.state, self._ts(bid=49900.0, ask=50100.0))
        self.assertEqual(pos.status, "buy_placed")

    async def test_tp_order_placed_when_bid_gte_tp(self):
        _fake_api.post_order = AsyncMock(return_value="sim_tp_001")
        await self.d._place_buy(self.state, 50000.0)
        pos = self.d.dca.positions[-1]
        pos.status = "long"   # advance manually
        tp = pos.tp_price
        await self.d._check_sim_fills(self.state, self._ts(bid=tp + 10, ask=tp + 20))
        self.assertEqual(pos.status, "tp_placed")

    async def test_sl_hit_closes_position(self):
        d2 = DCAStrategy(_dca_cfg(sl_pct=0.02))
        d2.ensure_schema(self.state.conn)
        await d2._place_buy(self.state, 50000.0)
        pos = d2.dca.positions[-1]
        pos.status = "long"
        sl = pos.sl_price
        await d2._check_sim_fills(self.state, self._ts(bid=sl - 10, ask=sl - 5))
        self.assertEqual(pos.status, "closed")


# ════════════════════════════════════════════════════════════════════════════
# SwingHoldStrategy — init validation
# ════════════════════════════════════════════════════════════════════════════

class TestSwingHoldStrategyInit(unittest.TestCase):

    def test_valid_config(self):
        s = _sh()
        self.assertEqual(s.sh.symbol, "BTCUSDT")

    def test_strategy_type(self):
        self.assertEqual(SwingHoldStrategy.STRATEGY_TYPE, "swinghold")

    def test_empty_support_raises(self):
        with self.assertRaises(ValueError):
            SwingHoldStrategy(_sh_cfg_override(support_levels=[]))

    def test_empty_resistance_raises(self):
        with self.assertRaises(ValueError):
            SwingHoldStrategy(_sh_cfg_override(resistance_levels=[]))

    def test_sell_fraction_one_raises(self):
        with self.assertRaises(ValueError):
            SwingHoldStrategy(_sh_cfg_override(sell_fraction=1.0))

    def test_sell_fraction_zero_raises(self):
        with self.assertRaises(ValueError):
            SwingHoldStrategy(_sh_cfg_override(sell_fraction=0.0))

    def test_order_size_zero_raises(self):
        with self.assertRaises(ValueError):
            SwingHoldStrategy(_sh_cfg_override(order_size_usdt=0))

    def test_max_positions_zero_raises(self):
        with self.assertRaises(ValueError):
            SwingHoldStrategy(_sh_cfg_override(max_positions=0))

    def test_hold_fraction_is_complement_of_sell(self):
        s = _sh()
        self.assertAlmostEqual(s.sh.hold_fraction, round(1.0 - s.sh.sell_fraction, 6))

    def test_levels_sorted_ascending(self):
        s = _sh()
        self.assertEqual(s.sh.support,    sorted(s.sh.support))
        self.assertEqual(s.sh.resistance, sorted(s.sh.resistance))


# ════════════════════════════════════════════════════════════════════════════
# SwingHoldStrategy — _resistances_above / _next_resistance
# ════════════════════════════════════════════════════════════════════════════

class TestSwingHoldResistances(unittest.TestCase):

    def setUp(self):
        self.s = _sh()   # resistance = [78000, 80000, 82500, 85000]

    def test_resistances_above_all_below(self):
        self.assertEqual(self.s._resistances_above(90000.0), [])

    def test_resistances_above_some(self):
        above = self.s._resistances_above(79000.0)
        self.assertEqual(above, [80000.0, 82500.0, 85000.0])

    def test_resistances_above_all_above(self):
        above = self.s._resistances_above(60000.0)
        self.assertEqual(len(above), 4)

    def test_resistances_above_excludes_exact_match(self):
        above = self.s._resistances_above(78000.0)
        self.assertNotIn(78000.0, above)
        self.assertEqual(above, [80000.0, 82500.0, 85000.0])

    def test_next_resistance_idx0(self):
        self.assertAlmostEqual(self.s._next_resistance(75000.0, 0), 78000.0)

    def test_next_resistance_idx1(self):
        self.assertAlmostEqual(self.s._next_resistance(75000.0, 1), 80000.0)

    def test_next_resistance_idx2(self):
        self.assertAlmostEqual(self.s._next_resistance(75000.0, 2), 82500.0)

    def test_next_resistance_idx3(self):
        self.assertAlmostEqual(self.s._next_resistance(75000.0, 3), 85000.0)

    def test_next_resistance_out_of_range(self):
        self.assertIsNone(self.s._next_resistance(75000.0, 10))

    def test_next_resistance_none_when_entry_above_all(self):
        self.assertIsNone(self.s._next_resistance(90000.0, 0))


# ════════════════════════════════════════════════════════════════════════════
# GridStrategy — cycle accounting (regression: PnL was stuck at $0)
# ════════════════════════════════════════════════════════════════════════════

# DDL mirrors live_bot.py (migration v2 + v3) so save/restore can be exercised.
_GRID_SCHEMA = """
CREATE TABLE grid_state (
    symbol TEXT PRIMARY KEY, grid_lower REAL, grid_upper REAL, grid_step REAL,
    order_size_usdt REAL, total_cycles INTEGER DEFAULT 0,
    total_profit_usd REAL DEFAULT 0.0, initialised INTEGER DEFAULT 0,
    halted INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE grid_levels (
    symbol TEXT, level_price REAL, buy_order_id TEXT, sell_order_id TEXT,
    buy_price REAL, sell_price REAL, status TEXT DEFAULT 'idle',
    filled_at_ts REAL, updated_at REAL, entry_price REAL,
    PRIMARY KEY (symbol, level_price));
"""


class TestGridCycleAccounting(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _fake_api.post_order  = AsyncMock(return_value="sim_x")
        _fake_api.compute_fee = MagicMock(return_value=0.0)
        _fake_api.FEE_RATE    = 0.0
        self.g     = _grid()
        self.state = _state()

    async def test_buy_fill_records_entry_price(self):
        lvl = self.g.levels[0]                  # price 100
        lvl.buy_price = 100.0
        lvl.status    = "buy_placed"
        await self.g._on_buy_filled(self.state, lvl)
        self.assertEqual(lvl.status, "sell_placed")
        self.assertAlmostEqual(lvl.sell_price, 101.0)   # buy + step(1.0)
        self.assertAlmostEqual(lvl.entry_price, 100.0)  # entry remembered

    async def test_buy_then_sell_counts_cycle_and_books_profit(self):
        lvl = self.g.levels[0]
        lvl.buy_price = 100.0
        lvl.status    = "buy_placed"
        await self.g._on_buy_filled(self.state, lvl)    # → SELL at 101, entry 100
        await self.g._on_sell_filled(self.state, lvl)   # SELL fills
        # qty = 50/100 = 0.5 ; profit = (101-100)*0.5 = 0.5 (fees 0)
        self.assertEqual(self.g.grid.total_cycles, 1)
        self.assertAlmostEqual(self.g.grid.total_profit_usd, 0.5)
        self.assertIsNone(lvl.entry_price)              # cleared after booking

    async def test_profit_is_net_of_fees(self):
        _fake_api.FEE_RATE = 0.001   # 0.1% per leg (round_trip_pnl model)
        lvl = self.g.levels[0]
        lvl.buy_price = 100.0
        lvl.status    = "buy_placed"
        await self.g._on_buy_filled(self.state, lvl)
        await self.g._on_sell_filled(self.state, lvl)
        # qty 0.5; gross (101-100)*0.5 = 0.5; fees 0.001*0.5*(100+101) = 0.1005 → 0.3995
        self.assertAlmostEqual(self.g.grid.total_profit_usd, 0.3995)

    async def test_init_placed_sell_does_not_count_cycle(self):
        """A SELL placed at init (no prior BUY → entry_price None) must not book PnL."""
        lvl = self.g.levels[3]                  # price 103
        lvl.sell_price  = 103.0
        lvl.entry_price = None
        lvl.status      = "sell_placed"
        await self.g._on_sell_filled(self.state, lvl)
        self.assertEqual(self.g.grid.total_cycles, 0)
        self.assertAlmostEqual(self.g.grid.total_profit_usd, 0.0)


class TestGridEntryPricePersistence(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        _fake_api.post_order  = AsyncMock(return_value="sim_x")
        _fake_api.compute_fee = MagicMock(return_value=0.0)
        _fake_api.FEE_RATE = 0.0
        self.state = _state()
        self.state.conn.executescript(_GRID_SCHEMA)

    async def test_entry_price_survives_save_and_restore(self):
        g1  = _grid()
        lvl = g1.levels[0]
        lvl.buy_price = 100.0
        lvl.status    = "buy_placed"
        await g1._on_buy_filled(self.state, lvl)        # entry_price = 100, sell_placed
        g1._save_state(self.state.conn)

        g2 = _grid()
        await g2.restore_from_db(self.state)
        restored = g2.levels[0]
        self.assertEqual(restored.status, "sell_placed")
        self.assertAlmostEqual(restored.entry_price, 100.0)


if __name__ == "__main__":
    unittest.main()
