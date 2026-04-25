"""
Tests for bot/live_bot.py

Uses in-memory SQLite and unittest.mock to exercise signal logic,
resolution, trade lifecycle, and state management without touching
the network or production data.
"""

import base64, hashlib, os, sqlite3, sys, time, unittest
from datetime import datetime, timezone

# Point TRADINEBOTTE_DIR to a temp location before importing live_bot so that
# module-level code (FileHandler, DB path, os.makedirs) never touches production data.
os.environ["TRADINEBOTTE_DIR"] = "/tmp/tradinebotte-test"

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))
import live_bot as lb


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_db() -> sqlite3.Connection:
    """In-memory SQLite DB with the full bot schema applied."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(lb.SCHEMA)
    return conn


def _make_state(conn=None) -> lb.BotState:
    if conn is None:
        conn = _make_db()
    return lb.BotState(conn)


def _make_token(
    market_id: str = "mkt1",
    direction: str = "UP",
    end_offset_secs: int = 300,
    bid: float = 0.50,
    ask: float = 0.51,
    ask_vol: float = 50.0,
    obi: float = 0.0,
) -> lb.TokenState:
    """Return a TokenState populated with the given values."""
    now_ms = int(time.time() * 1000)
    ts = lb.TokenState(
        token_id=f"{market_id}-{direction}",
        market_id=market_id,
        direction=direction,
        question="BTC Up or Down?",
        market_start_ms=now_ms - 60_000,
        market_end_ms=now_ms + end_offset_secs * 1000,
    )
    ts.best_bid = bid
    ts.best_ask = ask
    ts.ask_vol  = ask_vol
    ts.obi      = obi
    return ts


def _insert_trade(
    conn: sqlite3.Connection,
    market_id: str = "mkt1",
    direction: str = "UP",
    stake: float = 10.0,
    tokens: float = 10.256,
    fee: float = 0.021,
    capital_before: float = 100.0,
) -> int:
    """Insert an open trade and return its row id."""
    now_ms = int(time.time() * 1000)
    cur = conn.execute(
        "INSERT INTO trades (market_id, token_id, direction, question, "
        "signal_ts_ms, entry_ts_ms, entry_price, stake, tokens_bought, fee, "
        "cost_total, capital_before, resolved) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (market_id, f"{market_id}-{direction}", direction, "BTC Up?",
         now_ms, now_ms, 0.975, stake, tokens, fee, stake + fee, capital_before, 0),
    )
    conn.commit()
    return cur.lastrowid


# ─── TokenState ──────────────────────────────────────────────────────────────

class TestTokenState(unittest.TestCase):

    def test_secs_remaining_positive(self):
        ts = _make_token(end_offset_secs=120)
        self.assertGreater(ts.secs_remaining, 100.0)
        self.assertLess(ts.secs_remaining, 130.0)

    def test_secs_remaining_zero_when_past_end(self):
        now_ms = int(time.time() * 1000)
        ts = lb.TokenState("t", "m", "UP", "q", now_ms - 300_000, now_ms - 10_000)
        self.assertEqual(ts.secs_remaining, 0.0)

    def test_market_ended_false_when_in_future(self):
        ts = _make_token(end_offset_secs=300)
        self.assertFalse(ts.market_ended)

    def test_market_ended_true_past_grace(self):
        now_ms = int(time.time() * 1000)
        # End time 10s in the past — well past the 5s grace period.
        ts = lb.TokenState("t", "m", "UP", "q", now_ms - 300_000, now_ms - 10_000)
        self.assertTrue(ts.market_ended)

    def test_market_ended_false_within_grace(self):
        now_ms = int(time.time() * 1000)
        # End time 2s in the past — still inside the 5s grace window.
        ts = lb.TokenState("t", "m", "UP", "q", now_ms - 300_000, now_ms - 2_000)
        self.assertFalse(ts.market_ended)

    def test_seconds_elapsed_positive(self):
        ts = _make_token()  # market_start_ms = now - 60s
        self.assertGreater(ts.seconds_elapsed, 50.0)
        self.assertLess(ts.seconds_elapsed, 75.0)


# ─── BotState ────────────────────────────────────────────────────────────────

class TestBotState(unittest.TestCase):

    def test_win_rate_no_trades(self):
        self.assertAlmostEqual(_make_state().win_rate, 0.0)

    def test_win_rate_all_wins(self):
        state = _make_state()
        state.wins = 5
        self.assertAlmostEqual(state.win_rate, 100.0)

    def test_win_rate_mixed(self):
        state = _make_state()
        state.wins   = 3
        state.losses = 1
        self.assertAlmostEqual(state.win_rate, 75.0)


# ─── check_signal ─────────────────────────────────────────────────────────────

class TestCheckSignal(unittest.IsolatedAsyncioTestCase):

    def _signal_token(self, **kw) -> lb.TokenState:
        """Token that passes all entry guards under default parameters."""
        defaults = dict(bid=0.97, ask=0.975, ask_vol=50.0, obi=0.0, end_offset_secs=60)
        defaults.update(kw)
        return _make_token(**defaults)

    async def _trades_entered(self, state: lb.BotState, ts: lb.TokenState) -> int:
        before = len(state.open_trades)
        await lb.check_signal(state, ts)
        return len(state.open_trades) - before

    async def test_no_signal_already_signalled(self):
        state = _make_state()
        ts = self._signal_token()
        state.signalled.add(ts.market_id)
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_market_ended(self):
        state = _make_state()
        ts = self._signal_token(end_offset_secs=-10)  # ended 10s ago
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_below_threshold(self):
        state = _make_state()
        ts = self._signal_token(bid=0.95)  # SIGNAL_THRESHOLD = 0.96
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_ask_at_settlement(self):
        state = _make_state()
        ts = self._signal_token(ask=1.0)  # expired markets leak ask=1.0
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_thin_volume(self):
        state = _make_state()
        ts = self._signal_token(ask_vol=5.0)  # MIN_ASK_VOL = 10
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_insufficient_secs(self):
        state = _make_state()
        ts = self._signal_token(end_offset_secs=30)  # MIN_SECS_REMAINING = 45
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_obi_too_negative(self):
        state = _make_state()
        ts = self._signal_token(obi=-0.8)  # OBI_REJECT_THRESH = -0.50
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_insufficient_capital(self):
        state = _make_state()
        state.capital = 0.0
        ts = self._signal_token()
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_no_signal_daily_stop_loss(self):
        conn = _make_db()
        tid  = _insert_trade(conn)
        # Mark the trade as a loss large enough to breach the daily stop.
        today_ms = int(
            datetime.now(timezone.utc)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp() * 1000
        )
        conn.execute(
            "UPDATE trades SET resolved=1, outcome='LOSS', pnl_net=?, signal_ts_ms=? WHERE id=?",
            (-(lb.DAILY_STOP_LOSS + 1.0), today_ms + 1000, tid),
        )
        conn.commit()
        state = _make_state(conn)
        ts = self._signal_token()
        self.assertEqual(await self._trades_entered(state, ts), 0)

    async def test_signal_fires_and_trade_entered(self):
        state = _make_state()
        state.session = None  # simulation mode — no HTTP order call
        ts = self._signal_token()
        self.assertEqual(await self._trades_entered(state, ts), 1)
        self.assertIn(ts.market_id, state.signalled)
        self.assertIn(ts.market_id, state.open_trades)

    async def test_no_duplicate_entry_same_market(self):
        state = _make_state()
        state.session = None
        ts = self._signal_token()
        await lb.check_signal(state, ts)
        await lb.check_signal(state, ts)  # second call — already signalled
        self.assertEqual(len(state.open_trades), 1)


# ─── check_resolution ─────────────────────────────────────────────────────────

class TestCheckResolution(unittest.TestCase):

    def _setup(self, bid: float = 0.50, direction: str = "UP", market_ended: bool = False):
        conn = _make_db()
        tid  = _insert_trade(conn, direction=direction)
        state = _make_state(conn)
        end_offset = -10 if market_ended else 300
        ts = _make_token(bid=bid, direction=direction, end_offset_secs=end_offset)
        state.open_trades[ts.market_id]      = tid
        state.traded_direction[ts.market_id] = direction
        return state, ts, tid

    def test_no_resolution_no_open_trade(self):
        state = _make_state()
        ts = _make_token(bid=lb.WIN_THRESHOLD)
        lb.check_resolution(state, ts)  # must not raise or change state
        self.assertEqual(state.wins, 0)

    def test_no_resolution_wrong_direction(self):
        # We bought UP, but the WebSocket message is for the DOWN token.
        state, ts, _ = self._setup(bid=lb.WIN_THRESHOLD, direction="UP")
        ts.direction = "DOWN"
        lb.check_resolution(state, ts)
        self.assertEqual(state.wins, 0)

    def test_win_on_high_bid(self):
        state, ts, _ = self._setup(bid=lb.WIN_THRESHOLD)
        lb.check_resolution(state, ts)
        self.assertEqual(state.wins, 1)
        self.assertNotIn(ts.market_id, state.open_trades)

    def test_loss_on_low_bid(self):
        state, ts, _ = self._setup(bid=lb.LOSS_THRESHOLD)
        lb.check_resolution(state, ts)
        self.assertEqual(state.losses, 1)

    def test_win_on_expiry_above_half(self):
        state, ts, _ = self._setup(bid=0.60, market_ended=True)
        lb.check_resolution(state, ts)
        self.assertEqual(state.wins, 1)

    def test_loss_on_expiry_below_half(self):
        state, ts, _ = self._setup(bid=0.40, market_ended=True)
        lb.check_resolution(state, ts)
        self.assertEqual(state.losses, 1)


# ─── close_trade ─────────────────────────────────────────────────────────────

class TestCloseTrade(unittest.TestCase):

    def _setup(self, direction: str = "UP"):
        conn = _make_db()
        tid  = _insert_trade(conn, direction=direction, capital_before=100.0)
        state = _make_state(conn)
        state.capital = 100.0
        ts = _make_token(direction=direction)
        state.open_trades[ts.market_id]      = tid
        state.traded_direction[ts.market_id] = direction
        return state, ts, tid

    def test_win_increases_capital(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "WIN")
        self.assertGreater(state.capital, 100.0)

    def test_loss_decreases_capital(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "LOSS")
        self.assertLess(state.capital, 100.0)

    def test_trade_marked_resolved_in_db(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "WIN")
        row = state.conn.execute("SELECT resolved FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row[0], 1)

    def test_trade_removed_from_open_trades(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "WIN")
        self.assertNotIn(ts.market_id, state.open_trades)

    def test_win_increments_counter(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "WIN")
        self.assertEqual(state.wins, 1)
        self.assertEqual(state.losses, 0)

    def test_loss_increments_counter(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "LOSS")
        self.assertEqual(state.losses, 1)
        self.assertEqual(state.wins, 0)

    def test_pnl_recorded_in_db(self):
        state, ts, tid = self._setup()
        lb.close_trade(state, ts, tid, "WIN")
        row = state.conn.execute("SELECT pnl_net FROM trades WHERE id=?", (tid,)).fetchone()
        self.assertIsNotNone(row[0])
        self.assertGreater(row[0], 0)  # WIN should yield positive net PnL


# ─── restore_state_from_db ───────────────────────────────────────────────────

class TestRestoreState(unittest.TestCase):

    def test_empty_db_leaves_state_unchanged(self):
        state = _make_state()
        lb.restore_state_from_db(state)
        self.assertEqual(len(state.open_trades), 0)
        self.assertEqual(state.wins, 0)
        self.assertEqual(state.losses, 0)

    def test_open_trades_restored(self):
        conn = _make_db()
        _insert_trade(conn, market_id="mkt1")
        _insert_trade(conn, market_id="mkt2")
        state = _make_state(conn)
        lb.restore_state_from_db(state)
        self.assertIn("mkt1", state.open_trades)
        self.assertIn("mkt2", state.open_trades)
        self.assertIn("mkt1", state.signalled)

    def test_resolved_trades_counted(self):
        conn = _make_db()
        tid = _insert_trade(conn)
        conn.execute(
            "UPDATE trades SET resolved=1, outcome='WIN', pnl_net=1.5 WHERE id=?", (tid,)
        )
        conn.commit()
        state = _make_state(conn)
        lb.restore_state_from_db(state)
        self.assertEqual(state.wins, 1)
        self.assertAlmostEqual(state.total_pnl, 1.5)

    def test_capital_rebuilt_from_pnl(self):
        conn = _make_db()
        tid = _insert_trade(conn)
        conn.execute(
            "UPDATE trades SET resolved=1, outcome='WIN', pnl_net=5.0 WHERE id=?", (tid,)
        )
        conn.commit()
        state = _make_state(conn)
        lb.restore_state_from_db(state)
        self.assertAlmostEqual(state.capital, lb.CAPITAL_START + 5.0)

    def test_open_trade_not_in_total_trades_twice(self):
        conn = _make_db()
        _insert_trade(conn, market_id="mkt_open")
        state = _make_state(conn)
        lb.restore_state_from_db(state)
        # 0 resolved + 1 open = 1 total
        self.assertEqual(state.total_trades, 1)


# ─── _htpasswd_sha1 ──────────────────────────────────────────────────────────

class TestHtpasswd(unittest.TestCase):

    def test_prefix(self):
        self.assertTrue(lb._htpasswd_sha1("anything").startswith("{SHA}"))

    def test_known_value(self):
        expected = "{SHA}" + base64.b64encode(
            hashlib.sha1(b"password").digest()
        ).decode()
        self.assertEqual(lb._htpasswd_sha1("password"), expected)

    def test_different_passwords_differ(self):
        self.assertNotEqual(lb._htpasswd_sha1("abc"), lb._htpasswd_sha1("xyz"))


# ─── generate_status_html ─────────────────────────────────────────────────────

class TestGenerateStatusHtml(unittest.TestCase):

    def _state(self) -> lb.BotState:
        state = _make_state()
        state.capital     = 123.45
        state.total_pnl   = 3.45
        state.total_trades = 5
        state.wins         = 4
        state.losses       = 1
        return state

    def test_contains_capital(self):
        self.assertIn("123.45", lb.generate_status_html(self._state()))

    def test_contains_table(self):
        html = lb.generate_status_html(self._state())
        self.assertIn("<table>", html)
        self.assertIn("Direction", html)

    def test_no_trades_message_when_empty(self):
        self.assertIn("Aucun trade", lb.generate_status_html(self._state()))

    def test_win_rate_shown(self):
        state = self._state()
        html  = lb.generate_status_html(state)
        # win_rate with 4W/1L = 80.0%
        self.assertIn("80.0%", html)


# ─── handle_book_update (integration) ────────────────────────────────────────

class TestHandleBookUpdate(unittest.IsolatedAsyncioTestCase):

    async def test_state_updated_from_message(self):
        state = _make_state()
        state.session = None
        ts = _make_token(bid=0.50, ask=0.51)
        state.tokens[ts.token_id] = ts

        parsed = {
            "token_id": ts.token_id,
            "best_bid": 0.55,
            "best_ask": 0.56,
            "spread":   0.01,
            "bid_vol":  100.0,
            "ask_vol":  80.0,
            "obi":      0.1,
        }
        await lb.handle_book_update(state, parsed)
        self.assertAlmostEqual(ts.best_bid, 0.55)
        self.assertAlmostEqual(ts.best_ask, 0.56)
        self.assertAlmostEqual(ts.ask_vol, 80.0)

    async def test_unknown_token_ignored(self):
        state = _make_state()
        parsed = {
            "token_id": "unknown-token",
            "best_bid": 0.97, "best_ask": 0.975,
            "spread": 0.005, "bid_vol": 100.0,
            "ask_vol": 50.0, "obi": 0.0,
        }
        # Must not raise even if the token is not tracked.
        await lb.handle_book_update(state, parsed)
        self.assertEqual(len(state.open_trades), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
