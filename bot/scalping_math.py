#!/usr/bin/env python3
"""
Scalar indicator functions for scalping — one value per call (last bar).

Shared between bot/scalping_bot.py (live trading) and
scripts/backtest_scalping.py (simulation). Pure stdlib, no dependencies.
"""

import math


def sma_last(series, n):
    """SMA of the last n values in series."""
    if len(series) < n:
        return None
    return sum(series[-n:]) / n


def ema_last(series, n):
    """EMA(n) computed over the full series; returns the final (last) value."""
    if len(series) < n:
        return None
    k = 2.0 / (n + 1)
    val = sum(series[:n]) / n
    for x in series[n:]:
        val = x * k + val * (1 - k)
    return val


def atr_last(highs, lows, closes, n):
    """ATR(n) using EMA smoothing; returns the final value."""
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
    """Bollinger Bands for the last bar; returns (upper, mid, lower)."""
    mid = sma_last(closes, n)
    if mid is None:
        return None, None, None
    std = math.sqrt(sum((closes[-n + j] - mid) ** 2 for j in range(n)) / n)
    return mid + k * std, mid, mid - k * std


def vwap_last(closes, volumes, n):
    """Rolling VWAP over the last n bars."""
    if len(closes) < n:
        return None
    pv = sum(closes[-n + j] * volumes[-n + j] for j in range(n))
    v  = sum(volumes[-n + j] for j in range(n))
    return pv / v if v > 0 else closes[-1]


def vol_zscore_last(volumes, n):
    """Volume z-score: (last_vol - mean(n)) / std(n)."""
    if len(volumes) < n:
        return None
    w   = volumes[-n:]
    mu  = sum(w) / n
    std = math.sqrt(sum((v - mu) ** 2 for v in w) / n)
    return (volumes[-1] - mu) / std if std > 0 else 0.0


def rolling_max_last(series, n):
    """Maximum of the last n values in series."""
    if len(series) < n:
        return None
    return max(series[-n:])
