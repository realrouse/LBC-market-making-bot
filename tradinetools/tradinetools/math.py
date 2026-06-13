"""
Scalar indicator functions — one value per call (last bar).
Pure stdlib, no external dependencies.
"""

import math as _math


def sma_last(series, n):
    if len(series) < n:
        return None
    return sum(series[-n:]) / n


def ema_last(series, n):
    """EMA(n) seeded from the SMA of the first n values. Returns None when len(series) < n."""
    if len(series) < n:
        return None
    k = 2.0 / (n + 1)
    val = sum(series[:n]) / n
    for x in series[n:]:
        val = x * k + val * (1 - k)
    return val


def atr_last(highs, lows, closes, n):
    """ATR(n) using EMA of True Range. Requires len(closes) >= n+1 (one extra bar for TR)."""
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    return ema_last(trs, n)


def bollinger_last(closes, n, k):
    """Return (upper, mid, lower) for the last bar."""
    mid = sma_last(closes, n)
    if mid is None:
        return None, None, None
    std = _math.sqrt(sum((closes[-n + j] - mid) ** 2 for j in range(n)) / n)
    return mid + k * std, mid, mid - k * std


def vwap_last(closes, volumes, n):
    if len(closes) < n:
        return None
    pv = sum(closes[-n + j] * volumes[-n + j] for j in range(n))
    v  = sum(volumes[-n + j] for j in range(n))
    return pv / v if v > 0 else closes[-1]


def vol_zscore_last(volumes, n):
    if len(volumes) < n:
        return None
    w   = volumes[-n:]
    mu  = sum(w) / n
    std = _math.sqrt(sum((v - mu) ** 2 for v in w) / n)
    return (volumes[-1] - mu) / std if std > 0 else 0.0


def rolling_max_last(series, n):
    if len(series) < n:
        return None
    return max(series[-n:])
