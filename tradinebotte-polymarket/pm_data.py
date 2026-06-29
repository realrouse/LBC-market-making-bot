"""
Polymarket plugin — the data plane (Plan D step 4b).

The Polymarket book/market-update path extracted from the universal entrypoint: the
token-keyed `handle_book_update` dispatch, the snapshot writer, market discovery
(`register_market` / `purge_expired_markets` / `_register_market_from_feed`), and the
two data sources (the direct WebSocket lifecycle `ws_loop`/`_run_ws`/`_market_refresh_loop`
and the shared-feed consumer `feed_consumer_loop`).

`state` is duck-typed (the entrypoint's BotState): these read/mutate `state.tokens`,
`state.connector`, `state.strategy`, `state.config`, `state.conn`, etc., but import no
entrypoint type — so live_bot re-exports them and callers (incl. account_bot) are
unchanged. Shipped flat beside live_bot.py. Logs to the entrypoint's "live" logger.
"""

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
import websockets

from botcore.persistence import _persist_snapshot
from bot_utils import print_dashboard
from pm_types import TokenState
from tradinetools.zmq import make_sub
from tradinetools.schemas import MarketMessage

logger = logging.getLogger("live")


async def handle_book_update(state: Any, parsed: dict[str, Any]) -> None:
    """Apply a parsed book update to the token's state, then check for signals."""
    # Capture the monotonic clock as early as possible — before even the token
    # lookup — so t_ws represents the true start of processing this WS message.
    t_ws = time.monotonic()
    ts = state.tokens.get(parsed["token_id"])
    if not ts: return
    ts.best_bid  = parsed["best_bid"]
    ts.best_ask  = parsed["best_ask"]
    ts.spread    = parsed["spread"]
    ts.bid_vol   = parsed["bid_vol"]
    ts.ask_vol   = parsed["ask_vol"]
    ts.obi       = parsed["obi"]
    ts.last_update_ts = time.time()
    state.last_book_ts = ts.last_update_ts
    # Dispatch to the active strategy — always set (ThresholdStrategy by default in
    # BotState; GridStrategy/SwingStrategy for "grid"/"swing"). The threshold path now
    # lives inside ThresholdStrategy.on_book_update, so this dispatch is strategy-agnostic.
    await state.strategy.on_book_update(state, ts, _t_ws=t_ws)
    now = time.time()
    if now - ts.last_snapshot_ts >= state.config.snapshot_interval:
        ts.bid_history.append(ts.best_bid)   # smoothing history — kept even when
        ts.obi_history.append(ts.obi)        # snapshots are disabled (strategy input)
        _persist_snapshot(state, lambda: save_snapshot(state, ts))
        ts.last_snapshot_ts = now


def save_snapshot(state: Any, ts: TokenState) -> None:
    """Insert a snapshot row without committing — caller batches commits via handle_book_update."""
    state.conn.execute(
        "INSERT INTO snapshots (ts_ms, market_id, token_id, direction, "
        "secs_remaining, best_bid, best_ask, spread, ask_vol, obi, has_open_trade) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time() * 1000), ts.market_id, ts.token_id, ts.direction,
         ts.secs_remaining, ts.best_bid, ts.best_ask, ts.spread, ts.ask_vol, ts.obi,
         1 if ts.market_id in state.open_trades else 0)
    )


def purge_expired_markets(state: Any) -> int:
    """
    Remove tokens whose market has ended (plus grace period) and has no open
    trade, cleaning up tokens, market_tokens, and signalled in one pass.
    Returns the number of tokens removed.
    """
    expired = [tid for tid, ts in list(state.tokens.items())
               if ts.market_ended and ts.market_id not in state.open_trades]
    for tid in expired:
        ts = state.tokens.pop(tid, None)
        if ts:
            state.market_tokens.pop(ts.market_id, None)
            state.signalled.discard(ts.market_id)
    return len(expired)


def register_market(state: Any, market: dict[str, Any]) -> list[str]:
    """
    Add a market's UP and DOWN tokens to the state if not already tracked.
    Skips markets that have already ended. Returns a list of newly added token IDs
    so the caller can subscribe them to the WebSocket.
    """
    api = state.connector
    mid = api.get_market_id(market)
    if not mid: return []
    up = api.get_up_token_id(market)
    dn = api.get_down_token_id(market)
    if not up or not dn: return []
    sm = api.get_market_start_ts_ms(market)
    em = api.get_market_end_ts_ms(market)
    if em and time.time() * 1000 > em: return []
    q = api.get_market_question(market)[:80]
    new = []
    for tid, d in ((up, "UP"), (dn, "DOWN")):
        if tid not in state.tokens:
            state.tokens[tid] = TokenState(tid, mid, d, q, sm, em,
                                              vol_window=state.config.vol_window)
            new.append(tid)
    state.market_tokens[mid] = {"UP": up, "DOWN": dn}
    return new


def _register_market_from_feed(state: Any, msg: dict[str, Any]) -> None:
    """Register a market's UP/DOWN tokens from a feed 'market' message."""
    m = MarketMessage.from_dict(msg)
    if not m.market_id or not m.up_token_id or not m.dn_token_id:
        return
    if m.end_ms and time.time() * 1000 > m.end_ms:
        return
    q = (m.question or "")[:80]
    for tid, d in ((m.up_token_id, "UP"), (m.dn_token_id, "DOWN")):
        if tid not in state.tokens:
            state.tokens[tid] = TokenState(tid, m.market_id, d, q, m.start_ms, m.end_ms,
                                           vol_window=state.config.vol_window)
    state.market_tokens[m.market_id] = {"UP": m.up_token_id, "DOWN": m.dn_token_id}


async def ws_loop(state: Any, session: aiohttp.ClientSession) -> None:
    """
    Outer reconnection loop with exponential backoff.
    Doubles the wait time on each failure, capped at 60 seconds, to avoid
    hammering the WebSocket endpoint after network disruptions.
    """
    backoff = 1
    while True:
        try:
            await _run_ws(state, session)
            backoff = 1
        except Exception as e:
            logger.warning("WS error — reconnecting in %ds: %s", backoff, e, exc_info=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _market_refresh_loop(state: Any, session: aiohttp.ClientSession, ws: Any) -> None:
    """
    Background task: polls the exchange API every market_refresh seconds and
    subscribes newly discovered tokens while the main recv loop continues
    processing messages uninterrupted.
    """
    api = state.connector
    while True:
        await asyncio.sleep(state.config.market_refresh)
        try:
            nm = await api.get_markets(
                session,
                tag_id=state.config.market_tag_id,
                window_minutes=state.config.market_window_mins,
            )
            if nm is None:
                # API/network error (not an empty window) — keep current tokens, retry
                # next cycle. get_markets used to mask this as [], silently dropping markets.
                logger.warning("Market refresh: get_markets API error — keeping current tokens")
                continue
            ni = []
            for m in nm:
                ni.extend(register_market(state, m))
            if ni:
                for i in range(0, len(ni), api.WS_BATCH_SIZE):
                    await ws.send(api.make_subscribe_msg(ni[i:i + api.WS_BATCH_SIZE]))
                logger.info("New tokens: %d", len(ni))

            n = purge_expired_markets(state)
            if n:
                logger.info("Expired tokens purged: %d", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("Market refresh error: %s", e, exc_info=True)


async def _run_ws(state: Any, session: aiohttp.ClientSession) -> None:
    """
    One WebSocket session: fetch markets, subscribe to their tokens, then
    process messages until the connection drops.

    Market discovery runs in _market_refresh_loop (background task) so the
    recv loop is never blocked during the exchange API HTTP poll.

    The recv() timeout is 30s with continue rather than break: ping_interval=20
    / ping_timeout=10 already detects dead connections via WebSocket ping/pong,
    so a recv timeout just means no market messages arrived — normal between
    5-minute candles. We only reconnect if all tracked markets have expired.
    """
    api = state.connector
    markets = await api.get_markets(
        session,
        tag_id=state.config.market_tag_id,
        window_minutes=state.config.market_window_mins,
    )
    if markets is None:
        # API/network error — distinct from an empty window. Retry soon (don't treat a
        # failure as "no markets"; `is None` not falsiness, which would collide with []).
        logger.warning("get_markets API error — retrying shortly")
        await asyncio.sleep(5)
        return
    if not markets:
        logger.warning("No markets in window — waiting 30s")
        await asyncio.sleep(30)
        return

    new_ids = []
    for m in markets:
        new_ids.extend(register_market(state, m))

    # Include all non-expired tokens already in state, not just newly discovered.
    all_token_ids = list(new_ids)
    for tid, ts in state.tokens.items():
        if not ts.market_ended and tid not in all_token_ids:
            all_token_ids.append(tid)

    if not all_token_ids:
        logger.warning("No active tokens — waiting 30s")
        await asyncio.sleep(30)
        return

    state.session = session
    logger.info("Subscribing to %d tokens...", len(all_token_ids))

    async with websockets.connect(api.WS_URL, ping_interval=20, ping_timeout=10) as ws:
        for i in range(0, len(all_token_ids), api.WS_BATCH_SIZE):
            await ws.send(api.make_subscribe_msg(all_token_ids[i:i + api.WS_BATCH_SIZE]))
        ld = time.time()
        logger.info("WebSocket connected")
        refresh_task = asyncio.create_task(_market_refresh_loop(state, session, ws))

        try:
            while True:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    if not any(not t.market_ended for t in state.tokens.values()):
                        logger.warning("No active tokens — reconnecting")
                        break
                    continue
                except Exception as _exc:
                    logger.warning("Unexpected WS recv exception", exc_info=True)
                    break

                now = time.time()
                if now - ld >= state.config.dashboard_interval:
                    ld = now
                    print_dashboard(state, state.config)

                try:
                    msgs = json.loads(raw)
                    if isinstance(msgs, dict): msgs = [msgs]
                except Exception as _json_exc:
                    logger.debug("JSON parse failed: %s | raw=%r", _json_exc, raw[:200])
                    continue

                for msg in msgs:
                    p = api.parse_book_update(msg)
                    if p: await handle_book_update(state, p)
        finally:
            refresh_task.cancel()
            try:
                await refresh_task
            except asyncio.CancelledError:
                pass


async def feed_consumer_loop(state: Any, feed_addr: str) -> None:
    """Consume market + book updates from the shared feed and dispatch to the same
    strategy handlers as the direct WS path. SUB-and-warn only (no feed auto-start)."""
    import zmq.asyncio  # noqa: PLC0415
    ctx  = zmq.asyncio.Context()
    sock = make_sub(ctx, feed_addr)
    logger.info("Data source: shared feed %s (consumer mode — no direct WS)", feed_addr)
    last_msg = time.time()
    try:
        while True:
            try:
                raw = await asyncio.wait_for(sock.recv_json(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("No feed message for %.0fs — is the feed running on %s?",
                               time.time() - last_msg, feed_addr)
                continue
            last_msg = time.time()
            t = raw.get("t")
            if t == "market":
                _register_market_from_feed(state, raw)
            elif t == "book":
                if raw.get("token_id", "") not in state.tokens:
                    continue
                await handle_book_update(state, {k: v for k, v in raw.items() if k != "t"})
            # t == "ping": liveness only
    finally:
        sock.close(linger=0)
        ctx.term()
