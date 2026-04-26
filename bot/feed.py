#!/usr/bin/env python3
"""
feed.py — Shared WebSocket feed broadcaster (Option B multi-bot architecture)

Maintains a single WebSocket connection to Polymarket and republishes every
book update and market registration event over a ZeroMQ PUB socket.  One or
more account_bot.py processes subscribe to this feed and run independent
trading strategies without opening additional connections to the exchange.

Full architecture documentation: docs/multi.md

Message types published:
  {"t": "market", "market_id": "...", "question": "...",
   "up_token_id": "...", "dn_token_id": "...",
   "start_ms": ..., "end_ms": ...}
  {"t": "book",  "token_id": "...", "best_bid": ..., "best_ask": ...,
   "spread": ..., "bid_vol": ..., "ask_vol": ..., "obi": ...}
  {"t": "ping",  "ts": ...}

Usage:
  python3 bot/feed.py
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 python3 bot/feed.py
"""

import asyncio, json, logging, os, sys, time
from typing import Any
import aiohttp, websockets, zmq, zmq.asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_polymarket as api

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR", "tcp://127.0.0.1:5557")
MARKET_REFRESH  = 30   # seconds between Gamma API polls
PING_INTERVAL   = 10   # seconds between keepalive pings to subscribers
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("feed")

# ─── STATE ───────────────────────────────────────────────────────────────────
# registered: market_id → {"up": token_id, "dn": token_id, "end_ms": float}
# token_meta: token_id  → market_id (for routing book updates)
registered: dict[str, dict[str, Any]] = {}
token_meta: dict[str, str] = {}


def _pub_json(sock: zmq.asyncio.Socket, msg: dict[str, Any]) -> None:
    sock.send_json(msg, zmq.NOBLOCK)


def register_market(market: dict[str, Any]) -> list[str]:
    """
    Extract token IDs from a market dict and record them in state.
    Returns newly discovered token IDs (not yet in token_meta).
    """
    mid = api.get_market_id(market)
    if not mid:
        return []
    up = api.get_up_token_id(market)
    dn = api.get_down_token_id(market)
    if not up or not dn:
        return []
    em = api.get_market_end_ts_ms(market)
    if em and time.time() * 1000 > em:
        return []
    sm = api.get_market_start_ts_ms(market)
    q  = api.get_market_question(market)[:80]
    new = []
    for tid in (up, dn):
        if tid not in token_meta:
            token_meta[tid] = mid
            new.append(tid)
    if mid not in registered:
        registered[mid] = {"up": up, "dn": dn, "end_ms": em, "question": q, "start_ms": sm}
    return new


async def _market_refresh(sock: zmq.asyncio.Socket,
                           session: aiohttp.ClientSession,
                           ws: Any) -> None:
    while True:
        await asyncio.sleep(MARKET_REFRESH)
        try:
            markets = await api.get_markets(session)
            new_ids: list[str] = []
            for m in markets:
                tids = register_market(m)
                new_ids.extend(tids)
                if tids:
                    mid = api.get_market_id(m)
                    reg = registered.get(mid, {})
                    _pub_json(sock, {
                        "t": "market",
                        "market_id": mid,
                        "question":  reg.get("question", ""),
                        "up_token_id": reg.get("up", ""),
                        "dn_token_id": reg.get("dn", ""),
                        "start_ms":  reg.get("start_ms", 0),
                        "end_ms":    reg.get("end_ms", 0),
                    })
            if new_ids:
                for i in range(0, len(new_ids), api.WS_BATCH_SIZE):
                    await ws.send(api.make_subscribe_msg(new_ids[i:i + api.WS_BATCH_SIZE]))
                logger.info("Nouveaux tokens : %d", len(new_ids))

            # Purge expired markets.
            now_ms = time.time() * 1000
            expired = [mid for mid, r in list(registered.items())
                       if r.get("end_ms", 0) and now_ms > r["end_ms"]]
            for mid in expired:
                r = registered.pop(mid, {})
                for tid in (r.get("up", ""), r.get("dn", "")):
                    token_meta.pop(tid, None)
            if expired:
                logger.info("Marches expires purges : %d", len(expired))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Refresh erreur : %s", e)


async def _ping_loop(sock: zmq.asyncio.Socket) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            _pub_json(sock, {"t": "ping", "ts": int(time.time() * 1000)})
        except Exception:
            pass


async def _run_ws(sock: zmq.asyncio.Socket, session: aiohttp.ClientSession) -> None:
    markets = await api.get_markets(session)
    if not markets:
        logger.warning("Aucun marche — attente 30s")
        await asyncio.sleep(30)
        return

    for m in markets:
        tids = register_market(m)
        if tids:
            mid = api.get_market_id(m)
            reg = registered.get(mid, {})
            _pub_json(sock, {
                "t": "market",
                "market_id": mid,
                "question":  reg.get("question", ""),
                "up_token_id": reg.get("up", ""),
                "dn_token_id": reg.get("dn", ""),
                "start_ms":  reg.get("start_ms", 0),
                "end_ms":    reg.get("end_ms", 0),
            })

    all_token_ids = list(token_meta.keys())
    if not all_token_ids:
        logger.warning("Aucun token actif — attente 30s")
        await asyncio.sleep(30)
        return

    logger.info("Souscription %d tokens...", len(all_token_ids))
    async with websockets.connect(api.WS_URL, ping_interval=20, ping_timeout=10) as ws:
        for i in range(0, len(all_token_ids), api.WS_BATCH_SIZE):
            await ws.send(api.make_subscribe_msg(all_token_ids[i:i + api.WS_BATCH_SIZE]))
        logger.info("WebSocket connecte — diffusion sur %s", FEED_ADDR)

        refresh_task = asyncio.create_task(_market_refresh(sock, session, ws))
        ping_task    = asyncio.create_task(_ping_loop(sock))

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    if not token_meta:
                        logger.warning("Aucun token actif — reconnexion")
                        break
                    continue
                except Exception:
                    break

                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict):
                        msgs = [msgs]
                except Exception:
                    continue

                for msg in msgs:
                    p = api.parse_book_update(msg)
                    if p:
                        _pub_json(sock, {"t": "book", **p})
        finally:
            refresh_task.cancel()
            ping_task.cancel()
            for t in (refresh_task, ping_task):
                try:
                    await t
                except asyncio.CancelledError:
                    pass


async def main() -> None:
    ctx  = zmq.asyncio.Context()
    sock = ctx.socket(zmq.PUB)
    sock.bind(FEED_ADDR)
    logger.info("Feed PUB bind sur %s", FEED_ADDR)
    # Allow subscribers to connect before the first message.
    await asyncio.sleep(0.5)

    backoff = 1
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await _run_ws(sock, session)
                backoff = 1
            except Exception as e:
                logger.warning("WS erreur (%s) — reconnexion %ds", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    asyncio.run(main())
