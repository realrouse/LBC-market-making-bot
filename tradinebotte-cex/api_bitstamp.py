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

logger = logging.getLogger("live")

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
_has_creds = bool(
    os.environ.get("BITSTAMP_API_KEY")
    and os.environ.get("BITSTAMP_API_SECRET")
    and os.environ.get("BITSTAMP_CUSTOMER_ID")
)


def _auth_headers(method: str, path: str, body: str = "") -> dict:
    """Build Bitstamp v2 HMAC-SHA256 authentication headers."""
    api_key    = os.environ.get("BITSTAMP_API_KEY", "")
    api_secret = os.environ.get("BITSTAMP_API_SECRET", "").encode()
    timestamp  = str(int(time.time() * 1000))
    nonce      = uuid.uuid4().hex
    content_type = "application/x-www-form-urlencoded" if body else ""
    message = (
        f"BITSTAMP {api_key}"
        f"{method.upper()}"
        f"www.bitstamp.net"
        f"{path}"
        f""
        f"{content_type}"
        f"{timestamp}"
        f"{nonce}"
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


# ─── MARKET METADATA ─────────────────────────────────────────────────────────
def get_market_id(market):
    return market.get("symbol", "")


def get_market_question(market):
    return market.get("description", market.get("symbol", ""))


def get_market_end_ts_ms(market):
    return 0.0


def get_market_start_ts_ms(market):
    return 0.0


def get_up_token_id(market):
    return market.get("symbol", "")


def get_down_token_id(market):
    return market.get("symbol", "") + ":SELL"


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

    def pl(lst):
        r = []
        for item in lst:
            try:
                p, s = float(item[0]), float(item[1])
                if p > 0 and s > 0:
                    r.append((p, s))
            except Exception:
                continue
        return r

    bids = sorted(pl(bids_raw), reverse=True)
    asks = sorted(pl(asks_raw))
    if not bids and not asks:
        return None

    bb = bids[0][0] if bids else 0.0
    ba = asks[0][0] if asks else float("inf")
    bv = sum(s for _, s in bids[:5])
    av = sum(s for _, s in asks[:5])
    tv = bv + av
    obi = (bv - av) / tv if tv > 0 else 0.0

    return {
        "token_id": symbol,
        "best_bid": bb,
        "best_ask": ba,
        "spread":   max(0.0, ba - bb),
        "bid_vol":  bv,
        "ask_vol":  av,
        "obi":      obi,
    }


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────
async def get_markets(session, symbol=DEFAULT_SYMBOL, **_):
    """
    Fetch current ticker for the given symbol from Bitstamp REST.
    Returns a list with one normalized market dict.
    """
    clean = symbol.lower().split(":")[0]
    try:
        async with session.get(
            f"{BASE_URL}/ticker/{clean}/",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Bitstamp ticker HTTP %s", resp.status)
                return []
            data = await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("Bitstamp get_markets error: %s", exc)
        return []

    return [{
        "symbol":      clean.upper(),
        "description": f"BTC/USD spot ({clean.upper()})",
        "best_bid":    float(data.get("bid", 0)),
        "best_ask":    float(data.get("ask", 0)),
        "last":        float(data.get("last", 0)),
        "volume_24h":  float(data.get("volume", 0)),
        "timestamp":   int(data.get("timestamp", 0)),
    }]


# ─── ORDER BOOK (REST snapshot) ───────────────────────────────────────────────
async def get_order_book(session, symbol=DEFAULT_SYMBOL, depth=5):
    """Fetch a REST order book snapshot for the given symbol."""
    clean = symbol.lower().split(":")[0]
    try:
        async with session.get(
            f"{BASE_URL}/order_book/{clean}/",
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("Bitstamp order_book HTTP %s", resp.status)
                return {}
            return await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("Bitstamp get_order_book error: %s", exc)
        return {}


# ─── ORDER MANAGEMENT ────────────────────────────────────────────────────────
async def post_order(session, symbol, side, quantity, price=None,
                     order_type="limit", api_key=None, api_secret=None, **_):
    """
    Place a limit or market order on Bitstamp.

    side        : 'buy' | 'sell'
    quantity    : base asset amount (BTC for btcusd)
    price       : required for limit orders; None → market order
    order_type  : 'limit' | 'market'

    Returns the Bitstamp order dict on success, or None on failure.
    """
    if not _has_creds:
        logger.info("Bitstamp sim-mode: %s %s %s @ %s (no credentials)",
                    order_type, side, quantity, price)
        return {
            "id":     f"sim_{uuid.uuid4().hex[:8]}",
            "status": "simulated",
            "side":   side,
            "amount": str(quantity),
            "price":  str(price or 0),
        }

    clean = symbol.lower().split(":")[0]
    if order_type == "market":
        endpoint = f"/api/v2/{side}/market/{clean}/"
        body_dict = {"amount": str(quantity)}
    else:
        endpoint = f"/api/v2/{side}/{clean}/"
        body_dict = {"amount": str(quantity), "price": str(price)}

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
                logger.error("Bitstamp order error %s: %s", resp.status, data)
                return None
            logger.info("Bitstamp order placed: %s", data.get("id"))
            return data
    except Exception as exc:
        logger.error("Bitstamp post_order exception: %s", exc)
        return None


async def get_order_status(session, symbol, order_id):
    """Return Bitstamp order status string or None."""
    if not _has_creds:
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
    except Exception as exc:
        logger.warning("Bitstamp get_order_status: %s", exc)
        return None


async def cancel_order(session, symbol, order_id):
    """Cancel an open order. Returns True on success."""
    if not _has_creds:
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
    except Exception as exc:
        logger.warning("Bitstamp cancel_order: %s", exc)
        return False


async def get_open_orders(session, symbol=DEFAULT_SYMBOL):
    """Return list of open orders for the given symbol."""
    if not _has_creds:
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
            return await resp.json(content_type=None)
    except Exception as exc:
        logger.warning("Bitstamp get_open_orders: %s", exc)
        return []


# ─── OHLCV HISTORY (for backtesting / cycle analysis) ─────────────────────────
async def get_ohlcv(session, symbol=DEFAULT_SYMBOL, step=86400,
                    start: int = 0, end: int = 0, limit: int = 1000) -> list:
    """
    Fetch OHLCV candles from Bitstamp public API.

    step   : candle width in seconds (60/180/300/900/1800/3600/7200/
              14400/21600/43200/86400)
    start  : Unix timestamp (seconds, UTC)
    end    : Unix timestamp (seconds, UTC)
    limit  : max candles per request (max 1000)

    Returns list of dicts: {timestamp, open, high, low, close, volume}
    """
    clean = symbol.lower().split(":")[0]
    params: dict = {"step": step, "limit": limit}
    if start:
        params["start"] = start
    if end:
        params["end"] = end
    try:
        async with session.get(
            f"{BASE_URL}/ohlc/{clean}/",
            params=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            data = await resp.json(content_type=None)
        return [
            {
                "timestamp": int(c["timestamp"]),
                "open":      float(c["open"]),
                "high":      float(c["high"]),
                "low":       float(c["low"]),
                "close":     float(c["close"]),
                "volume":    float(c["volume"]),
            }
            for c in data.get("data", {}).get("ohlc", [])
        ]
    except Exception as exc:
        logger.warning("Bitstamp get_ohlcv error: %s", exc)
        return []
