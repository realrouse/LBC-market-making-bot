"""
Automated tests for bot/live_bot.py

Run with:
    bash scripts/run_tests.sh
    # or directly:
    .venv/bin/python3 -m unittest discover tests/ -v
"""

import os, sys, time, sqlite3, unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

# Redirect all bot I/O to ~/tmp so tests never touch /opt or write credentials.
# ~/tmp is per-user by definition — no PermissionError on shared servers.
_TEST_DIR = os.path.join(os.path.expanduser("~"), "tmp", "tradinebotte-test")
os.environ["TRADINEBOTTE_DIR"] = _TEST_DIR
os.makedirs(_TEST_DIR, exist_ok=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
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
        import time as _time
        self.state.daily_pnl = -35.0
        self.state._daily_pnl_day = int(_time.time() // 86400)
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


# ─── Daily PnL cache (amelioration II) ───────────────────────────────────────

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
        import time as _time
        state = make_state(conn=self.conn)
        state.daily_pnl = -25.0
        # Simulate "yesterday" by setting the day counter one behind current
        state._daily_pnl_day = int(_time.time() // 86400) - 1
        # check_signal reads state._daily_pnl_day and resets on rollover
        state.config = bot.BotConfig(signal_threshold=0.95)
        ts = make_token(best_bid=0.90)  # below threshold — signal won't fire
        import asyncio
        asyncio.run(bot.check_signal(state, ts))
        self.assertEqual(state.daily_pnl, 0.0,
                         "daily_pnl should reset to 0 after midnight rollover")
        self.assertEqual(state._daily_pnl_day, int(_time.time() // 86400))

    def test_restore_loads_today_pnl(self):
        import time as _time
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


# ─── Signalled restore for recent resolved trades (amelioration VI) ───────────

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
        self.assertIn("Aucun trade", bot_utils.generate_status_html(self._state()))

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
        ts, row = self._snap(market_id="mkt_snap")
        self.assertEqual(row[2], "mkt_snap")

    def test_token_id_stored(self):
        ts, row = self._snap(token_id="tok_snap")
        self.assertEqual(row[3], "tok_snap")

    def test_direction_stored(self):
        ts, row = self._snap(direction="DOWN")
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
        from datetime import datetime, timezone
        # Christmas 2026 (Fri Dec 25), 15:00 UTC
        cfg = bot.BotConfig(us_holiday_filter=True)
        dt = datetime(2026, 12, 25, 15, 0, 0, tzinfo=timezone.utc)
        self.assertFalse(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))

    def test_allows_on_holiday_when_filter_disabled(self):
        from datetime import datetime, timezone
        cfg = bot.BotConfig(us_holiday_filter=False)
        dt = datetime(2026, 12, 25, 15, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))

    def test_normal_weekday_not_blocked(self):
        from datetime import datetime, timezone
        cfg = bot.BotConfig(us_holiday_filter=True)
        # Wednesday 2026-05-06, 15:00 UTC — not a holiday
        dt = datetime(2026, 5, 6, 15, 0, 0, tzinfo=timezone.utc)
        self.assertTrue(bot.is_trading_hour(cfg, int(dt.timestamp() * 1000)))

    def test_holiday_blocks_independently_of_hour_filter(self):
        from datetime import datetime, timezone
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


class TestStrategyLoading(unittest.TestCase):
    """Verify the v2 strategy JSON exists, loads correctly, and has the sweep-optimised params."""

    _STRAT_DIR = os.path.join(os.path.dirname(__file__), "..", "strategies")

    def _load(self, name):
        return bot.load_strategy(os.path.join(self._STRAT_DIR, name))

    def test_v2_file_exists(self):
        path = os.path.join(self._STRAT_DIR, "polymarket_BTC5M_v2.json")
        self.assertTrue(os.path.exists(path))

    def test_v1_file_still_present(self):
        path = os.path.join(self._STRAT_DIR, "polymarket_BTC5M.json")
        self.assertTrue(os.path.exists(path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(bot.load_strategy("/nonexistent/strategy.json"))

    def test_v2_threshold(self):
        s = self._load("polymarket_BTC5M_v2.json")
        self.assertAlmostEqual(s["signal_threshold"], 0.95)

    def test_v2_min_secs(self):
        s = self._load("polymarket_BTC5M_v2.json")
        self.assertEqual(s["min_secs_remaining"], 45)

    def test_v2_obi(self):
        s = self._load("polymarket_BTC5M_v2.json")
        self.assertAlmostEqual(s["obi_reject_thresh"], -0.75)

    def test_v2_dsl(self):
        s = self._load("polymarket_BTC5M_v2.json")
        self.assertAlmostEqual(s["daily_stop_loss"], 30.0)

    def test_v2_lower_threshold_than_v1(self):
        v1 = self._load("polymarket_BTC5M.json")
        v2 = self._load("polymarket_BTC5M_v2.json")
        self.assertLess(v2["signal_threshold"], v1["signal_threshold"])


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


# ── Strategy factory ──────────────────────────────────────────────────────────

import strategies as _strategies_mod
from strategies.grid import GridStrategy, GridLevel, GridState

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
