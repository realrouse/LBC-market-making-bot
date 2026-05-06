"""
Binance spot API adapter — implements the same public interface as
api_polymarket.py: get_markets, post_order, parse_book_update, compute_fee,
and market metadata helpers.

Credentials: BINANCE_API_KEY and BINANCE_API_SECRET env vars,
or pass api_key / api_secret kwargs to post_order.

Compatibility note: the Polymarket signal (best_bid >= 0.96) uses a 0–1
probability scale. On Binance, best_bid is an absolute USDT price (e.g. 65000).
Strategy thresholds in strategies/*.json must be recalibrated for CEX use.

To switch live_bot.py to Binance:
    import api_binance as api   # line 62 in live_bot.py
"""

import hashlib, hmac, json, logging, os, time, uuid
from urllib.parse import urlencode
import aiohttp

logger = logging.getLogger("live")

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
BASE_URL      = "https://api.binance.com"
WS_URL        = "wss://stream.binance.com:9443/stream"  # combined stream endpoint
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
    return market.get("symbol", "")


def get_market_question(market):
    return market.get("question", market.get("symbol", ""))


def get_market_end_ts_ms(market):
    return 0.0  # spot markets have no expiry


def get_market_start_ts_ms(market):
    return 0.0  # not applicable to spot


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
                logger.warning("Binance API erreur : %d", resp.status)
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
    except Exception as e:
        logger.warning("Binance fetch erreur : %s", e)
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
        logger.warning("Binance — ordre simule (BINANCE_API_KEY/SECRET absents)")
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
                logger.error("Binance order erreur %d : %s", resp.status, data)
                return None
            oid = str(data.get("orderId", ""))
            return oid or None
    except Exception as e:
        logger.error("Binance post_order erreur : %s", e)
        return None
