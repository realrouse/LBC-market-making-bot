"""
Automated tests for bot/live_bot.py

Run with:
    bash scripts/run_tests.sh
    # or directly:
    .venv/bin/python3 -m unittest discover tests/ -v
"""

import os, sys, time, sqlite3, unittest
from datetime import datetime, timezone, timedelta

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
    """In-memory SQLite database with the production schema applied."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(bot.SCHEMA)
    conn.commit()
    return conn


def make_state():
    """BotState backed by an in-memory database."""
    return bot.BotState(make_db())


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
        await bot.check_signal(self.state, make_token(best_bid=0.95))
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
        today_ms = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
        self.state.conn.execute(
            "INSERT INTO trades "
            "(market_id, token_id, direction, stake, capital_before, resolved, pnl_net, signal_ts_ms) "
            "VALUES ('old','tok','UP',10,100,1,-35.0,?)",
            (today_ms + 1000,),
        )
        self.state.conn.commit()
        await bot.check_signal(self.state, make_token())
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_at_threshold_bid_fires(self):
        await bot.check_signal(self.state, make_token(best_bid=0.96))
        self.assertIn("mkt1", self.state.signalled)

    async def test_at_min_secs_remaining_blocked(self):
        await bot.check_signal(self.state, make_token(secs_remaining=44))
        self.assertNotIn("mkt1", self.state.signalled)

    async def test_above_min_secs_remaining_fires(self):
        # secs_remaining is computed live from time.time(), so use a value
        # safely above the 45 s limit rather than testing the exact boundary.
        await bot.check_signal(self.state, make_token(secs_remaining=60))
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


# ─── _htpasswd_sha1 ──────────────────────────────────────────────────────────

class TestHtpasswd(unittest.TestCase):

    def test_prefix(self):
        self.assertTrue(bot_utils._htpasswd_sha1("anything").startswith("{SHA}"))

    def test_known_value(self):
        import base64, hashlib
        expected = "{SHA}" + base64.b64encode(
            hashlib.sha1(b"password").digest()
        ).decode()
        self.assertEqual(bot_utils._htpasswd_sha1("password"), expected)

    def test_different_passwords_differ(self):
        self.assertNotEqual(bot_utils._htpasswd_sha1("abc"), bot_utils._htpasswd_sha1("xyz"))


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

    def _enable(self, weekday=None, weekend=None, us_open=True, us_close=True):
        bot.HOUR_FILTER_ENABLED  = True
        bot.WEEKDAY_UTC_RANGES   = weekday if weekday is not None else [(0, 8), (13, 22)]
        bot.WEEKEND_UTC_RANGES   = weekend if weekend is not None else []
        bot.US_WEEKLY_OPEN       = us_open
        bot.US_WEEKLY_CLOSE      = us_close

    def tearDown(self):
        bot.HOUR_FILTER_ENABLED  = False
        bot.WEEKDAY_UTC_RANGES   = []
        bot.WEEKEND_UTC_RANGES   = []
        bot.US_WEEKLY_OPEN       = True
        bot.US_WEEKLY_CLOSE      = True

    def _ts(self, iso: str) -> int:
        return int(datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp() * 1000)

    def test_disabled_always_true(self):
        # Filter off → all times allowed regardless of config
        self.assertTrue(bot.is_trading_hour(self._ts("2026-04-27 03:00:00")))  # Monday 3h

    def test_weekday_in_range(self):
        self._enable()
        self.assertTrue(bot.is_trading_hour(self._ts("2026-04-28 06:00:00")))  # Tuesday 6h

    def test_weekday_outside_range(self):
        self._enable()
        self.assertFalse(bot.is_trading_hour(self._ts("2026-04-28 10:00:00")))  # Tuesday 10h

    def test_monday_before_us_open_blocked(self):
        self._enable()
        self.assertFalse(bot.is_trading_hour(self._ts("2026-04-27 12:00:00")))  # Monday 12h

    def test_monday_before_us_open_minute_precision(self):
        self._enable()
        self.assertFalse(bot.is_trading_hour(self._ts("2026-04-27 13:29:00")))  # Monday 13h29

    def test_monday_after_us_open_allowed(self):
        self._enable()
        self.assertTrue(bot.is_trading_hour(self._ts("2026-04-27 14:00:00")))   # Monday 14h

    def test_friday_after_us_close_blocked(self):
        self._enable()
        self.assertFalse(bot.is_trading_hour(self._ts("2026-05-01 21:00:00")))  # Friday 21h

    def test_friday_before_us_close_allowed(self):
        self._enable()
        self.assertTrue(bot.is_trading_hour(self._ts("2026-05-01 15:00:00")))   # Friday 15h

    def test_weekend_blocked_by_default(self):
        self._enable(weekend=[])
        self.assertFalse(bot.is_trading_hour(self._ts("2026-04-26 15:00:00")))  # Saturday

    def test_weekend_allowed_when_configured(self):
        self._enable(weekend=[(13, 20)])
        self.assertTrue(bot.is_trading_hour(self._ts("2026-04-26 15:00:00")))   # Saturday 15h

    def test_weekend_outside_range_blocked(self):
        self._enable(weekend=[(13, 20)])
        self.assertFalse(bot.is_trading_hour(self._ts("2026-04-26 10:00:00")))  # Saturday 10h

    def test_empty_weekday_ranges_allows_all_hours(self):
        self._enable(weekday=[])
        self.assertTrue(bot.is_trading_hour(self._ts("2026-04-28 10:00:00")))   # Tuesday 10h

    def test_us_open_flag_disabled(self):
        # Monday 7h is in range (0-8) but would be blocked by US_WEEKLY_OPEN=True.
        # With us_open=False the special Monday constraint is lifted → allowed.
        self._enable(us_open=False)
        self.assertTrue(bot.is_trading_hour(self._ts("2026-04-27 07:00:00")))   # Monday 7h ok

    def test_us_close_flag_disabled(self):
        self._enable(us_close=False)
        self.assertTrue(bot.is_trading_hour(self._ts("2026-05-01 21:00:00")))   # Friday 21h ok

    def test_now_uses_current_time(self):
        bot.HOUR_FILTER_ENABLED = True
        bot.WEEKDAY_UTC_RANGES  = []
        bot.WEEKEND_UTC_RANGES  = []
        # No ts_ms → uses datetime.now() — just check it doesn't crash
        result = bot.is_trading_hour()
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
