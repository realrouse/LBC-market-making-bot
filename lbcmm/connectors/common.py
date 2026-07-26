"""Shared helpers for CEX connectors (ported from tradinebotte api_common)."""

from __future__ import annotations

import hashlib
import hmac as _hmac
import math
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from urllib.parse import urlencode


def decimals_of(step) -> int:
    exp = Decimal(str(step)).normalize().as_tuple().exponent
    return -exp if isinstance(exp, int) and exp < 0 else 0


def fmt_price(value: float, decimals: int) -> str:
    q = Decimal(1).scaleb(-decimals)
    return f"{Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP):.{decimals}f}"


def fmt_qty(value: float, decimals: int) -> str:
    q = Decimal(1).scaleb(-decimals)
    return f"{Decimal(str(value)).quantize(q, rounding=ROUND_DOWN):.{decimals}f}"


def parse_levels(raw: list) -> list:
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
        "spread": max(0.0, ba - bb) if ba != float("inf") else 0.0,
        "bid_vol": bv,
        "ask_vol": av,
        "obi": obi,
        "bids": bids,
        "asks": asks,
    }


def hmac_sign(params: dict, secret: str) -> str:
    query = urlencode(params)
    return _hmac.new(secret.encode("utf-8"), query.encode("utf-8"),
                     hashlib.sha256).hexdigest()


def decimals_for_price(ref_price) -> int:
    r = abs(float(ref_price))
    if r >= 1 or r == 0:
        return 2
    return min(8, 2 + int(math.ceil(-math.log10(r))))
