# pylint: disable=too-many-lines
"""
Automated tests for bot/live_bot.py

Run with:
    bash scripts/run_tests.sh
    # or directly:
    .venv/bin/python3 -m unittest discover tests/ -v
"""

import logging, os, sys, time, sqlite3, unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Redirect all bot I/O to ~/tmp so tests never touch /opt or write credentials.
# ~/tmp is per-user by definition — no PermissionError on shared servers.
_TEST_DIR = os.path.join(os.path.expanduser("~"), "tmp", "tradinebotte-test")
os.environ["TRADINEBOTTE_DIR"] = _TEST_DIR
os.makedirs(_TEST_DIR, exist_ok=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tradinebotte-cex"))
import live_bot as bot
import api_polymarket as api_poly
import bot_utils


# ── Test helpers ──────────────────────────────────────────────────────────────

def make_db():
    """In-memory SQLite database with the production schema and migrations applied."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(bot.SCHEMA)
    bot._apply_migrations(conn)
    conn.commit()
    return conn


def make_state(conn=None):
    """BotState backed by an in-memory database (or a provided connection)."""
    return bot.BotState(conn if conn is not None else make_db())


def make_token(
    market_id="mkt1",
    token_id="tok1",
    direction="UP",
    secs_remaining=90,
    best_bid=0.97,
    best_ask=0.975,
    ask_vol=50.0,
    obi=0.20,
):
    """
    Return a TokenState whose fields satisfy ALL signal conditions by default.
    Individual tests override the one field they want to test.
    """
    now_ms  = time.time() * 1000
    end_ms  = now_ms + secs_remaining * 1000
    ts = bot.TokenState(
        token_id, market_id, direction, "BTC UP/DOWN test",
        now_ms - 200_000, end_ms,
    )
    ts.best_bid = best_bid
    ts.best_ask = best_ask
    ts.bid_vol  = ask_vol
    ts.ask_vol  = ask_vol
    ts.obi      = obi
    ts.spread   = round(best_ask - best_bid, 4)
    return ts


def insert_trade(conn, market_id="mkt1", token_id="tok1", direction="UP",
                 stake=10.0, best_ask=0.975):
    """Insert an open trade row and return its rowid."""
    tokens_bought = stake / best_ask
    fee = api_poly.compute_fee(best_ask, tokens_bought)
    cur = conn.execute(
        "INSERT INTO trades "
        "(market_id, token_id, direction, stake, tokens_bought, fee, capital_before, resolved) "
        "VALUES (?,?,?,?,?,?,?,0)",
        (market_id, token_id, direction, stake, tokens_bought, fee, 100.0),
    )
    conn.commit()
    return cur.lastrowid


# ── compute_fee ───────────────────────────────────────────────────────────────

class TestComputeFee(unittest.TestCase):

    def test_known_value_at_96(self):
        # fee = 0.02 × min(0.96, 0.04) × 10 = 0.02 × 0.04 × 10 = 0.008
        self.assertAlmostEqual(api_poly.compute_fee(0.96, 10), 0.008)

    def test_symmetric_around_half(self):
        # fee at price p should equal fee at price (1−p)
        self.assertAlmostEqual(
            api_poly.compute_fee(0.30, 100),
            api_poly.compute_fee(0.70, 100),
        )

    def test_maximum_at_half(self):
        # fee per token is highest when the price is closest to 0.5
        self.assertGreater(api_poly.compute_fee(0.50, 10), api_poly.compute_fee(0.96, 10))

    def test_zero_tokens(self):
        self.assertAlmostEqual(api_poly.compute_fee(0.96, 0), 0.0)


# ── parse_book_message ────────────────────────────────────────────────────────

class TestParseBookMessage(unittest.TestCase):

    def _msg(self, event_type="book", asset_id="tok1",
             bids=None, asks=None):
        return {
            "event_type": event_type,
            "asset_id":   asset_id,
            "bids": bids if bids is not None else [{"price": "0.96", "size": "100"}],
            "asks": asks if asks is not None else [{"price": "0.97", "size": "200"}],
        }

    def test_parses_book_event(self):
        r = api_poly.parse_book_update(self._msg())
        self.assertIsNotNone(r)
        self.assertEqual(r["token_id"], "tok1")
        self.assertAlmostEqual(r["best_bid"], 0.96)
        self.assertAlmostEqual(r["best_ask"], 0.97)

    def test_parses_price_change_event(self):
        self.assertIsNotNone(api_poly.parse_book_update(self._msg(event_type="price_change")))

    def test_parses_last_trade_price_event(self):
        self.assertIsNotNone(api_poly.parse_book_update(self._msg(event_type="last_trade_price")))

    def test_ignores_unknown_event_type(self):
        self.assertIsNone(api_poly.parse_book_update(self._msg(event_type="tick")))

    def test_ignores_missing_asset_id(self):
        msg = {"event_type": "book", "bids": [], "asks": []}
        self.assertIsNone(api_poly.parse_book_update(msg))

    def test_ignores_empty_book(self):
        self.assertIsNone(api_poly.parse_book_update(self._msg(bids=[], asks=[])))

    def test_spread_computed_correctly(self):
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "0.95", "size": "10"}],
            asks=[{"price": "0.97", "size": "10"}],
        ))
        self.assertAlmostEqual(r["spread"], 0.02)

    def test_obi_balanced_book(self):
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "0.95", "size": "100"}],
            asks=[{"price": "0.96", "size": "100"}],
        ))
        self.assertAlmostEqual(r["obi"], 0.0)

    def test_obi_bid_heavy(self):
        # OBI = (300 − 100) / (300 + 100) = 0.5
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "0.95", "size": "300"}],
            asks=[{"price": "0.96", "size": "100"}],
        ))
        self.assertAlmostEqual(r["obi"], 0.5)

    def test_obi_ask_heavy(self):
        # OBI = (100 − 300) / (100 + 300) = -0.5
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "0.95", "size": "100"}],
            asks=[{"price": "0.96", "size": "300"}],
        ))
        self.assertAlmostEqual(r["obi"], -0.5)

    def test_depth_capped_at_top_5_levels(self):
        # 10 bid levels × 10 USD each; only top 5 should count
        bids = [{"price": str(round(0.95 - i * 0.01, 2)), "size": "10"} for i in range(10)]
        asks = [{"price": str(round(0.96 + i * 0.01, 2)), "size": "10"} for i in range(10)]
        r = api_poly.parse_book_update(self._msg(bids=bids, asks=asks))
        self.assertAlmostEqual(r["bid_vol"], 50.0)
        self.assertAlmostEqual(r["ask_vol"], 50.0)

    def test_list_format_price_levels(self):
        # Some Polymarket API versions send [price, size] arrays instead of dicts
        msg = {
            "event_type": "book",
            "asset_id":   "tok1",
            "bids": [["0.95", "100"]],
            "asks": [["0.96", "200"]],
        }
        r = api_poly.parse_book_update(msg)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["best_bid"], 0.95)

    def test_bids_sorted_descending(self):
        # Best bid = highest price, regardless of order in message
        r = api_poly.parse_book_update(self._msg(
            bids=[
                {"price": "0.90", "size": "10"},
                {"price": "0.95", "size": "10"},
                {"price": "0.92", "size": "10"},
            ],
            asks=[{"price": "0.97", "size": "10"}],
        ))
        self.assertAlmostEqual(r["best_bid"], 0.95)

    def test_asks_sorted_ascending(self):
        # Best ask = lowest price, regardless of order in message
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "0.93", "size": "10"}],
            asks=[
                {"price": "0.99", "size": "10"},
                {"price": "0.96", "size": "10"},
                {"price": "0.98", "size": "10"},
            ],
        ))
        self.assertAlmostEqual(r["best_ask"], 0.96)

    def test_non_numeric_price_skipped(self):
        # "N/A" prices (emitted during reconnect) must be silently discarded.
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "N/A", "size": "100"}, {"price": "0.95", "size": "50"}],
            asks=[{"price": "0.97", "size": "80"}],
        ))
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["best_bid"], 0.95)

    def test_all_non_numeric_prices_returns_none(self):
        # If every level has an unparseable price the message is useless.
        r = api_poly.parse_book_update(self._msg(
            bids=[{"price": "N/A", "size": "100"}],
            asks=[{"price": "N/A", "size": "80"}],
        ))
        self.assertIsNone(r)

    def test_missing_bids_and_asks_keys(self):
        # asset_id present but bids/asks keys completely absent → None.
        msg = {"event_type": "book", "asset_id": "tok1"}
        self.assertIsNone(api_poly.parse_book_update(msg))

    def test_bids_key_absent_asks_valid(self):
        # Only asks present — bids defaults to [] internally; result is valid
        # because asks alone can still produce a snapshot.
        msg = {"event_type": "book", "asset_id": "tok1",
               "asks": [{"price": "0.97", "size": "80"}]}
        r = api_poly.parse_book_update(msg)
        self.assertIsNotNone(r)
        self.assertAlmostEqual(r["best_ask"], 0.97)
        self.assertAlmostEqual(r["best_bid"], 0.0)


# ── Market metadata helpers ───────────────────────────────────────────────────

class TestMarketHelpers(unittest.TestCase):

    def _market(self):
        return {
            "conditionId":   "mkt1",
            "endDate":       "2026-04-23T20:00:00Z",
            "startDate":     "2026-04-23T19:55:00Z",
            "clobTokenIds":  ["up_tok", "down_tok"],
        }

    def test_end_ts_from_endDate(self):
        self.assertGreater(api_poly.get_market_end_ts_ms(self._market()), 0)

    def test_end_ts_missing_returns_zero(self):
        self.assertEqual(api_poly.get_market_end_ts_ms({}), 0.0)

    def test_end_ts_tries_fallback_keys(self):
        market = {"end_date": "2026-04-23T20:00:00Z"}
        self.assertGreater(api_poly.get_market_end_ts_ms(market), 0)

    def test_up_token_from_clobTokenIds(self):
        self.assertEqual(api_poly.get_up_token_id(self._market()), "up_tok")

    def test_down_token_from_clobTokenIds(self):
        self.assertEqual(api_poly.get_down_token_id(self._market()), "down_tok")

    def test_up_token_from_tokens_array(self):
        market = {"tokens": [
            {"token_id": "yes_tok", "outcome": "Yes"},
            {"token_id": "no_tok",  "outcome": "No"},
        ]}
        self.assertEqual(api_poly.get_up_token_id(market), "yes_tok")

    def test_down_token_from_tokens_array(self):
        market = {"tokens": [
            {"token_id": "yes_tok", "outcome": "Yes"},
            {"token_id": "no_tok",  "outcome": "No"},
        ]}
        self.assertEqual(api_poly.get_down_token_id(market), "no_tok")

    def test_up_token_missing_returns_none(self):
        self.assertIsNone(api_poly.get_up_token_id({}))

    def test_down_token_missing_returns_none(self):
        self.assertIsNone(api_poly.get_down_token_id({}))


# ── TokenState computed properties ────────────────────────────────────────────

class TestTokenState(unittest.TestCase):

    def test_secs_remaining_future(self):
        end_ms = (time.time() + 120) * 1000
        ts = bot.TokenState("t", "m", "UP", "q", 0, end_ms)
        self.assertAlmostEqual(ts.secs_remaining, 120, delta=2)

    def test_secs_remaining_past_is_zero(self):
        end_ms = (time.time() - 10) * 1000
        ts = bot.TokenState("t", "m", "UP", "q", 0, end_ms)
        self.assertEqual(ts.secs_remaining, 0.0)

    def test_secs_remaining_no_end_time(self):
        ts = bot.TokenState("t", "m", "UP", "q", 0, 0)
        self.assertEqual(ts.secs_remaining, 9999.0)

    def test_market_ended_false_for_future(self):
        end_ms = (time.time() + 60) * 1000
        ts = bot.TokenState("t", "m", "UP", "q", 0, end_ms)
        self.assertFalse(ts.market_ended)

    def test_market_ended_true_past_grace(self):
        # 10 s past end time, well beyond the 5 s grace period
        end_ms = (time.time() - 10) * 1000
        ts = bot.TokenState("t", "m", "UP", "q", 0, end_ms)
        self.assertTrue(ts.market_ended)

    def test_market_ended_false_within_grace(self):
        # 3 s past end time — still within the 5 s grace period
        end_ms = (time.time() - 3) * 1000
        ts = bot.TokenState("t", "m", "UP", "q", 0, end_ms)
        self.assertFalse(ts.market_ended)

    def test_seconds_elapsed(self):
        start_ms = (time.time() - 60) * 1000
        ts = bot.TokenState("t", "m", "UP", "q", start_ms, 0)
        self.assertAlmostEqual(ts.seconds_elapsed, 60, delta=2)


# ── register_market ───────────────────────────────────────────────────────────

class TestRegisterMarket(unittest.TestCase):

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    def _market(self, offset_min=3):
        """Return a market ending in `offset_min` minutes."""
        now = datetime.now(timezone.utc)
        return {
            "conditionId":  "mkt1",
            "endDate":   (now + timedelta(minutes=offset_min)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "startDate": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clobTokenIds": ["up_tok", "down_tok"],
            "question":     "BTC up or down?",
        }

    def test_registers_up_and_down_tokens(self):
        state  = self.state
        new_ids = bot.register_market(state, self._market())
        self.assertEqual(len(new_ids), 2)
        self.assertIn("up_tok",   state.tokens)
        self.assertIn("down_tok", state.tokens)

    def test_direction_assigned_correctly(self):
        state = self.state
        bot.register_market(state, self._market())
        self.assertEqual(state.tokens["up_tok"].direction,   "UP")
        self.assertEqual(state.tokens["down_tok"].direction, "DOWN")

    def test_no_duplicate_registration(self):
        state  = self.state
        market = self._market()
        bot.register_market(state, market)
        new_ids = bot.register_market(state, market)   # second call
        self.assertEqual(len(new_ids), 0)              # already tracked

    def test_skips_expired_market(self):
        state  = self.state
        market = self._market(offset_min=-10)           # ended 10 min ago
        new_ids = bot.register_market(state, market)
        self.assertEqual(len(new_ids), 0)

    def test_skips_market_without_condition_id(self):
        state  = self.state
        market = self._market()
        del market["conditionId"]
        new_ids = bot.register_market(state, market)
        self.assertEqual(len(new_ids), 0)


# ── check_signal guards ───────────────────────────────────────────────────────

class TestCheckSignal(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    async def test_fires_when_all_conditions_met(self):
        await bot.check_signal(self.state, make_token())
        self.assertIn("mkt1", self.state.signalled)
        self.assertEqual(self.state.total_trades, 1)

    async def test_blocked_already_signalled(self):
        self.state.signalled.add("mkt1")
        await bot.check_signal(self.state, make_token())
        self.assertEqual(self.state.total_trades, 0)

    async def test_blocked_market_ended(self):
        ts = make_token()
        ts.market_end_ms = (time.time() - 10) * 1000
        await bot.check_signal(self.state, ts)
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_bid_below_threshold(self):
        await bot.check_signal(self.state, make_token(best_bid=0.94))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_bid_above_entry_max(self):
        await bot.check_signal(self.state, make_token(best_bid=0.999, best_ask=0.999))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_ask_at_settlement_price(self):
        # best_ask >= 1.0 means the market has already resolved
        await bot.check_signal(self.state, make_token(best_ask=1.0))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_insufficient_time_remaining(self):
        await bot.check_signal(self.state, make_token(secs_remaining=30))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_obi_too_negative(self):
        await bot.check_signal(self.state, make_token(obi=-0.6))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_thin_ask_volume(self):
        await bot.check_signal(self.state, make_token(ask_vol=5.0))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_insufficient_capital(self):
        self.state.capital = 5.0
        await bot.check_signal(self.state, make_token())
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_blocked_daily_stop_loss(self):
        # daily_pnl is now an in-memory cache; set it directly so the
        # midnight-reset guard doesn't clear it before the check.
        self.state.daily_pnl = -35.0
        self.state._daily_pnl_day = int(time.time() // 86400)
        await bot.check_signal(self.state, make_token())
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_at_threshold_bid_fires(self):
        await bot.check_signal(self.state, make_token(best_bid=0.96))
        self.assertIn("mkt1", self.state.signalled)

    async def test_at_min_secs_remaining_blocked(self):
        await bot.check_signal(self.state, make_token(secs_remaining=29))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_above_min_secs_remaining_fires(self):
        # secs_remaining is computed live from time.time(), so use a value
        # safely above the 30 s limit rather than testing the exact boundary.
        await bot.check_signal(self.state, make_token(secs_remaining=60))
        self.assertIn("mkt1", self.state.signalled)

    async def test_blocked_by_hour_filter(self):
        # When is_trading_hour() returns False (filter active, outside window)
        # check_signal must not fire regardless of other conditions.
        with patch.object(bot, "is_trading_hour", return_value=False):
            await bot.check_signal(self.state, make_token())
        self.assertNotIn("mkt1", self.state.signalled)
        self.assertEqual(self.state.total_trades, 0)

    async def test_fires_when_hour_filter_allows(self):
        # Explicit guard: signal fires when is_trading_hour() returns True.
        with patch.object(bot, "is_trading_hour", return_value=True):
            await bot.check_signal(self.state, make_token())
        self.assertIn("mkt1", self.state.signalled)


# ── check_resolution ─────────────────────────────────────────────────────────

class TestCheckResolution(unittest.TestCase):

    def _state_with_open_trade(self, direction="UP"):
        state = make_state()
        self.addCleanup(state.conn.close)
        tid   = insert_trade(state.conn, direction=direction)
        state.open_trades["mkt1"]      = tid
        state.traded_direction["mkt1"] = direction
        return state, tid

    def test_win_on_high_bid(self):
        state, tid = self._state_with_open_trade()
        ts = make_token(best_bid=bot.WIN_THRESHOLD)
        bot.check_resolution(state, ts)
        row = state.conn.execute("SELECT outcome FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "WIN")

    def test_loss_on_low_bid(self):
        state, tid = self._state_with_open_trade()
        ts = make_token(best_bid=bot.LOSS_THRESHOLD)
        bot.check_resolution(state, ts)
        row = state.conn.execute("SELECT outcome FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "LOSS")

    def test_no_resolution_mid_market(self):
        state, _ = self._state_with_open_trade()
        ts = make_token(best_bid=0.50)
        bot.check_resolution(state, ts)
        self.assertIn("mkt1", state.open_trades)  # still open

    def test_skips_wrong_direction(self):
        # We traded UP but this token is DOWN — must not trigger resolution
        state, _ = self._state_with_open_trade(direction="UP")
        ts = make_token(best_bid=bot.WIN_THRESHOLD, direction="DOWN")
        bot.check_resolution(state, ts)
        self.assertIn("mkt1", state.open_trades)  # should remain open

    def test_skips_market_with_no_open_trade(self):
        state = make_state()
        self.addCleanup(state.conn.close)
        ts    = make_token(best_bid=bot.WIN_THRESHOLD)
        bot.check_resolution(state, ts)
        self.assertEqual(len(state.open_trades), 0)

    def test_win_at_market_expiry_above_half(self):
        state, tid = self._state_with_open_trade()
        ts = make_token(best_bid=0.60)
        ts.market_end_ms = (time.time() - 10) * 1000   # past end + grace
        bot.check_resolution(state, ts)
        row = state.conn.execute("SELECT outcome FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "WIN")

    def test_loss_at_market_expiry_below_half(self):
        state, tid = self._state_with_open_trade()
        ts = make_token(best_bid=0.40)
        ts.market_end_ms = (time.time() - 10) * 1000
        bot.check_resolution(state, ts)
        row = state.conn.execute("SELECT outcome FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], "LOSS")


# ── close_trade PnL ───────────────────────────────────────────────────────────

class TestCloseTrade(unittest.TestCase):

    def _setup(self, direction="UP"):
        state = make_state()
        self.addCleanup(state.conn.close)
        tid   = insert_trade(state.conn, direction=direction)
        state.open_trades["mkt1"]      = tid
        state.traded_direction["mkt1"] = direction
        return state, tid

    def test_win_increases_capital(self):
        state, _ = self._setup()
        before = state.capital
        ts = make_token(best_bid=bot.WIN_THRESHOLD)
        bot.check_resolution(state, ts)
        self.assertGreater(state.capital, before)

    def test_loss_decreases_capital(self):
        state, _ = self._setup()
        before = state.capital
        ts = make_token(best_bid=bot.LOSS_THRESHOLD)
        bot.check_resolution(state, ts)
        self.assertLess(state.capital, before)

    def test_win_increments_wins_counter(self):
        state, _ = self._setup()
        ts = make_token(best_bid=bot.WIN_THRESHOLD)
        bot.check_resolution(state, ts)
        self.assertEqual(state.wins, 1)
        self.assertEqual(state.losses, 0)

    def test_loss_increments_losses_counter(self):
        state, _ = self._setup()
        ts = make_token(best_bid=bot.LOSS_THRESHOLD)
        bot.check_resolution(state, ts)
        self.assertEqual(state.losses, 1)
        self.assertEqual(state.wins, 0)

    def test_trade_removed_from_open_trades(self):
        state, _ = self._setup()
        ts = make_token(best_bid=bot.WIN_THRESHOLD)
        bot.check_resolution(state, ts)
        self.assertNotIn("mkt1", state.open_trades)

    def test_pnl_net_stored_in_db(self):
        state, tid = self._setup()
        ts = make_token(best_bid=bot.WIN_THRESHOLD)
        bot.check_resolution(state, ts)
        row = state.conn.execute(
            "SELECT pnl_net, capital_after FROM trades WHERE id=?", (tid,)
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertAlmostEqual(row[1], state.capital)


# ── restore_state_from_db ─────────────────────────────────────────────────────

class TestRestoreState(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_restores_open_trade(self):
        insert_trade(self.conn)
        self.conn.execute("UPDATE trades SET market_id='mkt1', direction='UP' WHERE 1")
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertIn("mkt1", fresh.open_trades)
        self.assertIn("mkt1", fresh.signalled)

    def test_open_trade_in_traded_direction(self):
        insert_trade(self.conn, direction="DOWN")
        self.conn.execute("UPDATE trades SET market_id='mkt2' WHERE 1")
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertEqual(fresh.traded_direction.get("mkt2"), "DOWN")

    def test_capital_rebuilt_from_pnl(self):
        self.conn.execute(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, outcome, pnl_net) "
            "VALUES ('mkt1','tok1','UP',10,100,1,'WIN',5.0)"
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertAlmostEqual(fresh.capital, bot.CAPITAL_START + 5.0)

    def test_win_loss_counters_restored(self):
        self.conn.executemany(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, outcome, pnl_net) "
            "VALUES (?,?,?,10,100,1,?,?)",
            [
                ("mkt1", "tok1", "UP",   "WIN",  2.5),
                ("mkt2", "tok2", "DOWN", "WIN",  2.5),
                ("mkt3", "tok3", "UP",   "LOSS", -10.0),
            ],
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertEqual(fresh.wins,   2)
        self.assertEqual(fresh.losses, 1)

    def test_win_rate_after_restore(self):
        self.conn.executemany(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, outcome, pnl_net) "
            "VALUES (?,?,?,10,100,1,?,?)",
            [(f"m{i}", "t", "UP", "WIN", 1.0) for i in range(3)]
            + [("m9", "t", "UP", "LOSS", -10.0)],
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertAlmostEqual(fresh.win_rate, 75.0)

    def test_capital_goes_negative_on_all_losses(self):
        # 15 losses × -$10 = -$150 PnL → capital = $100 - $150 = -$50
        self.conn.executemany(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, outcome, pnl_net) "
            "VALUES (?,?,?,10,100,1,'LOSS',-10.0)",
            [(f"m{i}", "t", "UP") for i in range(15)],
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertLess(fresh.capital, 0,
                        "capital should be negative after enough losses")

    def test_negative_capital_equals_start_plus_pnl(self):
        # Formula holds even when total_pnl drives capital below zero.
        pnl_each = -10.0
        n = 15
        self.conn.executemany(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, outcome, pnl_net) "
            "VALUES (?,?,?,10,100,1,'LOSS',?)",
            [(f"m{i}", "t", "UP", pnl_each) for i in range(n)],
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        expected = bot.CAPITAL_START + pnl_each * n
        self.assertAlmostEqual(fresh.capital, expected)

    def test_negative_capital_loss_counter_correct(self):
        self.conn.executemany(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, outcome, pnl_net) "
            "VALUES (?,?,?,10,100,1,'LOSS',-10.0)",
            [(f"m{i}", "t", "UP") for i in range(5)],
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertEqual(fresh.losses, 5)
        self.assertEqual(fresh.wins, 0)


# ─── Daily PnL cache (enhancement II) ───────────────────────────────────────

class TestDailyPnlCache(unittest.TestCase):
    """
    Verify that close_trade updates state.daily_pnl incrementally and that
    restore_state_from_db initialises it from the DB on startup.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _close_via_resolution(self, direction, best_bid):
        state = make_state(conn=self.conn)
        tid = insert_trade(self.conn, direction=direction)
        state.open_trades["mkt1"]      = tid
        state.traded_direction["mkt1"] = direction
        ts = make_token(best_bid=best_bid)
        bot.check_resolution(state, ts)
        return state

    def test_win_increments_daily_pnl(self):
        state = self._close_via_resolution("UP", bot.WIN_THRESHOLD)
        self.assertGreater(state.daily_pnl, 0,
                           "daily_pnl should be positive after a WIN")

    def test_loss_decrements_daily_pnl(self):
        state = self._close_via_resolution("UP", bot.LOSS_THRESHOLD)
        self.assertLess(state.daily_pnl, 0,
                        "daily_pnl should be negative after a LOSS")

    def test_daily_pnl_matches_pnl_net_in_db(self):
        state = self._close_via_resolution("UP", bot.WIN_THRESHOLD)
        row = self.conn.execute(
            "SELECT pnl_net FROM trades WHERE resolved=1 LIMIT 1"
        ).fetchone()
        self.assertAlmostEqual(state.daily_pnl, row[0])

    def test_daily_pnl_accumulates_across_trades(self):
        state = make_state(conn=self.conn)
        for i in range(3):
            mid = f"mkt{i}"
            tid = insert_trade(self.conn, market_id=mid)
            state.open_trades[mid]      = tid
            state.traded_direction[mid] = "UP"
            ts = make_token(market_id=mid, best_bid=bot.WIN_THRESHOLD)
            bot.check_resolution(state, ts)
        self.assertGreater(state.daily_pnl, 0)
        # daily_pnl must equal sum of all pnl_net rows
        total = self.conn.execute(
            "SELECT COALESCE(SUM(pnl_net),0) FROM trades WHERE resolved=1"
        ).fetchone()[0]
        self.assertAlmostEqual(state.daily_pnl, total)

    def test_midnight_reset_clears_daily_pnl(self):
        state = make_state(conn=self.conn)
        state.daily_pnl = -25.0
        # Simulate "yesterday" by setting the day counter one behind current
        state._daily_pnl_day = int(time.time() // 86400) - 1
        # check_signal reads state._daily_pnl_day and resets on rollover
        state.config = bot.BotConfig(signal_threshold=0.95)
        ts = make_token(best_bid=0.90)  # below threshold — signal won't fire
        asyncio.run(bot.check_signal(state, ts))
        self.assertEqual(state.daily_pnl, 0.0,
                         "daily_pnl should reset to 0 after midnight rollover")
        self.assertEqual(state._daily_pnl_day, int(time.time() // 86400))

    def test_restore_loads_today_pnl(self):
        today_ms = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
        self.conn.executemany(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, "
            "resolved, outcome, pnl_net, signal_ts_ms) "
            "VALUES (?,?,?,10,100,1,'WIN',?,?)",
            [(f"m{i}", "t", "UP", 3.0, today_ms + i * 1000) for i in range(4)],
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertAlmostEqual(fresh.daily_pnl, 12.0,
                               msg="restore should sum today's pnl_net from DB")

    def test_restore_excludes_yesterday_pnl(self):
        yesterday_ms = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        ) - 86_400_000
        self.conn.execute(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, "
            "resolved, outcome, pnl_net, signal_ts_ms) "
            "VALUES ('m0','t','UP',10,100,1,'WIN',5.0,?)",
            (yesterday_ms,),
        )
        self.conn.commit()
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertAlmostEqual(fresh.daily_pnl, 0.0,
                               msg="yesterday's pnl_net must not count in daily_pnl")


# ─── Signalled restore for recent resolved trades (enhancement VI) ───────────

class TestSignalledRestore(unittest.TestCase):
    """
    restore_state_from_db must add recently resolved markets to signalled
    to prevent re-entry if the bot restarts within the same market window.
    """

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _insert_resolved(self, market_id: str, signal_ts_ms: int,
                         outcome: str = "WIN") -> None:
        self.conn.execute(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, "
            "resolved, outcome, pnl_net, signal_ts_ms) "
            "VALUES (?,?,?,10,100,1,?,1.0,?)",
            (market_id, "tok", "UP", outcome, signal_ts_ms),
        )
        self.conn.commit()

    def test_recent_resolved_added_to_signalled(self):
        now_ms = int(time.time() * 1000)
        self._insert_resolved("recent_mkt", now_ms - 120_000)  # 2 min ago
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertIn("recent_mkt", fresh.signalled)

    def test_old_resolved_not_added_to_signalled(self):
        now_ms = int(time.time() * 1000)
        self._insert_resolved("old_mkt", now_ms - 700_000)  # 11+ min ago
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertNotIn("old_mkt", fresh.signalled)

    def test_open_trade_still_in_signalled(self):
        # Unresolved trades must remain in signalled regardless of this feature.
        insert_trade(self.conn, market_id="open_mkt")
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertIn("open_mkt", fresh.signalled)

    def test_boundary_just_inside_window(self):
        now_ms = int(time.time() * 1000)
        self._insert_resolved("edge_mkt", now_ms - 599_000)  # 9m59s ago — inside
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertIn("edge_mkt", fresh.signalled)

    def test_boundary_just_outside_window(self):
        now_ms = int(time.time() * 1000)
        self._insert_resolved("edge2_mkt", now_ms - 601_000)  # 10m01s ago — outside
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        self.assertNotIn("edge2_mkt", fresh.signalled)

    def test_multiple_recent_markets_all_signalled(self):
        now_ms = int(time.time() * 1000)
        for i in range(4):
            self._insert_resolved(f"m{i}", now_ms - i * 60_000)
        fresh = bot.BotState(self.conn)
        bot.restore_state_from_db(fresh)
        for i in range(4):
            self.assertIn(f"m{i}", fresh.signalled)


# ─── _htpasswd ───────────────────────────────────────────────────────────────

class TestHtpasswd(unittest.TestCase):

    def test_prefix_bcrypt(self):
        if bot_utils._BCRYPT_AVAILABLE:
            self.assertTrue(bot_utils._htpasswd("anything").startswith("$2"))
        else:
            self.assertTrue(bot_utils._htpasswd("anything").startswith("{SHA}"))

    def test_different_passwords_differ(self):
        self.assertNotEqual(bot_utils._htpasswd("abc"), bot_utils._htpasswd("xyz"))

    def test_bcrypt_verifies(self):
        if not bot_utils._BCRYPT_AVAILABLE:
            self.skipTest("bcrypt not installed")
        import bcrypt
        h = bot_utils._htpasswd("secret")
        self.assertTrue(bcrypt.checkpw(b"secret", h.encode()))


# ─── generate_status_html ─────────────────────────────────────────────────────

class TestGenerateStatusHtml(unittest.TestCase):

    def _state(self):
        state = make_state()
        self.addCleanup(state.conn.close)
        state.capital      = 123.45
        state.total_pnl    = 3.45
        state.total_trades = 5
        state.wins         = 4
        state.losses       = 1
        return state

    def test_contains_capital(self):
        self.assertIn("123.45", bot_utils.generate_status_html(self._state()))

    def test_contains_table(self):
        html = bot_utils.generate_status_html(self._state())
        self.assertIn("<table>", html)
        self.assertIn("Direction", html)

    def test_no_trades_message_when_empty(self):
        self.assertIn("No resolved trades", bot_utils.generate_status_html(self._state()))

    def test_win_rate_shown(self):
        self.assertIn("80.0%", bot_utils.generate_status_html(self._state()))


# ─── handle_book_update (integration) ────────────────────────────────────────

class TestHandleBookUpdate(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    async def test_state_updated_from_message(self):
        self.state.session = None
        now_ms = int(time.time() * 1000)
        ts = bot.TokenState("tid", "mkt1", "UP", "q",
                            now_ms - 60_000, now_ms + 300_000)
        ts.best_bid = 0.50
        self.state.tokens["tid"] = ts
        parsed = {"token_id": "tid", "best_bid": 0.55, "best_ask": 0.56,
                  "spread": 0.01, "bid_vol": 100.0, "ask_vol": 80.0, "obi": 0.1}
        await bot.handle_book_update(self.state, parsed)
        self.assertAlmostEqual(ts.best_bid, 0.55)
        self.assertAlmostEqual(ts.ask_vol, 80.0)

    async def test_unknown_token_ignored(self):
        parsed = {"token_id": "unknown", "best_bid": 0.97, "best_ask": 0.975,
                  "spread": 0.005, "bid_vol": 100.0, "ask_vol": 50.0, "obi": 0.0}
        await bot.handle_book_update(self.state, parsed)
        self.assertEqual(len(self.state.open_trades), 0)


class TestIsTradingHour(unittest.TestCase):
    """Tests for is_trading_hour() with various UTC times and filter configs."""

    def _enable(self, weekday=None, weekend=None, us_open=True, us_close=True) -> bot.BotConfig:
        return bot.BotConfig(
            hour_filter_enabled=True,
            weekday_utc_ranges=weekday if weekday is not None else [(0, 8), (13, 22)],
            weekend_utc_ranges=weekend if weekend is not None else [],
            us_weekly_open=us_open,
            us_weekly_close=us_close,
        )

    def _ts(self, iso: str) -> int:
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)

    def test_disabled_always_true(self):
        cfg = bot.BotConfig()   # hour_filter_enabled=False by default
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-04-27 03:00:00")))  # Monday 3h

    def test_weekday_in_range(self):
        cfg = self._enable()
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-04-28 06:00:00")))  # Tuesday 6h

    def test_weekday_outside_range(self):
        cfg = self._enable()
        self.assertFalse(bot.is_trading_hour(cfg, self._ts("2026-04-28 10:00:00")))  # Tuesday 10h

    def test_monday_before_us_open_blocked(self):
        cfg = self._enable()
        self.assertFalse(bot.is_trading_hour(cfg, self._ts("2026-04-27 12:00:00")))  # Monday 12h

    def test_monday_before_us_open_minute_precision(self):
        cfg = self._enable()
        self.assertFalse(bot.is_trading_hour(cfg, self._ts("2026-04-27 13:29:00")))  # Monday 13h29

    def test_monday_after_us_open_allowed(self):
        cfg = self._enable()
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-04-27 14:00:00")))   # Monday 14h

    def test_friday_after_us_close_blocked(self):
        cfg = self._enable()
        self.assertFalse(bot.is_trading_hour(cfg, self._ts("2026-05-01 21:00:00")))  # Friday 21h

    def test_friday_before_us_close_allowed(self):
        cfg = self._enable()
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-05-01 15:00:00")))   # Friday 15h

    def test_weekend_blocked_by_default(self):
        cfg = self._enable(weekend=[])
        self.assertFalse(bot.is_trading_hour(cfg, self._ts("2026-04-26 15:00:00")))  # Saturday

    def test_weekend_allowed_when_configured(self):
        cfg = self._enable(weekend=[(13, 20)])
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-04-26 15:00:00")))   # Saturday 15h

    def test_weekend_outside_range_blocked(self):
        cfg = self._enable(weekend=[(13, 20)])
        self.assertFalse(bot.is_trading_hour(cfg, self._ts("2026-04-26 10:00:00")))  # Saturday 10h

    def test_empty_weekday_ranges_allows_all_hours(self):
        cfg = self._enable(weekday=[])
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-04-28 10:00:00")))   # Tuesday 10h

    def test_us_open_flag_disabled(self):
        # Monday 7h is in range (0-8) but would be blocked by US_WEEKLY_OPEN=True.
        # With us_open=False the special Monday constraint is lifted → allowed.
        cfg = self._enable(us_open=False)
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-04-27 07:00:00")))   # Monday 7h ok

    def test_us_close_flag_disabled(self):
        cfg = self._enable(us_close=False)
        self.assertTrue(bot.is_trading_hour(cfg, self._ts("2026-05-01 21:00:00")))   # Friday 21h ok

    def test_now_uses_current_time(self):
        cfg = bot.BotConfig(hour_filter_enabled=True, weekday_utc_ranges=[], weekend_utc_ranges=[])
        # No ts_ms → uses datetime.now() — just check it doesn't crash
        result = bot.is_trading_hour(cfg)
        self.assertIsInstance(result, bool)


class TestInWeekendSession(unittest.TestCase):
    """Tests for _in_weekend_session() — Fri 20:00 UTC → Mon 13:30 UTC."""

    def _ts(self, iso: str) -> int:
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)

    def test_saturday_is_weekend(self):
        self.assertTrue(bot._in_weekend_session(self._ts("2026-04-26 12:00:00")))  # Sat noon

    def test_sunday_is_weekend(self):
        self.assertTrue(bot._in_weekend_session(self._ts("2026-04-27 00:00:00")))  # Sun midnight

    def test_friday_before_close_is_not_weekend(self):
        self.assertFalse(bot._in_weekend_session(self._ts("2026-05-01 19:59:00")))  # Fri 19:59

    def test_friday_at_close_is_weekend(self):
        self.assertTrue(bot._in_weekend_session(self._ts("2026-05-01 20:00:00")))  # Fri 20:00

    def test_friday_after_close_is_weekend(self):
        self.assertTrue(bot._in_weekend_session(self._ts("2026-05-01 22:00:00")))  # Fri 22:00

    def test_monday_before_open_is_weekend(self):
        self.assertTrue(bot._in_weekend_session(self._ts("2026-04-27 07:00:00")))  # Mon 7h

    def test_monday_at_open_is_not_weekend(self):
        self.assertFalse(bot._in_weekend_session(self._ts("2026-04-27 13:30:00")))  # Mon 13:30

    def test_monday_after_open_is_not_weekend(self):
        self.assertFalse(bot._in_weekend_session(self._ts("2026-04-27 14:00:00")))  # Mon 14h

    def test_tuesday_is_not_weekend(self):
        self.assertFalse(bot._in_weekend_session(self._ts("2026-04-28 10:00:00")))  # Tue

    def test_no_args_uses_current_time(self):
        result = bot._in_weekend_session()
        self.assertIsInstance(result, bool)


# ── enter_live_trade ──────────────────────────────────────────────────────────

class TestEnterLiveTrade(unittest.IsolatedAsyncioTestCase):
    """
    enter_live_trade writes a DB row, updates BotState, and skips the CLOB API
    when state.session is None (simulation mode — no network calls in tests).
    """

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    async def test_db_row_written(self):
        await bot.enter_live_trade(self.state, make_token())
        count = self.state.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count, 1)

    async def test_resolved_is_zero(self):
        await bot.enter_live_trade(self.state, make_token())
        resolved = self.state.conn.execute("SELECT resolved FROM trades").fetchone()[0]
        self.assertEqual(resolved, 0)

    async def test_entry_price_is_best_ask(self):
        ts = make_token(best_ask=0.975)
        await bot.enter_live_trade(self.state, ts)
        ep = self.state.conn.execute("SELECT entry_price FROM trades").fetchone()[0]
        self.assertAlmostEqual(ep, 0.975)

    async def test_stake_stored(self):
        await bot.enter_live_trade(self.state, make_token())
        stake = self.state.conn.execute("SELECT stake FROM trades").fetchone()[0]
        self.assertAlmostEqual(stake, bot.STAKE)

    async def test_tokens_bought_correct(self):
        ts = make_token(best_ask=0.975)
        await bot.enter_live_trade(self.state, ts)
        expected = bot.STAKE / 0.975
        tb = self.state.conn.execute("SELECT tokens_bought FROM trades").fetchone()[0]
        self.assertAlmostEqual(tb, expected, places=6)

    async def test_fee_stored(self):
        ts = make_token(best_ask=0.975)
        await bot.enter_live_trade(self.state, ts)
        ep = 0.975
        expected_fee = api_poly.compute_fee(ep, bot.STAKE / ep)
        fee = self.state.conn.execute("SELECT fee FROM trades").fetchone()[0]
        self.assertAlmostEqual(fee, expected_fee, places=8)

    async def test_cost_total_is_stake_plus_fee(self):
        ts = make_token(best_ask=0.975)
        await bot.enter_live_trade(self.state, ts)
        row = self.state.conn.execute("SELECT stake, fee, cost_total FROM trades").fetchone()
        self.assertAlmostEqual(row[2], row[0] + row[1], places=8)

    async def test_capital_before_stored(self):
        capital_before = self.state.capital
        await bot.enter_live_trade(self.state, make_token())
        cb = self.state.conn.execute("SELECT capital_before FROM trades").fetchone()[0]
        self.assertAlmostEqual(cb, capital_before)

    async def test_capital_unchanged(self):
        capital_before = self.state.capital
        await bot.enter_live_trade(self.state, make_token())
        self.assertAlmostEqual(self.state.capital, capital_before)

    async def test_direction_stored(self):
        await bot.enter_live_trade(self.state, make_token(direction="DOWN"))
        direction = self.state.conn.execute("SELECT direction FROM trades").fetchone()[0]
        self.assertEqual(direction, "DOWN")

    async def test_market_id_stored(self):
        await bot.enter_live_trade(self.state, make_token(market_id="mkt_abc"))
        mid = self.state.conn.execute("SELECT market_id FROM trades").fetchone()[0]
        self.assertEqual(mid, "mkt_abc")

    async def test_open_trades_updated(self):
        ts = make_token(market_id="mkt1")
        await bot.enter_live_trade(self.state, ts)
        self.assertIn("mkt1", self.state.open_trades)
        self.assertIsInstance(self.state.open_trades["mkt1"], int)

    async def test_traded_direction_updated(self):
        await bot.enter_live_trade(self.state, make_token(direction="UP"))
        self.assertEqual(self.state.traded_direction["mkt1"], "UP")

    async def test_total_trades_incremented(self):
        self.assertEqual(self.state.total_trades, 0)
        await bot.enter_live_trade(self.state, make_token())
        self.assertEqual(self.state.total_trades, 1)

    async def test_no_clob_call_without_session(self):
        # state.session is None → api.post_order is never called;
        # clob_order_id must be NULL in DB (not a real order ID).
        await bot.enter_live_trade(self.state, make_token())
        oid = self.state.conn.execute("SELECT clob_order_id FROM trades").fetchone()[0]
        self.assertIsNone(oid)

    async def test_two_markets_independent(self):
        await bot.enter_live_trade(self.state, make_token(market_id="mktA", token_id="tokA"))
        await bot.enter_live_trade(self.state, make_token(market_id="mktB", token_id="tokB"))
        self.assertEqual(self.state.total_trades, 2)
        self.assertIn("mktA", self.state.open_trades)
        self.assertIn("mktB", self.state.open_trades)
        self.assertNotEqual(
            self.state.open_trades["mktA"],
            self.state.open_trades["mktB"],
        )


# ── save_snapshot ─────────────────────────────────────────────────────────────

class TestSaveSnapshot(unittest.TestCase):
    """
    save_snapshot writes one row to the `snapshots` table per call.
    All TokenState fields must be persisted verbatim; ts_ms must be a
    recent millisecond timestamp; has_open_trade must reflect whether the
    market currently has an open trade in BotState.
    """

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    def _snap(self, **kw):
        ts = make_token(**kw)
        bot.save_snapshot(self.state, ts)
        return ts, self.state.conn.execute("SELECT * FROM snapshots").fetchone()

    def test_row_inserted(self):
        bot.save_snapshot(self.state, make_token())
        count = self.state.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual(count, 1)

    def test_ts_ms_is_recent(self):
        _, row = self._snap()
        now_ms = int(time.time() * 1000)
        # ts_ms must be within 2 seconds of now
        self.assertAlmostEqual(row[1], now_ms, delta=2000)

    def test_ts_ms_is_integer(self):
        _, row = self._snap()
        self.assertIsInstance(row[1], int)

    def test_market_id_stored(self):
        _, row = self._snap(market_id="mkt_snap")
        self.assertEqual(row[2], "mkt_snap")

    def test_token_id_stored(self):
        _, row = self._snap(token_id="tok_snap")
        self.assertEqual(row[3], "tok_snap")

    def test_direction_stored(self):
        _, row = self._snap(direction="DOWN")
        self.assertEqual(row[4], "DOWN")

    def test_secs_remaining_stored(self):
        ts, row = self._snap(secs_remaining=120)
        # secs_remaining is computed live from time.time(); allow 2 s tolerance
        self.assertAlmostEqual(row[5], ts.secs_remaining, delta=2.0)

    def test_best_bid_stored(self):
        _, row = self._snap(best_bid=0.963)
        self.assertAlmostEqual(row[6], 0.963)

    def test_best_ask_stored(self):
        _, row = self._snap(best_ask=0.968)
        self.assertAlmostEqual(row[7], 0.968)

    def test_spread_stored(self):
        ts, row = self._snap(best_bid=0.963, best_ask=0.968)
        self.assertAlmostEqual(row[8], ts.spread)

    def test_ask_vol_stored(self):
        _, row = self._snap(ask_vol=42.5)
        self.assertAlmostEqual(row[9], 42.5)

    def test_obi_stored(self):
        _, row = self._snap(obi=-0.10)
        self.assertAlmostEqual(row[10], -0.10)

    def test_has_open_trade_false_when_no_trade(self):
        _, row = self._snap(market_id="mkt1")
        self.assertEqual(row[11], 0)

    def test_has_open_trade_true_when_open_trade_exists(self):
        self.state.open_trades["mkt1"] = 99
        _, row = self._snap(market_id="mkt1")
        self.assertEqual(row[11], 1)

    def test_has_open_trade_false_for_different_market(self):
        self.state.open_trades["mktOther"] = 99
        _, row = self._snap(market_id="mkt1")
        self.assertEqual(row[11], 0)

    def test_multiple_snapshots_accumulate(self):
        ts = make_token()
        bot.save_snapshot(self.state, ts)
        bot.save_snapshot(self.state, ts)
        bot.save_snapshot(self.state, ts)
        count = self.state.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        self.assertEqual(count, 3)

    def test_two_tokens_stored_independently(self):
        bot.save_snapshot(self.state, make_token(token_id="tokA", market_id="mktA"))
        bot.save_snapshot(self.state, make_token(token_id="tokB", market_id="mktB"))
        rows = self.state.conn.execute(
            "SELECT token_id FROM snapshots ORDER BY id"
        ).fetchall()
        self.assertEqual([r[0] for r in rows], ["tokA", "tokB"])

    def test_no_commit_after_save_snapshot(self):
        # M-7: save_snapshot no longer commits; row is visible in the same
        # connection but conn.in_transaction is True (uncommitted write pending).
        bot.save_snapshot(self.state, make_token())
        self.assertTrue(self.state.conn.in_transaction)


# ── circuit-breaker ───────────────────────────────────────────────────────────

class TestCircuitBreaker(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    def test_initial_state(self):
        self.assertEqual(self.state.api_fail_streak, 0)
        self.assertAlmostEqual(self.state.api_cooldown_until, 0.0)

    async def test_cooldown_blocks_signal(self):
        self.state.api_cooldown_until = time.time() + 300
        await bot.check_signal(self.state, make_token())
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_expired_cooldown_allows_signal(self):
        self.state.api_cooldown_until = time.time() - 1
        await bot.check_signal(self.state, make_token())
        self.assertIn("mkt1", self.state.signalled)

    async def test_streak_increments_on_api_failure(self):
        self.state.config = bot.BotConfig(private_key="0xdeadbeef")
        self.state.session = unittest.mock.AsyncMock()
        with patch("live_bot.api.post_order", new=unittest.mock.AsyncMock(return_value=None)):
            await bot.enter_live_trade(self.state, make_token())
        self.assertEqual(self.state.api_fail_streak, 1)

    async def test_cooldown_set_after_3_failures(self):
        self.state.config = bot.BotConfig(private_key="0xdeadbeef")
        self.state.session = unittest.mock.AsyncMock()
        self.state.api_fail_streak = 2
        with patch("live_bot.api.post_order", new=unittest.mock.AsyncMock(return_value=None)):
            await bot.enter_live_trade(self.state, make_token(market_id="mkt9", token_id="tok9"))
        self.assertGreater(self.state.api_cooldown_until, time.time())

    async def test_streak_resets_on_success(self):
        self.state.config = bot.BotConfig(private_key="0xdeadbeef")
        self.state.session = unittest.mock.AsyncMock()
        self.state.api_fail_streak = 2
        with patch("live_bot.api.post_order", new=unittest.mock.AsyncMock(return_value="ord_ok")):
            await bot.enter_live_trade(self.state, make_token())
        self.assertEqual(self.state.api_fail_streak, 0)

    async def test_no_streak_in_simulation_mode(self):
        # state.session is None → no CLOB call → streak must not change
        self.state.config = bot.BotConfig(private_key="0xdeadbeef")
        await bot.enter_live_trade(self.state, make_token())
        self.assertEqual(self.state.api_fail_streak, 0)

    async def test_no_db_insert_on_live_clob_failure(self):
        # H-1: post_order returns None in live mode → no ghost row, no open_trade entry
        self.state.config = bot.BotConfig(private_key="0xdeadbeef")
        self.state.session = unittest.mock.AsyncMock()
        with patch("live_bot.api.post_order", new=unittest.mock.AsyncMock(return_value=None)):
            await bot.enter_live_trade(self.state, make_token(market_id="mkt_ghost"))
        count = self.state.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count, 0, "ghost row must not be inserted on CLOB failure")
        self.assertNotIn("mkt_ghost", self.state.open_trades)

    async def test_db_insert_on_live_clob_success(self):
        # H-1 regression: post_order returns a valid order ID → row IS inserted
        self.state.config = bot.BotConfig(private_key="0xdeadbeef")
        self.state.session = unittest.mock.AsyncMock()
        with patch("live_bot.api.post_order", new=unittest.mock.AsyncMock(return_value="ord_123")):
            await bot.enter_live_trade(self.state, make_token(market_id="mkt_live"))
        count = self.state.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count, 1)
        self.assertIn("mkt_live", self.state.open_trades)

    async def test_db_insert_in_simulation_no_session(self):
        # H-1 regression: simulation mode (no session) → row IS inserted with oid=None
        await bot.enter_live_trade(self.state, make_token(market_id="mkt_sim"))
        count = self.state.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count, 1)
        oid = self.state.conn.execute("SELECT clob_order_id FROM trades").fetchone()[0]
        self.assertIsNone(oid)


# ── schema versioning ─────────────────────────────────────────────────────────

class TestSchemaVersioning(unittest.TestCase):

    def _fresh_conn(self):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.executescript(bot.SCHEMA)
        return conn

    def test_schema_version_table_exists(self):
        conn = self._fresh_conn()
        bot._apply_migrations(conn)
        conn.commit()
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()]
        self.assertIn("schema_version", tables)
        conn.close()

    def test_version_set_to_max_migration_key(self):
        conn = self._fresh_conn()
        bot._apply_migrations(conn)
        conn.commit()
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        self.assertEqual(ver, max(bot.MIGRATIONS))
        conn.close()

    def test_idempotent_second_call(self):
        conn = self._fresh_conn()
        bot._apply_migrations(conn)
        conn.commit()
        bot._apply_migrations(conn)  # must not raise or duplicate the row
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        self.assertEqual(count, 1)
        conn.close()

    def test_future_migration_applied(self):
        original = dict(bot.MIGRATIONS)
        try:
            bot.MIGRATIONS[99] = (
                "CREATE TABLE IF NOT EXISTS _test_mig (x INTEGER);"
            )
            conn = self._fresh_conn()
            bot._apply_migrations(conn)
            conn.commit()
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            self.assertIn("_test_mig", tables)
            ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
            self.assertEqual(ver, 99)
            conn.close()
        finally:
            bot.MIGRATIONS.clear()
            bot.MIGRATIONS.update(original)

    def test_partial_upgrade_from_version_0(self):
        # Simulate an old DB at version 0 (schema_version is empty).
        conn = self._fresh_conn()
        conn.commit()  # schema_version table exists but has no rows
        bot._apply_migrations(conn)
        conn.commit()
        ver = conn.execute("SELECT version FROM schema_version").fetchone()[0]
        self.assertEqual(ver, max(bot.MIGRATIONS))
        conn.close()


class TestUsHolidays(unittest.TestCase):
    """
    _us_holidays returns the correct observed dates for each of the 10 US
    federal holidays.  _is_us_holiday and is_trading_hour must block on those
    days when us_holiday_filter=True.
    """

    def _holidays(self, year):
        return bot._us_holidays(year)

    # ── fixed-date holidays ──────────────────────────────────────────────────

    def test_new_years_day_2026(self):
        from datetime import date
        self.assertIn(date(2026, 1, 1), self._holidays(2026))

    def test_independence_day_observed_2026(self):
        # July 4 2026 is a Saturday → observed Fri July 3
        from datetime import date
        self.assertIn(date(2026, 7, 3), self._holidays(2026))
        self.assertNotIn(date(2026, 7, 4), self._holidays(2026))

    def test_christmas_2026(self):
        # Dec 25 2026 is a Friday → no shift
        from datetime import date
        self.assertIn(date(2026, 12, 25), self._holidays(2026))

    def test_juneteenth_observed_2023(self):
        # June 19 2023 is a Monday → no shift
        from datetime import date
        self.assertIn(date(2023, 6, 19), self._holidays(2023))

    # ── floating holidays ────────────────────────────────────────────────────

    def test_thanksgiving_2025(self):
        # 4th Thursday of November 2025 = Nov 27
        from datetime import date
        self.assertIn(date(2025, 11, 27), self._holidays(2025))

    def test_memorial_day_2026(self):
        # Last Monday of May 2026 = May 25
        from datetime import date
        self.assertIn(date(2026, 5, 25), self._holidays(2026))

    def test_mlk_day_2026(self):
        # 3rd Monday of January 2026 = Jan 19
        from datetime import date
        self.assertIn(date(2026, 1, 19), self._holidays(2026))

    def test_good_friday_2025(self):
        # Easter 2025 = Apr 20 → Good Friday = Apr 18
        from datetime import date
        self.assertIn(date(2025, 4, 18), self._holidays(2025))

    def test_ten_holidays_per_year(self):
        self.assertEqual(len(self._holidays(2026)), 10)

    def test_lru_cache_same_object(self):
        # lru_cache means same year returns identical frozenset
        self.assertIs(self._holidays(2026), self._holidays(2026))

    # ── is_trading_hour integration ──────────────────────────────────────────

    def test_blocks_on_holiday_when_filter_enabled(self):
        # Christmas 2026 (Fri Dec 25), 15:00 UTC
        cfg = bot.BotConfig(us_holiday_filter=True)
        dt = datetime(2026, 12, 25, 15, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))

    def test_allows_on_holiday_when_filter_disabled(self):
        cfg = bot.BotConfig(us_holiday_filter=False)
        dt = datetime(2026, 12, 25, 15, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))

    def test_normal_weekday_not_blocked(self):
        cfg = bot.BotConfig(us_holiday_filter=True)
        # Wednesday 2026-05-06, 15:00 UTC — not a holiday
        dt = datetime(2026, 5, 6, 15, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))

    def test_holiday_blocks_independently_of_hour_filter(self):
        # us_holiday_filter=True blocks even when hour_filter_enabled=False
        cfg = bot.BotConfig(us_holiday_filter=True, hour_filter_enabled=False)
        dt = datetime(2026, 12, 25, 15, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))


class TestSnapshotInterval(unittest.IsolatedAsyncioTestCase):
    """
    BotConfig.snapshot_interval controls how often handle_book_update writes a
    snapshot row.  The default is SNAPSHOT_INTERVAL (1s); it can be overridden
    at construction time.  handle_book_update must respect the configured value.
    """

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    def _snap_count(self):
        return self.state.conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    def test_default_is_five(self):
        cfg = bot.BotConfig()
        self.assertEqual(cfg.snapshot_interval, bot.SNAPSHOT_INTERVAL)
        self.assertEqual(cfg.snapshot_interval, 1)

    def test_custom_value_stored(self):
        cfg = bot.BotConfig(snapshot_interval=1)
        self.assertEqual(cfg.snapshot_interval, 1)

    def test_large_value_stored(self):
        cfg = bot.BotConfig(snapshot_interval=60)
        self.assertEqual(cfg.snapshot_interval, 60)

    async def _update(self, ts):
        """Run one handle_book_update cycle for ts (session=None skips orders)."""
        self.state.session = None
        parsed = {
            "token_id": ts.token_id, "best_bid": ts.best_bid, "best_ask": ts.best_ask,
            "spread": 0.005, "bid_vol": 100.0, "ask_vol": ts.ask_vol, "obi": ts.obi,
        }
        await bot.handle_book_update(self.state, parsed)

    async def test_snapshot_written_after_interval_elapsed(self):
        ts = make_token(best_bid=0.50, best_ask=0.55)
        self.state.tokens[ts.token_id] = ts
        self.state.config = bot.BotConfig(snapshot_interval=1)
        ts.last_snapshot_ts = time.time() - 2  # 2s ago > 1s interval
        await self._update(ts)
        self.assertEqual(self._snap_count(), 1)

    async def test_snapshot_not_written_before_interval(self):
        ts = make_token(best_bid=0.50, best_ask=0.55)
        self.state.tokens[ts.token_id] = ts
        self.state.config = bot.BotConfig(snapshot_interval=60)
        ts.last_snapshot_ts = time.time() - 1  # 1s ago < 60s interval
        await self._update(ts)
        self.assertEqual(self._snap_count(), 0)

    async def test_one_second_interval_allows_rapid_snapshots(self):
        ts = make_token(best_bid=0.50, best_ask=0.55)
        self.state.tokens[ts.token_id] = ts
        self.state.config = bot.BotConfig(snapshot_interval=1)
        ts.last_snapshot_ts = 0.0  # never snapshotted → always elapsed
        await self._update(ts)
        self.assertEqual(self._snap_count(), 1)

    async def test_batch_commit_fires_after_interval(self):
        # M-7: commit fires when SNAPSHOT_COMMIT_SECS have elapsed.
        ts = make_token(best_bid=0.50, best_ask=0.55)
        self.state.tokens[ts.token_id] = ts
        self.state.config = bot.BotConfig(snapshot_interval=1)
        ts.last_snapshot_ts = 0.0
        self.state.last_snapshot_commit_ts = 0.0  # force commit on first update
        await self._update(ts)
        self.assertFalse(self.state.conn.in_transaction)  # committed

    async def test_batch_commit_deferred_within_interval(self):
        # M-7: commit is skipped when SNAPSHOT_COMMIT_SECS have NOT elapsed.
        ts = make_token(best_bid=0.50, best_ask=0.55)
        self.state.tokens[ts.token_id] = ts
        self.state.config = bot.BotConfig(snapshot_interval=1)
        ts.last_snapshot_ts = 0.0
        self.state.last_snapshot_commit_ts = time.time()  # just committed
        await self._update(ts)
        self.assertTrue(self.state.conn.in_transaction)  # not yet committed


class TestStrategyLoading(unittest.TestCase):
    """Verify the active strategy JSON loads correctly with sweep-optimised params."""

    _STRAT_DIR = os.path.join(os.path.dirname(__file__), "..", "strategies")

    def _load(self, name):
        return bot.load_strategy(os.path.join(self._STRAT_DIR, name))

    def test_file_exists(self):
        path = os.path.join(self._STRAT_DIR, "polymarket_BTC5M.json")
        self.assertTrue(os.path.exists(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(bot.load_strategy("/nonexistent/strategy.json"))

    def test_threshold(self):
        s = self._load("polymarket_BTC5M.json")
        self.assertAlmostEqual(s["signal_threshold"], 0.95)

    def test_min_secs(self):
        s = self._load("polymarket_BTC5M.json")
        self.assertEqual(s["min_secs_remaining"], 30)

    def test_obi(self):
        s = self._load("polymarket_BTC5M.json")
        self.assertAlmostEqual(s["obi_reject_thresh"], -0.75)

    def test_dsl(self):
        s = self._load("polymarket_BTC5M.json")
        self.assertAlmostEqual(s["daily_stop_loss"], 30.0)


# ── Connector factory ─────────────────────────────────────────────────────────

import connectors as _connectors_mod

class TestConnectorFactory(unittest.TestCase):
    """connectors.load() returns the correct api_* module."""

    def test_polymarket(self):
        mod = _connectors_mod.load("polymarket")
        self.assertTrue(hasattr(mod, "parse_book_update"))
        self.assertTrue(hasattr(mod, "post_order"))
        self.assertTrue(hasattr(mod, "compute_fee"))

    def test_binance(self):
        mod = _connectors_mod.load("binance")
        self.assertTrue(hasattr(mod, "parse_book_update"))
        self.assertTrue(hasattr(mod, "post_order"))

    def test_mexc(self):
        mod = _connectors_mod.load("mexc")
        self.assertTrue(hasattr(mod, "parse_book_update"))
        self.assertTrue(hasattr(mod, "post_order"))

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            _connectors_mod.load("unknown_exchange")

    def test_available_lists_three(self):
        avail = _connectors_mod.available()
        self.assertIn("polymarket", avail)
        self.assertIn("binance", avail)
        self.assertIn("mexc", avail)

    def test_polymarket_and_binance_share_interface(self):
        """Both modules expose the same public surface."""
        pm = _connectors_mod.load("polymarket")
        bn = _connectors_mod.load("binance")
        for attr in ("parse_book_update", "post_order", "compute_fee",
                     "make_subscribe_msg", "WS_URL", "WS_BATCH_SIZE"):
            self.assertTrue(hasattr(pm, attr), f"polymarket missing {attr}")
            self.assertTrue(hasattr(bn, attr), f"binance missing {attr}")

    def test_binance_grid_extensions(self):
        """Binance exposes cancel_order, get_order_status, get_open_orders."""
        mod = _connectors_mod.load("binance")
        for fn in ("cancel_order", "get_order_status", "get_open_orders"):
            self.assertTrue(hasattr(mod, fn), f"binance missing {fn}")

    def test_mexc_grid_extensions(self):
        mod = _connectors_mod.load("mexc")
        for fn in ("cancel_order", "get_order_status", "get_open_orders"):
            self.assertTrue(hasattr(mod, fn), f"mexc missing {fn}")


# ── Strategy factory ──────────────────────────────────────────────────────────

import strategy_engines as _strategies_mod
from strategy_engines.grid import GridStrategy

class TestStrategyFactory(unittest.TestCase):
    """strategies.load() returns the correct strategy or None for threshold."""

    def _grid_config(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 90000.0
        cfg.grid_upper           = 110000.0
        cfg.grid_levels          = 10
        cfg.grid_order_size_usdt = 50.0
        return cfg

    def test_threshold_returns_none(self):
        result = _strategies_mod.load("threshold", bot.BotConfig())
        self.assertIsNone(result)

    def test_grid_returns_strategy(self):
        s = _strategies_mod.load("grid", self._grid_config())
        self.assertIsInstance(s, GridStrategy)

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            _strategies_mod.load("no_such_strategy", bot.BotConfig())

    def test_available_includes_threshold_and_grid(self):
        avail = _strategies_mod.available()
        self.assertIn("threshold", avail)
        self.assertIn("grid", avail)


class TestGridStrategy(unittest.TestCase):
    """GridStrategy initialisation and level computation."""

    def _make(self, lower=90000.0, upper=110000.0, levels=10, size=50.0):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = lower
        cfg.grid_upper           = upper
        cfg.grid_levels          = levels
        cfg.grid_order_size_usdt = size
        return GridStrategy(cfg)

    def test_level_count(self):
        s = self._make(levels=10)
        self.assertEqual(len(s.levels), 10)

    def test_level_count_20(self):
        s = self._make(levels=20)
        self.assertEqual(len(s.levels), 20)

    def test_lower_bound(self):
        s = self._make(lower=90000.0, upper=110000.0, levels=10)
        self.assertAlmostEqual(s.levels[0].price, 90000.0)

    def test_upper_bound(self):
        s = self._make(lower=90000.0, upper=110000.0, levels=10)
        self.assertAlmostEqual(s.levels[-1].price, 110000.0)

    def test_grid_step(self):
        s = self._make(lower=90000.0, upper=110000.0, levels=11)
        self.assertAlmostEqual(s.grid.grid_step, 2000.0)

    def test_all_levels_idle_at_init(self):
        s = self._make()
        self.assertTrue(all(lvl.status == "idle" for lvl in s.levels))

    def test_invalid_bounds_raises(self):
        with self.assertRaises(ValueError):
            self._make(lower=110000.0, upper=90000.0)

    def test_zero_lower_raises(self):
        with self.assertRaises(ValueError):
            self._make(lower=0.0, upper=110000.0)

    def test_single_level_raises(self):
        with self.assertRaises(ValueError):
            self._make(levels=1)

    def test_zero_size_raises(self):
        with self.assertRaises(ValueError):
            self._make(size=0.0)

    def test_strategy_type(self):
        s = self._make()
        self.assertEqual(s.STRATEGY_TYPE, "grid")

    def test_level_at_price_exact(self):
        s = self._make(lower=90000.0, upper=110000.0, levels=11)
        lvl = s.level_at_price(92000.0)
        self.assertIsNotNone(lvl)
        self.assertAlmostEqual(lvl.price, 92000.0, places=0)

    def test_level_at_price_miss(self):
        s = self._make(lower=90000.0, upper=110000.0, levels=11)
        self.assertIsNone(s.level_at_price(50000.0))

    def test_stop_loss_below(self):
        s = self._make(lower=90000.0, upper=110000.0)
        self.assertTrue(s._check_stop_loss(89999.0))

    def test_stop_loss_above(self):
        s = self._make(lower=90000.0, upper=110000.0)
        self.assertTrue(s._check_stop_loss(110001.0))

    def test_no_stop_loss_inside(self):
        s = self._make(lower=90000.0, upper=110000.0)
        self.assertFalse(s._check_stop_loss(100000.0))

    def test_grid_not_initialised_at_start(self):
        s = self._make()
        self.assertFalse(s.grid.initialised)

    def test_grid_not_halted_at_start(self):
        s = self._make()
        self.assertFalse(s.grid.halted)


# ─── Grid strategy async behaviour ────────────────────────────────────────────

import asyncio, unittest.mock  # pylint: disable=wrong-import-position,wrong-import-order,ungrouped-imports

class _FakeTokenState:
    """Minimal TokenState substitute for grid tests."""
    def __init__(self, best_bid=100000.0, best_ask=100050.0):
        self.best_bid = best_bid
        self.best_ask = best_ask

class _FakeConn:
    """Minimal sqlite3.Connection substitute — discards all writes."""
    def execute(self, *_a, **_kw):
        return self
    def fetchone(self):
        return None
    def fetchall(self):
        return []
    def commit(self):
        pass

class _FakeState:
    """Minimal BotState substitute."""
    def __init__(self):
        self.session = object()
        self.conn    = _FakeConn()

def _run(coro):
    return asyncio.run(coro)


class TestGridInitialise(unittest.TestCase):
    """_initialise_grid places BUY below ask and SELL above ask."""

    def _make_strategy(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 98000.0
        cfg.grid_upper           = 102000.0
        cfg.grid_levels          = 5       # 98k 99k 100k 101k 102k, step=1000
        cfg.grid_order_size_usdt = 50.0
        cfg.connector            = "binance"
        return GridStrategy(cfg)

    def test_buy_placed_below_ask(self):
        s = self._make_strategy()
        ts = _FakeTokenState(best_bid=99900.0, best_ask=100000.0)
        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(side_effect=lambda *a, **kw: f"sim_{kw.get('side','')}")
        ):
            _run(s._initialise_grid(_FakeState(), ts))
        buys  = [l for l in s.levels if l.status == "buy_placed"]
        sells = [l for l in s.levels if l.status == "sell_placed"]
        self.assertTrue(all(l.price < 100000.0 for l in buys))
        self.assertTrue(all(l.price > 100000.0 for l in sells))

    def test_initialised_flag_set(self):
        s = self._make_strategy()
        ts = _FakeTokenState(best_bid=99900.0, best_ask=100000.0)
        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="sim_abc")
        ):
            _run(s._initialise_grid(_FakeState(), ts))
        self.assertTrue(s.grid.initialised)

    def test_level_at_ask_price_is_idle(self):
        """Level exactly at ask price must be skipped."""
        s = self._make_strategy()
        ts = _FakeTokenState(best_ask=100000.0)   # 100k is level index 2
        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="sim_x")
        ):
            _run(s._initialise_grid(_FakeState(), ts))
        lvl_100k = s.level_at_price(100000.0)
        self.assertEqual(lvl_100k.status, "idle")


class TestGridFills(unittest.TestCase):
    """_on_buy_filled and _on_sell_filled place counter-orders and track PnL."""

    def _make_strategy(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 90000.0
        cfg.grid_upper           = 110000.0
        cfg.grid_levels          = 21      # step = 1000
        cfg.grid_order_size_usdt = 50.0
        cfg.connector            = "binance"
        return GridStrategy(cfg)

    def test_buy_filled_places_sell_above(self):
        s  = self._make_strategy()
        lvl = s.level_at_price(99000.0)
        lvl.buy_order_id = "sim_buy1"
        lvl.buy_price    = 99000.0
        lvl.status       = "buy_placed"

        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="sim_sell1")
        ):
            _run(s._on_buy_filled(_FakeState(), lvl))

        self.assertEqual(lvl.status, "sell_placed")
        self.assertAlmostEqual(lvl.sell_price, 100000.0)
        self.assertIsNone(lvl.buy_order_id)

    def test_sell_filled_places_buy_below_and_counts_cycle(self):
        s   = self._make_strategy()
        lvl = s.level_at_price(99000.0)
        # Simulate the level after a full BUY→SELL cycle
        lvl.buy_price     = 99000.0
        lvl.sell_order_id = "sim_sell1"
        lvl.sell_price    = 100000.0
        lvl.status        = "sell_placed"

        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="sim_buy2")
        ):
            _run(s._on_sell_filled(_FakeState(), lvl))

        self.assertEqual(s.grid.total_cycles, 1)
        self.assertGreater(s.grid.total_profit_usd, 0.0)
        self.assertEqual(lvl.status, "buy_placed")
        self.assertAlmostEqual(lvl.buy_price, 99000.0)

    def test_sell_filled_profit_positive(self):
        s   = self._make_strategy()
        lvl = s.level_at_price(99000.0)
        lvl.buy_price     = 99000.0
        lvl.sell_order_id = "sim_sell1"
        lvl.sell_price    = 100000.0
        lvl.status        = "sell_placed"

        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="sim_buy2")
        ):
            _run(s._on_sell_filled(_FakeState(), lvl))

        # grid_step=1000, qty=50/99000≈0.000505, fees≈0.10 USDT
        self.assertGreater(s.grid.total_profit_usd, 0.3)
        self.assertLess(s.grid.total_profit_usd, 1.0)

    def test_buy_at_top_of_grid_goes_idle(self):
        """BUY at top level: sell_price would exceed grid_upper → idle."""
        s   = self._make_strategy()
        lvl = s.level_at_price(110000.0)   # grid_upper level
        lvl.buy_order_id = "sim_top"
        lvl.buy_price    = 110000.0
        lvl.status       = "buy_placed"

        post_mock = unittest.mock.AsyncMock(return_value="sim_sell_top")
        with unittest.mock.patch.object(s._api, "post_order", new=post_mock):
            _run(s._on_buy_filled(_FakeState(), lvl))

        self.assertEqual(lvl.status, "idle")
        post_mock.assert_not_called()

    def test_sell_at_bottom_of_grid_goes_idle(self):
        """SELL at bottom level: buy_price would be below grid_lower → idle."""
        s   = self._make_strategy()
        lvl = s.level_at_price(90000.0)   # grid_lower level
        lvl.sell_order_id = "sim_bot"
        lvl.sell_price    = 90000.0
        lvl.status        = "sell_placed"

        post_mock = unittest.mock.AsyncMock(return_value="sim_buy_bot")
        with unittest.mock.patch.object(s._api, "post_order", new=post_mock):
            _run(s._on_sell_filled(_FakeState(), lvl))

        self.assertEqual(lvl.status, "idle")
        post_mock.assert_not_called()


class TestGridSimFillDetection(unittest.TestCase):
    """_poll_fills detects simulated fills via price-crossing logic."""

    def _make_strategy(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 90000.0
        cfg.grid_upper           = 110000.0
        cfg.grid_levels          = 21
        cfg.grid_order_size_usdt = 50.0
        cfg.connector            = "binance"
        s = GridStrategy(cfg)
        s.grid.initialised = True
        return s

    def test_sim_buy_fill_detected(self):
        s   = self._make_strategy()
        lvl = s.level_at_price(99000.0)
        lvl.buy_order_id = "sim_buy1"
        lvl.buy_price    = 99000.0
        lvl.status       = "buy_placed"

        ts = _FakeTokenState(best_bid=98950.0, best_ask=98980.0)  # ask <= buy_price

        counter_oids = []
        async def fake_post(*a, **kw):
            oid = f"sim_counter_{len(counter_oids)}"
            counter_oids.append(kw.get("side", "?"))
            return oid

        with unittest.mock.patch.object(s._api, "post_order", new=fake_post):
            _run(s._poll_fills(_FakeState(), ts))

        self.assertIn("SELL", counter_oids)
        self.assertEqual(lvl.status, "sell_placed")

    def test_sim_sell_fill_detected(self):
        s   = self._make_strategy()
        lvl = s.level_at_price(99000.0)
        lvl.buy_price     = 99000.0
        lvl.sell_order_id = "sim_sell1"
        lvl.sell_price    = 100000.0
        lvl.status        = "sell_placed"

        ts = _FakeTokenState(best_bid=100100.0, best_ask=100150.0)  # bid >= sell_price

        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="sim_buy2")
        ):
            _run(s._poll_fills(_FakeState(), ts))

        self.assertEqual(s.grid.total_cycles, 1)

    def test_no_fill_when_price_not_crossed(self):
        s   = self._make_strategy()
        lvl = s.level_at_price(99000.0)
        lvl.buy_order_id = "sim_buy1"
        lvl.buy_price    = 99000.0
        lvl.status       = "buy_placed"

        ts = _FakeTokenState(best_bid=100000.0, best_ask=100050.0)  # ask > buy_price

        post_mock = unittest.mock.AsyncMock()
        with unittest.mock.patch.object(s._api, "post_order", new=post_mock):
            _run(s._poll_fills(_FakeState(), ts))

        post_mock.assert_not_called()
        self.assertEqual(lvl.status, "buy_placed")


class TestGridStopLoss(unittest.TestCase):
    """Stop-loss cancels all orders and halts the grid."""

    def _make_strategy(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 90000.0
        cfg.grid_upper           = 110000.0
        cfg.grid_levels          = 5
        cfg.grid_order_size_usdt = 50.0
        cfg.connector            = "binance"
        s = GridStrategy(cfg)
        s.grid.initialised = True
        # Arm two levels with simulated orders
        s.levels[0].buy_order_id  = "sim_buy_a"
        s.levels[0].buy_price     = 90000.0
        s.levels[0].status        = "buy_placed"
        s.levels[4].sell_order_id = "sim_sell_a"
        s.levels[4].sell_price    = 110000.0
        s.levels[4].status        = "sell_placed"
        return s

    def test_cancel_all_called(self):
        s  = self._make_strategy()
        cancel_calls = []
        async def fake_cancel(session, symbol, oid, **_):
            cancel_calls.append(oid)
            return True
        with unittest.mock.patch.object(s._api, "cancel_order", new=fake_cancel):
            _run(s._cancel_all_orders(_FakeState()))
        self.assertIn("sim_buy_a",  cancel_calls)
        self.assertIn("sim_sell_a", cancel_calls)

    def test_halted_after_cancel(self):
        s = self._make_strategy()
        with unittest.mock.patch.object(
            s._api, "cancel_order",
            new=unittest.mock.AsyncMock(return_value=True)
        ):
            _run(s._cancel_all_orders(_FakeState()))
        self.assertTrue(s.grid.halted)

    def test_all_levels_idle_after_cancel(self):
        s = self._make_strategy()
        with unittest.mock.patch.object(
            s._api, "cancel_order",
            new=unittest.mock.AsyncMock(return_value=True)
        ):
            _run(s._cancel_all_orders(_FakeState()))
        self.assertTrue(all(l.status == "idle" for l in s.levels))

    def test_on_book_update_halted_is_noop(self):
        s = self._make_strategy()
        s.grid.halted = True
        post_mock = unittest.mock.AsyncMock()
        ts = _FakeTokenState(best_bid=89000.0)   # below lower bound
        with unittest.mock.patch.object(s._api, "post_order", new=post_mock):
            _run(s.on_book_update(_FakeState(), ts))
        post_mock.assert_not_called()

    def test_on_book_update_triggers_stop_loss(self):
        s  = self._make_strategy()
        ts = _FakeTokenState(best_bid=85000.0)  # below grid_lower=90000
        with unittest.mock.patch.object(
            s._api, "cancel_order",
            new=unittest.mock.AsyncMock(return_value=True)
        ):
            _run(s.on_book_update(_FakeState(), ts))
        self.assertTrue(s.grid.halted)


class TestConnectorSimMode(unittest.TestCase):
    """Connector functions behave correctly without real credentials."""

    def test_binance_cancel_sim_order_returns_true(self):
        import api_binance
        result = _run(api_binance.cancel_order(None, "BTCUSDT", "sim_abc123"))
        self.assertTrue(result)

    def test_mexc_cancel_sim_order_returns_true(self):
        import api_mexc
        result = _run(api_mexc.cancel_order(None, "BTCUSDT", "sim_xyz456"))
        self.assertTrue(result)

    def test_binance_get_open_orders_no_creds_returns_empty(self):
        import api_binance
        result = _run(api_binance.get_open_orders(None, "BTCUSDT"))
        self.assertEqual(result, [])

    def test_mexc_get_open_orders_no_creds_returns_empty(self):
        import api_mexc
        result = _run(api_mexc.get_open_orders(None, "BTCUSDT"))
        self.assertEqual(result, [])

    def test_binance_get_order_status_no_creds_returns_none(self):
        import api_binance
        result = _run(api_binance.get_order_status(None, "BTCUSDT", "sim_abc"))
        self.assertIsNone(result)

    def test_mexc_get_order_status_no_creds_returns_none(self):
        import api_mexc
        result = _run(api_mexc.get_order_status(None, "BTCUSDT", "sim_abc"))
        self.assertIsNone(result)


# ── BotConfig connector / strategy_type fields ────────────────────────────────

class TestBotConfigStrategyFields(unittest.TestCase):
    """connector and strategy_type are propagated through make_config."""

    def test_defaults(self):
        cfg = bot.BotConfig()
        self.assertEqual(cfg.connector, "polymarket")
        self.assertEqual(cfg.strategy_type, "threshold")

    def test_grid_defaults(self):
        cfg = bot.BotConfig()
        self.assertEqual(cfg.grid_symbol, "BTCUSDT")
        self.assertEqual(cfg.grid_levels, 10)
        self.assertAlmostEqual(cfg.grid_lower, 0.0)
        self.assertAlmostEqual(cfg.grid_upper, 0.0)

    def test_state_strategy_is_none_by_default(self):
        conn = make_db()
        state = bot.BotState(conn)
        self.assertIsNone(state.strategy)

    def test_state_strategy_can_be_set(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 90000.0
        cfg.grid_upper           = 110000.0
        cfg.grid_levels          = 5
        cfg.grid_order_size_usdt = 50.0
        conn = make_db()
        state = bot.BotState(conn, cfg)
        state.strategy = GridStrategy(cfg)
        self.assertIsNotNone(state.strategy)
        self.assertEqual(state.strategy.STRATEGY_TYPE, "grid")


class TestUserDataStream(unittest.TestCase):
    """parse_user_stream_msg and make_user_stream_url for Binance and MEXC."""

    # ── Binance parse_user_stream_msg ─────────────────────────────────────────

    def test_binance_filled_buy(self):
        import api_binance
        msg = {"e": "executionReport", "s": "BTCUSDT",
               "i": 99999, "X": "FILLED", "S": "BUY"}
        result = api_binance.parse_user_stream_msg(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], "99999")
        self.assertEqual(result["status"],   "FILLED")
        self.assertEqual(result["side"],     "BUY")
        self.assertEqual(result["symbol"],   "BTCUSDT")

    def test_binance_partially_filled(self):
        import api_binance
        msg = {"e": "executionReport", "s": "BTCUSDT",
               "i": 88888, "X": "PARTIALLY_FILLED", "S": "SELL"}
        result = api_binance.parse_user_stream_msg(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "PARTIALLY_FILLED")

    def test_binance_new_order_ignored(self):
        import api_binance
        msg = {"e": "executionReport", "s": "BTCUSDT",
               "i": 11111, "X": "NEW", "S": "BUY"}
        self.assertIsNone(api_binance.parse_user_stream_msg(msg))

    def test_binance_wrong_event_type_ignored(self):
        import api_binance
        self.assertIsNone(api_binance.parse_user_stream_msg(
            {"e": "outboundAccountPosition"}))

    def test_binance_make_user_stream_url(self):
        import api_binance
        url = api_binance.make_user_stream_url("abc123")
        self.assertIn("abc123", url)
        self.assertTrue(url.startswith("wss://"))

    # ── MEXC parse_user_stream_msg ────────────────────────────────────────────

    def test_mexc_filled_buy(self):
        import api_mexc
        msg = {"s": "BTCUSDT", "d": {"i": "MX_001", "s": 2, "S": 1}}
        result = api_mexc.parse_user_stream_msg(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["order_id"], "MX_001")
        self.assertEqual(result["status"],   "FILLED")
        self.assertEqual(result["side"],     "BUY")

    def test_mexc_filled_sell(self):
        import api_mexc
        msg = {"s": "BTCUSDT", "d": {"i": "MX_002", "s": 2, "S": 2}}
        result = api_mexc.parse_user_stream_msg(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["side"], "SELL")

    def test_mexc_partially_filled(self):
        import api_mexc
        msg = {"s": "BTCUSDT", "d": {"i": "MX_003", "s": 3, "S": 1}}
        result = api_mexc.parse_user_stream_msg(msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "PARTIALLY_FILLED")

    def test_mexc_new_status_ignored(self):
        import api_mexc
        msg = {"s": "BTCUSDT", "d": {"i": "MX_004", "s": 1, "S": 1}}
        self.assertIsNone(api_mexc.parse_user_stream_msg(msg))

    def test_mexc_canceled_ignored(self):
        import api_mexc
        msg = {"s": "BTCUSDT", "d": {"i": "MX_005", "s": 4, "S": 1}}
        self.assertIsNone(api_mexc.parse_user_stream_msg(msg))

    def test_mexc_no_d_key_ignored(self):
        import api_mexc
        self.assertIsNone(api_mexc.parse_user_stream_msg({"s": "BTCUSDT"}))

    # ── api_binance public endpoint selection ─────────────────────────────────

    def test_binance_no_creds_ws_url_is_public(self):
        """WS_URL uses data-stream.binance.vision when no credentials are set."""
        import importlib
        import api_binance
        # Reload without credentials to confirm public path
        with unittest.mock.patch.dict(os.environ,
                                      {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""},
                                      clear=False):
            importlib.reload(api_binance)
        self.assertIn("data-stream.binance.vision", api_binance.WS_URL)
        self.assertTrue(api_binance.WS_URL.startswith("wss://"))

    def test_binance_no_creds_base_url_is_public(self):
        """BASE_URL uses data-api.binance.vision when no credentials are set."""
        import importlib
        import api_binance
        with unittest.mock.patch.dict(os.environ,
                                      {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""},
                                      clear=False):
            importlib.reload(api_binance)
        self.assertIn("data-api.binance.vision", api_binance.BASE_URL)

    def test_binance_with_creds_ws_url_is_live(self):
        """WS_URL uses stream.binance.com when credentials are present."""
        import importlib
        import api_binance
        with unittest.mock.patch.dict(os.environ,
                                      {"BINANCE_API_KEY": "key", "BINANCE_API_SECRET": "secret"},
                                      clear=False):
            importlib.reload(api_binance)
        self.assertIn("stream.binance.com", api_binance.WS_URL)
        # Restore no-creds state for subsequent tests
        with unittest.mock.patch.dict(os.environ,
                                      {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""},
                                      clear=False):
            importlib.reload(api_binance)

    def test_binance_with_creds_base_url_is_live(self):
        """BASE_URL uses api.binance.com when credentials are present."""
        import importlib
        import api_binance
        with unittest.mock.patch.dict(os.environ,
                                      {"BINANCE_API_KEY": "key", "BINANCE_API_SECRET": "secret"},
                                      clear=False):
            importlib.reload(api_binance)
        self.assertIn("api.binance.com", api_binance.BASE_URL)
        with unittest.mock.patch.dict(os.environ,
                                      {"BINANCE_API_KEY": "", "BINANCE_API_SECRET": ""},
                                      clear=False):
            importlib.reload(api_binance)

    def test_mexc_make_user_stream_url(self):
        import api_mexc
        url = api_mexc.make_user_stream_url("key_xyz")
        self.assertIn("key_xyz", url)
        self.assertTrue(url.startswith("wss://"))

    # ── _on_user_stream_fill dispatch ─────────────────────────────────────────

    def test_user_stream_fill_dispatches_buy(self):
        """_on_user_stream_fill calls _on_buy_filled for a matching BUY order."""
        cfg = bot.BotConfig()
        cfg.grid_symbol = "BTCUSDT"; cfg.grid_lower = 98000.0
        cfg.grid_upper = 102000.0; cfg.grid_levels = 5
        cfg.grid_order_size_usdt = 50.0; cfg.connector = "binance"
        s   = GridStrategy(cfg)
        lvl = s.grid.levels[0]
        lvl.buy_order_id = "ORD_BUY_1"
        lvl.buy_price    = 98000.0
        lvl.status       = "buy_placed"

        state = _FakeState()
        fill  = {"order_id": "ORD_BUY_1", "status": "FILLED",
                 "side": "BUY", "symbol": "BTCUSDT"}
        with unittest.mock.patch.object(
            s._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="ORD_SELL_2")
        ):
            _run(s._on_user_stream_fill(state, fill))
        self.assertEqual(lvl.status, "sell_placed")
        self.assertEqual(lvl.sell_order_id, "ORD_SELL_2")

    def test_user_stream_fill_unknown_order_is_noop(self):
        """Unknown order_id is silently ignored — no exception, no state change."""
        cfg = bot.BotConfig()
        cfg.grid_symbol = "BTCUSDT"; cfg.grid_lower = 98000.0
        cfg.grid_upper = 102000.0; cfg.grid_levels = 5
        cfg.grid_order_size_usdt = 50.0; cfg.connector = "binance"
        s = GridStrategy(cfg)
        state = _FakeState()
        fill  = {"order_id": "UNKNOWN_999", "status": "FILLED",
                 "side": "BUY", "symbol": "BTCUSDT"}
        _run(s._on_user_stream_fill(state, fill))  # must not raise
        self.assertTrue(all(l.status == "idle" for l in s.levels))


class TestGridPersistence(unittest.TestCase):
    """_save_state writes to DB; restore_from_db reloads it."""

    def _make_strategy(self):
        cfg = bot.BotConfig()
        cfg.grid_symbol          = "BTCUSDT"
        cfg.grid_lower           = 98000.0
        cfg.grid_upper           = 102000.0
        cfg.grid_levels          = 5
        cfg.grid_order_size_usdt = 50.0
        cfg.connector            = "binance"
        return GridStrategy(cfg)

    def _fake_state_with_db(self):
        conn = make_db()
        fs = _FakeState()
        fs.conn = conn
        return fs

    # ── _save_state ────────────────────────────────────────────────────────────

    def test_save_state_writes_grid_state_row(self):
        s     = self._make_strategy()
        state = self._fake_state_with_db()
        s.grid.initialised      = True
        s.grid.total_cycles     = 3
        s.grid.total_profit_usd = 1.23
        s._save_state(state.conn)
        row = state.conn.execute(
            "SELECT total_cycles, total_profit_usd, initialised FROM grid_state"
            " WHERE symbol='BTCUSDT'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 3)
        self.assertAlmostEqual(row[1], 1.23, places=4)
        self.assertEqual(row[2], 1)

    def test_save_state_writes_all_levels(self):
        s     = self._make_strategy()
        state = self._fake_state_with_db()
        s.grid.levels[0].buy_order_id = "ord_BUY_1"
        s.grid.levels[0].buy_price    = 98000.0
        s.grid.levels[0].status       = "buy_placed"
        s._save_state(state.conn)
        count = state.conn.execute(
            "SELECT COUNT(*) FROM grid_levels WHERE symbol='BTCUSDT'"
        ).fetchone()[0]
        self.assertEqual(count, 5)
        row = state.conn.execute(
            "SELECT buy_order_id, status FROM grid_levels"
            " WHERE symbol='BTCUSDT' AND level_price=98000.0"
        ).fetchone()
        self.assertEqual(row[0], "ord_BUY_1")
        self.assertEqual(row[1], "buy_placed")

    def test_save_state_upserts_on_second_call(self):
        s     = self._make_strategy()
        state = self._fake_state_with_db()
        s._save_state(state.conn)
        s.grid.total_cycles = 7
        s._save_state(state.conn)
        row = state.conn.execute(
            "SELECT total_cycles FROM grid_state WHERE symbol='BTCUSDT'"
        ).fetchone()
        self.assertEqual(row[0], 7)

    # ── restore_from_db — no saved state ───────────────────────────────────────

    def test_restore_returns_false_when_no_saved_state(self):
        s     = self._make_strategy()
        state = self._fake_state_with_db()
        result = _run(s.restore_from_db(state))
        self.assertFalse(result)

    # ── restore_from_db — config mismatch ─────────────────────────────────────

    def test_restore_returns_false_on_config_mismatch(self):
        s     = self._make_strategy()
        state = self._fake_state_with_db()
        s._save_state(state.conn)
        # Create a new strategy with different bounds
        cfg2 = bot.BotConfig()
        cfg2.grid_symbol          = "BTCUSDT"
        cfg2.grid_lower           = 95000.0   # changed
        cfg2.grid_upper           = 105000.0  # changed
        cfg2.grid_levels          = 5
        cfg2.grid_order_size_usdt = 50.0
        cfg2.connector            = "binance"
        s2 = GridStrategy(cfg2)
        result = _run(s2.restore_from_db(state))
        self.assertFalse(result)

    # ── restore_from_db — halted state ─────────────────────────────────────────

    def test_restore_halted_state_no_reconciliation(self):
        s     = self._make_strategy()
        state = self._fake_state_with_db()
        s.grid.initialised = True
        s.grid.halted      = True
        s._save_state(state.conn)

        s2 = self._make_strategy()
        with unittest.mock.patch.object(
            s2._api, "get_open_orders",
            new=unittest.mock.AsyncMock(return_value=[])
        ) as mock_oo:
            result = _run(s2.restore_from_db(state))
        self.assertTrue(result)
        self.assertTrue(s2.grid.halted)
        mock_oo.assert_not_called()   # no reconciliation for halted grids

    # ── restore_from_db — active state with offline fill ──────────────────────

    def test_restore_detects_offline_fill(self):
        """A BUY order that filled while the bot was down triggers _on_buy_filled."""
        s     = self._make_strategy()
        state = self._fake_state_with_db()

        # Simulate: grid is initialised, level 98000 has a BUY order
        s.grid.initialised = True
        lvl = s.grid.levels[0]   # price=98000
        lvl.buy_order_id = "ord_123"
        lvl.buy_price    = 98000.0
        lvl.status       = "buy_placed"
        s._save_state(state.conn)

        s2 = self._make_strategy()
        # open_orders returns empty list → ord_123 filled while bot was down
        with unittest.mock.patch.object(
            s2._api, "get_open_orders",
            new=unittest.mock.AsyncMock(return_value=[])
        ), unittest.mock.patch.object(
            s2._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="ord_SELL_new")
        ):
            result = _run(s2.restore_from_db(state))

        self.assertTrue(result)
        restored_lvl = s2.grid.levels[0]
        # After _on_buy_filled: a SELL counter-order should have been placed
        self.assertEqual(restored_lvl.status, "sell_placed")
        self.assertEqual(restored_lvl.sell_order_id, "ord_SELL_new")

    def test_restore_skips_fill_when_order_still_open(self):
        """An order still in open_orders is NOT treated as filled."""
        s     = self._make_strategy()
        state = self._fake_state_with_db()

        s.grid.initialised = True
        lvl = s.grid.levels[0]
        lvl.buy_order_id = "ord_123"
        lvl.buy_price    = 98000.0
        lvl.status       = "buy_placed"
        s._save_state(state.conn)

        s2 = self._make_strategy()
        open_order = {"order_id": "ord_123", "side": "BUY",
                      "price": 98000.0, "qty": 0.0005, "status": "NEW"}
        with unittest.mock.patch.object(
            s2._api, "get_open_orders",
            new=unittest.mock.AsyncMock(return_value=[open_order])
        ), unittest.mock.patch.object(
            s2._api, "post_order",
            new=unittest.mock.AsyncMock(return_value="ord_SELL_new")
        ) as mock_post:
            result = _run(s2.restore_from_db(state))

        self.assertTrue(result)
        mock_post.assert_not_called()
        restored_lvl = s2.grid.levels[0]
        self.assertEqual(restored_lvl.status, "buy_placed")


# ── _SessionFilter ────────────────────────────────────────────────────────────

class TestSessionFilter(unittest.TestCase):
    """_SessionFilter injects the session ID into every log record."""

    def test_filter_returns_true(self):
        sf = bot._SessionFilter()
        record = logging.LogRecord("live", logging.INFO, "", 0, "msg", (), None)
        self.assertTrue(sf.filter(record))

    def test_filter_attaches_session_attribute(self):
        sf = bot._SessionFilter()
        record = logging.LogRecord("live", logging.INFO, "", 0, "msg", (), None)
        sf.filter(record)
        self.assertTrue(hasattr(record, "session"))

    def test_session_matches_module_constant(self):
        sf = bot._SessionFilter()
        record = logging.LogRecord("live", logging.INFO, "", 0, "msg", (), None)
        sf.filter(record)
        self.assertEqual(record.session, bot._SESSION_ID)

    def test_session_id_is_eight_hex_chars(self):
        self.assertEqual(len(bot._SESSION_ID), 8)
        self.assertTrue(all(c in "0123456789ABCDEF" for c in bot._SESSION_ID))


# ── RejectionStats ────────────────────────────────────────────────────────────

class TestRejectionStats(unittest.TestCase):
    """RejectionStats starts at zero and each field increments independently."""

    def test_all_fields_start_at_zero(self):
        r = bot.RejectionStats()
        for field in ("signalled", "market_ended", "trading_hour", "best_bid",
                      "entry_max", "best_ask", "ask_vol", "secs_remaining",
                      "obi", "vol_filter", "capital", "daily_stop", "api_cooldown"):
            self.assertEqual(getattr(r, field), 0, field)

    def test_fields_are_independent(self):
        r = bot.RejectionStats()
        r.signalled += 1
        self.assertEqual(r.signalled, 1)
        self.assertEqual(r.best_bid, 0)

    def test_botstate_initialises_rejection_stats(self):
        state = make_state()
        self.addCleanup(state.conn.close)
        self.assertIsInstance(state.rejection_stats, bot.RejectionStats)

    def test_botstate_has_last_stats_log_timestamp(self):
        state = make_state()
        self.addCleanup(state.conn.close)
        self.assertIsInstance(state._last_stats_log, float)
        self.assertGreater(state._last_stats_log, 0)


class TestRejectionCounters(unittest.IsolatedAsyncioTestCase):
    """check_signal() increments the right counter for each rejection reason."""

    def setUp(self):
        self.state = make_state()
        # Freeze the periodic-log timer so it never fires during these tests.
        self.state._last_stats_log = time.time() + 10_000

    def tearDown(self):
        self.state.conn.close()

    async def test_counter_signalled(self):
        self.state.signalled.add("mkt1")
        await bot.check_signal(self.state, make_token())
        self.assertEqual(self.state.rejection_stats.signalled, 1)

    async def test_counter_market_ended(self):
        ts = make_token()
        ts.market_end_ms = (time.time() - 10) * 1000
        await bot.check_signal(self.state, ts)
        self.assertEqual(self.state.rejection_stats.market_ended, 1)

    async def test_counter_trading_hour(self):
        with patch.object(bot, "is_trading_hour", return_value=False):
            await bot.check_signal(self.state, make_token())
        self.assertEqual(self.state.rejection_stats.trading_hour, 1)

    async def test_counter_best_bid(self):
        await bot.check_signal(self.state, make_token(best_bid=0.50))
        self.assertEqual(self.state.rejection_stats.best_bid, 1)

    async def test_counter_best_ask(self):
        await bot.check_signal(self.state, make_token(best_ask=1.0))
        self.assertEqual(self.state.rejection_stats.best_ask, 1)

    async def test_counter_ask_vol(self):
        await bot.check_signal(self.state, make_token(ask_vol=1.0))
        self.assertEqual(self.state.rejection_stats.ask_vol, 1)

    async def test_counter_secs_remaining(self):
        await bot.check_signal(self.state, make_token(secs_remaining=5))
        self.assertEqual(self.state.rejection_stats.secs_remaining, 1)

    async def test_counter_obi(self):
        await bot.check_signal(self.state, make_token(obi=-0.9))
        self.assertEqual(self.state.rejection_stats.obi, 1)

    async def test_counter_capital(self):
        self.state.capital = 0.0
        await bot.check_signal(self.state, make_token())
        self.assertEqual(self.state.rejection_stats.capital, 1)

    async def test_counter_daily_stop(self):
        self.state.daily_pnl = -99.0
        self.state._daily_pnl_day = int(time.time() // 86400)
        await bot.check_signal(self.state, make_token())
        self.assertEqual(self.state.rejection_stats.daily_stop, 1)

    async def test_counter_api_cooldown(self):
        self.state.api_cooldown_until = time.time() + 300
        await bot.check_signal(self.state, make_token())
        self.assertEqual(self.state.rejection_stats.api_cooldown, 1)

    async def test_no_counters_incremented_on_fire(self):
        await bot.check_signal(self.state, make_token())
        r = self.state.rejection_stats
        total = sum(getattr(r, f) for f in vars(r))
        self.assertEqual(total, 0)

    async def test_periodic_log_resets_counters(self):
        # Back-date the timer so the periodic log fires on the next call.
        self.state._last_stats_log = time.time() - 61
        self.state.signalled.add("mkt1")
        await bot.check_signal(self.state, make_token())
        # After the reset the counter for this call should still be 1,
        # but the object has been replaced with a fresh RejectionStats.
        self.assertIsInstance(self.state.rejection_stats, bot.RejectionStats)
        # The timer should have been reset to now.
        self.assertAlmostEqual(self.state._last_stats_log, time.time(), delta=2)


# ── LATENCY ts_ms ─────────────────────────────────────────────────────────────

class TestLatencyLog(unittest.IsolatedAsyncioTestCase):
    """ts_ms must appear in the [LATENCY] log line."""

    async def test_latency_log_includes_ts_ms(self):
        state = make_state()
        self.addCleanup(state.conn.close)
        ts = make_token()

        logged: list[str] = []
        original_info = bot.logger.info

        def capture_info(msg, *args, **kwargs):
            logged.append(msg % args if args else msg)
            original_info(msg, *args, **kwargs)

        with patch.object(bot, "logger") as mock_logger:
            mock_logger.info.side_effect = capture_info
            mock_logger.warning = bot.logger.warning
            t_ws = time.monotonic()
            await bot.enter_live_trade(state, ts, _t_ws=t_ws)

        latency_lines = [m for m in logged if "[LATENCY]" in m]
        self.assertTrue(latency_lines, "No [LATENCY] line was logged")
        self.assertIn("ts_ms=", latency_lines[0])


# ── _PlainFmt / _ColorFmt ─────────────────────────────────────────────────────

class TestLogFormatters(unittest.TestCase):
    """Format helpers produce abbreviated level names and correct ANSI codes."""

    def _make_record(self, level=logging.INFO, msg="hello"):
        return logging.LogRecord("live", level, "", 0, msg, (), None)

    def test_plain_fmt_abbreviates_info(self):
        fmt = bot._PlainFmt(bot._LOG_FMT, datefmt=bot._LOG_DATE)
        # Inject session so the format string resolves.
        r = self._make_record()
        r.session = "TESTSESS"  # type: ignore[attr-defined]
        out = fmt.format(r)
        self.assertIn("INFO ", out)
        self.assertNotIn("INFO\n", out)

    def test_plain_fmt_abbreviates_warning(self):
        fmt = bot._PlainFmt(bot._LOG_FMT, datefmt=bot._LOG_DATE)
        r = self._make_record(logging.WARNING)
        r.session = "TESTSESS"  # type: ignore[attr-defined]
        out = fmt.format(r)
        self.assertIn("WARN ", out)

    def test_color_fmt_adds_ansi_on_warning(self):
        fmt = bot._ColorFmt(bot._LOG_FMT, datefmt=bot._LOG_DATE)
        r = self._make_record(logging.WARNING)
        r.session = "TESTSESS"  # type: ignore[attr-defined]
        out = fmt.format(r)
        self.assertIn("\033[", out)

    def test_color_fmt_no_ansi_on_info(self):
        fmt = bot._ColorFmt(bot._LOG_FMT, datefmt=bot._LOG_DATE)
        r = self._make_record(logging.INFO)
        r.session = "TESTSESS"  # type: ignore[attr-defined]
        out = fmt.format(r)
        self.assertNotIn("\033[", out)

    def test_log_fmt_contains_session_placeholder(self):
        self.assertIn("%(session)s", bot._LOG_FMT)


class TestComputeStake(unittest.TestCase):
    """Unit tests for compute_stake() — bid×secs dynamic sizing."""

    def _cfg(self, **kw):
        defaults = {
            "signal_threshold": 0.95,
            "stake": 10.0,
            "stake_bid_alpha": 0.0,
            "stake_secs_ref": 45.0,
            "stake_secs_alpha": 0.0,
            "stake_max": 15.0,
            "stake_max_pct_capital": 0.0,
        }
        defaults.update(kw)
        return bot.BotConfig(**defaults)

    # ── Flat mode (both alphas == 0) ──────────────────────────────────────────

    def test_flat_mode_returns_base_stake(self):
        cfg = self._cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 100.0), 10.0)

    def test_flat_mode_ignores_bid(self):
        cfg = self._cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.96, 100.0),
                         bot.compute_stake(cfg, 0.999, 100.0))

    def test_flat_mode_with_capital_cap(self):
        cfg = self._cfg(stake_max_pct_capital=0.12, capital_start=100.0)
        # 12% of 100 = 12, but stake=10 < 12 → returns stake
        self.assertEqual(bot.compute_stake(cfg, 0.97, 100.0, capital=100.0), 10.0)

    def test_flat_mode_capital_cap_shrinks_on_drawdown(self):
        cfg = self._cfg(stake_max_pct_capital=0.12, capital_start=100.0)
        # capital=50 → cap = 0.12*50 = 6.0 < stake=10 → returns 6.0
        self.assertAlmostEqual(bot.compute_stake(cfg, 0.97, 100.0, capital=50.0), 6.0)

    # ── Bid scaling ───────────────────────────────────────────────────────────

    def test_bid_alpha_boosts_above_threshold(self):
        cfg = self._cfg(stake_bid_alpha=2.0)
        # bid=0.97, threshold=0.95 → bid_score=0.4, boost=1.8 → 18, capped at 15
        result = bot.compute_stake(cfg, 0.97, 100.0)
        self.assertGreater(result, 10.0)
        self.assertLessEqual(result, 15.0)

    def test_bid_at_threshold_gives_base_stake(self):
        cfg = self._cfg(stake_bid_alpha=2.0)
        # bid_score=0 → boost=1.0 → stake=10, but floor=3 → 10
        self.assertAlmostEqual(bot.compute_stake(cfg, 0.95, 100.0), 10.0)

    def test_bid_at_maximum_capped_by_stake_max(self):
        cfg = self._cfg(stake_bid_alpha=2.0, stake_max=15.0)
        # bid=1.0 → bid_score=1.0, boost=3.0 → 30, capped at 15
        self.assertAlmostEqual(bot.compute_stake(cfg, 1.0, 100.0), 15.0)

    def test_stake_max_pct_caps_bid_boost(self):
        cfg = self._cfg(stake_bid_alpha=2.0, stake_max=15.0,
                        stake_max_pct_capital=0.12)
        # capital=80 → eff_max = min(15, 0.12*80) = min(15, 9.6) = 9.6
        result = bot.compute_stake(cfg, 1.0, 100.0, capital=80.0)
        self.assertAlmostEqual(result, 9.6, places=5)

    # ── Secs scaling ──────────────────────────────────────────────────────────

    def test_secs_below_ref_no_penalty(self):
        cfg = self._cfg(stake_bid_alpha=2.0, stake_secs_alpha=1.0,
                        stake_secs_ref=45.0)
        r_at_ref   = bot.compute_stake(cfg, 0.97, 45.0)
        r_below_ref = bot.compute_stake(cfg, 0.97, 20.0)
        self.assertEqual(r_at_ref, r_below_ref)

    def test_secs_above_ref_reduces_stake(self):
        cfg = self._cfg(stake_bid_alpha=2.0, stake_secs_alpha=1.0,
                        stake_secs_ref=45.0)
        r_at_ref  = bot.compute_stake(cfg, 0.97, 45.0)
        r_far_out = bot.compute_stake(cfg, 0.97, 200.0)
        self.assertGreater(r_at_ref, r_far_out)

    def test_secs_factor_floored_at_min(self):
        cfg = self._cfg(stake_bid_alpha=1.0, stake_secs_alpha=10.0,
                        stake_secs_ref=45.0)
        # With extreme secs, floor = base * 0.3 = 3.0
        result = bot.compute_stake(cfg, 0.95, 10000.0)
        self.assertGreaterEqual(result, 10.0 * bot._STAKE_SECS_MIN_FACTOR)

    # ── Kelly mode ────────────────────────────────────────────────────────────

    def _kelly_cfg(self, **kw):
        defaults = {
            "signal_threshold": 0.95,
            "stake": 10.0,
            "stake_bid_alpha": 0.0,
            "stake_secs_ref": 45.0,
            "stake_secs_alpha": 0.0,
            "stake_max": 50.0,
            "stake_max_pct_capital": 0.0,
            "kelly_fraction": 0.25,
            "kelly_min_trades": 30,
        }
        defaults.update(kw)
        return bot.BotConfig(**defaults)

    def test_kelly_disabled_by_default(self):
        cfg = self._cfg()   # kelly_fraction not set → 0.0
        self.assertEqual(cfg.kelly_fraction, 0.0)
        result = bot.compute_stake(cfg, 0.97, 60.0, capital=1000.0,
                                   win_rate=0.98, n_trades=100, ask=0.97)
        self.assertEqual(result, 10.0)  # flat mode unchanged

    def test_kelly_below_bootstrap_uses_flat(self):
        cfg = self._kelly_cfg(kelly_min_trades=30)
        # n_trades=10 < 30 → falls through to flat mode
        result = bot.compute_stake(cfg, 0.97, 60.0, capital=1000.0,
                                   win_rate=0.98, n_trades=10, ask=0.97)
        self.assertEqual(result, 10.0)

    def test_kelly_positive_edge_scales_with_capital(self):
        cfg = self._kelly_cfg(kelly_fraction=0.25, kelly_min_trades=30, stake_max=9999.0)
        # ask=0.96, WR=98%: f* ≈ 0.490; quarter-Kelly on $1000 ≈ $122
        ask = 0.96
        b_net = (1.0 / ask - 1.0) - bot.api.FEE_RATE * min(ask, 1.0 - ask) / ask
        f_star = (0.98 * b_net - 0.02) / b_net
        expected = 0.25 * f_star * 1000.0
        result = bot.compute_stake(cfg, 0.96, 60.0, capital=1000.0,
                                   win_rate=0.98, n_trades=50, ask=ask)
        self.assertAlmostEqual(result, expected, places=5)
        self.assertGreater(result, 0.0)

    def test_kelly_no_edge_returns_zero(self):
        cfg = self._kelly_cfg()
        # WR=50%, ask=0.99 → f* is deeply negative → returns 0
        result = bot.compute_stake(cfg, 0.96, 60.0, capital=1000.0,
                                   win_rate=0.50, n_trades=50, ask=0.99)
        self.assertEqual(result, 0.0)

    def test_kelly_capped_at_stake_max(self):
        cfg = self._kelly_cfg(kelly_fraction=1.0, stake_max=15.0)
        # WR=100% → f*=1.0, full Kelly at $1000 → $1000, capped at $15
        result = bot.compute_stake(cfg, 0.96, 60.0, capital=1000.0,
                                   win_rate=1.0, n_trades=50, ask=0.96)
        self.assertAlmostEqual(result, 15.0, places=5)

    def test_kelly_stake_proportional_to_capital(self):
        cfg = self._kelly_cfg(kelly_fraction=0.25, stake_max=9999.0)
        r1 = bot.compute_stake(cfg, 0.96, 60.0, capital=500.0,
                               win_rate=0.98, n_trades=50, ask=0.96)
        r2 = bot.compute_stake(cfg, 0.96, 60.0, capital=1000.0,
                               win_rate=0.98, n_trades=50, ask=0.96)
        self.assertAlmostEqual(r2, 2 * r1, places=5)

    def test_kelly_no_capital_returns_flat(self):
        cfg = self._kelly_cfg()
        # capital=0 → Kelly guard fails → flat path
        result = bot.compute_stake(cfg, 0.96, 60.0, capital=0.0,
                                   win_rate=0.98, n_trades=50, ask=0.96)
        self.assertEqual(result, 10.0)

    def test_kelly_no_ask_returns_flat(self):
        cfg = self._kelly_cfg()
        # ask=0 → Kelly guard fails → flat path
        result = bot.compute_stake(cfg, 0.96, 60.0, capital=1000.0,
                                   win_rate=0.98, n_trades=50, ask=0.0)
        self.assertEqual(result, 10.0)

    # ── Step-function (Curve B) tests ─────────────────────────────────────────

    def _step_cfg(self, s0=15.0, s1=12.0, s2=6.0, s3=6.0):
        return self._cfg(stake_step_enabled=True,
                         stake_step_s0=s0, stake_step_s1=s1,
                         stake_step_s2=s2, stake_step_s3=s3)

    def test_step_disabled_uses_flat(self):
        cfg = self._cfg(stake_step_enabled=False)
        self.assertEqual(bot.compute_stake(cfg, 0.97, 30.0), 10.0)

    def test_step_below_45_returns_s0(self):
        cfg = self._step_cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 30.0), 15.0)

    def test_step_at_45_returns_s1(self):
        # secs=45 ≥ first break → s1 zone
        cfg = self._step_cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 45.0), 12.0)

    def test_step_between_45_and_60_returns_s1(self):
        cfg = self._step_cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 52.0), 12.0)

    def test_step_at_60_returns_s2(self):
        cfg = self._step_cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 60.0), 6.0)

    def test_step_at_90_returns_s3(self):
        cfg = self._step_cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 90.0), 6.0)

    def test_step_above_90_returns_s3(self):
        cfg = self._step_cfg()
        self.assertEqual(bot.compute_stake(cfg, 0.97, 120.0), 6.0)

    def test_step_takes_priority_over_bidsecs(self):
        # bid×secs alpha is set, but step_enabled=True → step wins
        cfg = self._step_cfg()
        cfg = self._cfg(stake_step_enabled=True, stake_step_s0=15.0,
                        stake_step_s1=12.0, stake_step_s2=6.0, stake_step_s3=6.0,
                        stake_bid_alpha=2.0, stake_secs_alpha=1.0)
        self.assertEqual(bot.compute_stake(cfg, 0.97, 30.0), 15.0)

    def test_step_kelly_takes_priority_over_step(self):
        # Kelly enabled and bootstrapped → Kelly wins over step
        cfg = self._cfg(
            kelly_fraction=0.25, kelly_min_trades=10,
            stake_step_enabled=True, stake_step_s0=15.0,
            stake_step_s1=12.0, stake_step_s2=6.0, stake_step_s3=6.0,
            stake_max=9999.0,
        )
        result = bot.compute_stake(cfg, 0.97, 30.0, capital=1000.0,
                                   win_rate=0.98, n_trades=20, ask=0.97)
        # Kelly result is not 15.0 (the step s0 value)
        self.assertNotEqual(result, 15.0)
        self.assertGreater(result, 0.0)


class TestWeeklyStopLoss(unittest.IsolatedAsyncioTestCase):
    """check_signal() respects the weekly_stop_loss guard."""

    def _make_state(self, cfg):
        state = make_state()
        state.config = cfg
        return state

    async def test_weekly_stop_blocks_entry(self):
        cfg = bot.BotConfig(
            signal_threshold=0.95, weekly_stop_loss=60.0,
            daily_stop_loss=9999.0,
        )
        state = self._make_state(cfg)
        state.weekly_pnl = -61.0
        state._weekly_pnl_week = int(time.time() // (7 * 86400))

        ts = make_token(best_bid=0.97, best_ask=0.975, secs_remaining=120)
        before = state.total_trades
        await bot.check_signal(state, ts)
        self.assertEqual(state.total_trades, before)
        self.assertGreater(state.rejection_stats.weekly_stop, 0)

    async def test_weekly_stop_allows_entry_when_not_triggered(self):
        cfg = bot.BotConfig(
            signal_threshold=0.95, weekly_stop_loss=60.0,
            daily_stop_loss=9999.0, capital_start=1000.0,
        )
        state = self._make_state(cfg)
        state.weekly_pnl = -10.0
        state._weekly_pnl_week = int(time.time() // (7 * 86400))
        state.capital = 1000.0

        ts = make_token(best_bid=0.97, best_ask=0.975, secs_remaining=120)
        before = state.rejection_stats.weekly_stop
        await bot.check_signal(state, ts)
        self.assertEqual(state.rejection_stats.weekly_stop, before)

    async def test_weekly_pnl_resets_on_new_period(self):
        cfg = bot.BotConfig(weekly_stop_loss=60.0)
        state = self._make_state(cfg)
        state.weekly_pnl = -999.0
        state._weekly_pnl_week = -1         # force reset on next check_signal call

        ts = make_token(best_bid=0.50, best_ask=0.51, secs_remaining=120)
        await bot.check_signal(state, ts)
        self.assertEqual(state.weekly_pnl, 0.0)
        self.assertNotEqual(state._weekly_pnl_week, -1)


class TestMarketDiscoveryConfig(unittest.TestCase):
    """market_tag_id and market_window_mins are configurable via BotConfig."""

    def test_defaults(self):
        cfg = bot.BotConfig()
        self.assertEqual(cfg.market_tag_id, bot.MARKET_TAG_ID)
        self.assertEqual(cfg.market_window_mins, bot.MARKET_WINDOW_MINS)

    def test_override_via_botconfig(self):
        cfg = bot.BotConfig(market_tag_id=102467, market_window_mins=16)
        self.assertEqual(cfg.market_tag_id, 102467)
        self.assertEqual(cfg.market_window_mins, 16)

    def test_gamma_tag_constants_exported(self):
        self.assertEqual(api_poly.GAMMA_TAG_5M, 102892)
        self.assertEqual(api_poly.GAMMA_TAG_15M, 102467)

    def test_btc_updown_keywords_cover_both_timeframes(self):
        q5m  = "Bitcoin Up or Down - May 16, 4:30PM-4:35PM ET"
        q15m = "Bitcoin Up or Down - May 16, 4:30PM-4:45PM ET"
        def match_q(q):
            return any(kw in q.lower() for kw in api_poly.BTC_UPDOWN_KEYWORDS)
        self.assertTrue(match_q(q5m))
        self.assertTrue(match_q(q15m))

    def test_legacy_alias_still_works(self):
        self.assertIs(api_poly.BTC_5M_KEYWORDS, api_poly.BTC_UPDOWN_KEYWORDS)


# ── purge_expired_markets ─────────────────────────────────────────────────────

class TestPurgeExpiredMarkets(unittest.TestCase):
    """M-5: purge_expired_markets removes ended tokens with no open trade."""

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    def _add_token(self, token_id, market_id, ended=False):
        end_ms = int((time.time() - 10) * 1000) if ended else int((time.time() + 300) * 1000)
        ts = bot.TokenState(token_id, market_id, "UP", "q", 0, end_ms)
        self.state.tokens[token_id] = ts
        self.state.market_tokens[market_id] = {"UP": token_id, "DOWN": token_id + "_dn"}
        return ts

    def test_expired_token_removed(self):
        self._add_token("tok1", "mkt1", ended=True)
        n = bot.purge_expired_markets(self.state)
        self.assertEqual(n, 1)
        self.assertNotIn("tok1", self.state.tokens)
        self.assertNotIn("mkt1", self.state.market_tokens)

    def test_active_token_kept(self):
        self._add_token("tok1", "mkt1", ended=False)
        n = bot.purge_expired_markets(self.state)
        self.assertEqual(n, 0)
        self.assertIn("tok1", self.state.tokens)

    def test_expired_with_open_trade_kept(self):
        self._add_token("tok1", "mkt1", ended=True)
        self.state.open_trades["mkt1"] = 42  # open trade — must not purge
        n = bot.purge_expired_markets(self.state)
        self.assertEqual(n, 0)
        self.assertIn("tok1", self.state.tokens)

    def test_signalled_cleared_on_purge(self):
        self._add_token("tok1", "mkt1", ended=True)
        self.state.signalled.add("mkt1")
        bot.purge_expired_markets(self.state)
        self.assertNotIn("mkt1", self.state.signalled)

    def test_mixed_tokens_correct_count(self):
        self._add_token("tok_exp", "mkt_exp", ended=True)
        self._add_token("tok_live", "mkt_live", ended=False)
        n = bot.purge_expired_markets(self.state)
        self.assertEqual(n, 1)
        self.assertNotIn("tok_exp", self.state.tokens)
        self.assertIn("tok_live", self.state.tokens)


# ── ws_loop backoff ───────────────────────────────────────────────────────────

class TestWsLoopBackoff(unittest.IsolatedAsyncioTestCase):
    """M-5: ws_loop doubles backoff on failure, caps at 60, resets on success."""

    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    async def test_backoff_doubles_on_failure(self):
        sleep_calls = []

        async def _fake_sleep(n):
            sleep_calls.append(n)

        call_count = 0

        async def _failing_run_ws(state, session):
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                raise asyncio.CancelledError
            raise RuntimeError("ws error")

        with patch("live_bot._run_ws", _failing_run_ws), \
             patch("asyncio.sleep", _fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ws_loop(self.state, None)

        self.assertEqual(sleep_calls, [1, 2, 4])

    async def test_backoff_caps_at_60(self):
        sleep_calls = []

        async def _fake_sleep(n):
            sleep_calls.append(n)

        call_count = 0

        async def _failing_run_ws(state, session):
            nonlocal call_count
            call_count += 1
            if call_count >= 8:
                raise asyncio.CancelledError
            raise RuntimeError("ws error")

        with patch("live_bot._run_ws", _failing_run_ws), \
             patch("asyncio.sleep", _fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ws_loop(self.state, None)

        self.assertLessEqual(max(sleep_calls), 60)
        self.assertEqual(sleep_calls[-1], 60)

    async def test_backoff_resets_on_success(self):
        sleep_calls = []
        call_count = 0

        async def _fake_sleep(n):
            sleep_calls.append(n)

        async def _mixed_run_ws(state, session):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first failure")   # sleep(1), backoff→2
            if call_count == 2:
                return                                 # success → backoff reset to 1
            if call_count == 3:
                raise RuntimeError("second failure")  # sleep(1) — backoff was reset
            raise asyncio.CancelledError              # terminates loop

        with patch("live_bot._run_ws", _mixed_run_ws), \
             patch("asyncio.sleep", _fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bot.ws_loop(self.state, None)

        # First failure sleeps 1; after success backoff resets; second failure sleeps 1 again
        self.assertEqual(sleep_calls[0], 1)  # sleep after first failure
        self.assertEqual(sleep_calls[1], 1)  # backoff was reset — not 2


# ── _market_refresh_loop ──────────────────────────────────────────────────────

class TestMarketRefreshLoop(unittest.IsolatedAsyncioTestCase):
    """M-5: _market_refresh_loop registers new markets and purges expired ones."""

    def setUp(self):
        self.state = make_state()
        self.state.config = bot.BotConfig(market_refresh=30)

    def tearDown(self):
        self.state.conn.close()

    def _make_market(self, mid="mkt1", up="up1", dn="dn1", offset_min=3):
        now = datetime.now(timezone.utc)
        return {
            "conditionId":   mid,
            "endDate":    (now + timedelta(minutes=offset_min)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "startDate":  (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "clobTokenIds": [up, dn],
            "question":      "BTC up or down?",
        }

    async def test_new_markets_registered_and_subscribed(self):
        ws_sends = []

        class _FakeWs:
            async def send(self, msg):
                ws_sends.append(msg)

        sleep_count = 0

        async def _fake_sleep(n):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        market = self._make_market()
        with patch("live_bot.api.get_markets", unittest.mock.AsyncMock(return_value=[market])), \
             patch("asyncio.sleep", _fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bot._market_refresh_loop(self.state, None, _FakeWs())

        self.assertIn("up1", self.state.tokens)
        self.assertIn("dn1", self.state.tokens)
        self.assertTrue(len(ws_sends) > 0)

    async def test_expired_markets_purged(self):
        # Pre-populate an expired token with no open trade
        end_ms = int((time.time() - 10) * 1000)
        ts = bot.TokenState("tok_exp", "mkt_exp", "UP", "q", 0, end_ms)
        self.state.tokens["tok_exp"] = ts

        sleep_count = 0

        async def _fake_sleep(n):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with patch("live_bot.api.get_markets", unittest.mock.AsyncMock(return_value=[])), \
             patch("asyncio.sleep", _fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bot._market_refresh_loop(self.state, None, object())

        self.assertNotIn("tok_exp", self.state.tokens)

    async def test_api_error_does_not_crash_loop(self):
        sleep_count = 0

        async def _fake_sleep(n):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError

        with patch("live_bot.api.get_markets",
                   unittest.mock.AsyncMock(side_effect=RuntimeError("network down"))), \
             patch("asyncio.sleep", _fake_sleep):
            with self.assertRaises(asyncio.CancelledError):
                await bot._market_refresh_loop(self.state, None, object())

        # Loop survived the error — no tokens added, but no crash
        self.assertEqual(len(self.state.tokens), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
