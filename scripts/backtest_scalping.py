#!/usr/bin/env python3
"""
Scalping strategy backtester — BTC/USDT 1-minute OHLCV data.

Three strategy types (all configurable via JSON):
  candle_momentum — entry on strong directional candles confirmed by above-average
                    volume; exits at fixed TP/SL or timeout. NOTE: this is a candle-
                    based momentum proxy, NOT real order-book depth (L2 OBI requires
                    live WebSocket tick data unavailable in historical OHLCV).
  meanrev         — mean-reversion via rolling VWAP deviation + Bollinger Bands;
                    exits at VWAP re-touch or ATR-based stop.
  breakout        — ATR range-breakout with 2×ATR target and 1×ATR stop.

All strategies:
  - Single position at a time (no pyramiding)
  - Slippage and fees applied on entry and exit
  - Unclosed positions at end of data are force-closed at last candle close

Usage
-----
    python3 scripts/backtest_scalping.py --strategy strategies/scalping_candle_momentum.json
    python3 scripts/backtest_scalping.py --strategy strategies/scalping_meanrev.json
    python3 scripts/backtest_scalping.py --strategy strategies/scalping_breakout.json
    python3 scripts/backtest_scalping.py --compare              # all three side-by-side
    python3 scripts/backtest_scalping.py --strategy <file> <db_path>
"""

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent / "data" / "BTCUSDT3197d_20172026.db"

# ---------------------------------------------------------------------------
# Defaults (overridden by JSON config)
# ---------------------------------------------------------------------------
DEFAULTS = dict(
    strategy_type         = "candle_momentum",
    symbol                = "BTCUSDT",
    capital               = 10_000.0,
    fee_rate              = 0.001,      # 0.1% per side (Binance spot taker)
    slippage_pct          = 0.0005,     # 0.05% per side (market-order spread estimate)
    stake_frac            = 0.20,       # fraction of capital per trade

    # candle_momentum
    cm_body_ratio_thresh  = 0.60,       # (close-open)/(high-low) must exceed this
    cm_vol_z_window       = 20,         # candles for volume z-score baseline
    cm_vol_z_thresh       = 1.0,        # minimum volume z-score to confirm signal
    cm_min_range_pct      = 0.0003,     # min candle range / close (filters doji)
    cm_take_profit_pct    = 0.006,      # 0.6% TP (>3× round-trip cost of 0.31%)
    cm_stop_loss_pct      = 0.003,      # 0.3% SL → R:R = 2:1 gross
    cm_max_hold_minutes   = 10,

    # meanrev
    mr_bb_period          = 20,
    mr_bb_std_mult        = 2.0,
    mr_vwap_window        = 390,        # ~6.5 h in minutes
    mr_vwap_dev_thresh    = 0.005,      # close must be ≥ 0.5% below VWAP
    mr_atr_period         = 14,
    mr_sl_atr_mult        = 1.5,        # stop = entry - 1.5×ATR
    mr_max_hold_minutes   = 60,

    # breakout
    bo_range_period       = 20,         # candles to define the range high
    bo_atr_period         = 14,
    bo_min_atr_pct        = 0.001,      # ATR/close must exceed this to avoid flat markets
    bo_sl_atr_mult        = 1.0,        # stop = entry - 1×ATR
    bo_tp_atr_mult        = 2.0,        # target = entry + 2×ATR
    bo_max_hold_minutes   = 120,
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_klines(db_path: str) -> list:
    """Load 1m OHLCV klines from SQLite, validate timeframe, return list of dicts."""
    conn = sqlite3.connect(db_path)
    # Sanity-check: sample the interval between consecutive candles
    sample = conn.execute(
        "SELECT ts_ms FROM klines ORDER BY ts_ms LIMIT 3"
    ).fetchall()
    if len(sample) >= 2:
        interval_ms = sample[1][0] - sample[0][0]
        if interval_ms != 60_000:
            print(f"WARNING: expected 1m candles (60s), got {interval_ms//1000}s interval",
                  file=sys.stderr)
    rows = conn.execute(
        "SELECT ts_ms, open, high, low, close, volume FROM klines ORDER BY ts_ms"
    ).fetchall()
    conn.close()
    return [
        {"ts_ms": r[0], "open": r[1], "high": r[2],
         "low": r[3], "close": r[4], "volume": r[5]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def sma(series: list, n: int) -> list:
    out = [None] * (n - 1)
    for i in range(n - 1, len(series)):
        out.append(sum(series[i - n + 1: i + 1]) / n)
    return out


def ema(series: list, n: int) -> list:
    """Exponential moving average; first n-1 values are None."""
    out = [None] * (n - 1)
    k = 2.0 / (n + 1)
    seed = sum(series[:n]) / n
    out.append(seed)
    prev = seed
    for i in range(n, len(series)):
        cur = series[i] * k + prev * (1 - k)
        out.append(cur)
        prev = cur
    return out


def _true_range(highs: list, lows: list, closes: list) -> list:
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    return tr


def atr_series(highs: list, lows: list, closes: list, n: int) -> list:
    tr = _true_range(highs, lows, closes)
    return ema(tr, n)


def bollinger(closes: list, n: int, k: float) -> tuple:
    """Return (upper, mid, lower) each as list[float|None]."""
    mid = sma(closes, n)
    upper, lower = [], []
    for i, m in enumerate(mid):
        if m is None:
            upper.append(None)
            lower.append(None)
        else:
            std = math.sqrt(sum((closes[i - n + 1 + j] - m) ** 2
                                for j in range(n)) / n)
            upper.append(m + k * std)
            lower.append(m - k * std)
    return upper, mid, lower


def rolling_max(series: list, n: int) -> list:
    """Rolling maximum over n periods; first n-1 values are None."""
    out = [None] * (n - 1)
    for i in range(n - 1, len(series)):
        out.append(max(series[i - n + 1: i + 1]))
    return out


def vwap_rolling(closes: list, volumes: list, n: int) -> list:
    """Rolling VWAP over n candles."""
    out = [None] * (n - 1)
    for i in range(n - 1, len(closes)):
        pv = sum(closes[i - n + 1 + j] * volumes[i - n + 1 + j] for j in range(n))
        v  = sum(volumes[i - n + 1 + j] for j in range(n))
        out.append(pv / v if v > 0 else closes[i])
    return out


def vol_zscore(volumes: list, n: int) -> list:
    """Rolling z-score of volume."""
    out = [None] * (n - 1)
    for i in range(n - 1, len(volumes)):
        window = volumes[i - n + 1: i + 1]
        mu  = sum(window) / n
        std = math.sqrt(sum((v - mu) ** 2 for v in window) / n)
        out.append((volumes[i] - mu) / std if std > 0 else 0.0)
    return out


# ---------------------------------------------------------------------------
# Core backtest engine
# ---------------------------------------------------------------------------

def run_backtest(klines: list, p: dict) -> dict:
    """
    Simulate scalping strategy on klines list.

    Returns dict with: trades, final_capital, max_dd, sharpe, sortino,
    win_rate, avg_hold_minutes, fees_paid, start_date, end_date, years.
    """
    n = len(klines)
    if n < 25:
        return {}

    closes  = [k["close"]  for k in klines]
    opens   = [k["open"]   for k in klines]
    highs   = [k["high"]   for k in klines]
    lows    = [k["low"]    for k in klines]
    volumes = [k["volume"] for k in klines]

    # Pre-compute indicators (used selectively by strategy type)
    stype = p["strategy_type"]
    vol_z       = vol_zscore(volumes, p["cm_vol_z_window"])
    atr_vals    = atr_series(highs, lows, closes, p.get("mr_atr_period", p.get("bo_atr_period", 14)))
    bb_upper, bb_mid, bb_lower = bollinger(closes, p["mr_bb_period"], p["mr_bb_std_mult"])
    vwap        = vwap_rolling(closes, volumes, p["mr_vwap_window"])
    range_highs = rolling_max(highs, p["bo_range_period"])  # true range high: max over n periods

    capital    = p["capital"]
    position   = None   # None or {"entry_px", "tp", "sl", "entry_i", "qty", "cost"}
    trades     = []
    fees_paid  = 0.0
    fee        = p["fee_rate"]
    slip       = p["slippage_pct"]
    equity_curve = [capital]

    def _open_long(i: float, signal_px: float, tp: float, sl: float, label: str):
        nonlocal position, capital, fees_paid
        entry_px = signal_px * (1 + slip)
        cost     = capital * p["stake_frac"]
        qty      = cost / entry_px
        entry_fee = cost * fee
        fees_paid += entry_fee
        position = {
            "entry_px":  entry_px,
            "tp":        tp,
            "sl":        sl,
            "entry_i":   i,
            "qty":       qty,
            "cost":      cost,
            "label":     label,
        }

    def _close_long(i: int, exit_px: float, reason: str):
        nonlocal position, capital, fees_paid
        if position is None:
            return
        gross    = position["qty"] * exit_px * (1 - slip)
        exit_fee = gross * fee
        fees_paid += exit_fee
        pnl      = gross - exit_fee - position["cost"] - position["cost"] * fee
        hold_min = (i - position["entry_i"])
        trades.append({
            "entry_i":    position["entry_i"],
            "exit_i":     i,
            "entry_px":   position["entry_px"],
            "exit_px":    exit_px,
            "pnl":        pnl,
            "hold_min":   hold_min,
            "reason":     reason,
            "label":      position["label"],
        })
        capital  += pnl
        position  = None

    for i in range(1, n):
        hi = highs[i]
        lo = lows[i]
        cl = closes[i]
        ts = klines[i]["ts_ms"]

        # ── Check exit for open position ──────────────────────────────────
        if position is not None:
            tp = position["tp"]
            sl = position["sl"]
            hold = i - position["entry_i"]
            max_hold = {
                "candle_momentum": p["cm_max_hold_minutes"],
                "meanrev":         p["mr_max_hold_minutes"],
                "breakout":        p["bo_max_hold_minutes"],
            }.get(stype, 60)

            # SL checked before TP (conservative: assume worst fills first)
            if lo <= sl:
                _close_long(i, sl, "stop_loss")
            elif hi >= tp:
                _close_long(i, tp, "take_profit")
            elif hold >= max_hold:
                _close_long(i, cl, "timeout")

        # ── Entry signals (only when flat) ────────────────────────────────
        if position is not None:
            equity_curve.append(capital)
            continue

        if stype == "candle_momentum":
            # Proxy for order-book momentum: strong directional candle + volume spike
            rng = hi - lo
            if rng < closes[i - 1] * p["cm_min_range_pct"]:
                equity_curve.append(capital)
                continue
            body   = cl - opens[i]
            body_r = body / rng
            vz     = vol_z[i]
            if (vz is not None
                    and body_r >= p["cm_body_ratio_thresh"]
                    and vz >= p["cm_vol_z_thresh"]):
                tp = cl * (1 + p["cm_take_profit_pct"])
                sl = cl * (1 - p["cm_stop_loss_pct"])
                _open_long(i, cl, tp, sl, "cm_long")

        elif stype == "meanrev":
            bbl = bb_lower[i]
            vwp = vwap[i]
            atr = atr_vals[i]
            if bbl is None or vwp is None or atr is None:
                equity_curve.append(capital)
                continue
            # Enter when price is below BB lower AND below VWAP by threshold
            if (cl < bbl
                    and cl < vwp * (1 - p["mr_vwap_dev_thresh"])):
                tp = vwp           # exit at VWAP re-touch
                sl = cl - p["mr_sl_atr_mult"] * atr
                if sl < cl * 0.98:  # don't open if stop is >2% away (cap risk)
                    equity_curve.append(capital)
                    continue
                _open_long(i, cl, tp, sl, "mr_long")

        elif stype == "breakout":
            rh  = range_highs[i - 1]   # previous candle's range-high SMA (no look-ahead)
            atr = atr_vals[i]
            if rh is None or atr is None:
                equity_curve.append(capital)
                continue
            atr_pct = atr / closes[i - 1] if closes[i - 1] > 0 else 0
            if (cl > rh and atr_pct >= p["bo_min_atr_pct"]):
                tp = cl + p["bo_tp_atr_mult"] * atr
                sl = cl - p["bo_sl_atr_mult"] * atr
                _open_long(i, cl, tp, sl, "bo_long")

        equity_curve.append(capital)

    # Force-close any open position at end of data
    if position is not None:
        _close_long(n - 1, closes[-1], "end_of_data")

    # ── Metrics ───────────────────────────────────────────────────────────
    start_ts = klines[0]["ts_ms"] / 1000
    end_ts   = klines[-1]["ts_ms"] / 1000
    years    = (end_ts - start_ts) / 86400 / 365.25

    total_ret = capital / p["capital"]
    cagr      = total_ret ** (1 / years) - 1 if years > 0 else 0.0

    # Max drawdown from equity curve
    peak = p["capital"]
    max_dd = 0.0
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0

    avg_hold = sum(t["hold_min"] for t in trades) / len(trades) if trades else 0.0

    # Per-trade returns for Sharpe/Sortino
    rets = [t["pnl"] / p["capital"] for t in trades]
    if len(rets) > 1:
        mu   = sum(rets) / len(rets)
        std  = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
        drets = [r for r in rets if r < 0]
        dstd = math.sqrt(sum(r ** 2 for r in drets) / len(drets)) if drets else 0.0
        sharpe  = mu / std  if std  > 0 else 0.0
        sortino = mu / dstd if dstd > 0 else 0.0
    else:
        sharpe = sortino = 0.0

    calmar = cagr / max_dd if max_dd > 0 else 0.0

    def _date(ts_ms):
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    return {
        "trades":         trades,
        "final_capital":  capital,
        "total_ret":      total_ret,
        "cagr":           cagr,
        "max_dd":         max_dd,
        "calmar":         calmar,
        "sharpe":         sharpe,
        "sortino":        sortino,
        "win_rate":       win_rate,
        "wins":           len(wins),
        "losses":         len(losses),
        "avg_hold_min":   avg_hold,
        "fees_paid":      fees_paid,
        "years":          years,
        "start_date":     _date(klines[0]["ts_ms"]),
        "end_date":       _date(klines[-1]["ts_ms"]),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _print_report(result: dict, label: str, p: dict) -> None:
    if not result:
        print(f"{label}: no data")
        return
    r = result
    w = 72
    print("═" * w)
    print(f"SCALPING BACKTEST  {label}")
    print(f"  Strategy  : {p['strategy_type']}")
    print(f"  Period    : {r['start_date']} → {r['end_date']}  ({r['years']:.1f} yr)")
    print("═" * w)
    print(f"  Return    : ×{r['total_ret']:.2f}  (${p['capital']:,.0f} → ${r['final_capital']:,.0f})")
    print(f"  CAGR      : {r['cagr']*100:.1f}%")
    print(f"  Max DD    : {r['max_dd']*100:.1f}%")
    print(f"  Calmar    : {r['calmar']:.2f}")
    print(f"  Sharpe    : {r['sharpe']:.2f}  (per-trade)")
    print(f"  Sortino   : {r['sortino']:.2f}  (per-trade)")
    print("─" * w)
    print(f"  Trades    : {len(r['trades'])}  ({r['wins']} W / {r['losses']} L)")
    print(f"  Win rate  : {r['win_rate']*100:.1f}%")
    print(f"  Avg hold  : {r['avg_hold_min']:.1f} min")
    print(f"  Fees paid : ${r['fees_paid']:.2f}")
    print("═" * w)

    # Breakdown by exit reason
    reasons = {}
    for t in r["trades"]:
        reasons[t["reason"]] = reasons.get(t["reason"], 0) + 1
    if reasons:
        print("  Exit reasons:")
        for k, v in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {k:<20} {v:>5}")
        print("═" * w)


def _run_comparison(db_path: str) -> None:
    """Run all three strategy configs side-by-side."""
    configs = [
        Path(__file__).parent.parent / "strategies" / "scalping_candle_momentum.json",
        Path(__file__).parent.parent / "strategies" / "scalping_meanrev.json",
        Path(__file__).parent.parent / "strategies" / "scalping_breakout.json",
    ]
    print("Loading klines …", file=sys.stderr)
    klines = load_klines(db_path)
    print(f"  {len(klines):,} candles loaded.", file=sys.stderr)

    results = []
    for cfg_path in configs:
        if not cfg_path.exists():
            print(f"Config not found: {cfg_path}", file=sys.stderr)
            continue
        with open(cfg_path) as f:
            raw = json.load(f)
        p = dict(DEFAULTS)
        p.update({k: v for k, v in raw.items() if not k.startswith("_")})
        label = cfg_path.stem
        r = run_backtest(klines, p)
        if r:
            results.append((label, r, p))

    w = 80
    print()
    print("═" * w)
    print("SCALPING STRATEGY COMPARISON")
    print(f"  Period: {results[0][1]['start_date']} → {results[0][1]['end_date']}"
          f"  ({results[0][1]['years']:.1f} yr)" if results else "")
    print("═" * w)
    hdr = f"  {'Strategy':<30} {'Return':>8} {'CAGR':>7} {'MaxDD':>7} "
    hdr += f"{'Calmar':>7} {'Sharpe':>7} {'Trades':>7} {'WinRate':>8} {'AvgMin':>7}"
    print(hdr)
    print("  " + "─" * (w - 2))
    for label, r, p in results:
        print(
            f"  {label:<30} "
            f"×{r['total_ret']:>6.2f} "
            f"{r['cagr']*100:>6.1f}% "
            f"{r['max_dd']*100:>6.1f}% "
            f"{r['calmar']:>7.2f} "
            f"{r['sharpe']:>7.2f} "
            f"{len(r['trades']):>7} "
            f"{r['win_rate']*100:>7.1f}% "
            f"{r['avg_hold_min']:>7.1f}"
        )
    print("═" * w)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Scalping strategy backtest")
    ap.add_argument("db", nargs="?", default=str(DEFAULT_DB),
                    help="SQLite DB with 1m klines (default: BTCUSDT3197d_20172026.db)")
    ap.add_argument("--strategy", metavar="JSON",
                    help="Strategy config JSON file")
    ap.add_argument("--compare", action="store_true",
                    help="Run all three strategy configs and compare results")
    args = ap.parse_args()

    if args.compare:
        _run_comparison(args.db)
        return

    if not args.strategy:
        ap.error("--strategy <json> required (or use --compare)")

    cfg_path = Path(args.strategy)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        sys.exit(1)

    with open(cfg_path) as f:
        raw = json.load(f)

    p = dict(DEFAULTS)
    p.update({k: v for k, v in raw.items() if not k.startswith("_")})

    print(f"Loading klines from {args.db} …", file=sys.stderr)
    klines = load_klines(args.db)
    print(f"  {len(klines):,} candles loaded.", file=sys.stderr)

    result = run_backtest(klines, p)
    _print_report(result, cfg_path.stem, p)


if __name__ == "__main__":
    main()
