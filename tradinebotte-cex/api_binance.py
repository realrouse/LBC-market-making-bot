"""
Binance spot API adapter — implements the same public interface as
api_polymarket.py: get_markets, post_order, parse_book_update, compute_fee,
and market metadata helpers.

Grid trading extensions (not in api_polymarket):
    get_order_status(session, symbol, order_id) → status string or None
    cancel_order(session, symbol, order_id)     → bool
    get_open_orders(session, symbol)            → list[dict]

Credentials: BINANCE_API_KEY and BINANCE_API_SECRET env vars,
or pass api_key / api_secret kwargs to post_order.

Compatibility note: the Polymarket signal (best_bid >= 0.96) uses a 0–1
probability scale. On Binance, best_bid is an absolute USDT price (e.g. 65000).
Strategy thresholds in strategies/*.json must be recalibrated for CEX use.

To switch live_bot.py to Binance:
    import api_binance as api   # line 62 in live_bot.py
"""

import json
import logging
import os
import time
import uuid
import aiohttp
from api_common import book_snapshot, hmac_sign as _sign, parse_levels

logger = logging.getLogger("live")

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
# Public endpoints (https://data-api.binance.vision) require no API credentials
# and are dedicated to market data — use them in simulation/no-credentials mode.
_BASE_URL_LIVE   = "https://api.binance.com"
_BASE_URL_PUBLIC = "https://data-api.binance.vision"
_WS_URL_LIVE     = "wss://stream.binance.com:9443/stream"
_WS_URL_PUBLIC   = "wss://data-stream.binance.vision/stream"

_has_creds = bool(
    os.environ.get("BINANCE_API_KEY") and os.environ.get("BINANCE_API_SECRET")
)

BASE_URL      = _BASE_URL_LIVE if _has_creds else _BASE_URL_PUBLIC
WS_URL        = _WS_URL_LIVE   if _has_creds else _WS_URL_PUBLIC
WS_BATCH_SIZE = 10  # max depth streams per WebSocket connection

DEFAULT_SYMBOL = "BTCUSDT"

# Binance taker fee: 0.1% standard. Drop to 0.075% when paying fees in BNB.
FEE_RATE = 0.001


def compute_fee(price, quantity):
    """Binance taker fee: 0.1% of notional (price × quantity in USDT)."""
    return FEE_RATE * price * quantity


# ─── MARKET METADATA ─────────────────────────────────────────────────────────
# These helpers mirror api_polymarket's interface so live_bot.py can call them
# without modification. For spot markets, "UP token" = BUY side, "DOWN" = SELL.

def get_market_id(market):
    """Return the trading symbol as the market identifier."""
    return market.get("symbol", "")


def get_market_question(market):
    """Return a human-readable market description (falls back to symbol)."""
    return market.get("question", market.get("symbol", ""))


def get_market_end_ts_ms(_market):
    """Return 0 — spot markets have no scheduled expiry."""
    return 0.0


def get_market_start_ts_ms(_market):
    """Return 0 — start time is not applicable to spot markets."""
    return 0.0


def get_up_token_id(market):
    """BUY side maps to the UP direction."""
    return market.get("symbol", "")


def get_down_token_id(market):
    """SELL side maps to the DOWN direction."""
    return market.get("symbol", "") + ":SELL"


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────

def make_subscribe_msg(symbols):
    """
    Return a JSON subscribe message for Binance combined depth streams.
    Subscribes to btcusdt@depth5@100ms (5-level book, 100 ms refresh).
    Strips any ":SELL" suffix encoded in the symbol string.
    """
    streams = [
        f"{s.lower().split(':')[0]}@depth5@100ms"
        for s in symbols
    ]
    return json.dumps({
        "method": "SUBSCRIBE",
        "params": streams,
        "id": int(time.time() * 1000) % 1_000_000,
    })


# ─── ORDER BOOK ───────────────────────────────────────────────────────────────

def parse_book_update(msg):
    """
    Parse a Binance WebSocket depth message into a normalized price snapshot.

    Handles both stream formats:
      Individual stream: {"lastUpdateId": N, "bids": [...], "asks": [...]}
      Combined stream:   {"stream": "btcusdt@depth5", "data": {...}}

    OBI (Order Book Imbalance) = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    computed over top-5 levels on each side, same as api_polymarket.

    Returns {token_id, best_bid, best_ask, spread, bid_vol, ask_vol, obi}
    or None if the message is irrelevant or malformed.
    """
    if not isinstance(msg, dict):
        return None

    # Unwrap combined stream envelope if present
    stream = msg.get("stream", "")
    data   = msg.get("data", msg)
    symbol = stream.split("@")[0].upper() if stream else DEFAULT_SYMBOL

    bids_raw = data.get("bids") or data.get("b") or []
    asks_raw = data.get("asks") or data.get("a") or []
    if not bids_raw and not asks_raw:
        return None

    bids = sorted(parse_levels(bids_raw), reverse=True)
    asks = sorted(parse_levels(asks_raw))
    if not bids and not asks:
        return None

    return book_snapshot(bids, asks, symbol)


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────

async def get_markets(session, symbol=DEFAULT_SYMBOL, **_):
    """
    Fetch the current order book ticker for the given symbol from Binance REST.
    Returns a list with one normalized market dict.
    """
    try:
        async with session.get(
            f"{BASE_URL}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Binance API error : %d", resp.status)
                return []
            # content_type=None: bypass aiohttp's MIME check — some APIs omit charset
            data = await resp.json(content_type=None)
        return [{
            "symbol": symbol,
            "question": f"BTC/USDT spot — Binance ({symbol})",
            "best_bid": float(data.get("bidPrice", 0)),
            "best_ask": float(data.get("askPrice", 0)),
            "bid_qty":  float(data.get("bidQty", 0)),
            "ask_qty":  float(data.get("askQty", 0)),
        }]
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("Binance fetch error : %s", e)
        return []


# ─── ORDER PLACEMENT ──────────────────────────────────────────────────────────

async def post_order(session, symbol, price, size_usdc, *,
                     api_key=None, api_secret=None, side="BUY",
                     private_key=None, install_dir=None):  # pylint: disable=unused-argument
    """
    Submit a LIMIT order to Binance spot.

    Args:
        symbol    : trading pair, e.g. "BTCUSDT". Append ":SELL" to force
                    the SELL side (mirrors get_down_token_id convention).
        price     : limit price in USDT
        size_usdc : notional USDT amount (BUY) or proceeds target (SELL)
        api_key   : Binance API key (falls back to BINANCE_API_KEY env var)
        api_secret: Binance API secret (falls back to BINANCE_API_SECRET)
        side      : "BUY" or "SELL" — overridden by ":SELL" suffix in symbol
        private_key / install_dir: accepted but ignored (Polymarket compat)

    Returns order ID string on success, "sim_..." in dry-run, or None on error.
    """
    _key    = api_key    or os.environ.get("BINANCE_API_KEY", "")
    _secret = api_secret or os.environ.get("BINANCE_API_SECRET", "")

    _sym  = str(symbol).split(":", maxsplit=1)[0]
    _side = "SELL" if str(symbol).endswith(":SELL") else side.upper()

    if not _key or not _secret:
        logger.warning("Binance — simulated order (BINANCE_API_KEY/SECRET not set)")
        return f"sim_{uuid.uuid4().hex[:12]}"

    try:
        # 6 decimal places = Binance BTC lot size precision (1e-6 BTC minimum step)
        quantity = round(size_usdc / price, 6)
        params = {
            "symbol":      _sym,
            "side":        _side,
            "type":        "LIMIT",
            "timeInForce": "GTC",       # Good Till Cancelled — mandatory for LIMIT orders
            "quantity":    f"{quantity:.6f}",  # string, 6dp per lot size filter
            "price":       f"{price:.2f}",     # string, 2dp = cent precision in USDT
            "timestamp":   int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)

        async with session.post(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers={"X-MBX-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)  # bypass MIME check
            if resp.status != 200:
                logger.error("Binance order error %d : %.300s", resp.status, data)
                return None
            oid = str(data.get("orderId", ""))
            return oid or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance post_order error : %s", e)
        return None


async def post_market_order(session, symbol, quantity, *, side="SELL",
                            api_key=None, api_secret=None,
                            private_key=None, install_dir=None):  # pylint: disable=unused-argument
    """
    Submit a MARKET order — executed immediately at best available price.

    Used for emergency exits (stop-loss) where execution certainty matters
    more than price. quantity is in base asset (BTC), not USDT.

    Returns order ID string on success, "sim_..." in dry-run, or None on error.
    """
    _key    = api_key    or os.environ.get("BINANCE_API_KEY", "")
    _secret = api_secret or os.environ.get("BINANCE_API_SECRET", "")
    _sym    = str(symbol).split(":", maxsplit=1)[0]
    _side   = side.upper()

    if not _key or not _secret:
        logger.warning("Binance — simulated market order (BINANCE_API_KEY/SECRET not set)")
        return f"sim_{uuid.uuid4().hex[:12]}"

    try:
        params = {
            "symbol":    _sym,
            "side":      _side,
            "type":      "MARKET",
            "quantity":  f"{quantity:.6f}",
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.post(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers={"X-MBX-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.error("Binance market order error %d : %.300s", resp.status, data)
                return None
            return str(data.get("orderId", "")) or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance post_market_order error : %s", e)
        return None


# ─── ORDER MANAGEMENT (grid trading) ─────────────────────────────────────────

async def get_order_status(session, symbol, order_id, *,
                           api_key=None, api_secret=None):
    """
    Query the status of a single order.

    Returns "NEW" | "FILLED" | "CANCELED" | "PARTIALLY_FILLED" | "EXPIRED",
    or None on error or when running in simulation mode (no credentials).

    Weight: 4 (per Binance rate-limit documentation).
    """
    _key    = api_key    or os.environ.get("BINANCE_API_KEY", "")
    _secret = api_secret or os.environ.get("BINANCE_API_SECRET", "")
    if not _key or not _secret:
        return None
    if str(order_id).startswith("sim_"):
        return None
    try:
        params = {
            "symbol":    str(symbol).split(":", maxsplit=1)[0],
            "orderId":   int(order_id),
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.get(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers={"X-MBX-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("Binance get_order_status error %d : %.300s", resp.status, data)
                return None
            return str(data.get("status", "")) or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance get_order_status error : %s", e)
        return None


async def cancel_order(session, symbol, order_id, *,
                       api_key=None, api_secret=None):
    """
    Cancel an open order.

    Returns True on success (status == "CANCELED"), False on error.
    Simulated orders (sim_ prefix) and missing credentials both return True
    immediately — they require no real cancellation.

    Weight: 1.
    """
    if str(order_id).startswith("sim_"):
        return True
    _key    = api_key    or os.environ.get("BINANCE_API_KEY", "")
    _secret = api_secret or os.environ.get("BINANCE_API_SECRET", "")
    if not _key or not _secret:
        return True
    try:
        params = {
            "symbol":    str(symbol).split(":", maxsplit=1)[0],
            "orderId":   int(order_id),
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.delete(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers={"X-MBX-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("Binance cancel_order error %d : %.300s", resp.status, data)
                return False
            return str(data.get("status", "")) == "CANCELED"
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance cancel_order error : %s", e)
        return False


async def get_open_orders(session, symbol, *, api_key=None, api_secret=None):
    """
    Return all open orders for `symbol` as a normalised list.

    Each element: {"order_id": str, "side": str, "price": float,
                   "qty": float, "status": str}

    Returns [] on error or in simulation mode (no credentials).
    Prefer this over repeated get_order_status calls: costs 40 weight vs
    4 × N weight for N individual status queries.

    Weight: 40 (with symbol filter).
    """
    _key    = api_key    or os.environ.get("BINANCE_API_KEY", "")
    _secret = api_secret or os.environ.get("BINANCE_API_SECRET", "")
    if not _key or not _secret:
        return []
    try:
        params = {
            "symbol":    str(symbol).split(":", maxsplit=1)[0],
            "timestamp": int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)
        async with session.get(
            f"{BASE_URL}/api/v3/openOrders",
            params=params,
            headers={"X-MBX-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("Binance get_open_orders error %d : %.300s", resp.status, data)
                return []
            return [
                {
                    "order_id": str(o.get("orderId", "")),
                    "side":     str(o.get("side", "")),
                    "price":    float(o.get("price", 0)),
                    "qty":      float(o.get("origQty", 0)),
                    "status":   str(o.get("status", "")),
                }
                for o in data
            ]
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance get_open_orders error : %s", e)
        return []


# ─── USER DATA STREAM ─────────────────────────────────────────────────────────

async def get_listen_key(session, *, api_key=None, api_secret=None):  # pylint: disable=unused-argument
    """
    Create a new user data stream and return its listenKey (60-min TTL).
    Extend with keepalive_listen_key every 30 min before it expires.
    Returns the listenKey string, or None on error / missing credentials.
    Weight: 2.
    """
    key = api_key or os.environ.get("BINANCE_API_KEY", "")
    if not key:
        return None
    try:
        async with session.post(
            f"{BASE_URL}/api/v3/userDataStream",
            headers={"X-MBX-APIKEY": key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Binance get_listen_key error %d", resp.status)
                return None
            data = await resp.json(content_type=None)
            return data.get("listenKey") or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance get_listen_key error : %s", e)
        return None


async def keepalive_listen_key(session, listen_key, *, api_key=None, api_secret=None):  # pylint: disable=unused-argument
    """
    Extend the TTL of an existing listenKey (PUT /api/v3/userDataStream).
    Should be called every ~30 min to prevent key expiry (TTL = 60 min).
    Returns True on success.  Weight: 2.
    """
    key = api_key or os.environ.get("BINANCE_API_KEY", "")
    if not key or not listen_key:
        return False
    try:
        async with session.put(
            f"{BASE_URL}/api/v3/userDataStream",
            params={"listenKey": listen_key},
            headers={"X-MBX-APIKEY": key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("Binance keepalive_listen_key error : %s", e)
        return False


def make_user_stream_url(listen_key: str) -> str:
    """Return the WebSocket URL for the user data stream."""
    return f"wss://stream.binance.com:9443/ws/{listen_key}"


def parse_user_stream_msg(msg: dict) -> "dict | None":
    """
    Parse a Binance executionReport WebSocket event.

    Returns {"order_id", "status", "side", "symbol"} when the order status
    is FILLED or PARTIALLY_FILLED, otherwise None.

    Binance event shape:
        {"e": "executionReport", "s": symbol, "i": orderId,
         "X": currentOrderStatus, "S": side, ...}
    """
    if not isinstance(msg, dict) or msg.get("e") != "executionReport":
        return None
    status = str(msg.get("X", ""))
    if status not in ("FILLED", "PARTIALLY_FILLED"):
        return None
    return {
        "order_id": str(msg.get("i", "")),
        "status":   status,
        "side":     str(msg.get("S", "")),
        "symbol":   str(msg.get("s", "")),
    }
