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


def ichimoku_last(highs, lows, closes,
                  tenkan_n=9, kijun_n=26, senkou_b_n=52, displacement=26):
    """Ichimoku Kinko Hyo values for the latest bar.

    Returns a dict with five keys:

      tenkan       Conversion line, CURRENT: (max(high,9)+min(low,9))/2.
      kijun        Base line, CURRENT:       (max(high,26)+min(low,26))/2.
      cloud_top    ) The Senkou Span A/B that APPLY TO THE CURRENT BAR — i.e. the
      cloud_bottom ) spans computed `displacement` (26) bars ago, since the spans
                     are leading (plotted 26 bars ahead). cloud_top = max(A,B),
                     cloud_bottom = min(A,B). Compare live price to THESE, never to
                     the current forming spans (which describe the future cloud).
      chikou       close[t-displacement] — the price the lagging span is compared
                     against; a consumer holding the current close derives the
                     chikou signal as sign(close_now - chikou).

    Each key is None until enough history exists (cloud needs senkou_b_n +
    displacement = 78 bars). Uses only H/L/C lists (chronological, last = latest);
    stateless — recomputes midpoints at the current and displaced-back offsets.
    """
    n = len(closes)

    def _midpoint(k, back=0):
        """(max(high)+min(low))/2 over the k-bar window ending `back` bars ago."""
        if n < k + back:
            return None
        hi = highs[n - k - back: n - back]
        lo = lows[n - k - back: n - back]
        return (max(hi) + min(lo)) / 2.0

    tenkan = _midpoint(tenkan_n)
    kijun  = _midpoint(kijun_n)

    # Cloud applicable to the current bar: spans computed `displacement` bars ago.
    tenkan_past = _midpoint(tenkan_n, displacement)
    kijun_past  = _midpoint(kijun_n,  displacement)
    span_a = (tenkan_past + kijun_past) / 2.0 \
        if tenkan_past is not None and kijun_past is not None else None
    span_b = _midpoint(senkou_b_n, displacement)
    if span_a is not None and span_b is not None:
        cloud_top, cloud_bottom = max(span_a, span_b), min(span_a, span_b)
    else:
        cloud_top = cloud_bottom = None

    chikou = closes[n - 1 - displacement] if n > displacement else None

    return {
        "tenkan":       tenkan,
        "kijun":        kijun,
        "cloud_top":    cloud_top,
        "cloud_bottom": cloud_bottom,
        "chikou":       chikou,
    }
