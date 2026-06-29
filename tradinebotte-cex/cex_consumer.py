"""
CEX feed consumer — the CEX data path (Plan D step 4b).

Extracted from the universal entrypoint (live_bot.py) into the CEX package so the
polymarket entrypoint file no longer carries CEX-specific code. A CEX bot (grid/swing)
runs live_bot.py, which lazy-imports `cex_feed_consumer_loop` here only when
data_source=cex_feed — polymarket-only accounts never import it.

`state` is duck-typed (the same BotState the entrypoint builds): this loop reads
`state.strategy`, `state.config`, `state.conn`, `state.last_book_ts` — no polymarket
type. Logs to the entrypoint's "live" logger so output stays in one place.
"""

import asyncio
import logging
import time
from typing import Any

from tradinetools.zmq import make_sub
from botcore.persistence import _persist_snapshot

logger = logging.getLogger("live")


def save_cex_snapshot(state: Any, symbol: str, book: Any) -> None:
    """Insert a CEX book snapshot (grid/swing consumer mode) without committing.

    cex_feed_consumer_loop bypasses handle_book_update — and hence save_snapshot — so
    CEX bots need their own writer. The snapshots table is polymarket-shaped, so reuse
    it with the same placeholders the pre-data-plane direct-WS path wrote
    (market_id=token_id=symbol, direction='UP', secs_remaining=9999.0, has_open_trade=0),
    keeping the rows readable by backtest_grid / backtest_swing_dca and aligned with the
    historical CEX snapshots collected before 2026-06-16."""
    state.conn.execute(
        "INSERT INTO snapshots (ts_ms, market_id, token_id, direction, "
        "secs_remaining, best_bid, best_ask, spread, ask_vol, obi, has_open_trade) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (int(time.time() * 1000), symbol, symbol, "UP", 9999.0,
         book.best_bid, book.best_ask, book.spread, book.ask_vol, book.obi, 0)
    )


async def cex_feed_consumer_loop(state: Any, feed_addr: str, symbol: str,
                                 exchange: str | None = None) -> None:
    """Consume CEX book updates for `symbol` from the shared cex_feed and drive the
    grid/swing strategy directly (no handle_book_update — CEX bots don't need its
    polymarket token bookkeeping, and a token_id mismatch there fails silently).
    Order placement stays per-bot. SUB-and-warn only (no feed auto-start).

    Filters on (exchange, symbol): the shared cex_feed multiplexes several exchanges,
    and more than one can publish the SAME symbol (e.g. binance:BTCUSDT and
    mexc:BTCUSDT). `exchange` (the bot's connector) selects the right source; without
    it a 2nd BTCUSDT source would contaminate this bot's book stream."""
    import zmq.asyncio  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415
    ctx  = zmq.asyncio.Context()
    sock = make_sub(ctx, feed_addr)
    logger.info("Data source: shared CEX feed %s exchange=%s symbol=%s (consumer mode — no direct WS)",
                feed_addr, exchange or "any", symbol)
    last_msg = time.time()
    last_snap = 0.0
    try:
        while True:
            try:
                raw = await asyncio.wait_for(sock.recv_json(), timeout=30)
            except asyncio.TimeoutError:
                logger.warning("No cex_feed message for %.0fs — is cex_feed running on %s?",
                               time.time() - last_msg, feed_addr)
                continue
            last_msg = time.time()
            if raw.get("t") != "book" or raw.get("symbol") != symbol:
                continue
            if exchange is not None and raw.get("exchange") != exchange:
                continue
            ts = SimpleNamespace(
                best_bid=float(raw["best_bid"]), best_ask=float(raw["best_ask"]),
                spread=float(raw.get("spread", 0.0)), bid_vol=float(raw.get("bid_vol", 0.0)),
                ask_vol=float(raw.get("ask_vol", 0.0)), obi=float(raw.get("obi", 0.0)),
                last_update_ts=time.time())
            state.last_book_ts = time.time()
            if state.strategy is not None:
                await state.strategy.on_book_update(state, ts)
            # Record a snapshot via the shared persistence step — this loop bypasses
            # handle_book_update, so it calls _persist_snapshot itself (the same named
            # step the WS path uses); only the per-loop cadence gate lives here.
            now = time.time()
            if now - last_snap >= state.config.snapshot_interval:
                _persist_snapshot(state, lambda: save_cex_snapshot(state, symbol, ts))
                last_snap = now
    finally:
        sock.close(linger=0)
        ctx.term()
