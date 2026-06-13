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

import json, os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tradinebotte-polymarket"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tradinebotte-cex"))
import api_binance       as binance
import api_bitstamp      as bitstamp
import api_mexc          as mexc
import api_mexc_futures  as mexc_futures
import api_polymarket    as poly


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

    def test_bitstamp_has_full_interface(self):
        self._check_module(bitstamp)

    def test_mexc_has_full_interface(self):
        self._check_module(mexc)

    def test_mexc_futures_has_full_interface(self):
        self._check_module(mexc_futures)

    def test_polymarket_has_full_interface(self):
        self._check_module(poly)

    def test_fee_rate_is_float(self):
        for mod in (binance, bitstamp, mexc, mexc_futures, poly):
            self.assertIsInstance(
                mod.FEE_RATE, float,
                f"{mod.__name__}.FEE_RATE must be a float",
            )

    def test_compute_fee_returns_float(self):
        for mod in (binance, bitstamp, mexc, mexc_futures, poly):
            result = mod.compute_fee(65000.0, 0.001)
            self.assertIsInstance(
                result, float,
                f"{mod.__name__}.compute_fee must return float",
            )

    def test_parse_book_update_returns_dict_or_none(self):
        for mod in (binance, bitstamp, mexc, mexc_futures, poly):
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


# ── Bitstamp compute_fee ──────────────────────────────────────────────────────

class TestBitstampComputeFee(unittest.TestCase):

    def test_known_value(self):
        # FEE_RATE = 0.001; fee = 0.001 × 65000 × 0.001 = 0.065
        self.assertAlmostEqual(bitstamp.compute_fee(65000.0, 0.001), 0.065)

    def test_zero_quantity(self):
        self.assertAlmostEqual(bitstamp.compute_fee(65000.0, 0.0), 0.0)

    def test_zero_price(self):
        self.assertAlmostEqual(bitstamp.compute_fee(0.0, 1.0), 0.0)

    def test_proportional_to_price(self):
        f1 = bitstamp.compute_fee(65000.0, 0.001)
        f2 = bitstamp.compute_fee(130000.0, 0.001)
        self.assertAlmostEqual(f2, f1 * 2)

    def test_fee_rate_is_01_percent(self):
        self.assertAlmostEqual(bitstamp.FEE_RATE, 0.001)


# ── Bitstamp market metadata ──────────────────────────────────────────────────

class TestBitstampMetadata(unittest.TestCase):

    def test_get_market_id(self):
        self.assertEqual(bitstamp.get_market_id({"symbol": "BTCUSD"}), "BTCUSD")

    def test_get_market_id_missing(self):
        self.assertEqual(bitstamp.get_market_id({}), "")

    def test_get_up_token_id(self):
        self.assertEqual(bitstamp.get_up_token_id({"symbol": "BTCUSD"}), "BTCUSD")

    def test_get_down_token_id_has_sell_suffix(self):
        self.assertEqual(bitstamp.get_down_token_id({"symbol": "BTCUSD"}), "BTCUSD:SELL")

    def test_get_market_end_ts_ms_is_zero(self):
        self.assertEqual(bitstamp.get_market_end_ts_ms({"symbol": "BTCUSD"}), 0.0)

    def test_get_market_start_ts_ms_is_zero(self):
        self.assertEqual(bitstamp.get_market_start_ts_ms({"symbol": "BTCUSD"}), 0.0)

    def test_get_market_question_uses_description(self):
        m = {"symbol": "BTCUSD", "description": "BTC/USD spot"}
        self.assertEqual(bitstamp.get_market_question(m), "BTC/USD spot")

    def test_get_market_question_falls_back_to_symbol(self):
        self.assertEqual(bitstamp.get_market_question({"symbol": "BTCUSD"}), "BTCUSD")

    def test_up_and_down_tokens_differ(self):
        m = {"symbol": "BTCUSD"}
        self.assertNotEqual(bitstamp.get_up_token_id(m), bitstamp.get_down_token_id(m))


# ── Bitstamp parse_book_update ────────────────────────────────────────────────

class TestBitstampParseBookUpdate(unittest.TestCase):

    def _msg(self, bids, asks, channel="order_book_btcusd"):
        return {
            "event":   "data",
            "channel": channel,
            "data": {
                "timestamp":      "1700000000",
                "microtimestamp": "1700000000000000",
                "bids":  bids,
                "asks":  asks,
            },
        }

    def test_best_bid(self):
        r = self._msg([["65000", "0.5"], ["64990", "1.0"]], [["65010", "0.3"]])
        self.assertAlmostEqual(bitstamp.parse_book_update(r)["best_bid"], 65000.0)

    def test_best_ask(self):
        r = self._msg([["65000", "0.5"]], [["65010", "0.3"], ["65020", "0.8"]])
        self.assertAlmostEqual(bitstamp.parse_book_update(r)["best_ask"], 65010.0)

    def test_spread(self):
        r = self._msg([["65000", "0.5"]], [["65010", "0.3"]])
        self.assertAlmostEqual(bitstamp.parse_book_update(r)["spread"], 10.0)

    def test_token_id_from_channel(self):
        r = self._msg([["65000", "0.5"]], [["65010", "0.3"]], channel="order_book_ethusd")
        self.assertEqual(bitstamp.parse_book_update(r)["token_id"], "ETHUSD")

    def test_obi_bid_heavy(self):
        r = self._msg([["65000", "1.5"]], [["65010", "0.3"]])
        result = bitstamp.parse_book_update(r)
        self.assertAlmostEqual(result["obi"], (1.5 - 0.3) / (1.5 + 0.3), places=5)

    def test_obi_ask_heavy(self):
        r = self._msg([["65000", "0.3"]], [["65010", "1.5"]])
        self.assertLess(bitstamp.parse_book_update(r)["obi"], 0)

    def test_vol_capped_at_top_5(self):
        bids = [[str(65000 - i), "1.0"] for i in range(10)]
        r = self._msg(bids, [["65010", "0.3"]])
        self.assertAlmostEqual(bitstamp.parse_book_update(r)["bid_vol"], 5.0)

    def test_spread_non_negative(self):
        r = self._msg([["65000", "0.5"]], [["65010", "0.3"]])
        self.assertGreaterEqual(bitstamp.parse_book_update(r)["spread"], 0.0)

    def test_none_on_non_data_event(self):
        self.assertIsNone(bitstamp.parse_book_update({"event": "heartbeat"}))

    def test_none_on_empty_dict(self):
        self.assertIsNone(bitstamp.parse_book_update({}))

    def test_none_on_non_dict(self):
        self.assertIsNone(bitstamp.parse_book_update("bad"))
        self.assertIsNone(bitstamp.parse_book_update(None))

    def test_none_when_bids_and_asks_empty(self):
        r = self._msg([], [])
        self.assertIsNone(bitstamp.parse_book_update(r))

    def test_none_when_not_order_book_channel(self):
        r = self._msg([["65000", "0.5"]], [["65010", "0.3"]], channel="trade_btcusd")
        self.assertIsNone(bitstamp.parse_book_update(r))

    def test_malformed_price_level_skipped(self):
        r = self._msg([["bad", "0.5"], ["65000", "0.5"]], [["65010", "0.3"]])
        result = bitstamp.parse_book_update(r)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["best_bid"], 65000.0)

    def test_result_has_required_keys(self):
        r = self._msg([["65000", "0.5"]], [["65010", "0.3"]])
        result = bitstamp.parse_book_update(r)
        self.assertIsNotNone(result)
        for key in ("token_id", "best_bid", "best_ask", "spread", "bid_vol", "ask_vol", "obi"):
            self.assertIn(key, result)


# ── Bitstamp make_subscribe_msg ───────────────────────────────────────────────

class TestBitstampMakeSubscribeMsg(unittest.TestCase):

    def test_single_symbol_returns_json_string(self):
        msg = bitstamp.make_subscribe_msg(["btcusd"])
        self.assertIsInstance(msg, str)
        parsed = json.loads(msg)
        self.assertIsInstance(parsed, dict)

    def test_event_is_subscribe(self):
        parsed = json.loads(bitstamp.make_subscribe_msg(["btcusd"]))
        self.assertEqual(parsed["event"], "bts:subscribe")

    def test_channel_contains_symbol(self):
        parsed = json.loads(bitstamp.make_subscribe_msg(["btcusd"]))
        self.assertIn("btcusd", parsed["data"]["channel"])

    def test_strips_sell_suffix(self):
        parsed = json.loads(bitstamp.make_subscribe_msg(["BTCUSD:SELL"]))
        self.assertNotIn(":SELL", parsed["data"]["channel"])
        self.assertNotIn(":sell", parsed["data"]["channel"])

    def test_multiple_symbols_returns_list(self):
        msgs = bitstamp.make_subscribe_msg(["btcusd", "ethusd"])
        self.assertIsInstance(msgs, list)
        self.assertEqual(len(msgs), 2)

    def test_multiple_symbols_each_is_valid_json(self):
        msgs = bitstamp.make_subscribe_msg(["btcusd", "ethusd"])
        for m in msgs:
            parsed = json.loads(m)
            self.assertEqual(parsed["event"], "bts:subscribe")


# ── Bitstamp post_order simulation (no network) ───────────────────────────────

class TestBitstampPostOrderSimulated(unittest.IsolatedAsyncioTestCase):

    def _clear_creds(self):
        for k in ("BITSTAMP_API_KEY", "BITSTAMP_API_SECRET", "BITSTAMP_CUSTOMER_ID"):
            os.environ.pop(k, None)

    async def test_returns_str_without_credentials(self):
        self._clear_creds()
        result = await bitstamp.post_order(None, "BTCUSD", 65000.0, 65.0, side="BUY")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, str)

    async def test_sim_id_starts_with_sim(self):
        self._clear_creds()
        result = await bitstamp.post_order(None, "BTCUSD", 65000.0, 65.0, side="BUY")
        self.assertTrue(result.startswith("sim_"))

    async def test_sim_id_unique(self):
        self._clear_creds()
        r1 = await bitstamp.post_order(None, "BTCUSD", 65000.0, 65.0, side="BUY")
        r2 = await bitstamp.post_order(None, "BTCUSD", 65000.0, 65.0, side="BUY")
        self.assertNotEqual(r1, r2)

    async def test_sell_suffix_accepted(self):
        self._clear_creds()
        result = await bitstamp.post_order(None, "BTCUSD:SELL", 65000.0, 65.0, side="SELL")
        self.assertTrue(result.startswith("sim_"))


# ── MEXC Futures compute_fee ──────────────────────────────────────────────────

class TestMexcFuturesComputeFee(unittest.TestCase):

    def test_known_value(self):
        # FEE_RATE = 0.0006; fee = 0.0006 × 65000 × 0.001 = 0.039
        self.assertAlmostEqual(mexc_futures.compute_fee(65000.0, 0.001), 0.039)

    def test_zero_quantity(self):
        self.assertAlmostEqual(mexc_futures.compute_fee(65000.0, 0.0), 0.0)

    def test_zero_price(self):
        self.assertAlmostEqual(mexc_futures.compute_fee(0.0, 1.0), 0.0)

    def test_proportional_to_price(self):
        f1 = mexc_futures.compute_fee(65000.0, 0.001)
        f2 = mexc_futures.compute_fee(130000.0, 0.001)
        self.assertAlmostEqual(f2, f1 * 2)

    def test_proportional_to_quantity(self):
        f1 = mexc_futures.compute_fee(65000.0, 0.001)
        f2 = mexc_futures.compute_fee(65000.0, 0.002)
        self.assertAlmostEqual(f2, f1 * 2)

    def test_fee_lower_than_spot(self):
        # Futures taker (0.06%) should be lower than MEXC spot taker (0.20%)
        import api_mexc as _spot
        spot_fee    = _spot.compute_fee(65000.0, 0.001)
        futures_fee = mexc_futures.compute_fee(65000.0, 0.001)
        self.assertLess(futures_fee, spot_fee)


# ── MEXC Futures metadata ──────────────────────────────────────────────────────

class TestMexcFuturesMetadata(unittest.TestCase):

    def test_get_market_id(self):
        self.assertEqual(mexc_futures.get_market_id({"symbol": "BTC_USDT"}), "BTC_USDT")

    def test_get_market_id_missing(self):
        self.assertEqual(mexc_futures.get_market_id({}), "")

    def test_get_up_token_id(self):
        self.assertEqual(mexc_futures.get_up_token_id({"symbol": "BTC_USDT"}), "BTC_USDT")

    def test_get_down_token_id_has_short_suffix(self):
        self.assertEqual(mexc_futures.get_down_token_id({"symbol": "BTC_USDT"}),
                         "BTC_USDT:SHORT")

    def test_up_and_down_tokens_differ(self):
        m = {"symbol": "BTC_USDT"}
        self.assertNotEqual(mexc_futures.get_up_token_id(m),
                            mexc_futures.get_down_token_id(m))

    def test_get_market_end_ts_ms_is_zero(self):
        self.assertEqual(mexc_futures.get_market_end_ts_ms({}), 0.0)

    def test_get_market_start_ts_ms_is_zero(self):
        self.assertEqual(mexc_futures.get_market_start_ts_ms({}), 0.0)

    def test_get_market_question_falls_back_to_symbol(self):
        self.assertEqual(mexc_futures.get_market_question({"symbol": "BTC_USDT"}),
                         "BTC_USDT")

    def test_get_market_question_custom(self):
        m = {"symbol": "BTC_USDT", "question": "BTC perp"}
        self.assertEqual(mexc_futures.get_market_question(m), "BTC perp")

    def test_symbol_uses_underscore_format(self):
        self.assertIn("_", mexc_futures.DEFAULT_SYMBOL)


# ── MEXC Futures parse_book_update ────────────────────────────────────────────

class TestMexcFuturesParseBookUpdate(unittest.TestCase):

    def _valid_msg(self, symbol="BTC_USDT"):
        return {
            "channel": "push.depth.full",
            "data": {
                "bids": [["65000.00", "5", "1"], ["64900.00", "3", "2"]],
                "asks": [["65010.00", "4", "1"], ["65020.00", "2", "1"]],
            },
            "symbol": symbol,
            "ts": 1700000000000,
        }

    def test_returns_dict_for_valid_message(self):
        result = mexc_futures.parse_book_update(self._valid_msg())
        self.assertIsNotNone(result)
        self.assertIsInstance(result, dict)

    def test_required_keys_present(self):
        result = mexc_futures.parse_book_update(self._valid_msg())
        required = {"token_id", "best_bid", "best_ask", "spread", "bid_vol", "ask_vol", "obi"}
        self.assertTrue(required.issubset(result.keys()))

    def test_best_bid_and_ask_correct(self):
        result = mexc_futures.parse_book_update(self._valid_msg())
        self.assertAlmostEqual(result["best_bid"], 65000.0)
        self.assertAlmostEqual(result["best_ask"], 65010.0)

    def test_spread_positive(self):
        result = mexc_futures.parse_book_update(self._valid_msg())
        self.assertGreater(result["spread"], 0)

    def test_token_id_matches_symbol(self):
        result = mexc_futures.parse_book_update(self._valid_msg("ETH_USDT"))
        self.assertEqual(result["token_id"], "ETH_USDT")

    def test_obi_range(self):
        result = mexc_futures.parse_book_update(self._valid_msg())
        self.assertGreaterEqual(result["obi"], -1.0)
        self.assertLessEqual(result["obi"], 1.0)

    def test_returns_none_for_non_dict(self):
        self.assertIsNone(mexc_futures.parse_book_update("garbage"))
        self.assertIsNone(mexc_futures.parse_book_update(None))

    def test_returns_none_for_empty_dict(self):
        self.assertIsNone(mexc_futures.parse_book_update({}))

    def test_returns_none_for_wrong_channel(self):
        msg = self._valid_msg()
        msg["channel"] = "push.personal.order"
        self.assertIsNone(mexc_futures.parse_book_update(msg))

    def test_returns_none_when_no_levels(self):
        msg = {
            "channel": "push.depth.full",
            "data": {"bids": [], "asks": []},
            "symbol": "BTC_USDT",
        }
        self.assertIsNone(mexc_futures.parse_book_update(msg))


# ── MEXC Futures make_subscribe_msg ───────────────────────────────────────────

class TestMexcFuturesMakeSubscribeMsg(unittest.TestCase):

    def test_single_symbol_is_json(self):
        msg = mexc_futures.make_subscribe_msg(["BTC_USDT"])
        self.assertIsInstance(json.loads(msg), dict)

    def test_single_symbol_method(self):
        msg = json.loads(mexc_futures.make_subscribe_msg(["BTC_USDT"]))
        self.assertEqual(msg["method"], "sub.depth.full")

    def test_single_symbol_param(self):
        msg = json.loads(mexc_futures.make_subscribe_msg(["BTC_USDT"]))
        self.assertEqual(msg["param"]["symbol"], "BTC_USDT")

    def test_short_suffix_stripped(self):
        msg = json.loads(mexc_futures.make_subscribe_msg(["BTC_USDT:SHORT"]))
        self.assertEqual(msg["param"]["symbol"], "BTC_USDT")

    def test_multiple_symbols_returns_list(self):
        msg = json.loads(mexc_futures.make_subscribe_msg(["BTC_USDT", "ETH_USDT"]))
        self.assertIsInstance(msg, list)
        self.assertEqual(len(msg), 2)

    def test_multiple_symbols_have_method(self):
        msgs = json.loads(mexc_futures.make_subscribe_msg(["BTC_USDT", "ETH_USDT"]))
        for m in msgs:
            self.assertEqual(m["method"], "sub.depth.full")


# ── MEXC Futures post_order simulated ─────────────────────────────────────────

class TestMexcFuturesPostOrderSimulated(unittest.IsolatedAsyncioTestCase):

    def _clear_creds(self):
        os.environ.pop("MEXC_FUTURES_API_KEY", None)
        os.environ.pop("MEXC_FUTURES_API_SECRET", None)

    async def test_returns_sim_id_without_credentials(self):
        self._clear_creds()
        result = await mexc_futures.post_order(None, "BTC_USDT", 65000.0, 10.0)
        self.assertIsNotNone(result)
        self.assertTrue(str(result).startswith("sim_"))

    async def test_sim_id_is_unique(self):
        self._clear_creds()
        r1 = await mexc_futures.post_order(None, "BTC_USDT", 65000.0, 10.0)
        r2 = await mexc_futures.post_order(None, "BTC_USDT", 65000.0, 10.0)
        self.assertNotEqual(r1, r2)

    async def test_sell_side_returns_sim(self):
        self._clear_creds()
        result = await mexc_futures.post_order(None, "BTC_USDT", 65000.0, 10.0, side="SELL")
        self.assertTrue(str(result).startswith("sim_"))

    async def test_short_suffix_returns_sim(self):
        self._clear_creds()
        result = await mexc_futures.post_order(None, "BTC_USDT:SHORT", 65000.0, 10.0)
        self.assertTrue(str(result).startswith("sim_"))


# ── MEXC Futures get_order_status simulated ───────────────────────────────────

class TestMexcFuturesGetOrderStatus(unittest.IsolatedAsyncioTestCase):

    def _clear_creds(self):
        os.environ.pop("MEXC_FUTURES_API_KEY", None)
        os.environ.pop("MEXC_FUTURES_API_SECRET", None)

    async def test_returns_none_without_credentials(self):
        self._clear_creds()
        result = await mexc_futures.get_order_status(None, "BTC_USDT", "12345")
        self.assertIsNone(result)

    async def test_returns_none_for_sim_order(self):
        os.environ["MEXC_FUTURES_API_KEY"]    = "testkey"
        os.environ["MEXC_FUTURES_API_SECRET"] = "testsecret"
        try:
            result = await mexc_futures.get_order_status(None, "BTC_USDT", "sim_abc123")
            self.assertIsNone(result)
        finally:
            self._clear_creds()


# ── MEXC Futures cancel_order simulated ───────────────────────────────────────

class TestMexcFuturesCancelOrder(unittest.IsolatedAsyncioTestCase):

    def _clear_creds(self):
        os.environ.pop("MEXC_FUTURES_API_KEY", None)
        os.environ.pop("MEXC_FUTURES_API_SECRET", None)

    async def test_sim_order_returns_true(self):
        result = await mexc_futures.cancel_order(None, "BTC_USDT", "sim_abc123")
        self.assertTrue(result)

    async def test_no_credentials_returns_true(self):
        self._clear_creds()
        result = await mexc_futures.cancel_order(None, "BTC_USDT", "99999")
        self.assertTrue(result)


# ── MEXC Futures get_open_orders simulated ────────────────────────────────────

class TestMexcFuturesGetOpenOrders(unittest.IsolatedAsyncioTestCase):

    def _clear_creds(self):
        os.environ.pop("MEXC_FUTURES_API_KEY", None)
        os.environ.pop("MEXC_FUTURES_API_SECRET", None)

    async def test_returns_empty_list_without_credentials(self):
        self._clear_creds()
        result = await mexc_futures.get_open_orders(None, "BTC_USDT")
        self.assertEqual(result, [])


# ── MEXC Futures parse_user_stream_msg ────────────────────────────────────────

class TestMexcFuturesParseUserStreamMsg(unittest.TestCase):

    def _fill_msg(self, state=2, side=1, order_id="42"):
        return {
            "channel": "push.personal.order",
            "data": {
                "orderId": order_id,
                "state":   state,
                "side":    side,
                "symbol":  "BTC_USDT",
            },
        }

    def test_filled_open_long_returns_buy(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(state=2, side=1))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "FILLED")
        self.assertEqual(result["side"], "BUY")

    def test_filled_close_long_returns_sell(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(state=2, side=4))
        self.assertIsNotNone(result)
        self.assertEqual(result["side"], "SELL")

    def test_filled_open_short_returns_sell(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(state=2, side=3))
        self.assertIsNotNone(result)
        self.assertEqual(result["side"], "SELL")

    def test_filled_close_short_returns_buy(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(state=2, side=2))
        self.assertIsNotNone(result)
        self.assertEqual(result["side"], "BUY")

    def test_new_state_returns_dict(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(state=1))
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "NEW")

    def test_canceled_state_returns_none(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(state=3))
        self.assertIsNone(result)

    def test_order_id_preserved(self):
        result = mexc_futures.parse_user_stream_msg(self._fill_msg(order_id="99999"))
        self.assertEqual(result["order_id"], "99999")

    def test_wrong_channel_returns_none(self):
        msg = self._fill_msg()
        msg["channel"] = "push.depth.full"
        self.assertIsNone(mexc_futures.parse_user_stream_msg(msg))

    def test_non_dict_returns_none(self):
        self.assertIsNone(mexc_futures.parse_user_stream_msg("garbage"))
        self.assertIsNone(mexc_futures.parse_user_stream_msg(None))

    def test_missing_order_id_returns_none(self):
        msg = self._fill_msg()
        del msg["data"]["orderId"]
        self.assertIsNone(mexc_futures.parse_user_stream_msg(msg))


# ── MEXC Futures get_listen_key / make_user_stream_url ────────────────────────

class TestMexcFuturesUserStream(unittest.IsolatedAsyncioTestCase):

    def _clear_creds(self):
        os.environ.pop("MEXC_FUTURES_API_KEY", None)
        os.environ.pop("MEXC_FUTURES_API_SECRET", None)

    async def test_get_listen_key_returns_none_without_credentials(self):
        self._clear_creds()
        result = await mexc_futures.get_listen_key(None)
        self.assertIsNone(result)

    async def test_get_listen_key_returns_json_with_credentials(self):
        os.environ["MEXC_FUTURES_API_KEY"]    = "testkey"
        os.environ["MEXC_FUTURES_API_SECRET"] = "testsecret"
        try:
            result = await mexc_futures.get_listen_key(None)
            self.assertIsNotNone(result)
            payload = json.loads(result)
            self.assertEqual(payload["method"], "login")
            self.assertIn("apiKey", payload["param"])
            self.assertIn("signature", payload["param"])
        finally:
            self._clear_creds()

    async def test_keepalive_returns_true_for_nonempty_key(self):
        result = await mexc_futures.keepalive_listen_key(None, "anykey")
        self.assertTrue(result)

    async def test_keepalive_returns_false_for_empty_key(self):
        result = await mexc_futures.keepalive_listen_key(None, "")
        self.assertFalse(result)

    def test_make_user_stream_url_returns_ws_url(self):
        url = mexc_futures.make_user_stream_url("anykey")
        self.assertEqual(url, mexc_futures.WS_URL)

    def test_make_user_stream_url_ignores_key(self):
        self.assertEqual(
            mexc_futures.make_user_stream_url("key1"),
            mexc_futures.make_user_stream_url("key2"),
        )


# ── MEXC Futures connectors registry ──────────────────────────────────────────

class TestMexcFuturesRegistry(unittest.TestCase):

    def test_mexc_futures_in_registry(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tradinebotte-cex"))
        from connectors import available
        self.assertIn("mexc_futures", available())

    def test_connectors_load_mexc_futures(self):
        from connectors import load
        mod = load("mexc_futures")
        self.assertIs(mod, mexc_futures)


if __name__ == "__main__":
    unittest.main(verbosity=2)
