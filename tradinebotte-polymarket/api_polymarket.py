"""
Polymarket API adapter — implements the generic exchange interface used by the
live bot: get_markets, post_order, parse_book_update, compute_fee, and market
metadata helpers.

To add a new exchange in the future, create api_<exchange>.py with the same
public API surface and change the single import line in live_bot.py.
"""

import hashlib, json, logging, os, sysconfig, sys, uuid
from datetime import datetime, timezone, timedelta
import aiohttp

logger = logging.getLogger("live")

# ─── ENDPOINTS ───────────────────────────────────────────────────────────────
GAMMA_URL     = "https://gamma-api.polymarket.com/markets"
GAMMA_HEADERS = {"User-Agent": "Mozilla/5.0"}
GAMMA_PARAMS  = {"closed": "false", "limit": 100}
CLOB_URL      = "https://clob.polymarket.com"
WS_URL        = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_BATCH_SIZE = 50  # max token IDs per WebSocket subscribe message

# Polymarket tag IDs for timed Up/Down markets
GAMMA_TAG_5M  = 102892   # "5M"  — Bitcoin/ETH/XRP Up or Down 5-minute markets
GAMMA_TAG_15M = 102467   # "15M" — Bitcoin Up or Down 15-minute markets

# BTC Up/Down keyword filter — safety net after server-side tag filter; works for all timeframes
BTC_UPDOWN_KEYWORDS = ("bitcoin up or down", "btc up or down")

# Legacy alias kept for callers that import it directly
BTC_5M_KEYWORDS = BTC_UPDOWN_KEYWORDS

# ─── FEE ─────────────────────────────────────────────────────────────────────
# CRITICAL — backtested parameter (98.3% WR on 1663 trades). Do not modify
# without re-running the full backtest.
FEE_RATE = 0.02  # Polymarket taker fee: 2% of min(price, 1-price) × tokens


def compute_fee(entry_price, tokens_bought):
    """
    Polymarket taker fee: 2% of min(price, 1-price) × token_count.
    The fee is charged on the closer leg to 0.5 to avoid double-charging
    near-certain outcomes. At entry_price=0.97, fee ≈ 2% × 0.03 × tokens.
    """
    return FEE_RATE * min(entry_price, 1.0 - entry_price) * tokens_bought


# ─── MARKET METADATA ─────────────────────────────────────────────────────────
# Polymarket uses several field names across API versions; each helper tries
# all known variants so the bot works with older responses too.

def get_market_id(market):
    """Return the canonical market condition ID."""
    return (market.get("conditionId") or market.get("condition_id") or
            market.get("market_id") or market.get("id") or "")


def get_market_question(market):
    """Return the market question / title string."""
    return market.get("question", "")


def get_market_end_ts_ms(market):
    """Return market end time as Unix milliseconds, or 0.0 if not found."""
    for key in ("endDate", "end_time", "end_date_iso", "end_date"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                pass
    return 0.0


def get_market_start_ts_ms(market):
    """Return market start time as Unix milliseconds, or 0.0 if not found."""
    for key in ("startDate", "start_time", "start_date_iso", "start_date"):
        val = market.get(key)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                pass
    return 0.0


def get_up_token_id(market):
    """
    Extract the UP/YES token ID from a market object.
    clobTokenIds[0] is always the YES/UP token per Polymarket convention.
    Falls back to scanning the tokens array by outcome label.
    """
    clob = market.get("clobTokenIds")
    if clob:
        ids = json.loads(clob) if isinstance(clob, str) else clob
        if ids:
            return str(ids[0])
    tokens = market.get("tokens") or []
    for t in tokens:
        if isinstance(t, dict) and t.get("outcome", "").lower() in ("yes", "up"):
            return t.get("token_id")
    if tokens:
        t = tokens[0]
        return t.get("token_id") if isinstance(t, dict) else str(t)
    return None


def get_down_token_id(market):
    """
    Extract the DOWN/NO token ID from a market object.
    clobTokenIds[1] is always the NO/DOWN token per Polymarket convention.
    """
    clob = market.get("clobTokenIds")
    if clob:
        ids = json.loads(clob) if isinstance(clob, str) else clob
        if len(ids) > 1:
            return str(ids[1])
    tokens = market.get("tokens") or []
    for t in tokens:
        if isinstance(t, dict) and t.get("outcome", "").lower() in ("no", "down"):
            return t.get("token_id")
    if len(tokens) > 1:
        t = tokens[1]
        return t.get("token_id") if isinstance(t, dict) else str(t)
    return None


# ─── WEBSOCKET ────────────────────────────────────────────────────────────────

def make_subscribe_msg(token_ids):
    """Return a JSON string to subscribe to a batch of Polymarket token IDs."""
    return json.dumps({"type": "subscribe", "channel": "market", "assets_ids": token_ids})


# ─── ORDER BOOK ───────────────────────────────────────────────────────────────

def parse_book_update(msg):
    """
    Parse a Polymarket WebSocket message into a normalized price snapshot.

    The feed emits three event types we care about:
      "book"             — full order book snapshot (sent on subscribe)
      "price_change"     — incremental update when a level changes
      "last_trade_price" — last executed trade price

    OBI (Order Book Imbalance) = (bid_vol - ask_vol) / (bid_vol + ask_vol)
    Computed over the top 5 levels on each side. A negative OBI means the
    ask side is heavier, indicating sell pressure — entries are rejected when
    OBI < OBI_REJECT_THRESH (-0.50).

    Returns a dict with token_id, best_bid, best_ask, spread, bid_vol,
    ask_vol, obi, or None if the message is irrelevant or malformed.
    """
    event_type = msg.get("event_type") or msg.get("type", "")
    if event_type not in ("book", "price_change", "last_trade_price"):
        return None
    token_id = str(msg.get("asset_id") or "")
    if not token_id:
        return None

    def pl(lst):
        """Parse a list of price-level entries into (price, size) tuples."""
        r = []
        for item in lst:
            try:
                if isinstance(item, dict):
                    p, s = float(item.get("price", 0)), float(item.get("size", 0))
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    p, s = float(item[0]), float(item[1])
                else:
                    continue
                if p > 0 and s > 0:
                    r.append((p, s))
            except Exception:
                continue
        return r

    bids = sorted(pl(msg.get("bids") or []), reverse=True)  # highest price first
    asks = sorted(pl(msg.get("asks") or []))                 # lowest price first
    if not bids and not asks:
        return None
    bb = bids[0][0] if bids else 0.0
    ba = asks[0][0] if asks else 1.0
    bv = sum(s for _, s in bids[:5])  # aggregate top-5 bid depth
    av = sum(s for _, s in asks[:5])  # aggregate top-5 ask depth
    tv = bv + av
    obi = (bv - av) / tv if tv > 0 else 0.0
    return {
        "token_id": token_id, "best_bid": bb, "best_ask": ba,
        "spread": max(0.0, ba - bb), "bid_vol": bv, "ask_vol": av, "obi": obi,
    }


# ─── MARKET DISCOVERY ─────────────────────────────────────────────────────────

async def get_markets(session, *, tag_id: int = GAMMA_TAG_5M, window_minutes: int = 6):
    """
    Fetch active BTC Up/Down markets from the Polymarket Gamma API.

    CRITICAL: The temporal window filter is mandatory.
    Without it, the Gamma API returns expired markets whose order books
    still show stale prices that falsely trigger the signal.

    tag_id pre-filters server-side by market type:
      GAMMA_TAG_5M  (102892) — 5-minute markets, window ±6 min
      GAMMA_TAG_15M (102467) — 15-minute markets, window ±16 min
    BTC_UPDOWN_KEYWORDS is kept as a safety net for non-BTC assets in the same tag.

    Returns None on an API/network error (so callers can tell a failure from a genuinely
    empty market window — masking errors as [] caused the feed to stall silently); [] when
    the request succeeds but no market is in the window; the market list otherwise.
    """
    try:
        params = dict(GAMMA_PARAMS)
        params["tag_id"] = tag_id
        now_utc = datetime.now(timezone.utc)
        params["end_date_min"] = (now_utc - timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["end_date_max"] = (now_utc + timedelta(minutes=window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        async with session.get(
            GAMMA_URL, headers=GAMMA_HEADERS, params=params,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("Gamma API error: %d", resp.status)
                return None   # error sentinel — distinct from [] (no market in window)
            data = await resp.json(content_type=None)
        batch = data if isinstance(data, list) else data.get("data", data.get("markets", []))
        results = [
            m for m in batch
            if any(kw in m.get("question", "").lower() for kw in BTC_UPDOWN_KEYWORDS)
        ]
        logger.info("BTC markets (tag=%d, window=±%dm): %d", tag_id, window_minutes, len(results))
        return results
    except Exception as e:
        logger.warning("Fetch error: %s", e)
        return None   # error sentinel — distinct from [] (no market in window)


# ─── ORDER PLACEMENT ──────────────────────────────────────────────────────────

# Keyed by (sha256(private_key)[:16], install_dir) — built once on first order, reused thereafter.
# Key is a hash digest, not the raw private key, so the key material is never stored in the dict.
_clob_clients: dict = {}


def _init_clob_client(private_key: str, install_dir: str) -> tuple:
    """
    Build an authenticated ClobClient + import OrderArgs, inject the venv
    site-packages once, and cache the result.  Called only on the first order
    for a given (private_key, install_dir) pair.
    """
    _venv = os.path.join(install_dir, "venv")
    _site = sysconfig.get_path("purelib", vars={"platbase": _venv, "base": _venv})
    if _site and _site not in sys.path:
        sys.path.insert(0, _site)
    from py_clob_client.client import ClobClient          # noqa: PLC0415
    from py_clob_client.constants import POLYGON          # noqa: PLC0415
    from py_clob_client.clob_types import OrderArgs       # noqa: PLC0415
    client = ClobClient(CLOB_URL, key=private_key, chain_id=POLYGON, signature_type=0)
    client.set_api_creds(client.create_or_derive_api_creds())
    logger.info("CLOB client initialized (install_dir=%s)", install_dir)
    return client, OrderArgs


async def post_order(session, token_id, price, size_usdc, *, private_key, install_dir):
    """
    Submit a LIMIT BUY order to the Polymarket CLOB.

    py_clob_client is imported lazily (only installed inside the venv).
    The ClobClient is cached per (private_key, install_dir) so EIP-712 key
    derivation runs only once per process, not on every trade.

    Returns the order ID string on success, a "sim_..." string in dry-run
    mode (no private key), or None on error.
    """
    if not private_key:
        logger.warning("POLY_PRIVATE_KEY not set — order simulated")
        return f"sim_{uuid.uuid4().hex[:12]}"
    try:
        cache_key = (hashlib.sha256(private_key.encode()).hexdigest()[:16], install_dir)
        if cache_key not in _clob_clients:
            _clob_clients[cache_key] = _init_clob_client(private_key, install_dir)
        _client, OrderArgs = _clob_clients[cache_key]
        size_tokens = round(size_usdc / price, 4)
        _order = _client.create_order(OrderArgs(
            token_id=token_id, price=round(price, 4),
            size=size_tokens, side="BUY"))
        _resp = _client.post_order(_order)
        oid = str(_resp.get("orderID") or _resp.get("id") or "")
        if oid:
            return oid
        logger.warning("CLOB resp without orderID: keys=%s", list(_resp.keys()))
        return None
    except Exception as e:
        logger.error("CLOB error: %s", e)
        return None
