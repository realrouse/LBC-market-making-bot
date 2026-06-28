"""
MEXC Futures (perpetual swaps) API adapter.

Endpoint base : https://contract.mexc.com
WebSocket     : wss://contract.mexc.com/edge

Key differences from MEXC spot (api_mexc.py):
  Symbol format   : BTC_USDT (underscore, not BTCUSDT)
  Auth            : ApiKey + Request-Time + Signature headers
                    GET  → HMAC-SHA256(api_key + req_time + query_string, secret)
                    POST → HMAC-SHA256(api_key + req_time + json_body, secret)
  Orders          : in contracts (vol); 1 BTC_USDT contract = BTC_USDT_CONTRACT_SIZE BTC
                    side: 1=open_long  2=close_short  3=open_short  4=close_long
  Fees            : 0.02% maker / 0.06% taker (vs 0.20% spot)
  WS private auth : login message (not listenKey URL param); see get_listen_key docs.

Credentials: MEXC_FUTURES_API_KEY / MEXC_FUTURES_API_SECRET env vars.
Without credentials, order functions return "sim_..." IDs (simulation mode).
"""

import hashlib
import hmac as _hmac
import json
import logging
import os
import time
import uuid
from urllib.parse import urlencode

import aiohttp

from api_common import book_snapshot, parse_levels

logger = logging.getLogger(__name__)

# ── ENDPOINTS ─────────────────────────────────────────────────────────────────
BASE_URL      = "https://contract.mexc.com"
WS_URL        = "wss://contract.mexc.com/edge"
WS_BATCH_SIZE = 10

DEFAULT_SYMBOL = "BTC_USDT"

# 1 BTC_USDT perpetual contract = 0.001 BTC.
# Verify with GET /api/v1/contract/detail before trading at scale.
BTC_USDT_CONTRACT_SIZE = 0.001  # BTC per contract

# MEXC Futures fee rates (standard tier, no discount)
FEE_RATE       = 0.0006   # taker 0.06%
FEE_RATE_MAKER = 0.0002   # maker 0.02%

# Futures order side codes
_SIDE_OPEN_LONG   = 1
_SIDE_CLOSE_SHORT = 2
_SIDE_OPEN_SHORT  = 3
_SIDE_CLOSE_LONG  = 4

# Order type codes
_TYPE_LIMIT  = 1
_TYPE_MARKET = 5  # market order (fills at best available price)

# Open type
_OPEN_TYPE_ISOLATED = 1
_OPEN_TYPE_CROSS    = 2

# Order status codes returned by the REST API
_STATUS_MAP = {
    1: "NEW",
    2: "FILLED",
    3: "CANCELED",
    4: "INVALID",
}


def compute_fee(price, quantity):
    """MEXC Futures taker fee: 0.06% of notional (price × quantity in USDT)."""
    return FEE_RATE * price * quantity


# ── MARKET METADATA ───────────────────────────────────────────────────────────

def get_market_id(market):
    """Return the trading symbol as the market identifier."""
    return market.get("symbol", "")


def get_market_question(market):
    """Return a human-readable market description (falls back to symbol)."""
    return market.get("question", market.get("symbol", ""))


def get_market_end_ts_ms(_market):
    """Return 0 — perpetual futures have no scheduled expiry."""
    return 0.0


def get_market_start_ts_ms(_market):
    """Return 0 — start time is not applicable to perpetual contracts."""
    return 0.0


def get_up_token_id(market):
    """Long (buy) direction maps to UP."""
    return market.get("symbol", "")


def get_down_token_id(market):
    """Short direction maps to DOWN — ":SHORT" suffix mirrors ":SELL" convention."""
    return market.get("symbol", "") + ":SHORT"


# ── AUTH ──────────────────────────────────────────────────────────────────────

def _sign(api_key: str, secret: str, req_time: str, params_str: str) -> str:
    """
    MEXC Futures HMAC-SHA256 signature.

    Signed string: api_key + req_time + params_str
      GET  → params_str is the URL-encoded query (keys sorted alphabetically)
      POST → params_str is the JSON-encoded request body
    """
    msg = api_key + req_time + params_str
    return _hmac.new(
        secret.encode("utf-8"),
        msg.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _auth_headers(api_key: str, secret: str, params_str: str = "") -> dict:
    """Build the three auth headers required by every MEXC Futures private endpoint."""
    req_time = str(int(time.time() * 1000))
    return {
        "ApiKey":       api_key,
        "Request-Time": req_time,
        "Signature":    _sign(api_key, secret, req_time, params_str),
        "Content-Type": "application/json",
    }


# ── WEBSOCKET ─────────────────────────────────────────────────────────────────

def make_subscribe_msg(symbols):
    """
    Return a JSON subscribe message for MEXC Futures full-depth streams.

    Strips any ":SHORT"/":SELL" suffix and de-duplicates. A grid registers both
    an UP and a DOWN token for one market, and both map to the same underlying
    symbol. MEXC Futures accepts only ONE subscribe object per frame — sending a
    JSON array is rejected with {"channel":"rs.error","data":"invalid message"},
    which left the grid with no depth feed (last_book_ts stuck at 0).
    """
    seen = []
    for s in symbols:
        clean = s.split(":")[0]
        if clean not in seen:
            seen.append(clean)
    if len(seen) == 1:
        return json.dumps({
            "method": "sub.depth.full",
            "param":  {"symbol": seen[0], "limit": 5},
        })
    # Multiple distinct symbols would each need their own frame (set WS_BATCH_SIZE=1);
    # grids subscribe a single underlying symbol, so this path is not exercised today.
    return json.dumps([
        {"method": "sub.depth.full", "param": {"symbol": p, "limit": 5}}
        for p in seen
    ])


def make_ping_msg():
    """App-level keepalive for the public WS. MEXC Futures expects {"method":"ping"}
    and otherwise drops the socket (code 1005); cex_feed sends this periodically."""
    return json.dumps({"method": "ping"})


# ── ORDER BOOK ────────────────────────────────────────────────────────────────

def parse_book_update(msg):
    """
    Parse a MEXC Futures WebSocket full-depth push into a normalized snapshot.

    MEXC Futures depth message format:
    {
      "channel": "push.depth.full",
      "data": {
        "asks": [[price, vol, num_orders], ...],
        "bids": [[price, vol, num_orders], ...]
      },
      "symbol": "BTC_USDT",
      "ts": 1700000000000
    }

    Returns {token_id, best_bid, best_ask, spread, bid_vol, ask_vol, obi} or None.
    """
    if not isinstance(msg, dict):
        return None

    if "depth" not in msg.get("channel", ""):
        return None

    data   = msg.get("data")
    symbol = msg.get("symbol", DEFAULT_SYMBOL)
    if not isinstance(data, dict):
        return None

    bids_raw = data.get("bids") or []
    asks_raw = data.get("asks") or []
    if not bids_raw and not asks_raw:
        return None

    bids = sorted(parse_levels(bids_raw), reverse=True)
    asks = sorted(parse_levels(asks_raw))
    if not bids and not asks:
        return None

    return book_snapshot(bids, asks, symbol)


# ── MARKET DISCOVERY ──────────────────────────────────────────────────────────

async def get_markets(session, symbol=DEFAULT_SYMBOL, **_):
    """
    Fetch current order book ticker for the given symbol from MEXC Futures REST.
    Returns a 1-element list with the normalized market dict on success, or None on an
    API/network error (callers distinguish failure from data via `is None`).
    """
    _sym = symbol.split(":")[0]
    try:
        async with session.get(
            f"{BASE_URL}/api/v1/contract/ticker",
            params={"symbol": _sym},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("MEXC Futures get_markets error %d [symbol=%s]", resp.status, _sym)
                return None   # error sentinel — not [] ("ran fine, no data")
            outer = await resp.json(content_type=None)
        raw = outer.get("data") or {}
        return [{
            "symbol":   _sym,
            "question": f"BTC/USDT perpetual — MEXC Futures ({_sym})",
            "best_bid": float(raw.get("bid1", 0)),
            "best_ask": float(raw.get("ask1", 0)),
        }]
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("MEXC Futures get_markets error [symbol=%s]: %s", _sym, e)
        return None   # error sentinel — not [] ("ran fine, no data")


# ── ORDER PLACEMENT ───────────────────────────────────────────────────────────

def _to_vol(size_usdc: float, price: float) -> int:
    """Convert USDT notional to integer contract count for BTC_USDT perpetual."""
    return max(1, round(size_usdc / price / BTC_USDT_CONTRACT_SIZE))


async def post_order(session, symbol, price, size_usdc, *,
                     api_key=None, api_secret=None, side="BUY",
                     private_key=None, install_dir=None):  # pylint: disable=unused-argument
    """
    Submit a LIMIT order to MEXC Futures.

    Args:
        symbol    : perpetual pair, e.g. "BTC_USDT". Append ":SHORT" to force
                    open-short side (mirrors get_down_token_id convention).
        price     : limit price in USDT
        size_usdc : USDT notional — converted to contracts via BTC_USDT_CONTRACT_SIZE
        api_key   : MEXC Futures key (falls back to MEXC_FUTURES_API_KEY env var)
        api_secret: MEXC Futures secret (falls back to MEXC_FUTURES_API_SECRET)
        side      : "BUY" → open_long; "SELL" → close_long;
                    overridden by ":SHORT" suffix → open_short

    Returns order ID string on success, "sim_..." in dry-run, or None on error.
    """
    _key    = api_key    or os.environ.get("MEXC_FUTURES_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_FUTURES_API_SECRET", "")

    _sym = symbol.split(":")[0]
    if symbol.endswith(":SHORT"):
        fut_side = _SIDE_OPEN_SHORT
    elif side.upper() == "SELL":
        fut_side = _SIDE_CLOSE_LONG
    else:
        fut_side = _SIDE_OPEN_LONG

    if not _key or not _secret:
        logger.warning("MEXC Futures — order simulated (MEXC_FUTURES_API_KEY/SECRET not set)")
        return f"sim_{uuid.uuid4().hex[:12]}"

    vol      = _to_vol(size_usdc, price)
    body     = {
        "symbol":   _sym,
        "price":    str(price),
        "vol":      vol,
        "side":     fut_side,
        "type":     _TYPE_LIMIT,
        "openType": _OPEN_TYPE_ISOLATED,
        "leverage": 1,
    }
    body_str = json.dumps(body, separators=(",", ":"))

    try:
        async with session.post(
            f"{BASE_URL}/api/v1/private/order/submit",
            data=body_str,
            headers=_auth_headers(_key, _secret, body_str),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("success"):
                logger.error(
                    "MEXC Futures order error %d [side=%d %s vol=%d @ %.2f]: %s",
                    resp.status, fut_side, _sym, vol, price, str(data)[:200],
                )
                return None
            return str(data.get("data", "")) or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC Futures post_order error [side=%d %s @ %.2f]: %s",
                     fut_side, _sym, price, e)
        return None


async def post_market_order(session, symbol, side, quantity, *,
                            api_key=None, api_secret=None,
                            private_key=None, install_dir=None):  # pylint: disable=unused-argument
    """
    Submit a market order to MEXC Futures (emergency stop-loss exit).

    Args:
        side    : "SELL" → close_long; "BUY" → close_short
        quantity: amount in BTC (base asset) — converted to contracts internally
    """
    _key    = api_key    or os.environ.get("MEXC_FUTURES_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_FUTURES_API_SECRET", "")

    _sym     = symbol.split(":")[0]
    fut_side = _SIDE_CLOSE_LONG if side.upper() == "SELL" else _SIDE_CLOSE_SHORT

    if not _key or not _secret:
        logger.warning("MEXC Futures — market order simulated (no credentials)")
        return f"sim_{uuid.uuid4().hex[:12]}"

    vol      = max(1, round(quantity / BTC_USDT_CONTRACT_SIZE))
    body     = {
        "symbol":   _sym,
        "vol":      vol,
        "side":     fut_side,
        "type":     _TYPE_MARKET,
        "openType": _OPEN_TYPE_ISOLATED,
    }
    body_str = json.dumps(body, separators=(",", ":"))

    try:
        async with session.post(
            f"{BASE_URL}/api/v1/private/order/submit",
            data=body_str,
            headers=_auth_headers(_key, _secret, body_str),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("success"):
                logger.error("MEXC Futures market order error %d [%s]: %s",
                             resp.status, _sym, str(data)[:200])
                return None
            return str(data.get("data", "")) or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC Futures post_market_order error [%s]: %s", _sym, e)
        return None


# ── ORDER MANAGEMENT (grid / swing) ──────────────────────────────────────────

async def get_order_status(session, symbol, order_id, *,
                           api_key=None, api_secret=None):
    """
    Query the status of a single order.

    Returns "NEW" | "FILLED" | "CANCELED" | "INVALID",
    or None on error or in simulation mode (no credentials).

    MEXC Futures endpoint: GET /api/v1/private/order/get/{orderId}
    """
    _key    = api_key    or os.environ.get("MEXC_FUTURES_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_FUTURES_API_SECRET", "")
    if not _key or not _secret:
        return None
    if str(order_id).startswith("sim_"):
        return None

    _sym    = str(symbol).split(":")[0]
    qs      = urlencode({"symbol": _sym})
    headers = _auth_headers(_key, _secret, qs)
    try:
        async with session.get(
            f"{BASE_URL}/api/v1/private/order/get/{order_id}",
            params={"symbol": _sym},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("success"):
                logger.warning("MEXC Futures get_order_status error %d : %.300s",
                               resp.status, data)
                return None
            raw_state = (data.get("data") or {}).get("state", 0)
            return _STATUS_MAP.get(raw_state)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC Futures get_order_status error : %s", e)
        return None


async def cancel_order(session, symbol, order_id, *,
                       api_key=None, api_secret=None):
    """
    Cancel an open order.

    Returns True on success, False on error.
    Simulated orders (sim_ prefix) and missing credentials return True immediately.

    MEXC Futures endpoint: DELETE /api/v1/private/order/cancel/{orderId}
    """
    if str(order_id).startswith("sim_"):
        return True
    _key    = api_key    or os.environ.get("MEXC_FUTURES_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_FUTURES_API_SECRET", "")
    if not _key or not _secret:
        return True

    _sym    = str(symbol).split(":")[0]
    qs      = urlencode({"symbol": _sym})
    headers = _auth_headers(_key, _secret, qs)
    try:
        async with session.delete(
            f"{BASE_URL}/api/v1/private/order/cancel/{order_id}",
            params={"symbol": _sym},
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("success"):
                logger.warning("MEXC Futures cancel_order error %d : %.300s", resp.status, data)
                return False
            return True
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC Futures cancel_order error : %s", e)
        return False


async def get_open_orders(session, symbol, *, api_key=None, api_secret=None):
    """
    Return all open orders for `symbol` as a normalised list.

    Each element: {"order_id": str, "side": str, "price": float,
                   "qty": float, "status": str}

    Returns None on an API/network error (a transient failure must not be read as
    "no open orders"); [] in simulation mode (no credentials) or genuinely no orders.
    MEXC Futures endpoint: GET /api/v1/private/order/list/open_orders/{symbol}
    """
    _key    = api_key    or os.environ.get("MEXC_FUTURES_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_FUTURES_API_SECRET", "")
    if not _key or not _secret:
        return []

    _sym    = str(symbol).split(":")[0]
    qs      = urlencode({"symbol": _sym})
    headers = _auth_headers(_key, _secret, qs)
    try:
        async with session.get(
            f"{BASE_URL}/api/v1/private/order/list/open_orders/{_sym}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("success"):
                logger.warning("MEXC Futures get_open_orders error %d : %.300s", resp.status, data)
                return None   # error sentinel — not [] (sim/no-orders)
            orders = data.get("data") or []
            return [
                {
                    "order_id": str(o.get("orderId", "")),
                    "side":     "BUY" if o.get("side") in (_SIDE_OPEN_LONG, _SIDE_CLOSE_SHORT)
                                else "SELL",
                    "price":    float(o.get("price", 0)),
                    "qty":      float(o.get("vol", 0)) * BTC_USDT_CONTRACT_SIZE,
                    "status":   _STATUS_MAP.get(o.get("state", 0), "UNKNOWN"),
                }
                for o in orders
                if isinstance(o, dict)
            ]
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC Futures get_open_orders error : %s", e)
        return None   # error sentinel — not [] (sim/no-orders)


# ── USER DATA STREAM (WebSocket private) ──────────────────────────────────────

async def get_listen_key(session, *, api_key=None, api_secret=None):  # pylint: disable=unused-argument
    """
    Build and return the WebSocket login payload for MEXC Futures private stream.

    MEXC Futures private WS does not use a REST-issued listen key. Instead,
    authentication is done by sending a login message after connecting:
        {"method": "login", "param": {"apiKey": "...", "reqTime": "...", "signature": "..."}}
    where signature = HMAC-SHA256(api_key + req_time, secret).

    This function returns that JSON-encoded login payload as a string so that
    the grid strategy can pass it to the WebSocket as an opaque "listen key".
    Returns None when credentials are absent.
    """
    _key    = api_key    or os.environ.get("MEXC_FUTURES_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_FUTURES_API_SECRET", "")
    if not _key or not _secret:
        return None

    req_time = str(int(time.time() * 1000))
    sig      = _hmac.new(
        _secret.encode("utf-8"),
        (_key + req_time).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return json.dumps({
        "method": "login",
        "param":  {"apiKey": _key, "reqTime": req_time, "signature": sig},
    })


async def keepalive_listen_key(session, listen_key, *,  # pylint: disable=unused-argument
                               api_key=None, api_secret=None):
    """
    MEXC Futures private WS sessions do not expire via a listen-key TTL.
    This is a no-op that returns True to satisfy the connector interface.
    """
    return bool(listen_key)


def make_user_stream_url(listen_key: str) -> str:  # pylint: disable=unused-argument
    """
    Return the base WebSocket URL for MEXC Futures private stream.

    Unlike spot adapters, MEXC Futures auth is carried in the login message
    (stored in listen_key), not as a URL parameter.
    """
    return WS_URL


def parse_user_stream_msg(msg: dict) -> "dict | None":
    """
    Parse a MEXC Futures private order-update WebSocket event.

    MEXC Futures private order push format:
    {
      "channel": "push.personal.order",
      "data": {
        "orderId": "12345",
        "state":   2,      // 1=NEW 2=FILLED 3=CANCELED 4=INVALID
        "side":    1,      // 1=open_long 2=close_short 3=open_short 4=close_long
        "symbol":  "BTC_USDT"
      }
    }

    Returns {"order_id", "status", "side", "symbol"} for fill events, else None.
    Side is normalised to "BUY" (long-opening or short-closing) or
    "SELL" (long-closing or short-opening) to match the spot adapter convention.
    """
    if not isinstance(msg, dict):
        return None
    if msg.get("channel") != "push.personal.order":
        return None

    data = msg.get("data")
    if not isinstance(data, dict):
        return None

    raw_state = data.get("state", 0)
    if raw_state == 2:
        status = "FILLED"
    elif raw_state == 1:
        status = "NEW"
    else:
        return None

    raw_side = data.get("side", 0)
    if raw_side in (_SIDE_OPEN_LONG, _SIDE_CLOSE_SHORT):
        side = "BUY"
    elif raw_side in (_SIDE_OPEN_SHORT, _SIDE_CLOSE_LONG):
        side = "SELL"
    else:
        return None

    order_id = str(data.get("orderId", ""))
    if not order_id:
        return None

    return {
        "order_id": order_id,
        "status":   status,
        "side":     side,
        "symbol":   str(data.get("symbol", "")),
    }
