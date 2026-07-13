#!/usr/bin/env python3
"""cex_feed.py — shared CEX market-data feed (Binance + MEXC → ZeroMQ).

Opens ONE public WebSocket per (connector, symbol) and republishes every normalized
book update over a ZeroMQ PUB socket. grid/swing bots consume this feed (data plane)
instead of each opening their own exchange WS; order placement stays per-bot with
its own credentials (order plane).

One symbol per exchange keeps make_subscribe_msg in object form (MEXC rejects arrays).
Each exchange runs as an independent task — one dropping does not blind the other.

Published message:
  {"t": "book", "exchange": "binance", "symbol": "BTCUSDT",
   "best_bid": ..., "best_ask": ..., "spread": ..., "bid_vol": ..., "ask_vol": ...,
   "obi": ...}

Usage:
  python3 tradinebotte-cex/cex_feed.py
  TRADINEBOTTE_CEX_FEEDS='binance:BTCUSDT,mexc_futures:BTC_USDT' python3 cex_feed.py
"""

import argparse
import asyncio
import json
import os
import sys
import time

import websockets
import zmq
import zmq.asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tradinetools import heartbeat_loop, control_loop, resolve_bot_id  # noqa: E402
from tradinetools.zmq import make_pub, PORT_CEX_FEED     # noqa: E402
from tradinetools.logging import setup_logger            # noqa: E402

_INSTALL_DIR = os.environ.get("TRADINEBOTTE_DIR", os.getcwd())
# Fleet join key: generated unique bot_id (readable prefix + suffix), persisted per-role.
_BOT_ID = resolve_bot_id(_INSTALL_DIR, "cexfeed", "infra", "cexfeed")
FEED_ADDR = os.environ.get("TRADINEBOTTE_CEX_FEED_ADDR", f"tcp://127.0.0.1:{PORT_CEX_FEED}")
# "binance:BTCUSDT,mexc:BTCUSDT,mexc_futures:BTC_USDT" — one symbol per (exchange).
# Every external CEX data source is fetched once here and fanned out; bots never open
# their own WS. Consumers filter by (exchange, symbol), so multiple exchanges may
# publish the same symbol (binance + mexc spot BTCUSDT) without cross-contamination.
# mexc = MEXC spot (protobuf WS, binary frames — see api_mexc.WS_BINARY),
# mexc_futures = MEXC perp (JSON WS).
_FEEDS_ENV = os.environ.get(
    "TRADINEBOTTE_CEX_FEEDS", "binance:BTCUSDT,mexc:BTCUSDT,mexc:LBCUSDT,mexc_futures:BTC_USDT")
FEEDS = [tuple(s.split(":", 1)) for s in _FEEDS_ENV.split(",") if ":" in s]

logger = setup_logger("cex_feed", os.path.join(_INSTALL_DIR, "cex_feed.log"))

# Per-exchange last-publish timestamps for the heartbeat.
_last_pub: dict[str, float] = {}


async def _ws_keepalive(ws, ping_factory, interval: float = 15.0) -> None:
    """Send the connector's app-level ping every `interval`s. Some exchanges (MEXC
    spot + futures) close an idle public WS with code 1005 despite protocol-level
    pings, which starves the depth stream. `except Exception` (not BaseException)
    lets the recv loop drive reconnect on a closed socket while honouring cancel."""
    try:
        while True:
            await asyncio.sleep(interval)
            await ws.send(ping_factory())
    except Exception:  # noqa: BLE001  pylint: disable=broad-exception-caught
        pass


async def _exchange_task(pub: zmq.asyncio.Socket, connector: str, symbol: str) -> None:
    """Maintain one public WS to `connector` for `symbol`, republishing book updates.
    Reconnects with exponential backoff; never raises out (one leg stays isolated)."""
    from connectors import load as _load  # noqa: PLC0415
    api = _load(connector)
    backoff = 1
    while True:
        try:
            async with websockets.connect(api.WS_URL, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(api.make_subscribe_msg([symbol]))
                logger.info("cex_feed: %s %s connected (%s)", connector, symbol, api.WS_URL)
                backoff = 1
                # App-level keepalive for connectors that require it (MEXC).
                _ping = getattr(api, "make_ping_msg", None)
                ka = asyncio.create_task(_ws_keepalive(ws, _ping)) if _ping else None
                # Binary connectors (e.g. MEXC spot protobuf) hand the raw frame to
                # parse_book_update; the JSON path below is unchanged for the others.
                binary = getattr(api, "WS_BINARY", False)
                try:
                    while True:
                        raw = await ws.recv()
                        if binary:
                            # Text frames (subscribe ack) → parse_book_update returns None.
                            msgs = [raw]
                        else:
                            try:
                                msgs = json.loads(raw)
                            except Exception:  # pylint: disable=broad-exception-caught
                                continue
                            if isinstance(msgs, dict):
                                msgs = [msgs]
                        for m in msgs:
                            p = api.parse_book_update(m)
                            if not p:
                                continue
                            out = {"t": "book", "exchange": connector, "symbol": symbol,
                                   "best_bid": p["best_bid"], "best_ask": p["best_ask"],
                                   "spread": p.get("spread", 0.0), "bid_vol": p.get("bid_vol", 0.0),
                                   "ask_vol": p.get("ask_vol", 0.0), "obi": p.get("obi", 0.0)}
                            try:
                                pub.send_json(out, zmq.NOBLOCK)
                                _last_pub[connector] = time.time()
                            except zmq.ZMQError:
                                pass
                finally:
                    if ka is not None:
                        ka.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("cex_feed %s %s WS error — reconnect in %ds: %s",
                           connector, symbol, backoff, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def main() -> None:
    ctx = zmq.asyncio.Context()
    pub = make_pub(ctx, FEED_ADDR, "CEX_FEED")
    logger.info("cex_feed PUB on %s — feeds: %s", FEED_ADDR, FEEDS)
    tasks = [asyncio.create_task(_exchange_task(pub, c, s)) for c, s in FEEDS]
    tasks.append(asyncio.create_task(
        heartbeat_loop(_BOT_ID, _INSTALL_DIR,
                       lambda: {"exchanges": list(_last_pub.keys()),
                                "last_pub_ts": max(_last_pub.values(), default=0.0)})))
    tasks.append(asyncio.create_task(control_loop(_BOT_ID)))
    try:
        await asyncio.gather(*tasks)
    finally:
        pub.close(linger=0)
        ctx.term()


if __name__ == "__main__":
    argparse.ArgumentParser(description="tradinebotte CEX market-data feed").parse_args()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped.")
