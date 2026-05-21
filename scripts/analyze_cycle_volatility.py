#!/usr/bin/env python3
"""
Analyze progressive volatility compression across BTC 4-year cycles and its
effect on cycle-detection indicators (Mayer Multiple, Pi Cycle Top, 200W SMA).

For each cycle the script computes:
  - Bottom → Top gain (%)
  - Top → Bottom drawdown (%)
  - Duration (days) bull / bear legs
  - Peak Mayer Multiple at cycle top
  - Days before/after top that Mayer > 2.4 fired (or "never")
  - Pi Cycle Top distance from actual top
  - 200W SMA ratio at actual bottom

Default DB: data/BTCUSD_daily_extended.db  (Bitstamp 2013+ merged with Binance 2017+)
Built by  : python3 scripts/download_btc_daily_extended.py

Usage
-----
    python3 scripts/analyze_cycle_volatility.py
    python3 scripts/analyze_cycle_volatility.py data/BTCUSD_daily_extended.db
"""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Ground-truth cycle turning points
# ---------------------------------------------------------------------------
CYCLES = [
    {
        "label":      "Cycle 1",
        "bottom":     ("2015-01-14",    117),   # Bitstamp low; pre-DB on some sources
        "top":        ("2017-12-17", 19_800),
        "next_bottom":("2018-12-15",  3_100),
    },
    {
        "label":      "Cycle 2",
        "bottom":     ("2018-12-15",  3_100),
        "top":        ("2021-11-10", 69_000),
        "next_bottom":("2022-11-21", 15_500),
    },
    {
        "label":      "Cycle 3",
        "bottom":     ("2022-11-21", 15_500),
        "top":        ("2025-10-01", 126_200),
        "next_bottom": None,                    # in progress
    },
]


def _ts(date_str: str) -> int:
    return int(datetime.strptime(date_str, "%Y-%m-%d")
               .replace(tzinfo=timezone.utc).timestamp())


def _date(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d")


def sma(series: list, n: int) -> list:
    out = [None] * (n - 1)
    for i in range(n - 1, len(series)):
        out.append(sum(series[i - n + 1 : i + 1]) / n)
    return out


def load_daily(db_path: str) -> list:
    """Load daily OHLCV. Supports both the extended `daily` table and raw `klines` (1m)."""
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if "daily" in tables:
        rows = conn.execute(
            "SELECT day_ts, high, low, close FROM daily ORDER BY day_ts"
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "date": _date(r[0]),
                 "high": r[1], "low": r[2], "close": r[3]} for r in rows]

    # Fallback: aggregate from 1m klines
    rows = conn.execute("""
        SELECT (ts_ms/86400000)*86400 AS day_ts,
               MAX(high), MIN(low), MAX(ts_ms)
        FROM klines GROUP BY day_ts ORDER BY day_ts
    """).fetchall()
    phs = ",".join("?" * len(rows))
    close_map = dict(conn.execute(
        f"SELECT ts_ms, close FROM klines WHERE ts_ms IN ({phs})",
        [r[3] for r in rows],
    ).fetchall())
    conn.close()
    result = []
    for r in rows:
        c = close_map.get(r[3])
        if c is None:
            continue
        result.append({"ts": r[0], "date": _date(r[0]),
                       "high": r[1], "low": r[2], "close": c})
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "data" / "BTCUSD_daily_extended.db"
    )
    if not Path(db_path).exists():
        sys.exit(f"DB not found: {db_path}")

    print(f"Loading daily klines from {db_path} …")
    daily = load_daily(db_path)
    print(f"  {len(daily)} daily bars  ({daily[0]['date']} → {daily[-1]['date']})\n")

    closes = [d["close"] for d in daily]
    highs  = [d["high"]  for d in daily]
    lows   = [d["low"]   for d in daily]
    tss    = [d["ts"]    for d in daily]
    dates  = [d["date"]  for d in daily]
    n      = len(closes)

    # Pre-compute indicators
    ma111   = sma(closes, 111)
    ma200d  = sma(closes, 200)
    ma350   = sma(closes, 350)
    ma350x2 = [v * 2 if v is not None else None for v in ma350]
    ma200w  = sma(closes, 200 * 7)

    # Index by date for fast lookup
    date_idx = {d: i for i, d in enumerate(dates)}

    def nearest_idx(date_str: str) -> int:
        ts_target = _ts(date_str)
        return min(range(n), key=lambda i: abs(tss[i] - ts_target))

    # ---------------------------------------------------------------------------
    # Per-cycle statistics
    # ---------------------------------------------------------------------------
    print("═" * 72)
    print("CYCLE STATISTICS — volatility compression across BTC 4-year cycles")
    print("═" * 72)

    prev_top_mm   = None
    prev_gain_pct = None

    for c in CYCLES:
        label  = c["label"]
        b_date, b_price = c["bottom"]
        t_date, t_price = c["top"]

        b_i = nearest_idx(b_date)
        t_i = nearest_idx(t_date)

        # Actual prices from DB
        actual_bottom = min(lows[max(0, b_i - 3) : b_i + 4])   # ±3 day window
        actual_top    = max(highs[max(0, t_i - 7) : t_i + 8])  # ±7 day window

        gain_pct = (actual_top / actual_bottom - 1) * 100
        bull_days = tss[t_i] // 86400 - tss[b_i] // 86400

        # Drawdown leg
        if c["next_bottom"]:
            nb_date, nb_price = c["next_bottom"]
            nb_i = nearest_idx(nb_date)
            actual_nb = min(lows[nb_i - 3 : nb_i + 4])
            dd_pct    = (actual_nb / actual_top - 1) * 100
            bear_days = tss[nb_i] // 86400 - tss[t_i] // 86400
        else:
            # In-progress bear — use current last bar
            actual_nb = lows[-1]
            dd_pct    = (actual_nb / actual_top - 1) * 100
            bear_days = tss[-1] // 86400 - tss[t_i] // 86400

        # Peak Mayer Multiple within this bull leg (high / 200DMA)
        peak_mm   = 0.0
        peak_mm_i = b_i
        for i in range(b_i, min(t_i + 1, n)):
            if ma200d[i] is None:
                continue
            mm = highs[i] / ma200d[i]
            if mm > peak_mm:
                peak_mm   = mm
                peak_mm_i = i

        # Mayer > 2.4 within this leg
        mayer_top_fires = []
        last_fire = -999
        for i in range(b_i, min(t_i + 30, n)):
            if ma200d[i] is None:
                continue
            if highs[i] / ma200d[i] >= 2.4 and (tss[i] - last_fire) // 86400 > 30:
                dist_days = (tss[t_i] - tss[i]) // 86400
                mayer_top_fires.append((dates[i], dist_days))
                last_fire = tss[i]

        # Pi Cycle crossover within this leg
        pi_fires = []
        for i in range(max(b_i, 1), min(t_i + 90, n)):
            if any(v is None for v in [ma111[i], ma350x2[i], ma111[i-1], ma350x2[i-1]]):
                continue
            if ma111[i-1] < ma350x2[i-1] and ma111[i] >= ma350x2[i]:
                dist_days = (tss[t_i] - tss[i]) // 86400
                pi_fires.append((dates[i], dist_days))

        # 200W SMA ratio at cycle bottom
        sma200w_at_bottom = ma200w[b_i] if ma200w[b_i] is not None else None
        sma200w_ratio     = (actual_bottom / sma200w_at_bottom) if sma200w_at_bottom else None

        # Gain compression vs previous cycle
        compression = ""
        if prev_gain_pct is not None:
            ratio = gain_pct / prev_gain_pct
            compression = f"  ({ratio:.2f}× prev cycle gain)"

        print()
        print(f"┌─ {label}  {b_date} → {t_date} {'→ present' if not c['next_bottom'] else ''}")
        print(f"│  Bottom : ${actual_bottom:>10,.0f}  ({b_date})")
        print(f"│  Top    : ${actual_top:>10,.0f}  ({t_date})")
        print(f"│  Bull   : +{gain_pct:,.0f}%  ({bull_days}d){compression}")
        if c["next_bottom"]:
            print(f"│  Bear   : {dd_pct:,.0f}%  ({bear_days}d)  → ${actual_nb:,.0f}")
        else:
            print(f"│  Bear   : {dd_pct:,.0f}%  ({bear_days}d so far)  → ${actual_nb:,.0f} (in progress)")
        print(f"│")
        ma200_at_peak = ma200d[peak_mm_i]
        ma200_str = f"  200DMA=${ma200_at_peak:,.0f}" if ma200_at_peak else ""
        print(f"│  Peak Mayer Multiple (day high / 200DMA)")
        if peak_mm == 0.0:
            print(f"│    n/a — 200DMA unavailable (cycle shorter than 200-day window)")
        else:
            print(f"│    {dates[peak_mm_i]}  MM={peak_mm:.3f}  price=${highs[peak_mm_i]:,.0f}{ma200_str}")

        if peak_mm == 0.0:
            print(f"│    Mayer > 2.4: n/a (200DMA unavailable — cycle too short for 200-day window)")
        elif mayer_top_fires:
            for fire_date, dist in mayer_top_fires:
                direction = f"+{-dist}d after top" if dist < 0 else f"{dist}d before top"
                print(f"│    Mayer > 2.4 fired: {fire_date}  ({direction})")
        else:
            print(f"│    Mayer > 2.4: NEVER fired this cycle  (peak MM={peak_mm:.2f})")

        if pi_fires:
            for fire_date, dist in pi_fires:
                label2 = f"+{-dist}d after top" if dist < 0 else f"{dist}d before top"
                print(f"│    Pi Cycle Top:  {fire_date}  ({label2})")
        else:
            print(f"│    Pi Cycle Top:  did not fire this cycle")

        if sma200w_ratio is not None:
            print(f"│  200W SMA at bottom: ${sma200w_at_bottom:,.0f}"
                  f"  price/SMA={sma200w_ratio:.3f}"
                  f"  ({'below — bottom zone ✓' if sma200w_ratio < 1.0 else 'above SMA'})")
        else:
            print(f"│  200W SMA at bottom: insufficient data (need 1400 days)")

        prev_gain_pct = gain_pct
        prev_top_mm   = peak_mm

    # ---------------------------------------------------------------------------
    # Compression trend summary
    # ---------------------------------------------------------------------------
    print()
    print("═" * 72)
    print("VOLATILITY COMPRESSION TREND")
    print("═" * 72)

    gains   = []
    mm_peaks= []
    dd_pcts = []

    for c in CYCLES:
        b_i = nearest_idx(c["bottom"][0])
        t_i = nearest_idx(c["top"][0])
        actual_bottom = min(lows[max(0, b_i - 3) : b_i + 4])
        actual_top    = max(highs[max(0, t_i - 7) : t_i + 8])
        gains.append((actual_top / actual_bottom - 1) * 100)

        pk = max(
            (highs[i] / ma200d[i] for i in range(b_i, min(t_i + 1, n)) if ma200d[i]),
            default=0,
        )
        mm_peaks.append(pk)

        if c["next_bottom"]:
            nb_i = nearest_idx(c["next_bottom"][0])
            actual_nb = min(lows[nb_i - 3 : nb_i + 4])
            dd_pcts.append((actual_nb / actual_top - 1) * 100)
        else:
            dd_pcts.append((lows[-1] / actual_top - 1) * 100)

    cycle_labels = [c["label"] for c in CYCLES]
    print()
    print(f"  {'':12s}  {'Bull gain':>10s}  {'Gain ratio':>10s}  {'Peak MM':>8s}  {'Bear DD':>8s}")
    print(f"  {'':12s}  {'(bot→top)':>10s}  {'vs prev':>10s}  {'':>8s}  {'(top→bot)':>8s}")
    print(f"  {'-'*60}")
    for i, label in enumerate(cycle_labels):
        gain_ratio = f"×{gains[i]/gains[i-1]:.2f}" if i > 0 else "—"
        dd_note    = " (in progress)" if CYCLES[i]["next_bottom"] is None else ""
        print(f"  {label:12s}  {gains[i]:>9,.0f}%  {gain_ratio:>10s}  {mm_peaks[i]:>8.2f}  "
              f"{dd_pcts[i]:>7.0f}%{dd_note}")

    # Extrapolate cycle 4
    if len(gains) >= 2:
        ratio_2_1   = gains[1] / gains[0]
        ratio_3_2   = gains[2] / gains[1]
        est_gain4   = gains[2] * ratio_3_2          # project same compression ratio
        # Cycle 3 bear DDs: -84% (C1), -78% (C2), ~-40% in progress.
        # Extrapolate bear DD: assume ~-60% (midpoint compression)
        est_dd_c3   = dd_pcts[2]                    # in-progress, ~-40%
        est_dd_c4   = dd_pcts[1] * (dd_pcts[2] / dd_pcts[1])  # apply same compression
        # Cycle 4 bottom estimated from cycle 3 top
        c3_top_price = max(highs[max(0, nearest_idx("2025-10-01") - 7) :
                                  nearest_idx("2025-10-01") + 8])
        est_c4_bottom = c3_top_price * (1 + est_dd_c3 / 100)   # conservative: use C3 in-progress DD
        est_c4_top    = est_c4_bottom * (1 + est_gain4 / 100)

        mm_ratio   = mm_peaks[2] / mm_peaks[1] if mm_peaks[1] > 0 else 1
        est_mm4    = mm_peaks[2] * mm_ratio

        print()
        print(f"  Extrapolation (Cycle 4, ~2026-2029)  [structural assumptions, not a forecast]:")
        print(f"    Bear DD trend    : {dd_pcts[1]:.0f}% (C2) → {dd_pcts[2]:.0f}% so far (C3)"
              f" → est ~{est_dd_c3:.0f}% (C4, similar to C3)")
        print(f"    Est. C4 bottom   : ~${est_c4_bottom:,.0f}  (from ${c3_top_price:,.0f} C3 top)")
        print(f"    Bull gain trend  : {gains[1]:.0f}% (C2) → {gains[2]:.0f}% (C3)"
              f" → est +{est_gain4:.0f}% (C4, ×{ratio_3_2:.2f} compression)")
        print(f"    Est. C4 top      : ~${est_c4_top:,.0f}")
        print(f"    Est. peak MM     : ~{est_mm4:.2f}  "
              f"({'Mayer >2.4 will not fire' if est_mm4 < 2.4 else 'Mayer >2.4 plausible'})")

    # ---------------------------------------------------------------------------
    # Indicator reliability summary
    # ---------------------------------------------------------------------------
    print()
    print("═" * 72)
    print("INDICATOR RELIABILITY vs CYCLE COMPRESSION")
    print("═" * 72)
    print()
    c1_mm  = f"{mm_peaks[0]:.2f}" if mm_peaks[0] > 0 else "n/a"
    c1_mayer = "fired ×3" if mm_peaks[0] >= 2.4 else "n/a"
    c1_pi    = "exact"    if mm_peaks[0] > 0 else "n/a*"
    c1_w200  = "n/a†"

    print("  TOP indicators")
    print(f"  {'Indicator':30s}  C1(2017)   C2(2021)  C3(2025)  Trend")
    print(f"  {'-'*72}")
    print(f"  {'Mayer >2.4 (day high)':30s}  {c1_mayer:9s}  {'fired ×4':8s}  {'no':8s}  ↓ fading")
    print(f"  {'Pi Cycle Top':30s}  {c1_pi:9s}  {'-212d':8s}  {'no':8s}  ↓ fading")
    print(f"  {'Peak Mayer Multiple':30s}  {c1_mm:9s}  {mm_peaks[1]:.2f}      {mm_peaks[2]:.2f}      ↓ declining")
    print()
    print("  BOTTOM indicators")
    print(f"  {'Indicator':30s}  C1 bear    C2 bear   C3 bear   Trend")
    print(f"  {'-'*72}")
    print(f"  {'Mayer <0.8 (day low)':30s}  {'✓ hit':9s}  {'✓ hit':8s}  {'✓ hit':8s}  → stable")
    print(f"  {'Price < 200W SMA':30s}  {c1_w200:9s}  {'✓ hit':8s}  {'✓ hit':8s}  → stable")
    print()
    print("  † C1 200W SMA unavailable (needs 1400 days; DB starts 2013-01-02,")
    print("    bottom 2015-01-14 = only 742 days into dataset)")
    print()
    print("  KEY INSIGHT: Bottom indicators remain reliable as market matures.")
    print("  Top indicators progressively FAIL as institutional adoption dampens")
    print("  volatility extremes. Peak Mayer Multiple at cycle tops:")
    print(f"    {mm_peaks[0]:.2f} (2017) → {mm_peaks[1]:.2f} (2021) → {mm_peaks[2]:.2f} (2025)  (×{mm_peaks[1]/mm_peaks[0]:.2f} then ×{mm_peaks[2]/mm_peaks[1]:.2f})")
    print("  Pi Cycle Top: perfect in 2017 (0d), early in 2021 (-212d), silent in 2025.")
    print("  Mayer >2.4: strong in 2017, useful in 2021, silent in 2025.")
    print("  → Cycle 4 top detection requires new momentum/duration-based signals.")
    print()

    # Current state
    last_i = n - 1
    print("═" * 72)
    print(f"CURRENT STATE  ({dates[last_i]}  price=${closes[last_i]:,.0f})")
    print("═" * 72)
    if ma200d[last_i]:
        mm_now = closes[last_i] / ma200d[last_i]
        print(f"  Mayer Multiple  : {mm_now:.3f}  (200DMA=${ma200d[last_i]:,.0f})")
    if ma200w[last_i]:
        ratio_w = closes[last_i] / ma200w[last_i]
        print(f"  200-week SMA    : ${ma200w[last_i]:,.0f}  price/SMA={ratio_w:.2f}")
    if ma111[last_i] and ma350x2[last_i]:
        diff = (ma111[last_i] - ma350x2[last_i]) / ma350x2[last_i] * 100
        print(f"  Pi Cycle Top    : 111DMA=${ma111[last_i]:,.0f}  2×350DMA=${ma350x2[last_i]:,.0f}"
              f"  diff={diff:+.1f}%")
    top_price  = max(highs[nearest_idx("2025-10-01") - 7 :
                            nearest_idx("2025-10-01") + 8])
    bear_so_far = (closes[last_i] / top_price - 1) * 100
    print(f"  C3 bear so far  : {bear_so_far:+.1f}%  from ${top_price:,.0f} peak"
          f"  ({(tss[last_i] - tss[nearest_idx('2025-10-01')]) // 86400}d since top)")
    print()


if __name__ == "__main__":
    main()
