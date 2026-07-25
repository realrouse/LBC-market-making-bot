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

import json
import logging
import os
import time
import uuid
import aiohttp
from api_common import (book_snapshot, decimals_of, fmt_price, fmt_qty,
                        hmac_sign as _sign, parse_levels)

logger = logging.getLogger(__name__)

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────
BASE_URL      = "https://api.mexc.com"


def _write_headers(key):
    """Auth headers for a MEXC *write* (POST/PUT/DELETE) call.

    MEXC rejects a POST without `Content-Type: application/json` — `700013 Invalid
    content Type` — even though every parameter travels in the QUERY STRING and the
    body is empty. `application/x-www-form-urlencoded`, which is what the query-string
    framing would suggest, is rejected identically (both probed against /order/test).
    GETs must NOT carry it. This is why the first real MEXC order ever placed failed:
    until now every MEXC bot was simulated, so post_order never hit the live endpoint.
    """
    return {"X-MEXC-APIKEY": key, "Content-Type": "application/json"}


# Per-symbol (price_decimals, qty_decimals), fetched once from exchangeInfo. MEXC
# reports price precision as `quotePrecision` and the lot step as `baseSizePrecision`
# (verified BTCUSDT=2/6dp, ETHUSDT=2/4dp, LBCUSDT=6/3dp). Cache is process-lived; a
# symbol's tick does not change intraday.
_SYMBOL_PRECISION: dict = {}


async def get_symbol_precision(session, symbol):
    """Return (price_decimals, qty_decimals) for `symbol` from MEXC exchangeInfo, cached.

    exchangeInfo is a PUBLIC endpoint (works without credentials, i.e. in sim too).
    Returns None on failure so callers FAIL CLOSED: a real order must never be
    formatted with a guessed precision (that is exactly the ".2f floors LBC to 0.00"
    bug). Price precision = quotePrecision; qty step = decimals_of(baseSizePrecision).
    """
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
# MEXC migrated spot public WS to protobuf on wbs-api.mexc.com; the old wbs.mexc.com/ws
# + JSON depth channels are retired (every subscribe is rejected "Not Subscribed /
# Blocked!"). Public depth now streams as binary protobuf frames — WS_BINARY tells
# cex_feed not to json.loads the frame but to hand the raw bytes to parse_book_update.
WS_URL        = "wss://wbs-api.mexc.com/ws"
WS_BINARY     = True
WS_BATCH_SIZE = 10  # streams per WebSocket connection

DEFAULT_SYMBOL = "BTCUSDT"

# MEXC spot fees. NOT the 0.2% this used to assume (4x too high, making every paper sim
# needlessly pessimistic).
#
# MAKER = 0, and that is MEASURED, not hoped: our first real fill reports
# `isMaker=True commission=0 USDT` in myTrades. This is the rate that matters — the live
# bot posts resting maker orders precisely so it pays nothing.
#
# TAKER = 0.04% EFFECTIVE, and the two sources that look contradictory are both right:
#   /api/v3/tradeFee?symbol=LBCUSDT -> takerCommission 0.0005   (the account's BASE tier)
#   the MEXC account page           -> 0.0400%                  (EFFECTIVE, after MX)
# 0.0005 x 0.8 = 0.0004: the 20% MX deduction is applied at settlement and is NOT reflected
# in the API's advertised rate. So the API can confirm the base tier but can never confirm
# what we are actually charged. If the MX deduction lapses (no MX held / toggled off) this
# reverts to 0.0005 — treat 0.0004 as the current tier, not a constant of nature.
# (/api/v3/account exposes no commission fields at all on MEXC, unlike Binance: they come
# back None. tradeFee is the account-level endpoint, and needs the "view account details"
# key permission.) Only paper sims depend on this; real orders are maker-only.
FEE_RATE       = 0.0004   # taker, 0.04% effective (base 0.0005 x 0.8 MX deduction)
MAKER_FEE_RATE = 0.0      # verified 0 on a real trade


def compute_fee(price, quantity):
    """MEXC taker fee: 0.04% of notional (price × quantity in USDT). Maker fills are free
    (MAKER_FEE_RATE) — callers that know they rested should not use this."""
    return FEE_RATE * price * quantity


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────

def make_subscribe_msg(symbols):
    """
    Return a JSON subscribe message for MEXC spot limit-depth streams (protobuf).
    Format: spot@public.limit.depth.v3.api.pb@<SYMBOL>@5  (top-5 snapshot per frame)
    The subscribe request itself is JSON; the depth DATA streams back as binary
    protobuf (see parse_book_update). Strips any ":SELL" suffix in the symbol string.
    """
    params = [
        f"spot@public.limit.depth.v3.api.pb@{s.split(':')[0]}@5"
        for s in symbols
    ]
    return json.dumps({
        "method": "SUBSCRIPTION",
        "params": params,
    })


def make_ping_msg():
    """App-level keepalive for the public WS. MEXC spot closes the socket with
    code 1005 after ~30 s of silence (the websockets protocol PING is not honoured),
    so cex_feed sends this periodically to keep the depth stream alive."""
    return json.dumps({"method": "PING"})


# ─── ORDER BOOK ───────────────────────────────────────────────────────────────

def parse_book_update(msg):
    """
    Parse a MEXC spot depth frame into a normalized price snapshot.

    The spot WS (wbs-api.mexc.com) streams the limit-depth channel as BINARY protobuf
    (PushDataV3ApiWrapper → publicLimitDepths: top-5 asks/bids per frame). The subscribe
    ACK and any control frames arrive as text/JSON and carry no depth → return None.

    Accepts bytes (the protobuf frame). For robustness also tolerates a pre-decoded
    dict in the legacy {"d": {"asks":..,"bids":..}, "s": symbol} shape (tests).

    OBI = (bid_vol - ask_vol) / (bid_vol + ask_vol) over the top levels, via book_snapshot.
    Returns {token_id, best_bid, best_ask, spread, bid_vol, ask_vol, obi} or None.
    """
    if isinstance(msg, (bytes, bytearray)):
        return _parse_pb_depth(msg)
    if isinstance(msg, dict):
        data   = msg.get("d")
        symbol = msg.get("s", DEFAULT_SYMBOL)
        if not data:
            return None
        bids = sorted(parse_levels(data.get("bids") or []), reverse=True)
        asks = sorted(parse_levels(data.get("asks") or []))
        if not bids and not asks:
            return None
        return book_snapshot(bids, asks, symbol)
    return None   # text ack / unknown frame


def _parse_pb_depth(raw):
    """Decode a binary MEXC spot protobuf depth frame → normalized snapshot, or None."""
    try:
        import mexc_spot_depth_pb2 as _pb  # noqa: PLC0415  pylint: disable=import-outside-toplevel
    except ImportError:
        logger.error("mexc_spot_depth_pb2 not importable — is protobuf installed + _pb2 deployed?")
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


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────

async def get_markets(session, symbol=DEFAULT_SYMBOL, **_):
    """
    Fetch the current order book ticker for the given symbol from MEXC REST.
    Returns a 1-element list with the normalized market dict on success, or None on an
    API/network error (callers distinguish failure from data via `is None`).
    """
    try:
        async with session.get(
            f"{BASE_URL}/api/v3/ticker/bookTicker",
            params={"symbol": symbol},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("MEXC get_markets error %d [symbol=%s]", resp.status, symbol)
                return None   # error sentinel — not [] ("ran fine, no data")
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
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("MEXC get_markets fetch error [symbol=%s]: %s", symbol, e)
        return None   # error sentinel — not [] ("ran fine, no data")


# ─── ORDER PLACEMENT ──────────────────────────────────────────────────────────

async def post_order(session, symbol, price, size_usdc=None, *,
                     api_key=None, api_secret=None, side="BUY",
                     quantity=None, order_type="LIMIT",
                     private_key=None, install_dir=None):  # pylint: disable=unused-argument
    """
    Submit a LIMIT order to MEXC spot.

    `quantity` (base asset) and `size_usdc` (quote notional) are alternatives: pass
    quantity to size an order in COINS (a sell of a known holding), size_usdc to size it
    in USDT (a buy of a known budget). Passing quantity avoids the notional round-trip
    `(qty*price)/price`, which is why sells use it.

    `order_type="LIMIT_MAKER"` is post-only: the exchange guarantees the order never
    crosses. ⚠ MEXC does NOT reject a crossing LIMIT_MAKER at the API — it ACCEPTS it
    (HTTP 200 + orderId) and immediately auto-cancels it (verified live: status=CANCELED,
    executedQty=0). So a returned order id does NOT prove the order is resting; the
    caller must confirm with get_order before treating it as live.

    Args:
        symbol    : trading pair, e.g. "BTCUSDT". Append ":SELL" to force
                    the SELL side (the ':SELL' suffix convention).
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
        logger.warning("MEXC — order simulated (MEXC_API_KEY/SECRET not set)")
        return f"sim_{uuid.uuid4().hex[:12]}"

    prec = await get_symbol_precision(session, _sym)
    if prec is None:
        # Fail closed: never place a real order with a guessed tick/lot precision.
        logger.error("MEXC — no precision for %s, refusing order (fail-closed)", _sym)
        return None
    price_dec, qty_dec = prec

    try:
        if quantity is None:
            if size_usdc is None:
                logger.error("MEXC — post_order needs quantity or size_usdc")
                return None
            quantity = size_usdc / price      # base-asset amount, floored to lot step below
        params = {
            "symbol":      _sym,
            "side":        _side,
            "type":        order_type,
            # MEXC LIMIT orders default to GTC server-side; timeInForce is not required
            # (unlike Binance where omitting it returns an error).
            "quantity":    fmt_qty(quantity, qty_dec),   # floored to baseSizePrecision
            "price":       fmt_price(price, price_dec),   # rounded to quotePrecision tick
            "timestamp":   int(time.time() * 1000),
        }
        params["signature"] = _sign(params, _secret)

        async with session.post(
            f"{BASE_URL}/api/v3/order",
            params=params,
            headers=_write_headers(_key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json(content_type=None)  # bypass MIME check
            if resp.status != 200:
                logger.error("MEXC order error %d [%s %s qty=%s @ %s]: code=%s msg=%s",
                             resp.status, _side, _sym,
                             fmt_qty(quantity, qty_dec), fmt_price(price, price_dec),
                             data.get("code"), data.get("msg", str(data)[:200]))
                return None
            oid = str(data.get("orderId", ""))
            return oid or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC post_order error [%s %s @ %s]: %s",
                     _side, _sym, fmt_price(price, price_dec), e)
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
            # MEXC order ids are opaque STRINGS (e.g. "C01__4465…"), not Binance ints:
            # int() would raise, get swallowed by the broad except, and silently turn
            # every cancel/poll into a no-op. Verified: the API resolves a string id.
            "orderId":   str(order_id),
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
                logger.warning("MEXC get_order_status error %d : %.300s", resp.status, data)
                return None
            return str(data.get("status", "")) or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_order_status error : %s", e)
        return None


async def get_order(session, symbol, order_id, *, api_key=None, api_secret=None):
    """Full order detail for FILL reconciliation (GET /api/v3/order). Returns:
        {"status": str, "orig_qty": float, "executed_qty": float,
         "cummulative_quote_qty": float, "avg_price": float | None, "side": str}
    or None on error / simulation / a sim_ id. `avg_price` = cummulative_quote_qty /
    executed_qty (None until any fill). Unlike get_order_status (status string only), this
    carries the filled base amount + quote spent needed to credit REAL holdings/cost."""
    _key    = api_key    or os.environ.get("MEXC_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_API_SECRET", "")
    if not _key or not _secret:
        return None
    if str(order_id).startswith("sim_"):
        return None
    try:
        params = {
            "symbol":    str(symbol).split(":", maxsplit=1)[0],
            # MEXC order ids are opaque STRINGS (e.g. "C01__4465…"), not Binance ints:
            # int() would raise, get swallowed by the broad except, and silently turn
            # every cancel/poll into a no-op. Verified: the API resolves a string id.
            "orderId":   str(order_id),
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
                logger.warning("MEXC get_order error %d : %.300s", resp.status, data)
                return None
            exq = float(data.get("executedQty", 0) or 0)
            cqq = float(data.get("cummulativeQuoteQty", 0) or 0)
            return {
                "status":                str(data.get("status", "")),
                "orig_qty":              float(data.get("origQty", 0) or 0),
                "executed_qty":          exq,
                "cummulative_quote_qty": cqq,
                "avg_price":             (cqq / exq if exq > 0 else None),
                "side":                  str(data.get("side", "")),
            }
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_order error : %s", e)
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
            # MEXC order ids are opaque STRINGS (e.g. "C01__4465…"), not Binance ints:
            # int() would raise, get swallowed by the broad except, and silently turn
            # every cancel/poll into a no-op. Verified: the API resolves a string id.
            "orderId":   str(order_id),
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
                logger.warning("MEXC cancel_order error %d : %.300s", resp.status, data)
                return False
            # Success is HTTP 200 + the echoed orderId — NOT status=="CANCELED". MEXC's
            # DELETE response reports the order's status *before* the cancel (verified live:
            # a successful cancel echoes "NEW", and only a subsequent GET shows CANCELED).
            # Comparing to "CANCELED" therefore reported False on every successful cancel.
            return bool(data.get("orderId"))
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC cancel_order error : %s", e)
        return False


async def get_open_orders(session, symbol, *, api_key=None, api_secret=None):
    """
    Return all open orders for `symbol` as a normalised list.

    Each element: {"order_id": str, "side": str, "price": float,
                   "qty": float, "status": str}

    Returns None on an API/network error (a transient failure must not be read as
    "no open orders"); [] in simulation mode (no credentials) or genuinely no orders.
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
                logger.warning("MEXC get_open_orders error %d : %.300s", resp.status, data)
                return None   # error sentinel — not [] (sim/no-orders)
            return [
                {
                    "order_id":     str(o.get("orderId", "")),
                    "side":         str(o.get("side", "")),
                    "price":        float(o.get("price", 0)),
                    "qty":          float(o.get("origQty", 0)),
                    "status":       str(o.get("status", "")),
                    # Fill progress, so ONE openOrders call per tick can credit partial
                    # fills on resting orders — instead of a get_order per tracked order.
                    "executed_qty":          float(o.get("executedQty", 0) or 0),
                    "cummulative_quote_qty": float(o.get("cummulativeQuoteQty",
                                                         o.get("cumulativeQuoteQty", 0)) or 0),
                }
                for o in data
            ]
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_open_orders error : %s", e)
        return None   # error sentinel — not [] (sim/no-orders)


# ─── ACCOUNT ─────────────────────────────────────────────────────────────────

async def get_account(session, *, api_key=None, api_secret=None):
    """Signed GET /api/v3/account — spot account balances + trading permissions.

    Returns:
        {"can_trade": bool, "permissions": [str, ...],
         "balances": {ASSET: {"free": float, "locked": float}, ...}}
    or **None** on error / in simulation mode (no credentials).

    ⚠ None means UNKNOWN, never "zero balance": this endpoint needs the API key's
    *account-read* scope, which is a DIFFERENT permission from order placement/read —
    a key can post_order yet still get HTTP 400 code=700007 "No permission to access
    the endpoint" here. Callers must treat None as "can't tell" and fall back to their
    own internally-tracked balance, not assume an empty wallet.
    """
    _key    = api_key    or os.environ.get("MEXC_API_KEY", "")
    _secret = api_secret or os.environ.get("MEXC_API_SECRET", "")
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
                logger.warning("MEXC get_account error %d: code=%s msg=%s", resp.status,
                               data.get("code"), data.get("msg", str(data)[:200]))
                return None
            balances = {
                b["asset"]: {"free": float(b.get("free", 0.0)),
                             "locked": float(b.get("locked", 0.0))}
                for b in data.get("balances", []) if isinstance(b, dict) and b.get("asset")
            }
            return {"can_trade":   bool(data.get("canTrade", False)),
                    "permissions": data.get("permissions", []),
                    "balances":    balances}
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_account error: %s", e)
        return None


async def get_balance(session, asset, *, api_key=None, api_secret=None):
    """Free (available) balance for one asset, e.g. get_balance(session, "USDT") → float.
    Returns None when the account is unreadable (sim / missing scope / error) — None is
    UNKNOWN, not 0.0, so a caller never mistakes an unreadable key for an empty wallet."""
    acct = await get_account(session, api_key=api_key, api_secret=api_secret)
    if acct is None:
        return None
    return acct["balances"].get(str(asset).upper(), {}).get("free", 0.0)


# ─── USER DATA STREAM ─────────────────────────────────────────────────────────

async def get_listen_key(session, *, api_key=None, api_secret=None):  # pylint: disable=unused-argument
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
            headers=_write_headers(key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning("MEXC get_listen_key error %d", resp.status)
                return None
            data = await resp.json(content_type=None)
            return data.get("listenKey") or None
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC get_listen_key error : %s", e)
        return None


async def keepalive_listen_key(session, listen_key, *, api_key=None, api_secret=None):  # pylint: disable=unused-argument
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
            headers=_write_headers(key),
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            return resp.status == 200
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.error("MEXC keepalive_listen_key error : %s", e)
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
