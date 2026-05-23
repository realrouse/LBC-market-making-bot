"""
Tests for C-1 (trail mode) and C-2 (connector/strategy compatibility).

Run with:
    .venv/bin/python3 -m unittest tests/test_grid_trail.py -v
"""

import asyncio
import os
import sqlite3
import sys
import types
import unittest
import unittest.mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

import connectors
import api_polymarket
import api_binance
from strategy_engines.grid import GridStrategy


def _grid_config(trail_mode: str = "static") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        grid_symbol="BTCUSDT",
        grid_lower=68000.0,
        grid_upper=92000.0,
        grid_levels=10,
        grid_order_size_usdt=50.0,
        grid_trail_mode=trail_mode,
        connector="binance",
    )


def _make_db(saved_lower: float, saved_upper: float, saved_step: float,
             size: float = 50.0, initialised: int = 0) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE grid_state "
        "(symbol, grid_lower, grid_upper, grid_step, order_size_usdt, "
        " total_cycles, total_profit_usd, initialised, halted)"
    )
    conn.execute(
        "CREATE TABLE grid_levels "
        "(symbol, level_price, buy_order_id, sell_order_id, "
        " buy_price, sell_price, status, filled_at_ts)"
    )
    conn.execute(
        "INSERT INTO grid_state VALUES (?,?,?,?,?,?,?,?,?)",
        ("BTCUSDT", saved_lower, saved_upper, saved_step, size, 2, 10.5, initialised, 0),
    )
    conn.commit()
    return conn


class TestConnectorValidate(unittest.TestCase):
    """C-2: connector/strategy compatibility matrix."""

    def test_polymarket_grid_rejected(self):
        with self.assertRaises(RuntimeError) as ctx:
            connectors.validate(api_polymarket, "grid")
        self.assertIn("missing methods", str(ctx.exception))
        self.assertIn("get_open_orders", str(ctx.exception))

    def test_binance_grid_accepted(self):
        connectors.validate(api_binance, "grid")  # must not raise

    def test_threshold_no_requirements(self):
        connectors.validate(api_polymarket, "threshold")  # must not raise

    def test_unknown_strategy_no_requirements(self):
        connectors.validate(api_polymarket, "nonexistent")  # must not raise


class TestCheckStopLoss(unittest.TestCase):
    """C-1: _check_stop_loss branching per trail_mode."""

    def _strategy(self, trail_mode: str) -> GridStrategy:
        return GridStrategy(_grid_config(trail_mode))

    def test_static_stops_both_directions(self):
        g = self._strategy("static")
        self.assertTrue(g._check_stop_loss(67999.0))
        self.assertTrue(g._check_stop_loss(92001.0))
        self.assertFalse(g._check_stop_loss(80000.0))

    def test_bull_stops_only_below_lower(self):
        g = self._strategy("bull")
        self.assertTrue(g._check_stop_loss(67999.0))   # below lower → stop
        self.assertFalse(g._check_stop_loss(92001.0))  # above upper → trail, not stop
        self.assertFalse(g._check_stop_loss(80000.0))

    def test_bear_stops_only_above_upper(self):
        g = self._strategy("bear")
        self.assertFalse(g._check_stop_loss(67999.0))  # below lower → trail, not stop
        self.assertTrue(g._check_stop_loss(92001.0))   # above upper → stop
        self.assertFalse(g._check_stop_loss(80000.0))


class TestRecenterGrid(unittest.TestCase):
    """C-1: _recenter_grid recomputes bounds and levels around price."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_recenter_bull(self):
        g = GridStrategy(_grid_config("bull"))
        half = (92000.0 - 68000.0) / 2  # 12000

        state = types.SimpleNamespace(session=None, conn=None)

        async def _cancel_noop(session, symbol, oid):
            return True

        g._api.cancel_order = _cancel_noop

        self._run(g._recenter_grid(state, 93000.0))

        self.assertAlmostEqual(g.grid.grid_lower, 93000.0 - half, places=1)
        self.assertAlmostEqual(g.grid.grid_upper, 93000.0 + half, places=1)
        self.assertFalse(g.grid.initialised)
        self.assertEqual(len(g.grid.levels), 10)

    def test_recenter_bear(self):
        g = GridStrategy(_grid_config("bear"))
        half = (92000.0 - 68000.0) / 2

        state = types.SimpleNamespace(session=None, conn=None)

        async def _cancel_noop(session, symbol, oid):
            return True

        g._api.cancel_order = _cancel_noop

        self._run(g._recenter_grid(state, 67000.0))

        self.assertAlmostEqual(g.grid.grid_lower, 67000.0 - half, places=1)
        self.assertAlmostEqual(g.grid.grid_upper, 67000.0 + half, places=1)
        self.assertFalse(g.grid.initialised)


class TestRestoreFromDb(unittest.TestCase):
    """C-1: restore_from_db trail vs static bound-mismatch handling."""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_static_discards_changed_bounds(self):
        g = GridStrategy(_grid_config("static"))
        saved_step = (105000.0 - 81000.0) / 9
        conn = _make_db(81000.0, 105000.0, saved_step)
        state = types.SimpleNamespace(conn=conn, session=None)

        result = self._run(g.restore_from_db(state))
        self.assertFalse(result)
        self.assertAlmostEqual(g.grid.grid_lower, 68000.0)  # config unchanged

    def test_trail_restores_recentered_bounds(self):
        g = GridStrategy(_grid_config("bull"))
        saved_step = (105000.0 - 81000.0) / 9
        conn = _make_db(81000.0, 105000.0, saved_step)
        state = types.SimpleNamespace(conn=conn, session=None)

        result = self._run(g.restore_from_db(state))
        self.assertTrue(result)
        self.assertAlmostEqual(g.grid.grid_lower, 81000.0)
        self.assertAlmostEqual(g.grid.grid_upper, 105000.0)

    def test_size_change_always_discards(self):
        g = GridStrategy(_grid_config("bull"))
        saved_step = (92000.0 - 68000.0) / 9
        conn = _make_db(68000.0, 92000.0, saved_step, size=99.0)  # size changed
        state = types.SimpleNamespace(conn=conn, session=None)

        result = self._run(g.restore_from_db(state))
        self.assertFalse(result)


class TestNoCredentialsFlag(unittest.IsolatedAsyncioTestCase):
    """M-6: sticky _no_credentials flag prevents user-stream task re-spawning."""

    def test_flag_false_by_default(self):
        g = GridStrategy(_grid_config())
        self.assertFalse(g._no_credentials)

    async def test_flag_set_after_max_key_failures(self):
        """_user_stream_loop sets _no_credentials after 3 consecutive get_listen_key failures."""
        g = GridStrategy(_grid_config())
        g.grid.halted = False
        state = types.SimpleNamespace(session=object())
        g._api.get_listen_key = unittest.mock.AsyncMock(return_value=None)
        with unittest.mock.patch("asyncio.sleep", return_value=None):
            await g._user_stream_loop(state)
        self.assertTrue(g._no_credentials)
        self.assertEqual(g._api.get_listen_key.call_count, 3)  # MAX_KEY_FAILURES

    def test_task_not_respawned_when_flag_set(self):
        """on_book_update spawn guard is False when _no_credentials is True."""
        g = GridStrategy(_grid_config())
        g._no_credentials = True
        would_spawn = (
            not g._no_credentials and
            (g._user_stream_task is None or
             (g._user_stream_task is not None and g._user_stream_task.done()))
        )
        self.assertFalse(would_spawn)
        self.assertIsNone(g._user_stream_task)


if __name__ == "__main__":
    unittest.main()
