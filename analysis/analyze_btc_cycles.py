#!/usr/bin/env python3
"""
Detect BTC 4-year cycle tops and bottoms using three on-chain / price indicators.

Indicators
----------
  Pi Cycle Top   : 111-day MA crosses above 2×350-day MA → top signal
  200-week SMA   : price / 200-week SMA < 1.0 → historically a bottom zone
  Mayer Multiple : price / 200-day MA; >2.4 = top zone, <0.8 = bottom zone

Usage
-----
    python3 analysis/analyze_btc_cycles.py
    python3 analysis/analyze_btc_cycles.py data/BTCUSDT3197d_20172026.db
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Ground-truth events (manual, chart-verified)
# ---------------------------------------------------------------------------
TOPS = [
    ("2017-12-17", 19_800),
    ("2021-11-10", 69_000),
    ("2025-10-01", 126_200),   # approximate; DB shows Oct 2025 peak
]

BOTTOMS = [
    ("2018-12-15", 3_100),
    ("2022-11-21", 15_500),
]

MATCH_DAYS = 60   # ±60 days counts as a match


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ts(date_str: str) -> int:
    """YYYY-MM-DD → Unix seconds UTC."""
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def load_daily(db_path: str) -> list[dict]:
    """Aggregate 1m klines to daily OHLCV by streaming rows; returns list sorted by day_ts."""
    conn = sqlite3.connect(db_path)
    # Use a simple GROUP BY with last_close via MAX trick (ts is PK so last close = close at MAX ts)
    rows = conn.execute("""
        SELECT
            (ts_ms / 86400000) * 86400  AS day_ts,
            MAX(high)   AS high,
            MIN(low)    AS low,
            SUM(volume) AS volume,
            MAX(ts_ms)  AS last_ts_ms
        FROM klines
        GROUP BY day_ts
        ORDER BY day_ts
    """).fetchall()
    # Fetch close prices for the last candle of each day in a second pass
    last_ts_set = [r[4] for r in rows]
    placeholders = ",".join("?" * len(last_ts_set))
    close_map = dict(conn.execute(
        f"SELECT ts_ms, close FROM klines WHERE ts_ms IN ({placeholders})",
        last_ts_set
    ).fetchall())
    conn.close()

    result = []
    for r in rows:
        day_ts   = r[0]
        last_ms  = r[4]
        close_px = close_map.get(last_ms)
        if close_px is None:
            continue
        result.append({
            "ts":     day_ts,
            "date":   datetime.fromtimestamp(day_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
            "high":   r[1],
            "low":    r[2],
            "close":  close_px,
            "volume": r[3],
        })
    return result


def sma(series: list[float], n: int) -> list[float | None]:
    """Simple moving average; first n-1 values are None."""
    out: list[float | None] = [None] * (n - 1)
    for i in range(n - 1, len(series)):
        out.append(sum(series[i - n + 1 : i + 1]) / n)
    return out


def closest_event_days(ts_sec: int, events: list[tuple]) -> int:
    """Return minimum abs-days distance from ts_sec to any ground-truth event."""
    min_d = 10_000
    for date_str, _ in events:
        event_ts = _ts(date_str)
        d = abs(ts_sec - event_ts) // 86400
        if d < min_d:
            min_d = d
    return min_d


def print_signals(label: str, signals: list[dict],
                  ref_tops: list[tuple], ref_bottoms: list[tuple]) -> None:
    if not signals:
        print(f"  {label}: no signals found\n")
        return
    print(f"  {label}  ({len(signals)} signals)")
    for s in signals:
        top_dist    = closest_event_days(s["ts"], ref_tops)
        bottom_dist = closest_event_days(s["ts"], ref_bottoms)
        hit_top    = "✓ TOP hit"    if top_dist    <= MATCH_DAYS else f"  (top {top_dist}d away)"
        hit_bottom = "✓ BOT hit"    if bottom_dist <= MATCH_DAYS else f"  (bot {bottom_dist}d away)"
        print(f"    {s['date']}  ${s['price']:>10,.0f}  {hit_top}  {hit_bottom}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "data" / "BTCUSDT3197d_20172026.db"
    )
    if not Path(db_path).exists():
        sys.exit(f"DB not found: {db_path}")

    print(f"Loading daily klines from {db_path} …")
    daily = load_daily(db_path)
    print(f"  {len(daily)} daily bars  "
          f"({daily[0]['date']} → {daily[-1]['date']})\n")

    closes  = [d["close"] for d in daily]
    lows    = [d["low"]   for d in daily]
    highs   = [d["high"]  for d in daily]
    tss     = [d["ts"]    for d in daily]
    dates   = [d["date"]  for d in daily]
    n       = len(closes)

    # -----------------------------------------------------------------------
    # Indicator 1: Pi Cycle Top — 111DMA vs 2×350DMA
    # -----------------------------------------------------------------------
    ma111   = sma(closes, 111)
    ma350   = sma(closes, 350)
    ma350x2 = [v * 2 if v is not None else None for v in ma350]

    pi_top_signals = []
    for i in range(1, n):
        if ma111[i] is None or ma350x2[i] is None:
            continue
        if ma111[i - 1] is None or ma350x2[i - 1] is None:
            continue
        # Crossover: 111DMA goes from below to above 2×350DMA
        if ma111[i - 1] < ma350x2[i - 1] and ma111[i] >= ma350x2[i]:
            pi_top_signals.append({"ts": tss[i], "date": dates[i], "price": closes[i]})

    # -----------------------------------------------------------------------
    # Indicator 2: Mayer Multiple — price / 200DMA
    # -----------------------------------------------------------------------
    ma200d  = sma(closes, 200)

    mayer_top_signals    = []
    mayer_bottom_signals = []
    MAYER_TOP_THRESHOLD    = 2.4
    MAYER_BOTTOM_THRESHOLD = 0.8
    MAYER_COOLDOWN_DAYS    = 90   # suppress repeat signals within 90 days

    last_top_ts    = -10_000_000
    last_bottom_ts = -10_000_000

    for i in range(n):
        if ma200d[i] is None:
            continue
        mm_high = highs[i]  / ma200d[i]   # use day high for top detection
        mm_low  = lows[i]   / ma200d[i]   # use day low for bottom detection
        ts = tss[i]

        if mm_high >= MAYER_TOP_THRESHOLD and (ts - last_top_ts) // 86400 > MAYER_COOLDOWN_DAYS:
            mayer_top_signals.append({"ts": ts, "date": dates[i], "price": highs[i], "mm": mm_high})
            last_top_ts = ts

        if mm_low <= MAYER_BOTTOM_THRESHOLD and (ts - last_bottom_ts) // 86400 > MAYER_COOLDOWN_DAYS:
            mayer_bottom_signals.append({"ts": ts, "date": dates[i], "price": lows[i], "mm": mm_low})
            last_bottom_ts = ts

    # -----------------------------------------------------------------------
    # Indicator 3: 200-week SMA — bottom detector
    # -----------------------------------------------------------------------
    ma200w = sma(closes, 200 * 7)   # ~1400 daily bars

    w200_bottom_signals = []
    last_w200_bottom_ts = -10_000_000

    for i in range(n):
        if ma200w[i] is None:
            continue
        if lows[i] < ma200w[i] and (tss[i] - last_w200_bottom_ts) // 86400 > MAYER_COOLDOWN_DAYS:
            w200_bottom_signals.append({"ts": tss[i], "date": dates[i], "price": lows[i],
                                        "sma200w": ma200w[i]})
            last_w200_bottom_ts = tss[i]

    # -----------------------------------------------------------------------
    # Print signal tables
    # -----------------------------------------------------------------------
    print("=" * 65)
    print("GROUND-TRUTH TOPS")
    for d, p in TOPS:
        print(f"  {d}  ${p:>10,.0f}")
    print()
    print("GROUND-TRUTH BOTTOMS")
    for d, p in BOTTOMS:
        print(f"  {d}  ${p:>10,.0f}")
    print(f"\n(match window = ±{MATCH_DAYS} days)")
    print("=" * 65)
    print()

    print("── INDICATOR SIGNALS ──────────────────────────────────────────")
    print()

    print_signals("Pi Cycle Top (111DMA crosses 2×350DMA upward)", pi_top_signals, TOPS, BOTTOMS)

    print_signals("Mayer Multiple > 2.4  [TOP zone]",
                  mayer_top_signals, TOPS, BOTTOMS)
    print_signals("Mayer Multiple < 0.8  [BOTTOM zone]",
                  mayer_bottom_signals, TOPS, BOTTOMS)

    print_signals("Price < 200-week SMA  [BOTTOM zone]",
                  w200_bottom_signals, TOPS, BOTTOMS)

    # -----------------------------------------------------------------------
    # Current values (last bar)
    # -----------------------------------------------------------------------
    last_i   = n - 1
    last_date = dates[last_i]
    last_px   = closes[last_i]

    print("=" * 65)
    print(f"CURRENT VALUES  ({last_date}  price=${last_px:,.0f})")
    print("=" * 65)

    if ma111[last_i] and ma350x2[last_i]:
        diff_pct = (ma111[last_i] - ma350x2[last_i]) / ma350x2[last_i] * 100
        cross = "CROSSED (top signal active)" if ma111[last_i] >= ma350x2[last_i] else "not crossed"
        print(f"  Pi Cycle Top   111DMA=${ma111[last_i]:,.0f}  2×350DMA=${ma350x2[last_i]:,.0f}"
              f"  diff={diff_pct:+.1f}%  [{cross}]")
    else:
        print("  Pi Cycle Top   insufficient data")

    if ma200d[last_i]:
        mm_close = last_px        / ma200d[last_i]
        mm_low   = lows[last_i]   / ma200d[last_i]
        mm_high  = highs[last_i]  / ma200d[last_i]
        zone = "TOP zone" if mm_close >= 2.4 else ("BOTTOM zone" if mm_close <= 0.8 else "neutral")
        print(f"  Mayer Multiple close/200DMA={mm_close:.3f}  low/200DMA={mm_low:.3f}"
              f"  high/200DMA={mm_high:.3f}  [{zone}]  (200DMA=${ma200d[last_i]:,.0f})")
    else:
        print("  Mayer Multiple insufficient data")

    if ma200w[last_i]:
        ratio_close = last_px     / ma200w[last_i]
        ratio_low   = lows[last_i] / ma200w[last_i]
        zone = "LOW touched below 200W SMA" if ratio_low < 1.0 else f"above 200W SMA"
        print(f"  200-week SMA   ${ma200w[last_i]:,.0f}  close/SMA={ratio_close:.2f}"
              f"  low/SMA={ratio_low:.2f}  [{zone}]")
    else:
        print("  200-week SMA   insufficient data (need 1400 days)")

    print()


if __name__ == "__main__":
    main()
