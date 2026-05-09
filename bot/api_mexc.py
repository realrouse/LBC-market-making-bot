"""
MEXC spot API adapter — implements the same public interface as
api_polymarket.py: get_markets, post_order, parse_book_update, compute_fee,
and market metadata helpers.

Grid trading extensions (not in api_polymarket):
    get_order_status(session, symbol, order_id) → status string or None
    cancel_order(session, symbol, order_id)     → bool
    get_open_orders(session, symbol)            → list[dict]

MEXC v3 REST is Binance-compatible but uses different WebSocket framing
and the header key is X-MEXC-APIKEY instead of X-MBX-APIKEY.

Credentials: MEXC_API_KEY and MEXC_API_SECRET env vars,
or pass api_key / api_secret kwargs to post_order.

Compatibility note: the Polymarket signal (best_bid >= 0.96) uses a 0–1
probability scale. On MEXC, best_bid is an absolute USDT price (e.g. 65000).
Strategy thresholds in strategies/*.json must be recalibrated for CEX use.

To switch live_bot.py to MEXC:
    import api_mexc as api   # line 62 in live_bot.py
"""

import hashlib, hmac, json, logging, os, time, uuid
from urllib.parse import urlencode
import aiohttp

logger = logging.getLogger("live")

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
BASE_URL      = "https://api.mexc.com"
WS_URL        = "wss://wbs.mexc.com/ws"
WS_BATCH_SIZE = 10  # streams per WebSocket connection

DEFAULT_SYMBOL = "BTCUSDT"

# MEXC spot taker fee: 0.2% standard (0% maker with MEXC token in some tiers).
FEE_RATE = 0.002


def compute_fee(price, quantity):
    """MEXC taker fee: 0.2% of notional (price × quantity in USDT)."""
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


def get_market_end_ts_ms(market):
    """Return 0 — spot markets have no scheduled expiry."""
    return 0.0


def get_market_start_ts_ms(market):
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
    Return a JSON subscribe message for MEXC limit depth streams.
    Format: spot@public.limit.depth.v3.api@<SYMBOL>@5
    Strips any ":SELL" suffix encoded in the symbol string.
    """
    params = [
        f"spot@public.limit.depth.v3.api@{s.split(':')[0]}@5"
        for s in symbols
    ]
    return json.dumps({
        "method": "SUBSCRIPTION",
        "params": params,
    })


def _ping_msg():
    """Keepalive ping message for MEXC WebSocket (required every 30 s)."""
    return json.dumps({"method": "PING"})


# ─── ORDER BOOK ───────────────────────────────────────────────────────────────

def parse_book_update(msg):
    """
    Parse a MEXC WebSocket depth message into a normalized price snapshot.

    MEXC depth stream format:
    {
      "c": "spot@public.limit.depth.v3.api@BTCUSDT@5",
      "d": {
        "asks": [["price", "qty"], ...],
        "bids": [["price", "qty"], ...]
      },
      "s": "BTCUSDT",
      "t": 1610123456789
    }

    OBI (Order Book Imbalance) = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    computed over top-5 levels on each side, same as api_polymarket.

    Returns {token_id, best_bid, best_ask, spread, bid_vol, ask_vol, obi}
    or None if the message is irrelevant or malformed.
    """
    if not isinstance(msg, dict):
        return None

    # MEXC wraps depth data under the "d" key; symbol is in "s"
    data   = msg.get("d")
    symbol = msg.get("s", DEFAULT_SYMBOL)
    if not data:
        return None

    bids_raw = data.get("bids") or []
    asks_raw = data.get("asks") or []
    if not bids_raw and not asks_raw:
        return None

    def pl(lst):
        """Parse price-level list into (price, size) float tuples, skipping zeros/malformed."""
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
        "spread": max(0.0, ba - bb),
        "bid_vol": bv,
        "ask_vol": av,
        "obi": obi,
    }


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────

async def get_markets(session, symbol=DEFAULT_SYMBOL):
    """
    Fetch the current order book ticker for the given symbol from MEXC REST.
    Returns a list with one normalized market dict.
    """
    try:
        async with session.get(
            f"{BASE_URL}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("MEXC API erreur : %d", resp.status)
                return []
            # content_type=None: bypass aiohttp's MIME check — some APIs omit charset
            data = await resp.json(content_type=None)
        return [{
            "symbol": symbol,
            "question": f"BTC/USDT spot — MEXC ({symbol})",
            "best_bid": float(data.get("bidPrice", 0)),
            "best_ask": float(data.get("askPrice", 0)),
            "bid_qty":  float(data.get("bidQty", 0)),
            "ask_qty":  float(data.get("askQty", 0)),
        }]
    except Exception as e:
        logger.warning("MEXC fetch erreur : %s", e)
        return []


# ─── SIGNING ──────────────────────────────────────────────────────────────────

def _sign(params: dict, secret: str) -> str:
    """HMAC-SHA256 signature over the URL-encoded query string."""
    query = urlencode(params)
    return hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                    hashlib.sha256).hexdigest()


# ─── ORDER PLACEMENT ──────────────────────────────────────────────────────────

async def post_order(session, symbol, price, size_usdc, *,
                     api_key=None, api_secret=None, side="BUY",
                     private_key=None, install_dir=None):
    """
    Submit a LIMIT order to MEXC spot.

    Args:
        symbol    : trading pair, e.g. "BTCUSDT". Append ":SELL" to force
                    the SELL side (mirrors get_down_token_id convention).
        price     : limit price in USDT
        size_usdc : notional USDT amount (BUY) or proceeds target (SELL)
        api_key   : MEXC API key (falls back to MEXC_API_KEY env var)
        api_secret: MEXC API secret (falls back to MEXC_API_SECRET)
        side      : "BUY" or "SELL" — overridden by ":SELL" suffix in symbol
        private_key / install_dir: accepted but ignored (Polymarket compat)

    Returns order ID string on success, "sim_..." in dry-run, or None on error.
    """
    _key    = api_key    or os.environ.get("MEXC_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_API_SECRET", "")

    _sym  = str(symbol).split(":", maxsplit=1)[0]
    _side = "SELL" if str(symbol).endswith(":SELL") else side.upper()

    if not _key or not _secret:
        logger.warning("MEXC — ordre simule (MEXC_API_KEY/SECRET absents)")
        return f"sim_{uuid.uuid4().hex[:12]}"

    try:
        # 6 decimal places = MEXC BTC lot size precision (same as Binance, 1e-6 BTC minimum)
        quantity = round(size_usdc / price, 6)
        params = {
            "symbol":      _sym,
            "side":        _side,
            "type":        "LIMIT",
            # MEXC LIMIT orders default to GTC server-side; timeInForce is not required
            # (unlike Binance where omitting it returns an error).
            "quantity":    f"{quantity:.6f}",  # string, 6dp per lot size filter
            "price":       f"{price:.2f}",     # string, 2dp = cent precision in USDT
            "timestamp":   int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)

        async with session.post(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers={"X-MEXC-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)  # bypass MIME check
            if resp.status != 200:
                logger.error("MEXC order erreur %d : %s", resp.status, data)
                return None
            oid = str(data.get("orderId", ""))
            return oid or None
    except Exception as e:
        logger.error("MEXC post_order erreur : %s", e)
        return None


# ─── ORDER MANAGEMENT (grid trading) ─────────────────────────────────────────

async def get_order_status(session, symbol, order_id, *,
                           api_key=None, api_secret=None):
    """
    Query the status of a single order.

    Returns "NEW" | "FILLED" | "CANCELED" | "PARTIALLY_FILLED",
    or None on error or in simulation mode (no credentials).

    MEXC v3 endpoint: GET /api/v3/order (Binance-compatible).
    """
    _key    = api_key    or os.environ.get("MEXC_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_API_SECRET", "")
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
            headers={"X-MEXC-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("MEXC get_order_status erreur %d : %s", resp.status, data)
                return None
            return str(data.get("status", "")) or None
    except Exception as e:
        logger.error("MEXC get_order_status erreur : %s", e)
        return None


async def cancel_order(session, symbol, order_id, *,
                       api_key=None, api_secret=None):
    """
    Cancel an open order.

    Returns True on success (status == "CANCELED"), False on error.
    Simulated orders (sim_ prefix) and missing credentials return True immediately.

    MEXC v3 endpoint: DELETE /api/v3/order (Binance-compatible).
    """
    if str(order_id).startswith("sim_"):
        return True
    _key    = api_key    or os.environ.get("MEXC_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_API_SECRET", "")
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
            headers={"X-MEXC-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("MEXC cancel_order erreur %d : %s", resp.status, data)
                return False
            return str(data.get("status", "")) == "CANCELED"
    except Exception as e:
        logger.error("MEXC cancel_order erreur : %s", e)
        return False


async def get_open_orders(session, symbol, *, api_key=None, api_secret=None):
    """
    Return all open orders for `symbol` as a normalised list.

    Each element: {"order_id": str, "side": str, "price": float,
                   "qty": float, "status": str}

    Returns [] on error or in simulation mode.
    MEXC v3 endpoint: GET /api/v3/openOrders (Binance-compatible).
    """
    _key    = api_key    or os.environ.get("MEXC_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_API_SECRET", "")
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
            headers={"X-MEXC-APIKEY": _key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                logger.warning("MEXC get_open_orders erreur %d : %s", resp.status, data)
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
    except Exception as e:
        logger.error("MEXC get_open_orders erreur : %s", e)
        return []


# ─── USER DATA STREAM ─────────────────────────────────────────────────────────

async def get_listen_key(session, *, api_key=None, api_secret=None):
    """
    Create a new MEXC user data stream and return its listenKey.
    Extend with keepalive_listen_key every 30 min (TTL varies, assume 60 min).
    Returns the listenKey string, or None on error / missing credentials.
    """
    key = api_key or os.environ.get("MEXC_API_KEY", "")
    if not key:
        return None
    try:
        async with session.post(
            f"{BASE_URL}/api/v3/userDataStream",
            headers={"X-MEXC-APIKEY": key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("MEXC get_listen_key erreur %d", resp.status)
                return None
            data = await resp.json(content_type=None)
            return data.get("listenKey") or None
    except Exception as e:
        logger.error("MEXC get_listen_key erreur : %s", e)
        return None


async def keepalive_listen_key(session, listen_key, *, api_key=None, api_secret=None):
    """
    Extend the TTL of an existing MEXC listenKey (PUT /api/v3/userDataStream).
    Returns True on success.
    """
    key = api_key or os.environ.get("MEXC_API_KEY", "")
    if not key or not listen_key:
        return False
    try:
        async with session.put(
            f"{BASE_URL}/api/v3/userDataStream",
            params={"listenKey": listen_key},
            headers={"X-MEXC-APIKEY": key},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception as e:
        logger.error("MEXC keepalive_listen_key erreur : %s", e)
        return False


def make_user_stream_url(listen_key: str) -> str:
    """Return the WebSocket URL for the MEXC user data stream."""
    return f"{WS_URL}?listenKey={listen_key}"


def parse_user_stream_msg(msg: dict) -> "dict | None":
    """
    Parse a MEXC private order update WebSocket event.

    MEXC private stream wraps data under the "d" key and uses numeric status codes:
        1 = NEW, 2 = FILLED, 3 = PARTIALLY_FILLED, 4 = CANCELED, 5 = PARTIAL_CANCELED

    Side is also numeric: 1 = BUY, 2 = SELL.

    Returns {"order_id", "status", "side", "symbol"} for fill events, otherwise None.
    """
    if not isinstance(msg, dict):
        return None
    data = msg.get("d")
    if not isinstance(data, dict):
        return None

    raw_status = data.get("s", 0)
    if raw_status == 2:
        status = "FILLED"
    elif raw_status == 3:
        status = "PARTIALLY_FILLED"
    else:
        return None

    raw_side = data.get("S", 0)
    if raw_side == 1:
        side = "BUY"
    elif raw_side == 2:
        side = "SELL"
    else:
        return None

    order_id = str(data.get("i", "") or data.get("orderId", ""))
    if not order_id:
        return None

    return {
        "order_id": order_id,
        "status":   status,
        "side":     side,
        "symbol":   str(msg.get("s", "")),
    }
