"""Shared helpers for CEX connector adapters (api_binance, api_mexc, api_bitstamp)."""

import hashlib
import hmac as _hmac
from urllib.parse import urlencode


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
