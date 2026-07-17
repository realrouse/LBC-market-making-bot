# pylint: disable=protected-access
"""api_mexc.get_account / get_balance — signed spot-account read.

Exercises the real function body (no network) via a fake aiohttp session: response
parsing, the sim/no-creds short-circuit, and the load-bearing distinction that an
unreadable key (HTTP 400 code=700007 "no permission" — the state of the real LBC key)
returns None = UNKNOWN, never 0.0, so a caller can't mistake it for an empty wallet.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_mexc  # noqa: E402


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):   # noqa: ARG002 (aiohttp signature)
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


class _FakeSession:
    """Minimal stand-in: .get() returns an async-context-manager response, like aiohttp."""

    def __init__(self, status, payload):
        self._resp = _FakeResp(status, payload)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self._resp


_ACCOUNT_OK = {
    "canTrade": True,
    "permissions": ["SPOT"],
    "balances": [
        {"asset": "USDT", "free": "100.00000000", "locked": "0"},
        {"asset": "LBC",  "free": "26894.83",     "locked": "12.5"},
        {"asset": "ETH",  "free": "0",            "locked": "0"},
    ],
}


class TestGetAccount(unittest.IsolatedAsyncioTestCase):

    async def test_parses_balances_permissions_and_can_trade(self):
        sess = _FakeSession(200, _ACCOUNT_OK)
        acct = await api_mexc.get_account(sess, api_key="k", api_secret="s")
        self.assertIsNotNone(acct)
        self.assertTrue(acct["can_trade"])
        self.assertEqual(acct["permissions"], ["SPOT"])
        self.assertEqual(acct["balances"]["USDT"], {"free": 100.0, "locked": 0.0})
        self.assertEqual(acct["balances"]["LBC"], {"free": 26894.83, "locked": 12.5})
        # a signed request actually went out (signature appended, apikey header set)
        _url, kw = sess.calls[0]
        self.assertIn("signature", kw["params"])
        self.assertEqual(kw["headers"]["X-MEXC-APIKEY"], "k")

    async def test_no_credentials_is_sim_none_not_empty(self):
        sess = _FakeSession(200, _ACCOUNT_OK)
        self.assertIsNone(await api_mexc.get_account(sess, api_key="", api_secret=""))
        self.assertEqual(sess.calls, [])   # short-circuits before any request

    async def test_no_permission_700007_returns_none(self):
        # The real LBC key's current state: valid for orders, forbidden on /account.
        sess = _FakeSession(400, {"code": 700007, "msg": "No permission to access the endpoint."})
        self.assertIsNone(await api_mexc.get_account(sess, api_key="k", api_secret="s"))

    async def test_http_error_returns_none(self):
        sess = _FakeSession(418, {"code": -1003, "msg": "rate limited"})
        self.assertIsNone(await api_mexc.get_account(sess, api_key="k", api_secret="s"))


class TestGetBalance(unittest.IsolatedAsyncioTestCase):

    async def test_returns_free_for_asset_case_insensitive(self):
        sess = _FakeSession(200, _ACCOUNT_OK)
        self.assertEqual(await api_mexc.get_balance(sess, "usdt", api_key="k", api_secret="s"), 100.0)

    async def test_absent_asset_is_zero_when_account_readable(self):
        sess = _FakeSession(200, _ACCOUNT_OK)
        self.assertEqual(await api_mexc.get_balance(sess, "DOGE", api_key="k", api_secret="s"), 0.0)

    async def test_unreadable_account_is_none_not_zero(self):
        # UNKNOWN must not collapse to 0.0 — the caller falls back to internal tracking.
        sess = _FakeSession(400, {"code": 700007, "msg": "No permission"})
        self.assertIsNone(await api_mexc.get_balance(sess, "USDT", api_key="k", api_secret="s"))


class TestGetOrder(unittest.IsolatedAsyncioTestCase):
    """get_order carries the filled amounts (executed_qty + quote spent) that
    get_order_status omits — the basis for crediting REAL holdings on a fill."""

    async def test_filled_order_yields_avg_price(self):
        sess = _FakeSession(200, {"status": "FILLED", "origQty": "4000",
                                  "executedQty": "4000", "cummulativeQuoteQty": "10.0",
                                  "side": "BUY"})
        o = await api_mexc.get_order(sess, "LBCUSDT", 123, api_key="k", api_secret="s")
        self.assertEqual(o["status"], "FILLED")
        self.assertEqual(o["executed_qty"], 4000.0)
        self.assertEqual(o["cummulative_quote_qty"], 10.0)
        self.assertAlmostEqual(o["avg_price"], 0.0025)

    async def test_partial_fill(self):
        sess = _FakeSession(200, {"status": "PARTIALLY_FILLED", "origQty": "4000",
                                  "executedQty": "1000", "cummulativeQuoteQty": "2.5"})
        o = await api_mexc.get_order(sess, "LBCUSDT", 123, api_key="k", api_secret="s")
        self.assertEqual(o["executed_qty"], 1000.0)
        self.assertAlmostEqual(o["avg_price"], 0.0025)

    async def test_unfilled_order_avg_price_none(self):
        sess = _FakeSession(200, {"status": "NEW", "origQty": "4000",
                                  "executedQty": "0", "cummulativeQuoteQty": "0"})
        o = await api_mexc.get_order(sess, "LBCUSDT", 123, api_key="k", api_secret="s")
        self.assertEqual(o["status"], "NEW")
        self.assertIsNone(o["avg_price"])

    async def test_sim_id_and_no_creds_return_none(self):
        sess = _FakeSession(200, {"status": "FILLED"})
        self.assertIsNone(await api_mexc.get_order(sess, "LBCUSDT", "sim_abc", api_key="k", api_secret="s"))
        self.assertIsNone(await api_mexc.get_order(sess, "LBCUSDT", 1, api_key="", api_secret=""))

    async def test_error_status_returns_none(self):
        sess = _FakeSession(400, {"code": -2013, "msg": "Order does not exist."})
        self.assertIsNone(await api_mexc.get_order(sess, "LBCUSDT", 999, api_key="k", api_secret="s"))


class _RecordingSession:
    """Captures the verb/url/kwargs of one request so we can assert on framing."""

    def __init__(self, status=200, payload=None):
        self._resp = _FakeResp(status, payload if payload is not None else {})
        self.calls = []

    def _rec(self, verb):
        def go(url, **kw):
            self.calls.append((verb, url, kw))
            return self._resp
        return go

    def __getattr__(self, name):
        if name in ("get", "post", "put", "delete"):
            return self._rec(name)
        raise AttributeError(name)


class TestWriteFraming(unittest.IsolatedAsyncioTestCase):
    """Regression guards for the two bugs that broke the first REAL MEXC order.

    Both were invisible for the life of the project because every MEXC bot was
    simulated — post_order never reached the live endpoint until 2026-07-17.
    """

    async def test_post_order_sends_content_type_json(self):
        # MEXC 400s a POST without Content-Type: application/json (code 700013),
        # even though every param is in the query string and the body is empty.
        sess = _RecordingSession(200, {"orderId": "C01__abc"})
        api_mexc._SYMBOL_PRECISION["LBCUSDT"] = (6, 3)
        oid = await api_mexc.post_order(sess, "LBCUSDT", 0.0025, 10.0,
                                        api_key="k", api_secret="s", side="BUY")
        self.assertEqual(oid, "C01__abc")
        verb, _url, kw = sess.calls[-1]
        self.assertEqual(verb, "post")
        self.assertEqual(kw["headers"].get("Content-Type"), "application/json")

    async def test_get_order_does_not_send_content_type(self):
        # GETs must NOT carry it — only writes are gated.
        sess = _RecordingSession(200, {"status": "NEW", "origQty": "1", "executedQty": "0",
                                       "cummulativeQuoteQty": "0", "side": "BUY"})
        await api_mexc.get_order(sess, "LBCUSDT", "C01__abc", api_key="k", api_secret="s")
        _verb, _url, kw = sess.calls[-1]
        self.assertNotIn("Content-Type", kw["headers"])

    async def test_string_order_id_survives_to_the_wire(self):
        # MEXC order ids are opaque strings ("C01__…"). int() raised, the broad except
        # swallowed it, and every cancel/poll silently became a no-op.
        sess = _RecordingSession(200, {"status": "CANCELED"})
        ok = await api_mexc.cancel_order(sess, "LBCUSDT", "C01__4465xyz",
                                         api_key="k", api_secret="s")
        self.assertTrue(ok)
        _verb, _url, kw = sess.calls[-1]
        self.assertEqual(kw["params"]["orderId"], "C01__4465xyz")

    async def test_cancel_of_string_id_is_not_swallowed_into_false(self):
        sess = _RecordingSession(200, {"status": "CANCELED"})
        self.assertTrue(await api_mexc.cancel_order(sess, "LBCUSDT", "C01__zzz",
                                                    api_key="k", api_secret="s"))

    async def test_get_order_accepts_string_id(self):
        sess = _RecordingSession(200, {"status": "FILLED", "origQty": "4000",
                                       "executedQty": "4000", "cummulativeQuoteQty": "10.0",
                                       "side": "BUY"})
        got = await api_mexc.get_order(sess, "LBCUSDT", "C01__str", api_key="k", api_secret="s")
        self.assertIsNotNone(got)
        _verb, _url, kw = sess.calls[-1]
        self.assertEqual(kw["params"]["orderId"], "C01__str")


if __name__ == "__main__":
    unittest.main()
