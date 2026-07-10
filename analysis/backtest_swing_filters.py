#!/usr/bin/env python3
"""backtest_swing_filters.py — sweep swing's trend filters (Ichimoku 4h/daily cloud +
RSI(14,4h)/EMA200(4h)) by driving the REAL SwingStrategy engine on replayed 1m klines.

Faithful by construction: it feeds the engine's own on_book_update and reads its own
accounting (same approach as backtest_engine.py), and it exercises the real _trend_ok
filter code — including the Ichimoku cloud gate shipped in the live engine.

Two correctness locks (both TESTED by --selftest):
  * No-lookahead: at 1m bar i, the injected HTF indicator is the value AS OF the last
    HTF candle CLOSED at or before i's timestamp — never the candle that CONTAINS i
    (which would leak the future), mirroring the live service that publishes only on
    candle close.
  * Filters-OFF anchor: with every filter off + no injection, results must equal the
    plain engine-driven swing baseline (backtest_engine._run_swing). Verified before any
    sweep — if it drifts, the injection harness is broken.

Usage:
    python3 analysis/backtest_swing_filters.py <db> --selftest      # locks (fast db)
    python3 analysis/backtest_swing_filters.py <db>                 # sweep on one db
    python3 analysis/backtest_swing_filters.py --regimes            # bull/bear/range
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import math
import os
import sys
import types
from unittest.mock import MagicMock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "tradinebotte-cex"))
sys.path.insert(0, os.path.join(_REPO, "analysis"))
sys.path.insert(0, os.path.join(_REPO, "tradinetools"))
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

from strategy_engines.swing import SwingStrategy          # noqa: E402
import backtest_swing_dca as bsd                          # noqa: E402
from backtest_engine import _load_klines, _tick           # noqa: E402
from tradinetools.math import ichimoku_last               # noqa: E402

_TF = {"4h": 14_400_000, "1d": 86_400_000}
_REGIME_DBS = [
    ("bull 2024-25", "data/BTCUSDT_1m92d_bullrun20241015-20250115.db"),
    ("bear 2022",    "data/BTCUSDT_1m92d_bearmarket20220501-20220801.db"),
    ("range 90d",    "data/BTCUSDT_1m90d_range_20260208-20260509.db"),
]


# ─── timeframe aggregation ──────────────────────────────────────────────────
def aggregate(rows, tf_ms):
    """1m rows [(ts,o,h,l,c,v)] → HTF bars [{ts(bucket),o,h,l,c,close_ms}] wall-clock aligned."""
    bars = []
    cur = None
    for ts, o, h, l, c, _v in rows:
        b = (ts // tf_ms) * tf_ms
        if cur is None or b != cur["ts"]:
            if cur is not None:
                bars.append(cur)
            cur = {"ts": b, "o": o, "h": h, "l": l, "c": c, "close_ms": b + tf_ms}
        else:
            cur["h"] = max(cur["h"], h); cur["l"] = min(cur["l"], l); cur["c"] = c
    if cur is not None:
        bars.append(cur)
    return bars


# ─── per-HTF-bar indicator series (value AS OF that bar's close) ─────────────
def _ema_series(xs, n):
    out = [None] * len(xs)
    if len(xs) >= n:
        prev = sum(xs[:n]) / n
        out[n - 1] = prev
        k = 2.0 / (n + 1)
        for i in range(n, len(xs)):
            prev = xs[i] * k + prev * (1 - k)
            out[i] = prev
    return out


def _rsi_series(closes, n=14):
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        g = l = 0.0
        for k in range(i - n + 1, i + 1):
            d = closes[k] - closes[k - 1]
            if d > 0: g += d
            else:     l -= d
        g /= n; l /= n
        out[i] = 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)
    return out


def _atr_series(highs, lows, closes, n=14):
    tr = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        tr.append(max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    return _ema_series(tr, n)


def _ichi_series(highs, lows, closes):
    tops = [None] * len(closes); bots = [None] * len(closes)
    for j in range(len(closes)):
        lo = max(0, j - 79)                              # ichimoku only needs the last 78
        r = ichimoku_last(highs[lo:j + 1], lows[lo:j + 1], closes[lo:j + 1])
        tops[j] = r["cloud_top"]; bots[j] = r["cloud_bottom"]
    return tops, bots


def htf_series(bars):
    """Return per-HTF-bar dict-of-lists: rsi, ema200, atr, ichi_top, ichi_bottom, close_ms."""
    highs = [b["h"] for b in bars]; lows = [b["l"] for b in bars]; closes = [b["c"] for b in bars]
    tops, bots = _ichi_series(highs, lows, closes)
    return {
        "close_ms":   [b["close_ms"] for b in bars],
        "rsi":        _rsi_series(closes, 14),
        "ema200":     _ema_series(closes, 200),
        "atr":        _atr_series(highs, lows, closes, 14),
        "ichi_top":   tops,
        "ichi_bottom": bots,
    }


def align_last_closed(rows, close_ms, series_keys):
    """For each 1m row, index j of the last HTF bar CLOSED at/before the row's ts (no lookahead).
    Returns list aligned to rows, each a dict of the series values at j (or None before first close)."""
    out = []
    j = -1
    n = len(close_ms)
    for ts, *_ in rows:
        while j + 1 < n and close_ms[j + 1] <= ts:
            j += 1
        out.append(j)
    return out


# ─── drive the real engine with per-bar indicator injection ─────────────────
async def _drive_swing(engine, rows, inject):
    import sqlite3
    conn = sqlite3.connect(":memory:")
    SwingStrategy.ensure_schema(conn)
    state = types.SimpleNamespace(conn=conn, session=MagicMock())
    engine._ensure_indicators_task = lambda: None        # neutralise the ZMQ SUB
    equity = []                                          # realized-PnL curve for drawdown
    with contextlib.redirect_stdout(io.StringIO()):
        for idx, (_ts, o, h, l, c, _v) in enumerate(rows):
            inject(engine, idx)                          # set sw.last_* for THIS bar (no lookahead)
            for bid, ask in ((l, l), (h, h)):            # low tick then high tick
                await engine.on_book_update(state, _tick(bid=bid, ask=ask), None)
            equity.append(engine.sw.total_pnl)
    return equity


def _metrics(equity, total_pnl, total_trades, capital=10_000.0):
    peak = 0.0; max_dd = 0.0
    for e in equity:
        peak = max(peak, e)
        max_dd = max(max_dd, peak - e)                   # absolute $ drawdown of realized PnL
    # per-step realized-PnL changes → a crude Sharpe (0 mean baseline is fine for ranking)
    deltas = [equity[i] - equity[i - 1] for i in range(1, len(equity)) if equity[i] != equity[i - 1]]
    if len(deltas) > 1:
        mu = sum(deltas) / len(deltas)
        sd = math.sqrt(sum((d - mu) ** 2 for d in deltas) / len(deltas))
        sharpe = mu / sd if sd > 0 else 0.0
    else:
        sharpe = 0.0
    return {"pnl": total_pnl, "trades": total_trades, "max_dd": max_dd, "sharpe": sharpe}


# ─── one run for a given filter config ──────────────────────────────────────
def run(rows, center, ichi_mode="off", rsi_ema=False, precomp=None):
    """ichi_mode: 'off'|'4h'|'1d'. rsi_ema: enable RSI(4h)+EMA200(4h). precomp: cached series."""
    sup = sorted(round(center * (1 + o), 2) for o in bsd._DEF_SUPPORT_OFFSETS)
    res = sorted(round(center * (1 + o), 2) for o in bsd._DEF_RESISTANCE_OFFSETS)
    n = len(sup)
    p = bsd.SwingParams(support_levels=sup, resistance_levels=res, max_positions=n)
    cfg = {"symbol": "BTCUSDT", "support_levels": sup, "resistance_levels": res,
           "order_size_usdt": p.order_size_usdt, "max_positions": n,
           "sl_pct": p.sl_pct, "tp_pct_fallback": p.tp_pct_fallback,
           "trend_filter_enabled": rsi_ema, "ema200_filter_enabled": rsi_ema,
           "ichimoku_filter_enabled": ichi_mode != "off", "poll_interval": 0.0}
    s = SwingStrategy(types.SimpleNamespace(connector="binance", strategy_cfg=cfg))

    import time
    a4 = precomp["align_4h"]; s4 = precomp["s4"]
    ad = precomp["align_1d"]; sd_ = precomp["s1d"]

    def inject(engine, idx):
        now = time.time()
        if rsi_ema:
            j = a4[idx]
            if j >= 0:
                engine.sw.last_rsi = s4["rsi"][j]
                engine.sw.last_ema200 = s4["ema200"][j]
                engine.sw.last_atr = s4["atr"][j]
                engine.sw.last_ind_ts = now
                engine.sw.last_rsi_ts = now
        if ichi_mode == "4h":
            j = a4[idx]
            if j >= 0 and s4["ichi_bottom"][j] is not None:
                engine.sw.last_ichi_top = s4["ichi_top"][j]
                engine.sw.last_ichi_bottom = s4["ichi_bottom"][j]
                engine.sw.last_ichi_ts = now
        elif ichi_mode == "1d":
            j = ad[idx]
            if j >= 0 and sd_["ichi_bottom"][j] is not None:
                engine.sw.last_ichi_top = sd_["ichi_top"][j]
                engine.sw.last_ichi_bottom = sd_["ichi_bottom"][j]
                engine.sw.last_ichi_ts = now

    equity = asyncio.run(_drive_swing(s, rows, inject))
    return _metrics(equity, s.sw.total_pnl, s.sw.total_trades)


def precompute(rows):
    b4 = aggregate(rows, _TF["4h"]); b1 = aggregate(rows, _TF["1d"])
    s4 = htf_series(b4); s1 = htf_series(b1)
    return {"s4": s4, "s1d": s1,
            "align_4h": align_last_closed(rows, s4["close_ms"], s4),
            "align_1d": align_last_closed(rows, s1["close_ms"], s1)}


# ─── sweep + reporting ──────────────────────────────────────────────────────
_GRID = [("off",  False), ("4h",  False), ("1d",  False),
         ("off",  True),  ("4h",  True),  ("1d",  True)]


def sweep_db(db_path, label):
    rows = _load_klines(db_path)
    if not rows:
        print(f"{label}: no klines"); return
    center = rows[0][1]
    pc = precompute(rows)
    print(f"\n=== {label}  ({len(rows):,} candles) ===")
    print(f"  {'ichimoku':>9} {'rsi+ema':>8} {'trades':>7} {'PnL$':>10} {'maxDD$':>9} {'sharpe':>7}")
    base = None
    for ichi, re_ in _GRID:
        m = run(rows, center, ichi_mode=ichi, rsi_ema=re_, precomp=pc)
        if base is None:
            base = m["pnl"]
        print(f"  {ichi:>9} {str(re_):>8} {m['trades']:>7} {m['pnl']:>10.2f} "
              f"{m['max_dd']:>9.2f} {m['sharpe']:>7.3f}")


def selftest(db_path):
    """Lock 1: no-lookahead alignment. Lock 2: filters-OFF anchor == engine baseline."""
    from backtest_engine import _run_swing
    rows = _load_klines(db_path)
    center = rows[0][1]
    pc = precompute(rows)

    # Lock 1 — alignment never uses a close_ms in the future of the row ts.
    a4, cm4 = pc["align_4h"], pc["s4"]["close_ms"]
    ok = all(j < 0 or cm4[j] <= rows[i][0] for i, j in enumerate(a4))
    ok2 = all(j + 1 >= len(cm4) or cm4[j + 1] > rows[i][0] for i, j in enumerate(a4))
    print(f"Lock 1 no-lookahead: {'PASS' if ok and ok2 else 'FAIL'} "
          f"(each row maps to the last HTF close ≤ its ts)")

    # Lock 2 — filters off + no injection must equal the engine baseline.
    m = run(rows, center, ichi_mode="off", rsi_ema=False, precomp=pc)
    _desc, (_, bt_tr, eng_tr, _, bt_pnl, eng_pnl) = _run_swing(rows, center, None)
    match = (m["trades"] == eng_tr and abs(m["pnl"] - eng_pnl) < 1e-6)
    print(f"Lock 2 filters-OFF anchor: {'PASS' if match else 'FAIL'} "
          f"(harness trades={m['trades']} pnl={m['pnl']:.2f} vs engine trades={eng_tr} pnl={eng_pnl:.2f})")
    return ok and ok2 and match


def main():
    import logging
    logging.disable(logging.CRITICAL)                    # hush engine SL/order chatter
    ap = argparse.ArgumentParser()
    ap.add_argument("db", nargs="?", default=os.path.join(_REPO, "data", "BTCUSDT_1m90d_range_20260208-20260509.db"))
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--regimes", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        raise SystemExit(0 if selftest(args.db) else 1)
    if args.regimes:
        for lbl, rel in _REGIME_DBS:
            p = os.path.join(_REPO, rel)
            if os.path.exists(p):
                sweep_db(p, lbl)
            else:
                print(f"skip {lbl}: {p} missing")
        return
    sweep_db(args.db, os.path.basename(args.db))


if __name__ == "__main__":
    main()
