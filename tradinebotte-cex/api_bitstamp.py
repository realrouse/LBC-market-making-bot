"""
Bitstamp spot API adapter — implements the same public interface as
api_binance.py: get_markets, post_order, parse_book_update, compute_fee,
and market metadata helpers.

REST API:   https://www.bitstamp.net/api/v2/
WebSocket:  wss://ws.bitstamp.net  (live order book channel)

Credentials: BITSTAMP_API_KEY, BITSTAMP_API_SECRET, BITSTAMP_CUSTOMER_ID
env vars, required only for trading (post_order). Market data (get_markets,
parse_book_update) works without credentials.

No-credentials mode: BASE_URL points to the same public REST endpoint;
Bitstamp has no separate public-data mirror but all ticker/book endpoints
are unauthenticated.

To switch grid_bot.py to Bitstamp:
    import api_bitstamp as api   # replace api_binance import
"""

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from urllib.parse import urlencode

import aiohttp
from api_common import book_snapshot, parse_levels

logger = logging.getLogger(__name__)

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
BASE_URL      = "https://www.bitstamp.net/api/v2"
WS_URL        = "wss://ws.bitstamp.net"
WS_BATCH_SIZE = 20   # Bitstamp allows multiple channel subscriptions per socket

DEFAULT_SYMBOL = "btcusd"

# Bitstamp taker fee: 0.4% standard; drops with volume tiers. Use 0.1% as
# a conservative default for high-volume accounts.
FEE_RATE = 0.001


def compute_fee(price, quantity):
    """Bitstamp taker fee: FEE_RATE × notional."""
    return FEE_RATE * price * quantity


# ─── CREDENTIALS ──────────────────────────────────────────────────────────────
def _has_creds() -> bool:
    return bool(
        os.environ.get("BITSTAMP_API_KEY")
        and os.environ.get("BITSTAMP_API_SECRET")
        and os.environ.get("BITSTAMP_CUSTOMER_ID")
    )


def _auth_headers(method: str, path: str, body: str = "") -> dict:
    """Build Bitstamp v2 HMAC-SHA256 authentication headers.

    Message format per Bitstamp v2 spec:
      "BITSTAMP " + api_key + method + host + path + query + content_type
      + nonce + timestamp + "v2" + body
    Note: nonce comes BEFORE timestamp; "v2" version string is required.
    """
    api_key      = os.environ.get("BITSTAMP_API_KEY", "")
    api_secret   = os.environ.get("BITSTAMP_API_SECRET", "").encode()
    timestamp    = str(int(time.time() * 1000))
    nonce        = uuid.uuid4().hex
    content_type = "application/x-www-form-urlencoded" if body else ""
    message = (
        f"BITSTAMP {api_key}"
        f"{method.upper()}"
        f"www.bitstamp.net"
        f"{path}"
        f""
        f"{content_type}"
        f"{nonce}"
        f"{timestamp}"
        f"v2"
        f"{body}"
    ).encode()
    signature = hmac.new(api_secret, message, hashlib.sha256).hexdigest().upper()
    headers = {
        "X-Auth":            f"BITSTAMP {api_key}",
        "X-Auth-Signature":  signature,
        "X-Auth-Nonce":      nonce,
        "X-Auth-Timestamp":  timestamp,
        "X-Auth-Version":    "v2",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────
def make_subscribe_msg(symbols):
    """
    Return a JSON subscribe message for the Bitstamp live order book channel.
    Bitstamp uses per-channel subscriptions; send one message per symbol.
    Strip any ':SELL' suffix inserted by the grid bot.
    """
    events = []
    for s in symbols:
        clean = s.lower().split(":")[0]
        events.append({
            "event": "bts:subscribe",
            "data": {"channel": f"order_book_{clean}"},
        })
    # Bitstamp accepts one subscription per message; return the first as JSON.
    # The caller (feed.py) sends each message returned by make_subscribe_msg;
    # returning a list here allows multi-symbol subscription.
    if len(events) == 1:
        return json.dumps(events[0])
    return [json.dumps(e) for e in events]


# ─── ORDER BOOK ───────────────────────────────────────────────────────────────
def parse_book_update(msg):
    """
    Parse a Bitstamp WebSocket order_book message into a normalized snapshot.

    Bitstamp format:
        {
          "event": "data",
          "channel": "order_book_btcusd",
          "data": {
            "timestamp": "...", "microtimestamp": "...",
            "bids": [["price", "size"], ...],
            "asks": [["price", "size"], ...]
          }
        }

    OBI (Order Book Imbalance) = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    computed over top-5 levels on each side, matching api_binance convention.

    Returns normalized dict or None.
    """
    if not isinstance(msg, dict):
        return None
    if msg.get("event") != "data":
        return None

    channel = msg.get("channel", "")
    if not channel.startswith("order_book_"):
        return None
    symbol = channel.replace("order_book_", "").upper()

    data     = msg.get("data", {})
    bids_raw = data.get("bids", [])
    asks_raw = data.get("asks", [])

    bids = sorted(parse_levels(bids_raw), reverse=True)
    asks = sorted(parse_levels(asks_raw))
    if not bids and not asks:
        return None

    return book_snapshot(bids, asks, symbol)


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────
async def get_markets(session, symbol=DEFAULT_SYMBOL, **_):
    """
    Fetch current ticker for the given symbol from Bitstamp REST.
    Returns a 1-element list with the normalized market dict on success, or None on an
    API/network error (callers distinguish failure from data via `is None`).
    """
    clean = symbol.lower().split(":")[0]
    try:
        async with session.get(
            f"{BASE_URL}/ticker/{clean}/",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Bitstamp ticker HTTP %s [symbol=%s]", resp.status, clean)
                return None   # error sentinel — not [] ("ran fine, no data")
            data = await resp.json(content_type=None)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Bitstamp get_markets error: %s", exc)
        return None   # error sentinel — not [] ("ran fine, no data")

    return [{
        "symbol":      clean.upper(),
        "description": f"BTC/USD spot ({clean.upper()})",
        "best_bid":    float(data.get("bid", 0)),
        "best_ask":    float(data.get("ask", 0)),
        "last":        float(data.get("last", 0)),
        "volume_24h":  float(data.get("volume", 0)),
        "timestamp":   int(data.get("timestamp", 0)),
    }]


# ─── ORDER MANAGEMENT ────────────────────────────────────────────────────────
async def post_order(session, symbol, price, size_usdc, *, side="BUY", **_):
    """
    Place a limit order on Bitstamp.

    Implements the unified connector interface used by all strategy engines:
      post_order(session, symbol, price, size_usdc, *, side="BUY"|"SELL")

    price     : limit price in quote currency
    size_usdc : order size in USDT/USD → converted to base quantity (BTC)

    Returns the order ID string on success, "sim_..." in no-credential mode,
    or None on failure.
    """
    bs_side = side.lower()   # "BUY" → "buy", "SELL" → "sell"
    quantity = size_usdc / price if price > 0 else 0.0

    if not _has_creds():
        oid = f"sim_{uuid.uuid4().hex[:8]}"
        logger.info("Bitstamp sim: %s %s qty=%.6f @ %.2f  [%s]",
                    bs_side, symbol, quantity, price, oid)
        return oid

    clean    = symbol.lower().split(":")[0]
    endpoint = f"/api/v2/{bs_side}/{clean}/"
    body_dict = {"amount": f"{quantity:.6f}", "price": f"{price:.2f}"}
    body = urlencode(body_dict)
    headers = _auth_headers("POST", endpoint, body)

    try:
        async with session.post(
            f"https://www.bitstamp.net{endpoint}",
            data=body_dict,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or data.get("status") == "error":
                logger.error("Bitstamp order error %s [%s %s qty=%.6f @ %.2f]: %s",
                             resp.status, bs_side, symbol, quantity, price,
                             data.get("reason", str(data)[:200]))
                return None
            oid = str(data.get("id", ""))
            logger.info("Bitstamp order placed: %s [%s %s qty=%.6f @ %.2f]",
                        oid, bs_side, symbol, quantity, price)
            return oid or None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("Bitstamp post_order exception [%s %s @ %.2f]: %s",
                     bs_side, symbol, price, exc)
        return None


async def get_order_status(session, symbol, order_id):  # pylint: disable=unused-argument
    """Return Bitstamp order status string or None."""
    if not _has_creds():
        return "simulated"
    endpoint = "/api/v2/order_status/"
    body_dict = {"id": str(order_id)}
    body = urlencode(body_dict)
    headers = _auth_headers("POST", endpoint, body)
    try:
        async with session.post(
            f"https://www.bitstamp.net{endpoint}",
            data=body_dict,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            return data.get("status")
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Bitstamp get_order_status: %s", exc)
        return None


async def cancel_order(session, symbol, order_id):  # pylint: disable=unused-argument
    """Cancel an open order. Returns True on success."""
    if not _has_creds():
        return True
    endpoint = "/api/v2/cancel_order/"
    body_dict = {"id": str(order_id)}
    body = urlencode(body_dict)
    headers = _auth_headers("POST", endpoint, body)
    try:
        async with session.post(
            f"https://www.bitstamp.net{endpoint}",
            data=body_dict,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            return data.get("id") is not None
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Bitstamp cancel_order: %s", exc)
        return False


async def get_open_orders(session, symbol=DEFAULT_SYMBOL):
    """Return normalized open orders for the given symbol.

    Normalized format (matches api_binance/api_mexc contract):
      {"order_id": str, "side": str, "price": float, "qty": float, "status": str}

    Returns None on an API/network error (a transient failure must not be read as
    "no open orders"); [] in simulation mode (no credentials) or genuinely no orders.
    """
    if not _has_creds():
        return []
    clean = symbol.lower().split(":")[0]
    endpoint = f"/api/v2/open_orders/{clean}/"
    headers = _auth_headers("POST", endpoint)
    try:
        async with session.post(
            f"https://www.bitstamp.net{endpoint}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            raw = await resp.json(content_type=None)
        # Bitstamp type: "0"=buy, "1"=sell (may also be int 0/1)
        return [
            {
                "order_id": str(o.get("id", "")),
                "side":     "BUY" if str(o.get("type", "1")) == "0" else "SELL",
                "price":    float(o.get("price", 0)),
                "qty":      float(o.get("amount", 0)),
                "status":   "open",
            }
            for o in (raw if isinstance(raw, list) else [])
        ]
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.warning("Bitstamp get_open_orders: %s", exc)
        return None   # error sentinel — not [] (sim/no-orders)


# ─── USER STREAM STUBS ───────────────────────────────────────────────────────
# Bitstamp does not support a Binance-style user data stream (listen key).
# Grid strategy falls back to REST polling for fills when these return None.

async def get_listen_key(session, symbol=None) -> str | None:  # pylint: disable=unused-argument
    return None


async def keepalive_listen_key(session, listen_key: str) -> None:  # pylint: disable=unused-argument
    return


def make_user_stream_url(listen_key: str) -> str:  # pylint: disable=unused-argument
    return ""


def parse_user_stream_msg(msg: dict) -> dict | None:  # pylint: disable=unused-argument
    return None
