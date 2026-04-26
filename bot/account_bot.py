#!/usr/bin/env python3
"""
account_bot.py — Per-account trading bot, subscribes to feed.py via ZeroMQ

This process subscribes to a running feed.py PUB socket and runs the full
tradinebotte trading logic for one account in isolation.  Multiple instances
can run in parallel — each pointed at its own TRADINEBOTTE_DIR — without
sharing a WebSocket connection or interfering with each other.

Full architecture documentation: docs/multi.md

Usage:
  TRADINEBOTTE_DIR=~/account-a python3 bot/account_bot.py
  TRADINEBOTTE_DIR=~/account-b python3 bot/account_bot.py
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/acc python3 bot/account_bot.py

The TRADINEBOTTE_DIR env var must be set before import so live_bot picks up
the correct DB / log / config paths at module level.  Each account needs its
own config.json with its own private key.

Message types consumed from feed.py:
  {"t": "market", "market_id": ..., "question": ..., "up_token_id": ...,
   "dn_token_id": ..., "start_ms": ..., "end_ms": ...}
  {"t": "book",  "token_id": ..., "best_bid": ..., "best_ask": ...,
   "spread": ..., "bid_vol": ..., "ask_vol": ..., "obi": ...}
  {"t": "ping",  "ts": ...}
"""

import asyncio, json, logging, os, sys, time
import zmq, zmq.asyncio

# ─── CONFIGURE INSTALL DIR BEFORE IMPORTING live_bot ─────────────────────────
# live_bot reads TRADINEBOTTE_DIR at module level (INSTALL_DIR, DB_PATH, etc.)
# so the env var must be set before the import statement.
_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR", "tcp://127.0.0.1:5557")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import live_bot after path setup — this triggers all module-level config loads.
import live_bot as bot

logger = logging.getLogger("account")

# Reconnect timeout: if no message from the feed within this many seconds,
# warn and keep waiting (feed may reconnect to the exchange automatically).
FEED_TIMEOUT = 60  # seconds


def _register_from_market_msg(state: bot.BotState, msg: dict) -> None:
    """Build a TokenState pair from a feed "market" message."""
    mid = msg.get("market_id", "")
    up  = msg.get("up_token_id", "")
    dn  = msg.get("dn_token_id", "")
    q   = msg.get("question", "")[:80]
    sm  = int(msg.get("start_ms", 0))
    em  = int(msg.get("end_ms", 0))
    if not mid or not up or not dn:
        return
    if em and time.time() * 1000 > em:
        return
    for tid, direction in ((up, "UP"), (dn, "DOWN")):
        if tid not in state.tokens:
            state.tokens[tid] = bot.TokenState(tid, mid, direction, q, sm, em)
    state.market_tokens[mid] = {"UP": up, "DOWN": dn}


async def _run(state: bot.BotState) -> None:
    ctx  = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(_FEED_ADDR)
    sock.setsockopt(zmq.SUBSCRIBE, b"")   # subscribe to all messages
    logger.info("Connecte au feed : %s", _FEED_ADDR)

    last_msg_ts = time.time()

    while True:
        try:
            raw = await asyncio.wait_for(sock.recv_json(), timeout=FEED_TIMEOUT)
            last_msg_ts = time.time()
        except asyncio.TimeoutError:
            idle = time.time() - last_msg_ts
            logger.warning("Aucun message du feed depuis %.0fs — feed actif ?", idle)
            continue
        except Exception as e:
            logger.error("Erreur reception feed : %s", e)
            await asyncio.sleep(5)
            continue

        t = raw.get("t")
        if t == "market":
            _register_from_market_msg(state, raw)

        elif t == "book":
            token_id = raw.get("token_id", "")
            if token_id not in state.tokens:
                continue
            # Strip the "t" key before passing to handle_book_update.
            parsed = {k: v for k, v in raw.items() if k != "t"}
            await bot.handle_book_update(state, parsed)

        elif t == "ping":
            pass  # keepalive — no action needed

        # Purge expired markets periodically (re-use feed purge logic).
        now_ms = time.time() * 1000
        expired = [
            tid for tid, ts in list(state.tokens.items())
            if ts.market_ended and ts.market_id not in state.open_trades
        ]
        for tid in expired:
            ts_obj = state.tokens.pop(tid, None)
            if ts_obj:
                state.market_tokens.pop(ts_obj.market_id, None)
                state.signalled.discard(ts_obj.market_id)


async def main() -> None:
    logger.info("=" * 65)
    logger.info("  ACCOUNT BOT — dir=%s", bot.INSTALL_DIR)
    logger.info("  Feed : %s", _FEED_ADDR)
    logger.info("=" * 65)

    conn  = bot.init_db()
    state = bot.BotState(conn)
    bot.restore_state_from_db(state)

    # aiohttp session for CLOB order placement (no WS here — feed handles that).
    import aiohttp
    async with aiohttp.ClientSession() as session:
        state.session = session
        await _run(state)


if __name__ == "__main__":
    asyncio.run(main())
