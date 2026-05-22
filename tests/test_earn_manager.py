"""
Tests for bot/earn_manager.py

All tests are fully offline — no real Binance API calls.
HTTP responses are mocked via unittest.mock.

Coverage:
  TestSimMode           — behaviour without credentials
  TestGetPosition       — normal + error + empty
  TestGetApr            — normal + error
  TestSubscribe         — success, too small, no product, error
  TestRedeem            — success, cap at parked, nothing parked, error
  TestRedeemAll         — success, nothing parked, error
  TestParkIdle          — various balances vs keep_liquid
  TestEnsureLiquid      — no redeem needed, partial redeem, full parked
  TestProductDiscovery  — caching behaviour
  TestHelperFunctions   — _sign, _has_creds
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import earn_manager as em
from earn_manager import EarnManager, MIN_LIQUID_USDT, MIN_SUBSCRIBE_USDT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session_mock():
    """Return a mock aiohttp.ClientSession."""
    return MagicMock()


def _resp_mock(data: dict, status: int = 200):
    """Return a context-manager mock that yields a response with json()."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__  = AsyncMock(return_value=False)
    return cm


def _product_list(product_id="USDT001", apr="0.04"):
    return {
        "rows": [{"productId": product_id,
                  "latestAnnualPercentageRate": apr,
                  "status": "SUBSCRIBABLE"}],
        "total": 1,
    }


def _with_creds(fn):
    """Decorator: inject fake Binance credentials; works for both sync and async test methods."""
    if asyncio.iscoroutinefunction(fn):
        async def wrapper(self):
            with patch.dict(os.environ, {
                "BINANCE_API_KEY":    "test_key",
                "BINANCE_API_SECRET": "test_secret",
            }):
                await fn(self)
    else:
        def wrapper(self):
            with patch.dict(os.environ, {
                "BINANCE_API_KEY":    "test_key",
                "BINANCE_API_SECRET": "test_secret",
            }):
                fn(self)
    wrapper.__name__ = fn.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Sim mode (no credentials)
# ---------------------------------------------------------------------------

class TestSimMode(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        os.environ.pop("BINANCE_API_KEY",    None)
        os.environ.pop("BINANCE_API_SECRET", None)

    async def test_get_position_returns_zero(self):
        earn = EarnManager(_session_mock())
        self.assertAlmostEqual(await earn.get_position(), 0.0)

    async def test_get_apr_returns_zero(self):
        earn = EarnManager(_session_mock())
        self.assertAlmostEqual(await earn.get_apr(), 0.0)

    async def test_subscribe_returns_true(self):
        earn = EarnManager(_session_mock())
        self.assertTrue(await earn.subscribe(1000.0))

    async def test_redeem_returns_true(self):
        earn = EarnManager(_session_mock())
        self.assertTrue(await earn.redeem(1000.0))

    async def test_redeem_all_returns_true(self):
        earn = EarnManager(_session_mock())
        self.assertTrue(await earn.redeem_all())

    async def test_park_idle_no_api_call(self):
        session = _session_mock()
        earn = EarnManager(session)
        await earn.park_idle(5000.0)
        session.get.assert_not_called()
        session.post.assert_not_called()

    async def test_ensure_liquid_returns_true(self):
        earn = EarnManager(_session_mock())
        self.assertTrue(await earn.ensure_liquid(10_000.0))

    async def test_enabled_is_false_without_creds(self):
        earn = EarnManager(_session_mock())
        self.assertFalse(earn._enabled)


# ---------------------------------------------------------------------------
# get_position
# ---------------------------------------------------------------------------

class TestGetPosition(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_returns_total_amount(self):
        session = _session_mock()
        session.get.return_value = _resp_mock(
            {"rows": [{"totalAmount": "4980.5"}], "total": 1}
        )
        earn = EarnManager(session)
        pos = await earn.get_position()
        self.assertAlmostEqual(pos, 4980.5)

    @_with_creds
    async def test_sums_multiple_rows(self):
        session = _session_mock()
        session.get.return_value = _resp_mock(
            {"rows": [{"totalAmount": "1000.0"}, {"totalAmount": "500.0"}], "total": 2}
        )
        earn = EarnManager(session)
        self.assertAlmostEqual(await earn.get_position(), 1500.0)

    @_with_creds
    async def test_empty_rows_returns_zero(self):
        session = _session_mock()
        session.get.return_value = _resp_mock({"rows": [], "total": 0})
        earn = EarnManager(session)
        self.assertAlmostEqual(await earn.get_position(), 0.0)

    @_with_creds
    async def test_network_error_returns_zero(self):
        session = _session_mock()
        session.get.side_effect = Exception("network error")
        earn = EarnManager(session)
        self.assertAlmostEqual(await earn.get_position(), 0.0)


# ---------------------------------------------------------------------------
# get_apr
# ---------------------------------------------------------------------------

class TestGetApr(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_returns_apr_as_float(self):
        session = _session_mock()
        session.get.return_value = _resp_mock(_product_list(apr="0.045"))
        earn = EarnManager(session)
        self.assertAlmostEqual(await earn.get_apr(), 0.045)

    @_with_creds
    async def test_empty_product_list_returns_zero(self):
        session = _session_mock()
        session.get.return_value = _resp_mock({"rows": [], "total": 0})
        earn = EarnManager(session)
        self.assertAlmostEqual(await earn.get_apr(), 0.0)

    @_with_creds
    async def test_error_returns_zero(self):
        session = _session_mock()
        session.get.side_effect = Exception("timeout")
        earn = EarnManager(session)
        self.assertAlmostEqual(await earn.get_apr(), 0.0)


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------

class TestSubscribe(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_success(self):
        session = _session_mock()
        # First GET: product discovery.  POST: subscribe.
        session.get.return_value  = _resp_mock(_product_list())
        session.post.return_value = _resp_mock({"success": True, "purchaseId": "abc123"})
        earn = EarnManager(session)
        self.assertTrue(await earn.subscribe(500.0))
        session.post.assert_called_once()

    @_with_creds
    async def test_amount_below_minimum_returns_false(self):
        session = _session_mock()
        earn = EarnManager(session)
        result = await earn.subscribe(MIN_SUBSCRIBE_USDT - 0.01)
        self.assertFalse(result)
        session.post.assert_not_called()

    @_with_creds
    async def test_no_product_returns_false(self):
        session = _session_mock()
        session.get.return_value = _resp_mock({"rows": [], "total": 0})
        earn = EarnManager(session)
        self.assertFalse(await earn.subscribe(100.0))
        session.post.assert_not_called()

    @_with_creds
    async def test_api_failure_returns_false(self):
        session = _session_mock()
        session.get.return_value  = _resp_mock(_product_list())
        session.post.return_value = _resp_mock({"success": False, "msg": "limit exceeded"})
        earn = EarnManager(session)
        self.assertFalse(await earn.subscribe(100.0))

    @_with_creds
    async def test_network_error_returns_false(self):
        session = _session_mock()
        session.get.return_value = _resp_mock(_product_list())
        session.post.side_effect = Exception("connection refused")
        earn = EarnManager(session)
        self.assertFalse(await earn.subscribe(100.0))

    @_with_creds
    async def test_exact_minimum_is_allowed(self):
        session = _session_mock()
        session.get.return_value  = _resp_mock(_product_list())
        session.post.return_value = _resp_mock({"success": True, "purchaseId": "xyz"})
        earn = EarnManager(session)
        self.assertTrue(await earn.subscribe(MIN_SUBSCRIBE_USDT))


# ---------------------------------------------------------------------------
# redeem
# ---------------------------------------------------------------------------

class TestRedeem(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_success(self):
        session = _session_mock()
        # Calls: get product, get position, post redeem
        session.get.side_effect = [
            _resp_mock(_product_list()),               # product discovery
            _resp_mock({"rows": [{"totalAmount": "5000"}], "total": 1}),  # position
        ]
        session.post.return_value = _resp_mock({"success": True})
        earn = EarnManager(session)
        self.assertTrue(await earn.redeem(1000.0))

    @_with_creds
    async def test_caps_at_parked_amount(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [{"totalAmount": "200"}], "total": 1}),
        ]
        session.post.return_value = _resp_mock({"success": True})
        earn = EarnManager(session)
        self.assertTrue(await earn.redeem(10_000.0))
        # Verify that the redeemed amount was capped at 200
        call_params = session.post.call_args.kwargs["params"]
        self.assertIn("200", call_params.get("amount", ""))

    @_with_creds
    async def test_nothing_parked_returns_true(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [], "total": 0}),
        ]
        earn = EarnManager(session)
        self.assertTrue(await earn.redeem(500.0))
        session.post.assert_not_called()

    @_with_creds
    async def test_api_failure_returns_false(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [{"totalAmount": "1000"}], "total": 1}),
        ]
        session.post.return_value = _resp_mock({"success": False})
        earn = EarnManager(session)
        self.assertFalse(await earn.redeem(500.0))

    @_with_creds
    async def test_network_error_returns_false(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [{"totalAmount": "1000"}], "total": 1}),
        ]
        session.post.side_effect = Exception("timeout")
        earn = EarnManager(session)
        self.assertFalse(await earn.redeem(500.0))


# ---------------------------------------------------------------------------
# redeem_all
# ---------------------------------------------------------------------------

class TestRedeemAll(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_success(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [{"totalAmount": "9500"}], "total": 1}),
        ]
        session.post.return_value = _resp_mock({"success": True})
        earn = EarnManager(session)
        self.assertTrue(await earn.redeem_all())
        # redeemAll=true must be in params
        call_params = session.post.call_args.kwargs["params"]
        self.assertEqual(call_params.get("redeemAll"), "true")

    @_with_creds
    async def test_nothing_parked_returns_true_no_post(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [], "total": 0}),
        ]
        earn = EarnManager(session)
        self.assertTrue(await earn.redeem_all())
        session.post.assert_not_called()

    @_with_creds
    async def test_api_failure_returns_false(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [{"totalAmount": "1000"}], "total": 1}),
        ]
        session.post.return_value = _resp_mock({"success": False})
        earn = EarnManager(session)
        self.assertFalse(await earn.redeem_all())


# ---------------------------------------------------------------------------
# park_idle
# ---------------------------------------------------------------------------

class TestParkIdle(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_parks_above_keep_liquid(self):
        session = _session_mock()
        session.get.return_value  = _resp_mock(_product_list())
        session.post.return_value = _resp_mock({"success": True, "purchaseId": "p1"})
        earn = EarnManager(session)
        await earn.park_idle(5000.0, keep_liquid=20.0)
        session.post.assert_called_once()
        # Should subscribe 5000 - 20 = 4980
        call_params = session.post.call_args.kwargs["params"]
        self.assertAlmostEqual(float(call_params["amount"]), 4980.0, places=4)

    @_with_creds
    async def test_balance_below_keep_liquid_no_subscribe(self):
        session = _session_mock()
        earn = EarnManager(session)
        await earn.park_idle(10.0, keep_liquid=20.0)
        session.post.assert_not_called()

    @_with_creds
    async def test_tiny_surplus_below_minimum_no_subscribe(self):
        session = _session_mock()
        earn = EarnManager(session)
        # surplus = 20.5 - 20.0 = 0.5 < MIN_SUBSCRIBE_USDT
        await earn.park_idle(20.0 + MIN_SUBSCRIBE_USDT - 0.01, keep_liquid=20.0)
        session.post.assert_not_called()

    @_with_creds
    async def test_uses_default_keep_liquid(self):
        session = _session_mock()
        session.get.return_value  = _resp_mock(_product_list())
        session.post.return_value = _resp_mock({"success": True, "purchaseId": "p2"})
        earn = EarnManager(session)
        await earn.park_idle(MIN_LIQUID_USDT + 100.0)
        session.post.assert_called_once()


# ---------------------------------------------------------------------------
# ensure_liquid
# ---------------------------------------------------------------------------

class TestEnsureLiquid(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_no_parked_returns_true_without_redeem(self):
        session = _session_mock()
        session.get.side_effect = [
            _resp_mock(_product_list()),
            _resp_mock({"rows": [], "total": 0}),
        ]
        earn = EarnManager(session)
        self.assertTrue(await earn.ensure_liquid(10_000.0))
        session.post.assert_not_called()

    @_with_creds
    async def test_needed_below_keep_liquid_no_redeem(self):
        session = _session_mock()
        earn = EarnManager(session)
        # needed = 5 < keep_liquid (default 20) → shortfall ≤ 0 → no redeem
        self.assertTrue(await earn.ensure_liquid(5.0, keep_liquid=20.0))
        session.post.assert_not_called()

    @_with_creds
    async def test_redeems_shortfall(self):
        session = _session_mock()
        # ensure_liquid calls get_position() first, then redeem() calls _product() + get_position()
        pos_row = {"rows": [{"totalAmount": "8000"}], "total": 1}
        session.get.side_effect = [
            _resp_mock(pos_row),           # ensure_liquid → get_position()
            _resp_mock(_product_list()),   # redeem → _product()
            _resp_mock(pos_row),           # redeem → get_position()
        ]
        session.post.return_value = _resp_mock({"success": True})
        earn = EarnManager(session)
        self.assertTrue(await earn.ensure_liquid(5000.0, keep_liquid=20.0))
        # shortfall = 5000 - 20 = 4980 → redeem 4980
        call_params = session.post.call_args.kwargs["params"]
        self.assertAlmostEqual(float(call_params["amount"]), 4980.0, places=4)

    @_with_creds
    async def test_caps_redeem_at_parked(self):
        session = _session_mock()
        # Only 500 parked but need 10000; redeem is capped at 500
        pos_row = {"rows": [{"totalAmount": "500"}], "total": 1}
        session.get.side_effect = [
            _resp_mock(pos_row),           # ensure_liquid → get_position()
            _resp_mock(_product_list()),   # redeem → _product()
            _resp_mock(pos_row),           # redeem → get_position()
        ]
        session.post.return_value = _resp_mock({"success": True})
        earn = EarnManager(session)
        await earn.ensure_liquid(10_000.0, keep_liquid=20.0)
        call_params = session.post.call_args.kwargs["params"]
        self.assertAlmostEqual(float(call_params["amount"]), 500.0, places=4)


# ---------------------------------------------------------------------------
# Product ID caching
# ---------------------------------------------------------------------------

class TestProductDiscovery(unittest.IsolatedAsyncioTestCase):

    @_with_creds
    async def test_product_id_cached_after_first_call(self):
        session = _session_mock()
        session.get.return_value  = _resp_mock(_product_list())
        session.post.return_value = _resp_mock({"success": True, "purchaseId": "x"})
        earn = EarnManager(session)
        # Two subscribe calls → only one product-discovery GET
        await earn.subscribe(100.0)
        await earn.subscribe(100.0)
        # get() is called once for discovery + once per position check
        discovery_calls = [
            c for c in session.get.call_args_list
            if "flexible/list" in str(c)
        ]
        self.assertEqual(len(discovery_calls), 1)

    @_with_creds
    async def test_product_id_stored_on_instance(self):
        session = _session_mock()
        session.get.return_value  = _resp_mock(_product_list(product_id="USDT42"))
        session.post.return_value = _resp_mock({"success": True, "purchaseId": "y"})
        earn = EarnManager(session)
        await earn.subscribe(100.0)
        self.assertEqual(earn._product_id, "USDT42")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestHelperFunctions(unittest.TestCase):

    def test_has_creds_false_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove keys if present
            os.environ.pop("BINANCE_API_KEY",    None)
            os.environ.pop("BINANCE_API_SECRET", None)
            self.assertFalse(em._has_creds())

    def test_has_creds_true_with_env(self):
        with patch.dict(os.environ, {
            "BINANCE_API_KEY":    "k",
            "BINANCE_API_SECRET": "s",
        }):
            self.assertTrue(em._has_creds())

    def test_sign_adds_timestamp(self):
        with patch.dict(os.environ, {"BINANCE_API_SECRET": "test"}):
            p = em._sign({"foo": "bar"})
            self.assertIn("timestamp", p)
            self.assertIsInstance(p["timestamp"], int)

    def test_sign_adds_signature(self):
        with patch.dict(os.environ, {"BINANCE_API_SECRET": "test"}):
            p = em._sign({"foo": "bar"})
            self.assertIn("signature", p)
            self.assertIsInstance(p["signature"], str)
            self.assertEqual(len(p["signature"]), 64)  # SHA-256 hex = 64 chars

    def test_sign_signature_is_deterministic_for_same_input(self):
        with patch.dict(os.environ, {"BINANCE_API_SECRET": "mysecret"}):
            import time
            fixed_ts = 1_700_000_000_000
            with patch.object(time, "time", return_value=fixed_ts / 1000):
                p1 = em._sign({"amount": "100"})
                p2 = em._sign({"amount": "100"})
            self.assertEqual(p1["signature"], p2["signature"])

    def test_headers_include_api_key(self):
        with patch.dict(os.environ, {"BINANCE_API_KEY": "mykey123"}):
            h = em._headers()
            self.assertEqual(h["X-MBX-APIKEY"], "mykey123")

    def test_min_liquid_usdt_is_positive(self):
        self.assertGreater(MIN_LIQUID_USDT, 0)

    def test_min_subscribe_usdt_is_positive(self):
        self.assertGreater(MIN_SUBSCRIBE_USDT, 0)

    def test_min_subscribe_less_than_min_liquid(self):
        self.assertLess(MIN_SUBSCRIBE_USDT, MIN_LIQUID_USDT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
