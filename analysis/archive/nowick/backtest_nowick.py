#!/usr/bin/env python3
"""
No-Wick strategy backtester — BTC/USDT.  [ARCHIVED — DO NOT DEPLOY]

Verdict (Phase-0, 2026-07-09): the FTMO no-wick pattern has NO exploitable edge on
BTC 15m spot. Momentum (long) AND fade (short) are both FLAT at zero cost (×1.01 /
×1.02), i.e. the signal is noise w.r.t. future returns; at realistic 0.31% round-
trip cost every config loses (literal ×0.64). The live engine (Phases 1-5) was NOT
built. This harness is kept only as reproducible evidence and a reusable multi-TF
candle/breakout backtest skeleton. See README.md in this directory.

Reproduce (from the repo root, tradinebotte/):
    python3 analysis/archive/nowick/backtest_nowick.py --strategy analysis/archive/nowick/nowick_BTCUSDT.json --regimes
    python3 analysis/archive/nowick/backtest_nowick.py --strategy analysis/archive/nowick/nowick_BTCUSDT.json --forward

Signal (long-only), evaluated on COMPLETED signal-timeframe candles (default 15m,
aggregated from 1m source data):

  1. Wickless bullish candle : close > open  AND  upper_wick / range < max_upper_wick_pct
                               (upper_wick = high - close). Candle closes at/near its peak.
  2. Relative volume         : candle_volume / SMA(volume, N) >= rvol_min
  3. Breakout                : close > rolling_max(high, breakout_lookback) of PRIOR candles
  4. Higher-timeframe align  : the last COMPLETED 4h candle is bullish (close > open)

Entry:  market at the signal candle close (+ slippage).
Stop:   initial = signal candle low - sl_buffer_pct ; then a TRAILING ATR stop
        (high_water - atr_trail_multiplier * ATR) that only ratchets up.
Exit:   stop hit (simulated on 1m granularity for realism) or max_hold_minutes.

This module deliberately lives beside backtest_scalping.py rather than inside its
single-timeframe run_backtest loop: no-wick needs multi-timeframe aggregation
(15m signal + 4h context) and a trailing-stop exit that the fixed-TP/SL 1m loop
does not model. It REUSES the shared primitives (load_klines, atr_series, sma,
rolling_max) and the reporting helper from backtest_scalping — same "reuse the
primitives, own the orchestration" split as the live service/engine design.

Usage
-----
    python3 analysis/backtest_nowick.py --strategy strategies/scalping/nowick_BTCUSDT.json
    python3 analysis/backtest_nowick.py --strategy <file> data/BTCUSDT_1m92d_bullrun20241015-20250115.db
    python3 analysis/backtest_nowick.py --strategy <file> --regimes   # run across regime DBs
"""

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

# Archived location: analysis/archive/nowick/ → analysis/ is parents[2], repo root
# is parents[3]. Reuse the shared primitives + reporting from the scalping backtester.
_ANALYSIS_DIR = Path(__file__).resolve().parents[2]
_REPO_ROOT    = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ANALYSIS_DIR))
from backtest_scalping import (  # noqa: E402
    load_klines,
    sma,
    atr_series,
    rolling_max,
    _print_report,
)

DEFAULT_DB = _REPO_ROOT / "data" / "BTCUSDT3197d_20172026.db"

_TF_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
    "1d": 86_400_000,
}

DEFAULTS = dict(
    strategy_type          = "nowick",
    symbol                 = "BTCUSDT",
    capital                = 10_000.0,
    fee_rate               = 0.001,      # 0.1% per side (Binance spot taker)
    slippage_pct           = 0.0005,     # 0.05% per side
    stake_frac             = 0.20,       # fraction of capital per trade

    signal_timeframe       = "15m",
    htf_timeframe          = "4h",

    max_upper_wick_pct     = 0.10,       # upper wick must be < 10% of candle range
    min_body_ratio         = 0.50,       # body / range floor (rejects small-body wickless bars)
    rvol_window            = 20,         # candles for the volume baseline SMA
    rvol_min               = 1.5,        # candle_volume / SMA(volume) must exceed this
    require_4h_alignment   = True,
    require_level_break    = True,
    breakout_lookback      = 96,         # prior signal-TF candles for the breakout high (96×15m = 24h)

    atr_period             = 14,
    atr_trail_multiplier   = 2.0,        # trailing stop distance = mult × ATR
    sl_buffer_pct          = 0.001,      # initial stop = signal_low × (1 - this)
    max_hold_minutes       = 1440,       # hard timeout (minutes of 1m data)

    # fade variant (short the wickless bull candle)
    fade_tp_pct            = 0.004,      # cover at entry × (1 - this) — retracement target
    fade_sl_buffer_pct     = 0.001,      # stop = signal_high × (1 + this)
    fade_max_hold_minutes  = 240,        # 4h cap (k=4 hint is a 1h horizon)
)


# ---------------------------------------------------------------------------
# Timeframe aggregation
# ---------------------------------------------------------------------------

def aggregate(klines: list, tf_ms: int) -> list:
    """
    Aggregate 1m klines into tf_ms bars aligned to wall-clock boundaries.

    Each output bar carries the [i0, i1] inclusive index range of the source 1m
    candles that compose it, so the exit simulation can walk 1m granularity after
    an entry without re-searching.
    """
    if not klines:
        return []
    bars = []
    cur = None
    for i, k in enumerate(klines):
        bucket = (k["ts_ms"] // tf_ms) * tf_ms
        if cur is None or bucket != cur["ts_ms"]:
            if cur is not None:
                bars.append(cur)
            cur = {
                "ts_ms": bucket,
                "open":  k["open"], "high": k["high"],
                "low":   k["low"],  "close": k["close"],
                "volume": k["volume"],
                "i0": i, "i1": i,
            }
        else:
            cur["high"]   = max(cur["high"], k["high"])
            cur["low"]    = min(cur["low"],  k["low"])
            cur["close"]  = k["close"]
            cur["volume"] += k["volume"]
            cur["i1"]     = i
    if cur is not None:
        bars.append(cur)
    return bars


def _htf_bull_at(htf_bars: list, ts_ms: int) -> "bool | None":
    """
    Colour of the last HTF bar COMPLETED strictly before ts_ms (no look-ahead).

    Returns True (bull) / False (bear) / None (no completed HTF bar yet).
    """
    prev = None
    for b in htf_bars:
        # b covers [b.ts_ms, b.ts_ms + tf). It is "completed before ts_ms" only
        # once ts_ms has reached the start of a LATER bar.
        if b["ts_ms"] < (ts_ms // _TF_MS_HTF) * _TF_MS_HTF:
            prev = b
        else:
            break
    if prev is None:
        return None
    return prev["close"] > prev["open"]


# ---------------------------------------------------------------------------
# Core backtest
# ---------------------------------------------------------------------------

def run_backtest(klines: list, p: dict) -> dict:
    """Simulate the no-wick strategy. Returns the same metrics dict shape as
    backtest_scalping.run_backtest (so _print_report renders it)."""
    global _TF_MS_HTF
    n1 = len(klines)
    if n1 < 200:
        return {}

    sig_tf = _TF_MS[p["signal_timeframe"]]
    _TF_MS_HTF = _TF_MS[p["htf_timeframe"]]

    bars = aggregate(klines, sig_tf)
    htf  = aggregate(klines, _TF_MS_HTF)
    nb   = len(bars)
    if nb < p["breakout_lookback"] + p["rvol_window"] + 5:
        return {}

    highs   = [b["high"]   for b in bars]
    lows    = [b["low"]    for b in bars]
    closes  = [b["close"]  for b in bars]
    opens   = [b["open"]   for b in bars]
    volumes = [b["volume"] for b in bars]

    vol_sma   = sma(volumes, p["rvol_window"])
    atr_vals  = atr_series(highs, lows, closes, p["atr_period"])
    # Breakout reference: rolling max of highs over the lookback window, taken on
    # the PRIOR bar (index i-1) so the current bar's own high never leaks in.
    brk_high  = rolling_max(highs, p["breakout_lookback"])

    capital   = p["capital"]
    fee       = p["fee_rate"]
    slip      = p["slippage_pct"]
    trades    = []
    fees_paid = 0.0
    equity_curve = [capital]

    for i in range(1, nb):
        rng = highs[i] - lows[i]
        if rng <= 0:
            continue

        # ── Confluence checks (all use info available at bar i's close) ────────
        upper_wick = highs[i] - closes[i]
        body       = closes[i] - opens[i]

        wick_ok = (closes[i] > opens[i]
                   and upper_wick / rng < p["max_upper_wick_pct"]
                   and (body / rng) >= p["min_body_ratio"])
        if not wick_ok:
            equity_curve.append(capital)
            continue

        vs = vol_sma[i]
        rvol_ok = vs is not None and vs > 0 and (volumes[i] / vs) >= p["rvol_min"]
        if not rvol_ok:
            equity_curve.append(capital)
            continue

        if p["require_level_break"]:
            ref = brk_high[i - 1]
            if ref is None or closes[i] <= ref:
                equity_curve.append(capital)
                continue

        if p["require_4h_alignment"]:
            htf_bull = _htf_bull_at(htf, bars[i]["ts_ms"])
            if htf_bull is not True:
                equity_curve.append(capital)
                continue

        atr = atr_vals[i]
        if atr is None or atr <= 0:
            equity_curve.append(capital)
            continue

        # ── Enter long at bar close, then trail an ATR stop over 1m candles ────
        entry_px = closes[i] * (1 + slip)
        cost     = capital * p["stake_frac"]
        qty      = cost / entry_px
        fees_paid += cost * fee

        trail_dist = p["atr_trail_multiplier"] * atr
        stop       = min(lows[i] * (1 - p["sl_buffer_pct"]), entry_px - trail_dist)
        high_water = highs[i]

        exit_px = None
        exit_reason = None
        # 1m candles from the first candle AFTER the signal bar to end of data.
        j0 = bars[i]["i1"] + 1
        j_end = min(n1, j0 + p["max_hold_minutes"])
        for j in range(j0, j_end):
            klo = klines[j]["low"]
            khi = klines[j]["high"]
            # Conservative intrabar order for a long: assume the low prints before
            # the high, so a stop can fire before the same candle ratchets it up.
            if klo <= stop:
                exit_px = stop
                exit_reason = "trail_stop"
                break
            if khi > high_water:
                high_water = khi
                stop = max(stop, high_water - trail_dist)
        if exit_px is None:
            # Timed out or ran off the end of data — close at last seen close.
            last_j = min(j_end, n1) - 1
            exit_px = klines[last_j]["close"]
            exit_reason = "timeout" if j_end < n1 else "end_of_data"

        gross    = qty * exit_px * (1 - slip)
        exit_fee = gross * fee
        fees_paid += exit_fee
        pnl = gross - exit_fee - cost - cost * fee
        capital += pnl
        trades.append({
            "entry_i":  i,
            "exit_i":   i,
            "entry_px": entry_px,
            "exit_px":  exit_px,
            "pnl":      pnl,
            "hold_min": (min(j, j_end) - j0) if exit_reason == "trail_stop" else (j_end - j0),
            "reason":   exit_reason,
            "label":    "nowick_long",
        })
        equity_curve.append(capital)

    return _metrics(trades, equity_curve, klines, p, fees_paid)


def run_fade(klines: list, p: dict) -> dict:
    """
    FADE variant (mean-reversion): SHORT the wickless bullish signal candle instead
    of chasing it, on the k=4 negative-forward-return hint. Exit = fixed retracement
    TP below entry, stop above the signal candle high, or timeout. Simulated intrabar
    on 1m. Requires a futures venue to short (spot accounts cannot) — backtest only.
    """
    global _TF_MS_HTF
    n1 = len(klines)
    if n1 < 200:
        return {}
    sig_tf = _TF_MS[p["signal_timeframe"]]
    _TF_MS_HTF = _TF_MS[p["htf_timeframe"]]
    bars = aggregate(klines, sig_tf)
    htf  = aggregate(klines, _TF_MS_HTF)
    nb   = len(bars)
    if nb < p["breakout_lookback"] + p["rvol_window"] + 5:
        return {}

    highs=[b["high"] for b in bars]; lows=[b["low"] for b in bars]
    closes=[b["close"] for b in bars]; opens=[b["open"] for b in bars]
    volumes=[b["volume"] for b in bars]
    vol_sma = sma(volumes, p["rvol_window"])
    brk_high = rolling_max(highs, p["breakout_lookback"])

    capital=p["capital"]; fee=p["fee_rate"]; slip=p["slippage_pct"]
    trades=[]; fees_paid=0.0; equity_curve=[capital]

    for i in range(1, nb):
        rng = highs[i] - lows[i]
        if rng <= 0:
            continue
        upper_wick = highs[i] - closes[i]; body = closes[i] - opens[i]
        if not (closes[i] > opens[i] and upper_wick / rng < p["max_upper_wick_pct"]
                and body / rng >= p["min_body_ratio"]):
            equity_curve.append(capital); continue
        vs = vol_sma[i]
        if not (vs and vs > 0 and volumes[i] / vs >= p["rvol_min"]):
            equity_curve.append(capital); continue
        if p["require_level_break"] and (brk_high[i-1] is None or closes[i] <= brk_high[i-1]):
            equity_curve.append(capital); continue
        if p["require_4h_alignment"] and _htf_bull_at(htf, bars[i]["ts_ms"]) is not True:
            equity_curve.append(capital); continue

        # SHORT at bar close; TP below, SL above the signal high.
        entry_px = closes[i] * (1 - slip)
        cost     = capital * p["stake_frac"]
        qty      = cost / entry_px
        fees_paid += cost * fee
        tp = entry_px * (1 - p["fade_tp_pct"])
        sl = highs[i] * (1 + p["fade_sl_buffer_pct"])

        j0 = bars[i]["i1"] + 1
        j_end = min(n1, j0 + p["fade_max_hold_minutes"])
        exit_px=None; reason=None
        for j in range(j0, j_end):
            khi = klines[j]["high"]; klo = klines[j]["low"]
            # Conservative for a short: assume the high (adverse) prints before the low.
            if khi >= sl:
                exit_px = sl; reason = "stop_loss"; break
            if klo <= tp:
                exit_px = tp; reason = "take_profit"; break
        if exit_px is None:
            last_j = min(j_end, n1) - 1
            exit_px = klines[last_j]["close"]
            reason = "timeout" if j_end < n1 else "end_of_data"

        # Short PnL: profit when exit < entry. Fee on both notionals.
        exit_notional = qty * exit_px
        fees_paid += exit_notional * fee
        pnl = qty * (entry_px - exit_px) - cost * fee - exit_notional * fee
        capital += pnl
        trades.append({"entry_i": i, "exit_i": i, "entry_px": entry_px,
                       "exit_px": exit_px, "pnl": pnl,
                       "hold_min": (min(j, j_end) - j0) if reason in ("stop_loss","take_profit") else (j_end - j0),
                       "reason": reason, "label": "nowick_fade"})
        equity_curve.append(capital)

    return _metrics(trades, equity_curve, klines, p, fees_paid)


def _metrics(trades, equity_curve, klines, p, fees_paid) -> dict:
    """Build the metrics dict (same shape/keys as backtest_scalping)."""
    start_ts = klines[0]["ts_ms"] / 1000
    end_ts   = klines[-1]["ts_ms"] / 1000
    years    = (end_ts - start_ts) / 86400 / 365.25

    capital   = equity_curve[-1] if equity_curve else p["capital"]
    total_ret = capital / p["capital"]
    cagr      = total_ret ** (1 / years) - 1 if years > 0 and total_ret > 0 else 0.0

    peak = p["capital"]; max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        max_dd = max(max_dd, (peak - eq) / peak)

    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) if trades else 0.0
    avg_hold = sum(t["hold_min"] for t in trades) / len(trades) if trades else 0.0

    rets = [t["pnl"] / p["capital"] for t in trades]
    if len(rets) > 1:
        mu  = sum(rets) / len(rets)
        std = math.sqrt(sum((r - mu) ** 2 for r in rets) / len(rets))
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
        "trades": trades, "final_capital": capital, "total_ret": total_ret,
        "cagr": cagr, "max_dd": max_dd, "calmar": calmar,
        "sharpe": sharpe, "sortino": sortino, "win_rate": win_rate,
        "wins": len(wins), "losses": len(losses), "avg_hold_min": avg_hold,
        "fees_paid": fees_paid, "years": years,
        "start_date": _date(klines[0]["ts_ms"]), "end_date": _date(klines[-1]["ts_ms"]),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_REGIME_DBS = [
    ("bull 2024-25", "data/BTCUSDT_1m92d_bullrun20241015-20250115.db"),
    ("bear 2022",    "data/BTCUSDT_1m92d_bearmarket20220501-20220801.db"),
    ("range 90d",    "data/BTCUSDT_1m90d_range_20260208-20260509.db"),
]


def forward_edge(klines: list, p: dict, horizons=(4, 8, 16, 32)) -> None:
    """
    Exit-agnostic edge test: mean forward return of the signal-TF close, k bars
    after each no-wick signal, vs the unconditional k-bar baseline over the same
    series. Isolates ENTRY quality from the trailing-stop exit design — the
    discriminator between "the entry has no edge" and "the exit/fees kill an edge".
    """
    global _TF_MS_HTF
    sig_tf = _TF_MS[p["signal_timeframe"]]
    _TF_MS_HTF = _TF_MS[p["htf_timeframe"]]
    bars = aggregate(klines, sig_tf)
    htf  = aggregate(klines, _TF_MS_HTF)
    highs=[b["high"] for b in bars]; lows=[b["low"] for b in bars]
    closes=[b["close"] for b in bars]; opens=[b["open"] for b in bars]
    vols=[b["volume"] for b in bars]
    vsma = sma(vols, p["rvol_window"]); brk = rolling_max(highs, p["breakout_lookback"])

    sig = []
    for i in range(1, len(bars)):
        rng = highs[i] - lows[i]
        if rng <= 0:
            continue
        uw = highs[i] - closes[i]; body = closes[i] - opens[i]
        if not (closes[i] > opens[i] and uw / rng < p["max_upper_wick_pct"]
                and body / rng >= p["min_body_ratio"]):
            continue
        vs = vsma[i]
        if not (vs and vs > 0 and vols[i] / vs >= p["rvol_min"]):
            continue
        if p["require_level_break"] and (brk[i - 1] is None or closes[i] <= brk[i - 1]):
            continue
        if p["require_4h_alignment"] and _htf_bull_at(htf, bars[i]["ts_ms"]) is not True:
            continue
        sig.append(i)

    print(f"  signals: {len(sig)}   (round-trip cost ≈ "
          f"{(2*p['fee_rate']+2*p['slippage_pct'])*1e4:.0f} bps)")
    print(f"  {'k(sig bars)':>12} {'signal_ret%':>12} {'baseline%':>11} {'edge(bps)':>10}")
    for k in horizons:
        rr = [(closes[i + k] - closes[i]) / closes[i] for i in sig if i + k < len(closes)]
        base = [(closes[i + k] - closes[i]) / closes[i] for i in range(len(closes) - k)]
        s = sum(rr) / len(rr) if rr else 0.0
        b = sum(base) / len(base) if base else 0.0
        print(f"  {k:>12} {s*100:>11.3f} {b*100:>10.3f} {(s-b)*1e4:>9.1f}")


def _load_params(strategy_path: str) -> dict:
    with open(strategy_path) as f:
        raw = json.load(f)
    p = dict(DEFAULTS)
    p.update({k: v for k, v in raw.items() if not k.startswith("_")})
    return p


def main() -> None:
    ap = argparse.ArgumentParser(description="No-Wick strategy backtest")
    ap.add_argument("db", nargs="?", default=str(DEFAULT_DB),
                    help="SQLite DB with 1m klines")
    ap.add_argument("--strategy", metavar="JSON", required=True,
                    help="Strategy config JSON file")
    ap.add_argument("--regimes", action="store_true",
                    help="Run across the regime DBs (bull / bear / range)")
    ap.add_argument("--forward", action="store_true",
                    help="Exit-agnostic forward-return edge test (entry quality vs baseline)")
    args = ap.parse_args()

    p = _load_params(args.strategy)
    repo = _REPO_ROOT

    if args.forward:
        print(f"Loading klines from {args.db} …", file=sys.stderr)
        kl = load_klines(args.db)
        print(f"  {len(kl):,} candles.", file=sys.stderr)
        print(f"\nFORWARD-RETURN EDGE TEST  [{Path(args.strategy).stem}]")
        forward_edge(kl, p)
        return

    if args.regimes:
        for label, rel in _REGIME_DBS:
            dbp = repo / rel
            if not dbp.exists():
                print(f"skip {label}: {dbp} missing", file=sys.stderr)
                continue
            print(f"\nLoading {label} klines …", file=sys.stderr)
            kl = load_klines(str(dbp))
            print(f"  {len(kl):,} candles.", file=sys.stderr)
            _print_report(run_backtest(kl, p), f"{Path(args.strategy).stem}  [{label}]", p)
        return

    print(f"Loading klines from {args.db} …", file=sys.stderr)
    kl = load_klines(args.db)
    print(f"  {len(kl):,} candles.", file=sys.stderr)
    _print_report(run_backtest(kl, p), Path(args.strategy).stem, p)


if __name__ == "__main__":
    main()
