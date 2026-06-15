#!/usr/bin/env python3
# pylint: disable=duplicate-code  # ws recv loop mirrors live_bot.py by design
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
  python3 tradinebotte-polymarket/feed.py
  python3 tradinebotte-polymarket/feed.py --verbose
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 python3 bot/feed.py
"""

import argparse, asyncio, json, logging, os, sys, time
from typing import Any
import aiohttp, websockets, zmq, zmq.asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_polymarket as api
from tradinetools import heartbeat_loop, control_loop
from tradinetools.zmq import make_pub, default_ipc_addr
from tradinetools.logging import setup_logger
from tradinetools.schemas import MarketMessage, PingMessage

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
_PORT_BASE = os.environ.get("TRADINEBOTTE_PORT_BASE")
FEED_ADDR  = os.environ.get(
    "TRADINEBOTTE_FEED_ADDR",
    f"tcp://127.0.0.1:{_PORT_BASE}" if _PORT_BASE else default_ipc_addr("tradinebotte-feed"),
)
MARKET_REFRESH  = 30   # seconds between Gamma API polls
PING_INTERVAL   = 10   # seconds between keepalive pings to subscribers
_INSTALL_DIR    = os.environ.get("TRADINEBOTTE_DIR", os.getcwd())

logger = setup_logger("feed", os.path.join(_INSTALL_DIR, "feed.log"))

# Set by _parse_args() — used throughout for conditional debug logging.
VERBOSE = False

# ─── STATE ───────────────────────────────────────────────────────────────────
registered: dict[str, dict[str, Any]] = {}  # market_id → {up, dn, end_ms, question, start_ms}
token_meta: dict[str, str] = {}             # token_id  → market_id

# Heartbeat sentinels — updated by _run_ws, read by the heartbeat lambda.
_ws_connected: bool = False
_last_book_ts: float = 0.0
_msgs_total: int = 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="tradinebotte feed broadcaster")
    p.add_argument("--verbose", action="store_true",
                   help="Enable DEBUG logging — very detailed, for diagnostics only")
    return p.parse_args()


def _pub_json(sock: zmq.asyncio.Socket, msg: dict[str, Any]) -> None:
    """Publish one JSON message on the PUB socket (non-blocking) and log it in VERBOSE mode."""
    sock.send_json(msg, zmq.NOBLOCK)
    if VERBOSE:
        t = msg.get("t", "?")
        if t == "book":
            logger.debug("[PUB book] token=%.12s bid=%.4f ask=%.4f obi=%.3f",
                         msg.get("token_id", ""), msg.get("best_bid", 0),
                         msg.get("best_ask", 0), msg.get("obi", 0))
        elif t == "market":
            logger.debug("[PUB market] %s %s", msg.get("market_id", ""),
                         msg.get("question", "")[:60])
        elif t == "ping":
            logger.debug("[PUB ping] ts=%d", msg.get("ts", 0))


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
    if VERBOSE and new:
        logger.debug("[REGISTER] market=%s q=%s new_tokens=%d", mid, q[:40], len(new))
    return new


async def _market_refresh(sock: zmq.asyncio.Socket,
                           session: aiohttp.ClientSession,
                           ws: Any) -> None:
    """
    Background task: poll the Gamma API every MARKET_REFRESH seconds, publish
    new market registrations on the PUB socket, subscribe newly discovered tokens
    to the WebSocket, and purge expired markets from the local registry.
    """
    while True:
        await asyncio.sleep(MARKET_REFRESH)
        try:
            if VERBOSE:
                logger.debug("[REFRESH] polling Gamma API...")
            markets = await api.get_markets(session)
            if VERBOSE:
                logger.debug("[REFRESH] %d markets returned", len(markets))
            new_ids: list[str] = []
            for m in markets:
                tids = register_market(m)
                new_ids.extend(tids)
                if tids:
                    mid = api.get_market_id(m)
                    reg = registered.get(mid, {})
                    _pub_json(sock, MarketMessage(
                        market_id=mid,
                        question=reg.get("question", ""),
                        up_token_id=reg.get("up", ""),
                        dn_token_id=reg.get("dn", ""),
                        start_ms=reg.get("start_ms", 0),
                        end_ms=reg.get("end_ms", 0),
                    ).to_dict())
            if new_ids:
                for i in range(0, len(new_ids), api.WS_BATCH_SIZE):
                    await ws.send(api.make_subscribe_msg(new_ids[i:i + api.WS_BATCH_SIZE]))
                logger.info("New tokens: %d", len(new_ids))

            # Purge expired markets — 5 s grace matches live_bot's resolution window.
            now_ms = time.time() * 1000
            expired = [mid for mid, r in list(registered.items())
                       if r.get("end_ms", 0) and now_ms > r["end_ms"] + 5_000]
            for mid in expired:
                r = registered.pop(mid, {})
                for tid in (r.get("up", ""), r.get("dn", "")):
                    token_meta.pop(tid, None)
            if expired:
                logger.info("Expired markets purged: %d", len(expired))
            if VERBOSE:
                logger.debug("[REFRESH] done — %d markets, %d active tokens",
                             len(registered), len(token_meta))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Refresh error: %s", e)


async def _ping_loop(sock: zmq.asyncio.Socket) -> None:
    """Send a keepalive ping message to all subscribers every PING_INTERVAL seconds."""
    while True:
        await asyncio.sleep(PING_INTERVAL)
        try:
            _pub_json(sock, PingMessage(ts=int(time.time() * 1000)).to_dict())
        except Exception:
            pass


async def _run_ws(sock: zmq.asyncio.Socket, session: aiohttp.ClientSession) -> None:
    """
    One WebSocket session: fetch active markets, subscribe their tokens, then relay
    every book-update message to subscribers via the PUB socket until the connection
    drops.  _market_refresh and _ping_loop run as concurrent background tasks.
    """
    global _ws_connected, _last_book_ts, _msgs_total  # pylint: disable=global-statement
    if VERBOSE:
        logger.debug("[WS] calling get_markets...")
    markets = await api.get_markets(session)
    logger.info("BTC 5-min markets: %d", len(markets))
    if not markets:
        logger.warning("No markets — waiting 30s")
        await asyncio.sleep(30)
        return

    for m in markets:
        tids = register_market(m)
        if tids:
            mid = api.get_market_id(m)
            reg = registered.get(mid, {})
            _pub_json(sock, MarketMessage(
                market_id=mid,
                question=reg.get("question", ""),
                up_token_id=reg.get("up", ""),
                dn_token_id=reg.get("dn", ""),
                start_ms=reg.get("start_ms", 0),
                end_ms=reg.get("end_ms", 0),
            ).to_dict())

    all_token_ids = list(token_meta.keys())
    if not all_token_ids:
        logger.warning("No active tokens — waiting 30s")
        await asyncio.sleep(30)
        return

    logger.info("Subscribing to %d tokens...", len(all_token_ids))
    if VERBOSE:
        logger.debug("[WS] tokens: %s", all_token_ids[:6])
    async with websockets.connect(api.WS_URL, ping_interval=20, ping_timeout=10) as ws:
        for i in range(0, len(all_token_ids), api.WS_BATCH_SIZE):
            await ws.send(api.make_subscribe_msg(all_token_ids[i:i + api.WS_BATCH_SIZE]))
        logger.info("WebSocket connected — broadcasting on %s", FEED_ADDR)
        _ws_connected = True

        refresh_task = asyncio.create_task(_market_refresh(sock, session, ws))
        ping_task    = asyncio.create_task(_ping_loop(sock))

        msgs_received = 0
        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    if not token_meta:
                        logger.warning("No active tokens — reconnecting")
                        break
                    if VERBOSE:
                        logger.debug("[WS] 30s timeout (normal during quiet period), "
                                     "%d msgs received so far", msgs_received)
                    continue
                except Exception as e:
                    if VERBOSE:
                        logger.debug("[WS] recv exception: %s", e)
                    break

                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict):
                        msgs = [msgs]
                except Exception as _json_exc:
                    logger.debug("JSON parse failed: %s | raw=%r", _json_exc, raw[:200])
                    continue

                for msg in msgs:
                    if VERBOSE:
                        logger.debug("[WS RAW] %s", str(msg)[:200])
                    p = api.parse_book_update(msg)
                    if p:
                        msgs_received += 1
                        _last_book_ts = time.time()
                        _msgs_total += 1
                        if VERBOSE and msgs_received % 50 == 0:
                            logger.debug("[WS] %d book updates published", msgs_received)
                        _pub_json(sock, {"t": "book", **p})
        finally:
            _ws_connected = False
            refresh_task.cancel()
            ping_task.cancel()
            for t in (refresh_task, ping_task):
                try:
                    await t
                except asyncio.CancelledError:
                    pass


async def main() -> None:
    ctx  = zmq.asyncio.Context()
    sock = make_pub(ctx, FEED_ADDR, "FEED_ADDR")
    logger.info("Feed PUB bound on %s", FEED_ADDR)
    if VERBOSE:
        logger.debug("Verbose mode active — full DEBUG logs enabled")
        logger.debug("[ZMQ] PUB socket bound on %s", FEED_ADDR)
    await asyncio.sleep(0.5)

    _hb_task = asyncio.create_task(
        heartbeat_loop("feed", os.path.dirname(os.path.abspath(__file__)),
                       lambda: {"ws_connected": _ws_connected,
                                "last_book_ts": _last_book_ts,
                                "msgs_total": _msgs_total})
    )
    # Infra service: control surface for ping/status only (no destructive commands).
    _ctl_task = asyncio.create_task(control_loop("feed"))
    try:
        backoff = 1
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            while True:
                try:
                    await _run_ws(sock, session)
                    backoff = 1
                except Exception as e:
                    logger.warning("WS error — reconnecting in %ds: %s", backoff, e)
                    if VERBOSE:
                        import traceback
                        logger.debug("[WS] traceback: %s", traceback.format_exc())
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
    finally:
        _hb_task.cancel()
        _ctl_task.cancel()
        await asyncio.gather(_hb_task, _ctl_task, return_exceptions=True)


if __name__ == "__main__":
    args = _parse_args()
    if args.verbose:
        VERBOSE = True
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    asyncio.run(main())
