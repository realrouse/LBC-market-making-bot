"""
Tests for bot/api_binance.py and bot/api_mexc.py.

Coverage:
  - TestApiContract   : both modules expose the same public interface as api_polymarket
  - TestBinanceComputeFee / TestMexcComputeFee : pure fee arithmetic
  - TestBinanceMetadata / TestMexcMetadata     : market metadata helpers
  - TestBinanceParseBookUpdate / TestMexcParseBookUpdate : WebSocket message parsing
  - TestBinanceMakeSubscribeMsg / TestMexcMakeSubscribeMsg : subscribe message format
  - TestBinancePostOrderSimulated / TestMexcPostOrderSimulated : dry-run (no network)

All tests are offline — no real API calls.
"""

import importlib, inspect, json, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import api_binance  as binance
import api_mexc     as mexc
import api_polymarket as poly


# ── contract ──────────────────────────────────────────────────────────────────

class TestApiContract(unittest.TestCase):
    """
    All three API modules must expose the same public interface so live_bot.py
    can swap exchanges with a single import change.
    """

    REQUIRED_FUNCTIONS = [
        "compute_fee",
        "get_market_id",
        "get_market_question",
        "get_market_end_ts_ms",
        "get_market_start_ts_ms",
        "get_up_token_id",
        "get_down_token_id",
        "parse_book_update",
        "post_order",
        "make_subscribe_msg",
    ]

    def _check_module(self, mod):
        for name in self.REQUIRED_FUNCTIONS:
            self.assertTrue(
                hasattr(mod, name),
                f"{mod.__name__} is missing required function: {name}",
            )

    def test_binance_has_full_interface(self):
        self._check_module(binance)

    def test_mexc_has_full_interface(self):
        self._check_module(mexc)

    def test_polymarket_has_full_interface(self):
        self._check_module(poly)

    def test_fee_rate_is_float(self):
        for mod in (binance, mexc, poly):
            self.assertIsInstance(
                mod.FEE_RATE, float,
                f"{mod.__name__}.FEE_RATE must be a float",
            )

    def test_compute_fee_returns_float(self):
        for mod in (binance, mexc, poly):
            result = mod.compute_fee(65000.0, 0.001)
            self.assertIsInstance(
                result, float,
                f"{mod.__name__}.compute_fee must return float",
            )

    def test_parse_book_update_returns_dict_or_none(self):
        for mod in (binance, mexc, poly):
            result = mod.parse_book_update({})
            self.assertIn(
                result, [None] + [result] if isinstance(result, dict) else [None],
                f"{mod.__name__}.parse_book_update must return dict or None",
            )

    def test_parse_book_update_result_keys(self):
        REQUIRED_KEYS = {"token_id", "best_bid", "best_ask", "spread", "bid_vol", "ask_vol", "obi"}

        binance_msg = {
            "bids": [["65000.00", "0.5"]],
            "asks": [["65010.00", "0.3"]],
        }
        mexc_msg = {
            "d": {"bids": [["65000.00", "0.5"]], "asks": [["65010.00", "0.3"]]},
            "s": "BTCUSDT",
        }
        poly_msg = {
            "event_type": "book",
            "asset_id": "tok1",
            "bids": [{"price": "0.96", "size": "100"}],
            "asks": [{"price": "0.97", "size": "80"}],
        }

        for mod, msg in ((binance, binance_msg), (mexc, mexc_msg), (poly, poly_msg)):
            result = mod.parse_book_update(msg)
            if result is not None:
                self.assertTrue(
                    REQUIRED_KEYS.issubset(result.keys()),
                    f"{mod.__name__}.parse_book_update result missing keys: "
                    f"{REQUIRED_KEYS - result.keys()}",
                )


# ── Binance compute_fee ───────────────────────────────────────────────────────

class TestBinanceComputeFee(unittest.TestCase):

    def test_known_value(self):
        # FEE_RATE = 0.001; fee = 0.001 × 65000 × 0.001 = 0.065
        self.assertAlmostEqual(binance.compute_fee(65000.0, 0.001), 0.065)

    def test_zero_quantity(self):
        self.assertAlmostEqual(binance.compute_fee(65000.0, 0.0), 0.0)

    def test_zero_price(self):
        self.assertAlmostEqual(binance.compute_fee(0.0, 1.0), 0.0)

    def test_proportional_to_price(self):
        f1 = binance.compute_fee(65000.0, 0.001)
        f2 = binance.compute_fee(130000.0, 0.001)
        self.assertAlmostEqual(f2, f1 * 2)

    def test_proportional_to_quantity(self):
        f1 = binance.compute_fee(65000.0, 0.001)
        f2 = binance.compute_fee(65000.0, 0.002)
        self.assertAlmostEqual(f2, f1 * 2)

    def test_fee_rate_is_01_percent(self):
        self.assertAlmostEqual(binance.FEE_RATE, 0.001)


# ── MEXC compute_fee ──────────────────────────────────────────────────────────

class TestMexcComputeFee(unittest.TestCase):

    def test_known_value(self):
        # FEE_RATE = 0.002; fee = 0.002 × 65000 × 0.001 = 0.13
        self.assertAlmostEqual(mexc.compute_fee(65000.0, 0.001), 0.13)

    def test_zero_quantity(self):
        self.assertAlmostEqual(mexc.compute_fee(65000.0, 0.0), 0.0)

    def test_zero_price(self):
        self.assertAlmostEqual(mexc.compute_fee(0.0, 1.0), 0.0)

    def test_mexc_fee_double_binance(self):
        # MEXC 0.2% vs Binance 0.1% — same notional, MEXC is 2×
        fb = binance.compute_fee(65000.0, 0.001)
        fm = mexc.compute_fee(65000.0, 0.001)
        self.assertAlmostEqual(fm, fb * 2)

    def test_fee_rate_is_02_percent(self):
        self.assertAlmostEqual(mexc.FEE_RATE, 0.002)


# ── Binance market metadata ───────────────────────────────────────────────────

class TestBinanceMetadata(unittest.TestCase):

    def test_get_market_id(self):
        self.assertEqual(binance.get_market_id({"symbol": "BTCUSDT"}), "BTCUSDT")

    def test_get_market_id_missing(self):
        self.assertEqual(binance.get_market_id({}), "")

    def test_get_up_token_id(self):
        self.assertEqual(binance.get_up_token_id({"symbol": "BTCUSDT"}), "BTCUSDT")

    def test_get_up_token_id_missing(self):
        self.assertEqual(binance.get_up_token_id({}), "")

    def test_get_down_token_id(self):
        self.assertEqual(binance.get_down_token_id({"symbol": "BTCUSDT"}), "BTCUSDT:SELL")

    def test_get_down_token_id_missing(self):
        self.assertEqual(binance.get_down_token_id({}), ":SELL")

    def test_get_market_end_ts_ms_is_zero(self):
        # spot markets have no expiry
        self.assertEqual(binance.get_market_end_ts_ms({"symbol": "BTCUSDT"}), 0.0)

    def test_get_market_start_ts_ms_is_zero(self):
        self.assertEqual(binance.get_market_start_ts_ms({"symbol": "BTCUSDT"}), 0.0)

    def test_get_market_question_from_question(self):
        self.assertEqual(
            binance.get_market_question({"question": "BTC up?"}), "BTC up?"
        )

    def test_get_market_question_falls_back_to_symbol(self):
        self.assertEqual(
            binance.get_market_question({"symbol": "BTCUSDT"}), "BTCUSDT"
        )


# ── MEXC market metadata ──────────────────────────────────────────────────────

class TestMexcMetadata(unittest.TestCase):

    def test_get_market_id(self):
        self.assertEqual(mexc.get_market_id({"symbol": "BTCUSDT"}), "BTCUSDT")

    def test_get_market_id_missing(self):
        self.assertEqual(mexc.get_market_id({}), "")

    def test_get_up_token_id(self):
        self.assertEqual(mexc.get_up_token_id({"symbol": "BTCUSDT"}), "BTCUSDT")

    def test_get_down_token_id(self):
        self.assertEqual(mexc.get_down_token_id({"symbol": "BTCUSDT"}), "BTCUSDT:SELL")

    def test_get_market_end_ts_ms_is_zero(self):
        self.assertEqual(mexc.get_market_end_ts_ms({"symbol": "BTCUSDT"}), 0.0)

    def test_get_market_start_ts_ms_is_zero(self):
        self.assertEqual(mexc.get_market_start_ts_ms({"symbol": "BTCUSDT"}), 0.0)

    def test_down_token_has_sell_suffix(self):
        self.assertTrue(mexc.get_down_token_id({"symbol": "ETHUSDT"}).endswith(":SELL"))

    def test_up_and_down_tokens_differ(self):
        m = {"symbol": "BTCUSDT"}
        self.assertNotEqual(mexc.get_up_token_id(m), mexc.get_down_token_id(m))


# ── Binance parse_book_update ─────────────────────────────────────────────────

class TestBinanceParseBookUpdate(unittest.TestCase):

    def _individual(self, bids, asks):
        return {"bids": bids, "asks": asks}

    def _combined(self, bids, asks, stream="btcusdt@depth5"):
        return {"stream": stream, "data": {"bids": bids, "asks": asks}}

    def test_individual_best_bid(self):
        msg = self._individual([["65000.00", "0.5"], ["64990.00", "1.0"]], [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertAlmostEqual(r["best_bid"], 65000.0)

    def test_individual_best_ask(self):
        msg = self._individual([["65000.00", "0.5"]], [["65010.00", "0.3"], ["65020.00", "0.8"]])
        r = binance.parse_book_update(msg)
        self.assertAlmostEqual(r["best_ask"], 65010.0)

    def test_individual_spread(self):
        msg = self._individual([["65000.00", "0.5"]], [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertAlmostEqual(r["spread"], 10.0)

    def test_individual_token_id_is_default_symbol(self):
        msg = self._individual([["65000.00", "0.5"]], [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertEqual(r["token_id"], binance.DEFAULT_SYMBOL)

    def test_combined_token_id_from_stream(self):
        msg = self._combined([["65000.00", "0.5"]], [["65010.00", "0.3"]], stream="ethusdt@depth5")
        r = binance.parse_book_update(msg)
        self.assertEqual(r["token_id"], "ETHUSDT")

    def test_obi_bid_heavy(self):
        # bid_vol=1.5, ask_vol=0.3 → OBI = (1.5 - 0.3) / (1.5 + 0.3) ≈ 0.667
        msg = self._individual([["65000.00", "1.5"]], [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertAlmostEqual(r["obi"], (1.5 - 0.3) / (1.5 + 0.3), places=5)

    def test_obi_ask_heavy(self):
        msg = self._individual([["65000.00", "0.3"]], [["65010.00", "1.5"]])
        r = binance.parse_book_update(msg)
        self.assertLess(r["obi"], 0)

    def test_spread_non_negative(self):
        msg = self._individual([["65000.00", "0.5"]], [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertGreaterEqual(r["spread"], 0.0)

    def test_none_on_empty_dict(self):
        self.assertIsNone(binance.parse_book_update({}))

    def test_none_on_non_dict(self):
        self.assertIsNone(binance.parse_book_update("bad"))
        self.assertIsNone(binance.parse_book_update(None))
        self.assertIsNone(binance.parse_book_update([]))

    def test_none_when_all_prices_zero(self):
        msg = self._individual([["0", "1"]], [["0", "1"]])
        self.assertIsNone(binance.parse_book_update(msg))

    def test_none_when_bids_and_asks_empty(self):
        msg = self._individual([], [])
        self.assertIsNone(binance.parse_book_update(msg))

    def test_short_key_aliases(self):
        # Binance also sends bids as "b" and asks as "a"
        msg = {"b": [["65000.00", "0.5"]], "a": [["65010.00", "0.3"]]}
        r = binance.parse_book_update(msg)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["best_bid"], 65000.0)

    def test_malformed_price_level_skipped(self):
        msg = self._individual([["bad", "0.5"], ["65000.00", "0.5"]], [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["best_bid"], 65000.0)

    def test_vol_capped_at_top_5(self):
        bids = [[str(65000 - i), "1.0"] for i in range(10)]
        msg = self._individual(bids, [["65010.00", "0.3"]])
        r = binance.parse_book_update(msg)
        self.assertAlmostEqual(r["bid_vol"], 5.0)


# ── MEXC parse_book_update ────────────────────────────────────────────────────

class TestMexcParseBookUpdate(unittest.TestCase):

    def _msg(self, bids, asks, symbol="BTCUSDT"):
        return {
            "c": f"spot@public.limit.depth.v3.api@{symbol}@5",
            "d": {"bids": bids, "asks": asks},
            "s": symbol,
            "t": 1610123456789,
        }

    def test_best_bid(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "0.5"], ["64990.00", "1.0"]], [["65010.00", "0.3"]]))
        self.assertAlmostEqual(r["best_bid"], 65000.0)

    def test_best_ask(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "0.5"]], [["65010.00", "0.3"], ["65020.00", "0.8"]]))
        self.assertAlmostEqual(r["best_ask"], 65010.0)

    def test_token_id_from_symbol(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "0.5"]], [["65010.00", "0.3"]], symbol="ETHUSDT"))
        self.assertEqual(r["token_id"], "ETHUSDT")

    def test_spread(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "0.5"]], [["65010.00", "0.3"]]))
        self.assertAlmostEqual(r["spread"], 10.0)

    def test_obi_bid_heavy(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "1.5"]], [["65010.00", "0.3"]]))
        self.assertAlmostEqual(r["obi"], (1.5 - 0.3) / (1.5 + 0.3), places=5)

    def test_obi_ask_heavy(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "0.3"]], [["65010.00", "1.5"]]))
        self.assertLess(r["obi"], 0)

    def test_none_when_no_d_key(self):
        self.assertIsNone(mexc.parse_book_update({"s": "BTCUSDT"}))

    def test_none_when_d_is_none(self):
        self.assertIsNone(mexc.parse_book_update({"d": None, "s": "BTCUSDT"}))

    def test_none_when_bids_and_asks_empty(self):
        self.assertIsNone(mexc.parse_book_update(self._msg([], [])))

    def test_none_on_non_dict(self):
        self.assertIsNone(mexc.parse_book_update("bad"))
        self.assertIsNone(mexc.parse_book_update(None))

    def test_none_when_all_prices_zero(self):
        self.assertIsNone(mexc.parse_book_update(self._msg([["0", "1"]], [["0", "1"]])))

    def test_malformed_price_level_skipped(self):
        r = mexc.parse_book_update(self._msg([["bad", "0.5"], ["65000.00", "0.5"]], [["65010.00", "0.3"]]))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["best_bid"], 65000.0)

    def test_vol_capped_at_top_5(self):
        bids = [[str(65000 - i), "1.0"] for i in range(10)]
        r = mexc.parse_book_update(self._msg(bids, [["65010.00", "0.3"]]))
        self.assertAlmostEqual(r["bid_vol"], 5.0)

    def test_spread_non_negative(self):
        r = mexc.parse_book_update(self._msg([["65000.00", "0.5"]], [["65010.00", "0.3"]]))
        self.assertGreaterEqual(r["spread"], 0.0)


# ── Binance make_subscribe_msg ────────────────────────────────────────────────

class TestBinanceMakeSubscribeMsg(unittest.TestCase):

    def test_returns_valid_json(self):
        msg = binance.make_subscribe_msg(["BTCUSDT"])
        parsed = json.loads(msg)
        self.assertIsInstance(parsed, dict)

    def test_method_is_subscribe(self):
        parsed = json.loads(binance.make_subscribe_msg(["BTCUSDT"]))
        self.assertEqual(parsed["method"], "SUBSCRIBE")

    def test_contains_depth_stream(self):
        parsed = json.loads(binance.make_subscribe_msg(["BTCUSDT"]))
        self.assertIn("btcusdt@depth5@100ms", parsed["params"])

    def test_strips_sell_suffix(self):
        parsed = json.loads(binance.make_subscribe_msg(["BTCUSDT:SELL"]))
        # ":SELL" must not appear in the stream name
        for p in parsed["params"]:
            self.assertNotIn(":sell", p.lower())

    def test_multiple_symbols(self):
        parsed = json.loads(binance.make_subscribe_msg(["BTCUSDT", "ETHUSDT"]))
        self.assertEqual(len(parsed["params"]), 2)


# ── MEXC make_subscribe_msg ───────────────────────────────────────────────────

class TestMexcMakeSubscribeMsg(unittest.TestCase):

    def test_returns_valid_json(self):
        msg = mexc.make_subscribe_msg(["BTCUSDT"])
        parsed = json.loads(msg)
        self.assertIsInstance(parsed, dict)

    def test_method_is_subscription(self):
        parsed = json.loads(mexc.make_subscribe_msg(["BTCUSDT"]))
        self.assertEqual(parsed["method"], "SUBSCRIPTION")

    def test_contains_mexc_depth_prefix(self):
        parsed = json.loads(mexc.make_subscribe_msg(["BTCUSDT"]))
        self.assertTrue(
            any("spot@public.limit.depth.v3.api" in p for p in parsed["params"])
        )

    def test_strips_sell_suffix(self):
        parsed = json.loads(mexc.make_subscribe_msg(["BTCUSDT:SELL"]))
        for p in parsed["params"]:
            self.assertNotIn(":SELL", p)

    def test_multiple_symbols(self):
        parsed = json.loads(mexc.make_subscribe_msg(["BTCUSDT", "ETHUSDT"]))
        self.assertEqual(len(parsed["params"]), 2)


# ── post_order simulation (no network) ───────────────────────────────────────

class TestBinancePostOrderSimulated(unittest.IsolatedAsyncioTestCase):

    async def test_returns_sim_id_without_credentials(self):
        env_backup = {k: os.environ.pop(k, None) for k in ("BINANCE_API_KEY", "BINANCE_API_SECRET")}
        try:
            result = await binance.post_order(None, "BTCUSDT", 65000.0, 10.0)
            self.assertIsNotNone(result)
            self.assertTrue(str(result).startswith("sim_"))
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    async def test_sim_id_is_unique(self):
        os.environ.pop("BINANCE_API_KEY", None)
        os.environ.pop("BINANCE_API_SECRET", None)
        r1 = await binance.post_order(None, "BTCUSDT", 65000.0, 10.0)
        r2 = await binance.post_order(None, "BTCUSDT", 65000.0, 10.0)
        self.assertNotEqual(r1, r2)

    async def test_sell_suffix_accepted(self):
        os.environ.pop("BINANCE_API_KEY", None)
        os.environ.pop("BINANCE_API_SECRET", None)
        result = await binance.post_order(None, "BTCUSDT:SELL", 65000.0, 10.0)
        self.assertTrue(str(result).startswith("sim_"))


class TestMexcPostOrderSimulated(unittest.IsolatedAsyncioTestCase):

    async def test_returns_sim_id_without_credentials(self):
        env_backup = {k: os.environ.pop(k, None) for k in ("MEXC_API_KEY", "MEXC_API_SECRET")}
        try:
            result = await mexc.post_order(None, "BTCUSDT", 65000.0, 10.0)
            self.assertIsNotNone(result)
            self.assertTrue(str(result).startswith("sim_"))
        finally:
            for k, v in env_backup.items():
                if v is not None:
                    os.environ[k] = v

    async def test_sim_id_is_unique(self):
        os.environ.pop("MEXC_API_KEY", None)
        os.environ.pop("MEXC_API_SECRET", None)
        r1 = await mexc.post_order(None, "BTCUSDT", 65000.0, 10.0)
        r2 = await mexc.post_order(None, "BTCUSDT", 65000.0, 10.0)
        self.assertNotEqual(r1, r2)

    async def test_sell_suffix_accepted(self):
        os.environ.pop("MEXC_API_KEY", None)
        os.environ.pop("MEXC_API_SECRET", None)
        result = await mexc.post_order(None, "BTCUSDT:SELL", 65000.0, 10.0)
        self.assertTrue(str(result).startswith("sim_"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
