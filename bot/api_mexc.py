"""
MEXC spot API adapter — implements the same public interface as
api_polymarket.py: get_markets, post_order, parse_book_update, compute_fee,
and market metadata helpers.

MEXC v3 REST is Binance-compatible but uses different WebSocket framing.

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
