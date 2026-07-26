"""MEXC spot connector for LBCUSDT (adapted from tradinebotte api_mexc, GPL-3.0)."""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp

from lbcmm.connectors.common import (
    book_snapshot,
    decimals_of,
    fmt_price,
    fmt_qty,
    hmac_sign as _sign,
    parse_levels,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://api.mexc.com"
WS_URL = "wss://wbs-api.mexc.com/ws"
WS_BINARY = True
DEFAULT_SYMBOL = "LBCUSDT"
FEE_RATE = 0.0004
MAKER_FEE_RATE = 0.0

_SYMBOL_PRECISION: dict = {}


def _write_headers(key: str) -> dict:
    return {"X-MEXC-APIKEY": key, "Content-Type": "application/json"}


def _creds(api_key=None, api_secret=None):
    return (
        api_key or os.environ.get("MEXC_API_KEY", ""),
        api_secret or os.environ.get("MEXC_API_SECRET", ""),
    )


def is_live(api_key=None, api_secret=None) -> bool:
    k, s = _creds(api_key, api_secret)
    return bool(k and s)


async def get_symbol_precision(session, symbol):
    sym = str(symbol).split(":", maxsplit=1)[0]
    cached = _SYMBOL_PRECISION.get(sym)
    if cached is not None:
        return cached
    try:
        async with session.get(
            f"{BASE_URL}/api/v3/exchangeInfo",
            params={"symbol": sym},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
        s = data["symbols"][0]
        prec = (int(s["quotePrecision"]), decimals_of(s["baseSizePrecision"]))
        _SYMBOL_PRECISION[sym] = prec
        logger.info("MEXC precision [%s]: price=%ddp qty=%ddp", sym, prec[0], prec[1])
        return prec
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_symbol_precision failed [%s]: %s", sym, e)
        return None


def make_subscribe_msg(symbols):
    params = [
        f"spot@public.limit.depth.v3.api.pb@{s.split(':')[0]}@5"
        for s in symbols
    ]
    return json.dumps({"method": "SUBSCRIPTION", "params": params})


def make_ping_msg():
    return json.dumps({"method": "PING"})


def parse_book_update(msg):
    if isinstance(msg, (bytes, bytearray)):
        return _parse_pb_depth(msg)
    if isinstance(msg, dict):
        data = msg.get("d")
        symbol = msg.get("s", DEFAULT_SYMBOL)
        if not data:
            return None
        bids = sorted(parse_levels(data.get("bids") or []), reverse=True)
        asks = sorted(parse_levels(data.get("asks") or []))
        if not bids and not asks:
            return None
        return book_snapshot(bids, asks, symbol)
    return None


def _parse_pb_depth(raw):
    try:
        from lbcmm.connectors import mexc_spot_depth_pb2 as _pb  # noqa: PLC0415
    except ImportError:
        # generated module may be importable as top-level name
        try:
            import mexc_spot_depth_pb2 as _pb  # type: ignore  # noqa: PLC0415
        except ImportError:
            logger.error("mexc_spot_depth_pb2 not importable — install protobuf")
            return None
    try:
        wrapper = _pb.PushDataV3ApiWrapper()
        wrapper.ParseFromString(bytes(raw))
    except Exception as exc:  # pylint: disable=broad-except
        logger.debug("MEXC pb decode failed: %s", exc)
        return None
    depth = wrapper.publicLimitDepths
    if not depth.bids and not depth.asks:
        return None
    symbol = wrapper.symbol or DEFAULT_SYMBOL
    bids = sorted(parse_levels([[lv.price, lv.quantity] for lv in depth.bids]), reverse=True)
    asks = sorted(parse_levels([[lv.price, lv.quantity] for lv in depth.asks]))
    if not bids and not asks:
        return None
    return book_snapshot(bids, asks, symbol)


async def get_book_ticker(session, symbol=DEFAULT_SYMBOL):
    try:
        async with session.get(
            f"{BASE_URL}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        return {
            "symbol": symbol,
            "best_bid": float(data.get("bidPrice", 0)),
            "best_ask": float(data.get("askPrice", 0)),
            "bid_qty": float(data.get("bidQty", 0)),
            "ask_qty": float(data.get("askQty", 0)),
        }
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("MEXC bookTicker error: %s", e)
        return None


async def get_depth(session, symbol=DEFAULT_SYMBOL, limit: int = 100):
    """Public order book. Returns {bids:[(p,q)], asks:[(p,q)], mid, ...} or None."""
    try:
        async with session.get(
            f"{BASE_URL}/api/v3/depth",
            params={"symbol": symbol, "limit": limit},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("MEXC depth HTTP %s", resp.status)
                return None
            data = await resp.json(content_type=None)
        bids = sorted(parse_levels(data.get("bids") or []), reverse=True)
        asks = sorted(parse_levels(data.get("asks") or []))
        if not bids and not asks:
            return None
        snap = book_snapshot(bids, asks, symbol, depth=min(20, len(bids), len(asks)) or 5)
        mid = 0.0
        if snap["best_bid"] and snap["best_ask"] and snap["best_ask"] != float("inf"):
            mid = (snap["best_bid"] + snap["best_ask"]) / 2.0
        snap["mid"] = mid
        snap["bids"] = bids
        snap["asks"] = asks
        return snap
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("MEXC depth error: %s", e)
        return None


def depth_within_pct(book: dict, pct: float = 2.0) -> dict:
    """Sum quote notional of bids within -pct% of mid and asks within +pct% of mid."""
    mid = book.get("mid") or 0.0
    if mid <= 0:
        bb, ba = book.get("best_bid", 0), book.get("best_ask", 0)
        if bb and ba and ba != float("inf"):
            mid = (bb + ba) / 2.0
    if mid <= 0:
        return {"mid": 0.0, "bid_usd": 0.0, "ask_usd": 0.0, "pct": pct}
    lo = mid * (1.0 - pct / 100.0)
    hi = mid * (1.0 + pct / 100.0)
    bid_usd = sum(p * q for p, q in book.get("bids", []) if p >= lo)
    ask_usd = sum(p * q for p, q in book.get("asks", []) if p <= hi)
    return {"mid": mid, "bid_usd": bid_usd, "ask_usd": ask_usd, "pct": pct, "lo": lo, "hi": hi}


# CoinGecko-style ladder for the public depth expander UI
DEPTH_LADDER_PCTS = (2.0, 5.0, 10.0, 15.0, 30.0, 50.0, 75.0)


def depth_ladder(book: dict, pcts: tuple | list | None = None) -> list[dict]:
    """depth_within_pct for each band (2%, 5%, … 75%)."""
    out = []
    for pct in (pcts or DEPTH_LADDER_PCTS):
        d = depth_within_pct(book, float(pct))
        out.append(d)
    return out


async def post_order(
    session,
    symbol,
    price,
    size_usdc=None,
    *,
    api_key=None,
    api_secret=None,
    side="BUY",
    quantity=None,
    order_type="LIMIT_MAKER",
    paper: bool = False,
):
    _key, _secret = _creds(api_key, api_secret)
    _sym = str(symbol).split(":", maxsplit=1)[0]
    _side = side.upper()

    if paper or not _key or not _secret:
        logger.info("MEXC paper order %s %s @ %s", _side, _sym, price)
        return f"sim_{uuid.uuid4().hex[:12]}"

    prec = await get_symbol_precision(session, _sym)
    if prec is None:
        logger.error("MEXC — no precision for %s, refusing order", _sym)
        return None
    price_dec, qty_dec = prec

    try:
        if quantity is None:
            if size_usdc is None:
                return None
            quantity = size_usdc / price
        params = {
            "symbol": _sym,
            "side": _side,
            "type": order_type,
            "quantity": fmt_qty(quantity, qty_dec),
            "price": fmt_price(price, price_dec),
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.post(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers=_write_headers(_key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.error(
                    "MEXC order error %d: code=%s msg=%s",
                    resp.status,
                    data.get("code"),
                    data.get("msg", str(data)[:200]),
                )
                return None
            return str(data.get("orderId", "")) or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC post_order error: %s", e)
        return None


async def cancel_order(session, symbol, order_id, *, api_key=None, api_secret=None):
    if str(order_id).startswith("sim_"):
        return True
    _key, _secret = _creds(api_key, api_secret)
    if not _key or not _secret:
        return True
    try:
        params = {
            "symbol": str(symbol).split(":", maxsplit=1)[0],
            "orderId": str(order_id),
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.delete(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers=_write_headers(_key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("MEXC cancel error %d: %s", resp.status, data)
                return False
            return bool(data.get("orderId"))
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC cancel_order: %s", e)
        return False


async def get_open_orders(session, symbol, *, api_key=None, api_secret=None):
    _key, _secret = _creds(api_key, api_secret)
    if not _key or not _secret:
        return []
    try:
        params = {
            "symbol": str(symbol).split(":", maxsplit=1)[0],
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.get(
            f"{BASE_URL}/api/v3/openOrders",
            params=params,
            headers={"X-MEXC-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                return None
            return [
                {
                    "order_id": str(o.get("orderId", "")),
                    "side": str(o.get("side", "")),
                    "price": float(o.get("price", 0)),
                    "qty": float(o.get("origQty", 0)),
                    "status": str(o.get("status", "")),
                    "executed_qty": float(o.get("executedQty", 0) or 0),
                }
                for o in data
            ]
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_open_orders: %s", e)
        return None


async def get_account(session, *, api_key=None, api_secret=None):
    _key, _secret = _creds(api_key, api_secret)
    if not _key or not _secret:
        return None
    try:
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params, _secret)
        async with session.get(
            f"{BASE_URL}/api/v3/account",
            params=params,
            headers={"X-MEXC-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                return None
            balances = {
                b["asset"]: {
                    "free": float(b.get("free", 0.0)),
                    "locked": float(b.get("locked", 0.0)),
                }
                for b in data.get("balances", [])
                if isinstance(b, dict) and b.get("asset")
            }
            return {
                "can_trade": bool(data.get("canTrade", False)),
                "balances": balances,
            }
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_account: %s", e)
        return None


def ensure_pb2_path():
    """Ensure generated protobuf module is importable."""
    here = Path(__file__).resolve().parent
    if str(here) not in os.sys.path:
        os.sys.path.insert(0, str(here))
