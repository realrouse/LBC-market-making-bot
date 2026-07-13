"""Shared helpers for CEX connector adapters (api_binance, api_mexc, api_bitstamp)."""

import hashlib
import hmac as _hmac
import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode


# ─── PER-SYMBOL PRICE / QUANTITY PRECISION ───────────────────────────────────
# A trading pair's tick (price decimals) and lot step (quantity decimals) are
# defined by the EXCHANGE, not the asset class — BTCUSDT quotes in cents (2dp)
# but LBCUSDT quotes to 6dp. Formatting every pair as f"{price:.2f}" silently
# floors a sub-cent price to "0.00", so precision must be derived from the
# exchange's exchangeInfo per symbol (see each connector's get_symbol_precision)
# and threaded through order formatting. These helpers do the formatting; the
# connectors own the fetch + cache.

def decimals_of(step) -> int:
    """Number of decimal places implied by a tick/step size.

    Accepts the string or float exchanges report for tick/lot size:
    '0.001'->3, '1e-6'->6, '0.01'->2, '1'->0, '100'->0. Used to turn a
    stepSize/baseSizePrecision into a decimal count for fixed-point formatting.
    """
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


async def warm_symbol_precision(api, session, symbol):
    """Prime a connector's per-symbol precision cache at boot, so the first real order
    doesn't pay the exchangeInfo round-trip and an unreachable exchange surfaces now
    (a warning) rather than as a fail-closed rejection mid-trade. No-op when the
    connector exposes no get_symbol_precision (e.g. the sim-only accumulation path).
    Returns the (price_decimals, qty_decimals) tuple, or None on failure."""
    get_prec = getattr(api, "get_symbol_precision", None)
    if get_prec is None:
        return None
    try:
        return await get_prec(session, str(symbol).split(":", maxsplit=1)[0])
    except Exception:  # pylint: disable=broad-exception-caught
        return None


def decimals_for_price(ref_price) -> int:
    """Sensible decimal count for a price of this magnitude, for STORING/LOGGING a
    derived price (a TP/SL) when no exchange tick is on hand — BTC ~64000 → 2dp,
    LBC ~0.002 → 6dp. NOT for order placement: a placed order is always formatted
    with the exchange's real tick via fmt_price(price, get_symbol_precision(...)).
    Order-placement code passes the raw float and lets the connector round; threshold
    comparisons use the raw float and must not round at all.
    """
    r = abs(float(ref_price))
    if r >= 1 or r == 0:
        return 2
    return min(8, 2 + int(math.ceil(-math.log10(r))))


def fmt_price(value: float, decimals: int) -> str:
    """Fixed-point price string rounded (nearest) to `decimals` places.

    Round-to-nearest is correct for a price: it lands on the closest valid tick.
    Decimal avoids binary float artefacts (0.00211 → "0.002110", never "0.00").
    """
    q = Decimal(1).scaleb(-decimals)
    return f"{Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP):.{decimals}f}"


def fmt_qty(value: float, decimals: int) -> str:
    """Fixed-point quantity string FLOORED to `decimals` places (the lot step).

    Floor, never round up: rounding a BUY quantity up overspends the budget and
    rounding a SELL up oversells the held balance — both get rejected or overfill.
    """
    q = Decimal(1).scaleb(-decimals)
    return f"{Decimal(str(value)).quantize(q, rounding=ROUND_DOWN):.{decimals}f}"


def parse_levels(raw: list) -> list:
    """Parse raw price-level data into (price, size) float tuples, skipping zeros/malformed."""
    result = []
    for item in raw:
        try:
            p, s = float(item[0]), float(item[1])
            if p > 0 and s > 0:
                result.append((p, s))
        except Exception:  # pylint: disable=broad-exception-caught
            continue
    return result


def book_snapshot(bids: list, asks: list, symbol: str, depth: int = 5) -> dict:
    """Build the normalized book snapshot dict from sorted (price, size) lists."""
    bb = bids[0][0] if bids else 0.0
    ba = asks[0][0] if asks else float("inf")
    bv = sum(s for _, s in bids[:depth])
    av = sum(s for _, s in asks[:depth])
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


def hmac_sign(params: dict, secret: str) -> str:
    """HMAC-SHA256 signature over the URL-encoded query string."""
    query = urlencode(params)
    return _hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                     hashlib.sha256).hexdigest()
