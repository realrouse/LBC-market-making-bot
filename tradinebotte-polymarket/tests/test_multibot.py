"""
tests/test_multibot.py — Tests for the ZeroMQ multi-bot architecture

Covers three scenarios:
  1. feed.register_market()    — unit tests (new tokens, expired, missing fields, duplicates)
  2. account_bot helpers       — unit tests (_register_from_market_msg, book routing)
  3. Integration — Option A    — one account_bot receives feed messages and updates state
  4. Integration — Option B    — two account_bots share the same feed simultaneously

No network access or real exchange credentials required.
All ZMQ communication uses TCP on a loopback port allocated at test startup.
All SQLite databases are in-memory.

Run with:
    bash scripts/run_tests.sh
"""

import asyncio
import os
import sqlite3
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta

_TEST_DIR = os.path.join(os.path.expanduser("~"), "tmp", "tradinebotte-test")
os.environ.setdefault("TRADINEBOTTE_DIR", _TEST_DIR)
os.makedirs(_TEST_DIR, exist_ok=True)

BOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, BOT_DIR)

import live_bot as bot
import api_polymarket as api_poly
import feed as feed_mod
import account_bot as abot


# ── helpers ───────────────────────────────────────────────────────────────────

def _market_dict(mid="m1", q="Bitcoin up or down", up="tok-up", dn="tok-dn",
                 end_offset_s=300, start_offset_s=-60):
    """Minimal market dict compatible with api_polymarket helpers."""
    now = datetime.now(timezone.utc)
    start = (now + timedelta(seconds=start_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = (now + timedelta(seconds=end_offset_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "conditionId": mid,
        "question": q,
        "clobTokenIds": [up, dn],
        "startDate": start,
        "endDate": end,
    }


def _market_msg(mid="m1", q="Bitcoin up or down", up="tok-up", dn="tok-dn",
                end_offset_s=300, start_offset_s=-60):
    """feed.py "market" message dict."""
    now_ms = time.time() * 1000
    return {
        "t": "market",
        "market_id": mid,
        "question": q,
        "up_token_id": up,
        "dn_token_id": dn,
        "start_ms": int(now_ms + start_offset_s * 1000),
        "end_ms":   int(now_ms + end_offset_s * 1000),
    }


def _book_msg(token_id="tok-up", best_bid=0.97, best_ask=0.975,
              bid_vol=100.0, ask_vol=80.0, obi=0.11):
    """feed.py "book" message dict."""
    return {
        "t": "book",
        "token_id": token_id,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread": round(best_ask - best_bid, 4),
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "obi": obi,
    }


def make_state():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    bot.apply_base_schema(conn)   # schema_version + bot_meta (neutral base)
    conn.executescript(bot.SCHEMA)
    conn.commit()
    # Inject the threshold default + connector the way the entrypoint does (Plan D step 3b/4b).
    return bot.BotState(conn, strategy=bot.ThresholdStrategy(), connector=api_poly)


# ── 1. feed.register_market() ─────────────────────────────────────────────────

class TestFeedRegisterMarket(unittest.TestCase):
    def setUp(self):
        feed_mod.registered.clear()
        feed_mod.token_meta.clear()

    def tearDown(self):
        feed_mod.registered.clear()
        feed_mod.token_meta.clear()

    def test_registers_both_tokens(self):
        new = feed_mod.register_market(_market_dict())
        self.assertIn("tok-up", new)
        self.assertIn("tok-dn", new)
        self.assertIn("tok-up", feed_mod.token_meta)
        self.assertIn("tok-dn", feed_mod.token_meta)
        self.assertIn("m1", feed_mod.registered)

    def test_returns_only_new_tokens_on_duplicate_call(self):
        feed_mod.register_market(_market_dict())
        new = feed_mod.register_market(_market_dict())
        self.assertEqual(new, [])

    def test_skips_expired_market(self):
        new = feed_mod.register_market(_market_dict(end_offset_s=-10))
        self.assertEqual(new, [])
        self.assertNotIn("m1", feed_mod.registered)

    def test_skips_market_without_condition_id(self):
        m = _market_dict()
        del m["conditionId"]
        new = feed_mod.register_market(m)
        self.assertEqual(new, [])

    def test_skips_market_without_tokens(self):
        m = _market_dict()
        del m["clobTokenIds"]
        new = feed_mod.register_market(m)
        self.assertEqual(new, [])

    def test_registers_multiple_distinct_markets(self):
        feed_mod.register_market(_market_dict(mid="m1", up="u1", dn="d1"))
        feed_mod.register_market(_market_dict(mid="m2", up="u2", dn="d2"))
        self.assertEqual(len(feed_mod.registered), 2)
        self.assertEqual(len(feed_mod.token_meta), 4)

    def test_market_meta_stored(self):
        feed_mod.register_market(_market_dict(mid="m1", q="BTC Q", up="u1", dn="d1"))
        r = feed_mod.registered["m1"]
        self.assertEqual(r["up"], "u1")
        self.assertEqual(r["dn"], "d1")
        self.assertIn("BTC Q", r["question"])


# ── 2. account_bot._register_from_market_msg() ────────────────────────────────

class TestAccountBotRegister(unittest.TestCase):
    def setUp(self):
        self.state = make_state()

    def tearDown(self):
        self.state.conn.close()

    def test_creates_up_and_down_token_states(self):
        abot._register_from_market_msg(self.state, _market_msg())
        self.assertIn("tok-up", self.state.tokens)
        self.assertIn("tok-dn", self.state.tokens)

    def test_direction_set_correctly(self):
        abot._register_from_market_msg(self.state, _market_msg())
        self.assertEqual(self.state.tokens["tok-up"].direction, "UP")
        self.assertEqual(self.state.tokens["tok-dn"].direction, "DOWN")

    def test_market_tokens_map_updated(self):
        abot._register_from_market_msg(self.state, _market_msg())
        self.assertIn("m1", self.state.market_tokens)
        self.assertEqual(self.state.market_tokens["m1"]["UP"], "tok-up")
        self.assertEqual(self.state.market_tokens["m1"]["DOWN"], "tok-dn")

    def test_skips_expired_market(self):
        abot._register_from_market_msg(self.state, _market_msg(end_offset_s=-10))
        self.assertNotIn("tok-up", self.state.tokens)

    def test_skips_message_missing_token_ids(self):
        msg = _market_msg()
        del msg["up_token_id"]
        abot._register_from_market_msg(self.state, msg)
        self.assertNotIn("tok-up", self.state.tokens)

    def test_skips_message_missing_market_id(self):
        msg = _market_msg()
        del msg["market_id"]
        abot._register_from_market_msg(self.state, msg)
        self.assertEqual(len(self.state.tokens), 0)

    def test_duplicate_registration_is_idempotent(self):
        abot._register_from_market_msg(self.state, _market_msg())
        abot._register_from_market_msg(self.state, _market_msg())
        self.assertEqual(len(self.state.tokens), 2)

    def test_question_stored_in_token_state(self):
        abot._register_from_market_msg(self.state, _market_msg(q="My market question"))
        self.assertEqual(self.state.tokens["tok-up"].question, "My market question")

    def test_end_ms_stored_in_token_state(self):
        now_ms = time.time() * 1000
        abot._register_from_market_msg(self.state, _market_msg(end_offset_s=300))
        ts = self.state.tokens["tok-up"]
        # end_ms should be approximately now + 300 s
        self.assertAlmostEqual(ts.market_end_ms, now_ms + 300_000, delta=5000)


# ── 3 & 4. Integration: ZMQ round-trip ───────────────────────────────────────

TEST_PORT = 15000 + (os.getuid() % 900)  # per-uid to avoid conflicts on shared servers
TEST_ADDR = f"tcp://127.0.0.1:{TEST_PORT}"


class TestSingleBotIntegration(unittest.IsolatedAsyncioTestCase):
    """Option A — one account_bot subscribes to a fake feed and processes messages."""

    async def asyncSetUp(self):
        import zmq.asyncio as zaio
        self.ctx = zaio.Context()
        self.pub = self.ctx.socket(__import__("zmq").PUB)
        self.pub.bind(TEST_ADDR)
        await asyncio.sleep(0.2)   # let SUB sockets connect

        self.state = make_state()
        self._saved_addr = abot._FEED_ADDR
        abot._FEED_ADDR = TEST_ADDR

        self.run_task = asyncio.create_task(abot._run(self.state))
        await asyncio.sleep(0.3)   # wait for subscriber to attach

    async def asyncTearDown(self):
        self.run_task.cancel()
        try:
            await self.run_task
        except asyncio.CancelledError:
            pass
        abot._FEED_ADDR = self._saved_addr
        self.pub.close()
        self.ctx.term()
        self.state.conn.close()

    async def _send(self, msg, delay=0.15):
        self.pub.send_json(msg, __import__("zmq").NOBLOCK)
        await asyncio.sleep(delay)

    # ── market message ────────────────────────────────────────────────────────

    async def test_market_message_registers_tokens(self):
        await self._send(_market_msg())
        self.assertIn("tok-up", self.state.tokens)
        self.assertIn("tok-dn", self.state.tokens)

    async def test_market_message_sets_direction(self):
        await self._send(_market_msg())
        self.assertEqual(self.state.tokens["tok-up"].direction, "UP")
        self.assertEqual(self.state.tokens["tok-dn"].direction, "DOWN")

    # ── book message ─────────────────────────────────────────────────────────

    async def test_book_message_updates_bid_ask(self):
        await self._send(_market_msg())
        await self._send(_book_msg("tok-up", best_bid=0.50, best_ask=0.52))
        ts = self.state.tokens["tok-up"]
        self.assertAlmostEqual(ts.best_bid, 0.50)
        self.assertAlmostEqual(ts.best_ask, 0.52)

    async def test_book_message_updates_obi(self):
        await self._send(_market_msg())
        await self._send(_book_msg("tok-up", obi=0.35))
        self.assertAlmostEqual(self.state.tokens["tok-up"].obi, 0.35)

    async def test_book_message_for_unknown_token_is_ignored(self):
        await self._send(_book_msg("unknown-token", best_bid=0.99))
        # No crash, state empty
        self.assertEqual(len(self.state.tokens), 0)

    # ── signal fires when threshold reached ───────────────────────────────────

    async def test_signal_above_threshold_creates_db_row(self):
        await self._send(_market_msg())
        # Send a bid that exceeds SIGNAL_THRESHOLD (0.96)
        await self._send(_book_msg("tok-up", best_bid=0.97, best_ask=0.975,
                                   ask_vol=50.0), delay=0.3)
        count = self.state.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE market_id='m1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    async def test_signal_below_threshold_creates_no_trade(self):
        await self._send(_market_msg())
        await self._send(_book_msg("tok-up", best_bid=0.80, best_ask=0.82,
                                   ask_vol=50.0), delay=0.2)
        count = self.state.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        self.assertEqual(count, 0)

    # ── ping message ─────────────────────────────────────────────────────────

    async def test_ping_message_does_not_crash(self):
        await self._send({"t": "ping", "ts": int(time.time() * 1000)})
        # No exception raised, state unchanged
        self.assertEqual(len(self.state.tokens), 0)


class TestTwoBotIntegration(unittest.IsolatedAsyncioTestCase):
    """Option B — two account_bots share the same feed simultaneously."""

    async def asyncSetUp(self):
        import zmq
        import zmq.asyncio as zaio
        self.ctx = zaio.Context()
        self.pub = self.ctx.socket(zmq.PUB)
        self.pub.bind(TEST_ADDR)
        await asyncio.sleep(0.2)

        self.state_a = make_state()
        self.state_b = make_state()
        self._saved_addr = abot._FEED_ADDR
        abot._FEED_ADDR = TEST_ADDR

        self.task_a = asyncio.create_task(abot._run(self.state_a))
        self.task_b = asyncio.create_task(abot._run(self.state_b))
        await asyncio.sleep(0.3)

    async def asyncTearDown(self):
        for t in (self.task_a, self.task_b):
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        abot._FEED_ADDR = self._saved_addr
        self.pub.close()
        self.ctx.term()
        self.state_a.conn.close()
        self.state_b.conn.close()

    async def _send(self, msg, delay=0.15):
        self.pub.send_json(msg, __import__("zmq").NOBLOCK)
        await asyncio.sleep(delay)

    # ── both bots receive market registrations ────────────────────────────────

    async def test_both_bots_register_market(self):
        await self._send(_market_msg())
        self.assertIn("tok-up", self.state_a.tokens)
        self.assertIn("tok-up", self.state_b.tokens)

    async def test_both_bots_receive_book_update(self):
        await self._send(_market_msg())
        await self._send(_book_msg("tok-up", best_bid=0.60, best_ask=0.62))
        self.assertAlmostEqual(self.state_a.tokens["tok-up"].best_bid, 0.60)
        self.assertAlmostEqual(self.state_b.tokens["tok-up"].best_bid, 0.60)

    # ── each bot has isolated state ───────────────────────────────────────────

    async def test_trade_in_bot_a_does_not_appear_in_bot_b(self):
        await self._send(_market_msg())
        await self._send(_book_msg("tok-up", best_bid=0.97, best_ask=0.975,
                                   ask_vol=50.0), delay=0.3)
        count_a = self.state_a.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE market_id='m1'"
        ).fetchone()[0]
        count_b = self.state_b.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE market_id='m1'"
        ).fetchone()[0]
        # Both bots should have entered independently (own DBs)
        self.assertEqual(count_a, 1)
        self.assertEqual(count_b, 1)

    async def test_capital_is_independent_per_bot(self):
        # Modify bot_a's capital directly — bot_b must be unaffected
        self.state_a.capital = 50.0
        self.state_b.capital = 200.0
        await self._send(_market_msg())
        await asyncio.sleep(0.1)
        self.assertAlmostEqual(self.state_a.capital, 50.0)
        self.assertAlmostEqual(self.state_b.capital, 200.0)

    async def test_both_bots_receive_multiple_markets(self):
        await self._send(_market_msg(mid="m1", up="u1", dn="d1"))
        await self._send(_market_msg(mid="m2", up="u2", dn="d2"))
        self.assertEqual(len(self.state_a.tokens), 4)
        self.assertEqual(len(self.state_b.tokens), 4)

    async def test_feed_restart_does_not_corrupt_bot_state(self):
        """Simulate feed sending markets twice (reconnect scenario)."""
        await self._send(_market_msg())
        await self._send(_market_msg())   # duplicate — must be idempotent
        self.assertEqual(len(self.state_a.tokens), 2)
        self.assertEqual(len(self.state_b.tokens), 2)


if __name__ == "__main__":
    unittest.main()
