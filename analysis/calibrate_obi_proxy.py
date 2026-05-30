#!/usr/bin/env python3
"""
Calibration study: validate kline OBI proxy against real L2 OBI from ob_snapshots.

The accumulation bot uses a kline-based OBI proxy:
    proxy = (taker_buy_vol / total_vol - 0.5) * 2   [Binance 1h klines, column 9]
    EMA-smoothed with obi_ema_alpha

The indicators service measures real L2 OBI:
    obi_raw = (bid_depth_vol - ask_depth_vol) / (bid_depth_vol + ask_depth_vol)
    obi_ema = EMA(obi_raw, alpha=0.05)

This script answers:
  Q1. Do price-direction changes correlate with real L2 OBI?
      (∆mid at snapshot frequency vs obi_ema — tests whether proxy direction matches)
  Q2. Do synthetic hourly candle returns correlate with hourly mean OBI?
      (validates that hourly kline momentum is a reasonable OBI proxy)
  Q3. How persistent are OBI regimes? (autocorrelation, threshold exceedance duration)
  Q4. What is the real OBI distribution? (validates if ±0.50 threshold is calibrated)

Usage:
    python3 analysis/calibrate_obi_proxy.py
    python3 analysis/calibrate_obi_proxy.py --db data/live_ob_2026-05-26.db
    python3 analysis/calibrate_obi_proxy.py --mode perp
    python3 analysis/calibrate_obi_proxy.py --threshold 0.50
"""

import argparse
import math
import sqlite3
import sys
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT  = Path(__file__).resolve().parent.parent
DEFAULT_DB = REPO_ROOT / "data" / "live_ob_2026-05-26.db"


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def pearson(xs: List[float], ys: List[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx  = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy  = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return float("nan")
    return num / (dx * dy)


def percentiles(xs: List[float], pcts: List[float]) -> List[float]:
    s = sorted(xs)
    n = len(s)
    result = []
    for p in pcts:
        idx = (n - 1) * p / 100.0
        lo  = int(idx)
        hi  = min(lo + 1, n - 1)
        result.append(s[lo] + (idx - lo) * (s[hi] - s[lo]))
    return result


def histogram(xs: List[float], bins: int = 10) -> str:
    if not xs:
        return ""
    mn, mx = min(xs), max(xs)
    width = (mx - mn) / bins if mx > mn else 1.0
    counts = [0] * bins
    for x in xs:
        i = min(int((x - mn) / width), bins - 1)
        counts[i] += 1
    peak = max(counts)
    lines = []
    for i, c in enumerate(counts):
        lo = mn + i * width
        hi = lo + width
        bar = "█" * int(c / peak * 30)
        lines.append(f"  [{lo:+.3f},{hi:+.3f}) {bar} {c}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_snapshots(db_path: Path, mode: str) -> List[tuple]:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT ts_ms, best_bid, best_ask, obi_ema "
        "FROM ob_snapshots WHERE mode=? AND obi_ema IS NOT NULL ORDER BY ts_ms",
        (mode,)
    ).fetchall()
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# Q1: snapshot-frequency ∆mid vs obi_ema
# ---------------------------------------------------------------------------

def q1_delta_mid_vs_obi(rows: List[tuple]) -> None:
    print("\n── Q1: Snapshot ∆mid vs OBI_ema correlation ─────────────────────────")
    print("   Does immediate price direction track OBI direction?")
    print("   (positive OBI = bid-heavy → price should rise)")

    mids    = [(r[1] + r[2]) / 2 for r in rows if r[1] and r[2]]
    obis    = [r[3] for r in rows if r[1] and r[2]]
    deltas  = [mids[i] - mids[i-1] for i in range(1, len(mids))]
    obi_lag = obis[:-1]   # OBI at time t, ∆mid from t to t+1

    corr_lead = pearson(obi_lag, deltas)
    print(f"   Corr(OBI[t], ∆mid[t→t+1])  = {corr_lead:+.4f}  "
          f"({'✓ significant' if abs(corr_lead) > 0.05 else '✗ weak'})")

    # How often does OBI sign match ∆mid sign?
    agree = sum(1 for o, d in zip(obi_lag, deltas) if (o > 0) == (d > 0) and d != 0)
    total = sum(1 for d in deltas if d != 0)
    print(f"   Sign agreement rate          = {agree/total*100:.1f}%  ({agree}/{total})")
    print(f"   (50% = random, >55% = useful)")


# ---------------------------------------------------------------------------
# Q2: hourly synthetic candles vs hourly mean OBI
# ---------------------------------------------------------------------------

def q2_hourly_proxy(rows: List[tuple]) -> None:
    print("\n── Q2: Hourly price return vs hourly mean OBI ────────────────────────")
    print("   Does hourly candle close/open return track hourly OBI?")
    print("   (validates kline-based proxy for accumulation bot)")

    by_hour: dict = {}
    for ts_ms, bid, ask, obi in rows:
        if not bid or not ask:
            continue
        hour = ts_ms // 3_600_000
        mid  = (bid + ask) / 2
        if hour not in by_hour:
            by_hour[hour] = {"mids": [], "obis": []}
        by_hour[hour]["mids"].append(mid)
        by_hour[hour]["obis"].append(obi)

    hours = sorted(by_hour.keys())
    if len(hours) < 4:
        print("   Not enough hourly data.")
        return

    hour_returns = []
    hour_obis    = []
    for h in hours:
        mids = by_hour[h]["mids"]
        if len(mids) < 2:
            continue
        ret = (mids[-1] - mids[0]) / mids[0]   # close-to-open return within hour
        obi = sum(by_hour[h]["obis"]) / len(by_hour[h]["obis"])
        hour_returns.append(ret)
        hour_obis.append(obi)

    corr = pearson(hour_obis, hour_returns)
    print(f"   Hours analyzed               : {len(hour_returns)}")
    print(f"   Corr(hourly_OBI, hourly_ret) = {corr:+.4f}  "
          f"({'✓ significant' if abs(corr) > 0.15 else '✗ weak'})")
    print(f"   Interpretation: kline proxy validity = "
          f"{'HIGH (>0.30)' if abs(corr) > 0.30 else 'MEDIUM (0.15-0.30)' if abs(corr) > 0.15 else 'LOW (<0.15)'}")
    print(f"   Note: this is a necessary but not sufficient condition for proxy validity.")
    print(f"         Real proxy also uses taker volume (column 9), not just price direction.")

    # Directional accuracy
    agree = sum(1 for o, r in zip(hour_obis, hour_returns) if (o > 0) == (r > 0))
    print(f"   Hourly sign agreement        = {agree}/{len(hour_returns)} "
          f"({agree/len(hour_returns)*100:.0f}%)")


# ---------------------------------------------------------------------------
# Q3: OBI regime persistence
# ---------------------------------------------------------------------------

def q3_regime_persistence(rows: List[tuple], threshold: float) -> None:
    print(f"\n── Q3: OBI regime persistence (threshold=±{threshold:.2f}) ──────────────")
    print("   How long does OBI stay above/below threshold once triggered?")

    obis = [r[3] for r in rows if r[3] is not None]
    tss  = [r[0] for r in rows if r[3] is not None]

    if not obis:
        print("   No data.")
        return

    # Compute streak lengths in snapshots and seconds
    above_durations = []   # OBI > +thresh
    below_durations = []   # OBI < -thresh

    in_streak = None
    streak_start = None
    for i, (ts, obi) in enumerate(zip(tss, obis)):
        zone = "above" if obi > threshold else "below" if obi < -threshold else "neutral"
        if zone != in_streak:
            if in_streak == "above" and streak_start is not None:
                above_durations.append(ts - streak_start)
            elif in_streak == "below" and streak_start is not None:
                below_durations.append(ts - streak_start)
            in_streak    = zone if zone != "neutral" else None
            streak_start = ts if zone != "neutral" else None

    def summarize(label: str, durations_ms: List[int]) -> None:
        if not durations_ms:
            print(f"   {label}: no streaks found")
            return
        d_s = [d / 1000 for d in durations_ms]
        p10, p50, p90 = percentiles(d_s, [10, 50, 90])
        print(f"   {label} streaks: n={len(d_s)}  "
              f"p10={p10:.0f}s  median={p50:.0f}s  p90={p90:.0f}s  "
              f"mean={sum(d_s)/len(d_s):.0f}s")

    summarize(f"OBI>+{threshold:.2f} (bid-heavy)", above_durations)
    summarize(f"OBI<-{threshold:.2f} (ask-heavy)", below_durations)

    # Autocorrelation at lag 1
    if len(obis) > 2:
        ac = pearson(obis[:-1], obis[1:])
        print(f"   OBI autocorrelation (lag=1)  = {ac:+.4f}  "
              f"({'high persistence' if ac > 0.80 else 'moderate' if ac > 0.50 else 'low'})")


# ---------------------------------------------------------------------------
# Q4: Real OBI distribution
# ---------------------------------------------------------------------------

def q4_distribution(rows: List[tuple], threshold: float) -> None:
    print(f"\n── Q4: Real L2 OBI distribution ──────────────────────────────────────")
    print("   Is the ±{:.2f} threshold well-calibrated?".format(threshold))

    obis = [r[3] for r in rows if r[3] is not None]
    if not obis:
        print("   No data.")
        return

    mn   = min(obis)
    mx   = max(obis)
    mean = sum(obis) / len(obis)
    p5, p25, p50, p75, p95 = percentiles(obis, [5, 25, 50, 75, 95])

    print(f"   n={len(obis):,}  min={mn:+.4f}  max={mx:+.4f}")
    print(f"   mean={mean:+.4f}  p5={p5:+.4f}  p25={p25:+.4f}  "
          f"p50={p50:+.4f}  p75={p75:+.4f}  p95={p95:+.4f}")

    above = sum(1 for o in obis if o > threshold)
    below = sum(1 for o in obis if o < -threshold)
    neutral = len(obis) - above - below
    print(f"   OBI>+{threshold:.2f}: {above/len(obis)*100:.1f}%  "
          f"OBI<-{threshold:.2f}: {below/len(obis)*100:.1f}%  "
          f"neutral: {neutral/len(obis)*100:.1f}%")
    print(f"   Recommendation: threshold triggers on {(above+below)/len(obis)*100:.1f}% of snapshots")

    print("\n   Distribution histogram:")
    print(histogram(obis))


# ---------------------------------------------------------------------------
# Summary recommendation
# ---------------------------------------------------------------------------

def summarize_recommendations(threshold: float) -> None:
    print(f"\n── Summary ────────────────────────────────────────────────────────────")
    print(f"   Accumulation bot entry: obi_entry_thresh = ±{threshold:.2f}")
    print(f"   Key question: Is kline taker-flow proxy a valid substitute for L2 OBI?")
    print(f"")
    print(f"   Structural difference:")
    print(f"     L2 OBI  = (bid_depth - ask_depth) / (bid_depth + ask_depth)  [depth]")
    print(f"     Kline   = (taker_buy - taker_sell) / total_vol               [flow]")
    print(f"   These measure different market phenomena. Correlation > 0.30 would")
    print(f"   suggest they align directionally (both reflect supply/demand imbalance).")
    print(f"   Correlation < 0.15 means the proxy is NOT tracking real OBI dynamics.")
    print(f"")
    print(f"   Data limitation: only 3 days of L2 OBI data available (May 23-26).")
    print(f"   Collect 4+ weeks before drawing firm conclusions about proxy validity.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db",        default=str(DEFAULT_DB), help="ob_snapshots SQLite DB")
    parser.add_argument("--mode",      choices=["spot", "perp", "both"], default="spot")
    parser.add_argument("--threshold", type=float, default=0.50,
                        help="OBI threshold for regime/distribution analysis (default: 0.50)")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    modes = ["spot", "perp"] if args.mode == "both" else [args.mode]

    for mode in modes:
        print(f"\n{'='*70}")
        print(f"OBI Proxy Calibration Study — {mode.upper()}")
        print(f"DB: {db_path}")
        rows = load_snapshots(db_path, mode)
        if not rows:
            print(f"No data for mode={mode}")
            continue
        print(f"Snapshots: {len(rows):,}  "
              f"({rows[0][0]//1000} → {rows[-1][0]//1000})")
        q1_delta_mid_vs_obi(rows)
        q2_hourly_proxy(rows)
        q3_regime_persistence(rows, args.threshold)
        q4_distribution(rows, args.threshold)
        summarize_recommendations(args.threshold)


if __name__ == "__main__":
    main()
