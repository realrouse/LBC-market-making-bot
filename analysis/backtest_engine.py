#!/usr/bin/env python3
"""backtest_engine.py — drive the REAL strategy engines in sim mode on replayed klines,
instead of a re-implemented backtest.

Why: the standalone backtests (backtest_grid.py, backtest_swing_dca.py) re-implement the
strategy logic and can drift from the live strategy_engines. The engines already run a
self-simulated fill path in sim mode (no API key → orders get "sim_" ids; fills detected
from the price stream inside _poll_fills, driven by on_book_update). So a faithful backtest
is: feed historical prices to the real engine's on_book_update and read its own accounting.

Covers grid + swing (dca/swinghold reuse the same driver). Prints the engine-driven result
alongside the re-implemented backtest on the SAME grid/levels — the delta IS the drift.

Scope/caveats (see docs/backtest-fidelity.md):
  * Intra-candle order: each candle is replayed as a low tick then a high tick (buys check
    best_ask≤price, sells check best_bid≥price), the same approximation the old backtests make.
  * poll_interval forced to 0 so fills are checked every tick (the live 2s gate is wall-clock).
  * Swing's RSI(4h)/EMA200 trend filters are DISABLED here (the old backtest has none) — an
    apples-to-apples "no trend filter" comparison. Feeding historical RSI is future work.

Usage:
    python3 analysis/backtest_engine.py <db>                 # grid (default)
    python3 analysis/backtest_engine.py <db> --strategy swing
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import os
import sqlite3
import sys
import types
from unittest.mock import MagicMock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "tradinebotte-cex"))    # strategy_engines, connectors, api_*
sys.path.insert(0, os.path.join(_REPO, "analysis"))
os.environ.pop("BINANCE_API_KEY", None)                        # force sim mode (sim_ ids, no HTTP)
os.environ.pop("BINANCE_API_SECRET", None)

from strategy_engines.grid import GridStrategy               # noqa: E402
from strategy_engines.swing import SwingStrategy             # noqa: E402
import backtest_grid as bg                                   # noqa: E402
import backtest_swing_dca as bsd                             # noqa: E402

# grid schema (mirrors live_bot.py migrations) so _save_state upsert works. Swing brings its
# own via SwingStrategy.ensure_schema().
_GRID_SCHEMA = """
CREATE TABLE grid_state (
    symbol TEXT PRIMARY KEY, grid_lower REAL, grid_upper REAL, grid_step REAL,
    order_size_usdt REAL, total_cycles INTEGER DEFAULT 0,
    total_profit_usd REAL DEFAULT 0.0, initialised INTEGER DEFAULT 0,
    halted INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE grid_levels (
    symbol TEXT, level_price REAL, buy_order_id TEXT, sell_order_id TEXT,
    buy_price REAL, sell_price REAL, status TEXT DEFAULT 'idle',
    filled_at_ts REAL, updated_at REAL, entry_price REAL,
    PRIMARY KEY (symbol, level_price));
"""


def _load_klines(db_path: str) -> list[tuple]:
    """[(ts, open, high, low, close, volume), …] ordered by time. (backtest_grid wants 5
    cols, backtest_swing_dca wants 6 incl. volume — load 6, slice for grid.)"""
    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(klines)")]
        tcol = "ts_open" if "ts_open" in cols else cols[0]
        vcol = "volume" if "volume" in cols else ("vol" if "vol" in cols else "0")
        rows = con.execute(
            f"SELECT {tcol}, open, high, low, close, {vcol} FROM klines ORDER BY {tcol}"
        ).fetchall()
    finally:
        con.close()
    return [(int(t), float(o), float(h), float(l), float(c), float(v or 0))
            for t, o, h, l, c, v in rows]


def _tick(bid: float, ask: float):
    ts = types.SimpleNamespace()
    ts.best_bid = bid
    ts.best_ask = ask
    return ts


async def _drive(engine, rows: list[tuple], schema_sql: str | None = None,
                 ensure_schema=None) -> None:
    """Feed the klines to a real engine via on_book_update (low tick then high tick per
    candle). Sets up an in-memory state; stops early if the engine halts."""
    conn = sqlite3.connect(":memory:")
    if schema_sql:
        conn.executescript(schema_sql)
    if ensure_schema:
        ensure_schema(conn)
    state = types.SimpleNamespace(conn=conn, session=MagicMock())
    with contextlib.redirect_stdout(io.StringIO()):        # hush the connector's sim chatter
        for row in rows:
            high, low = row[2], row[3]
            await engine.on_book_update(state, _tick(bid=low, ask=low), None)
            await engine.on_book_update(state, _tick(bid=high, ask=high), None)
            if getattr(getattr(engine, "grid", None), "halted", False):
                break


def _run_grid(rows, center, args):
    lower = center * (1 - args.range_pct / 100)
    upper = center * (1 + args.range_pct / 100)
    cfg = types.SimpleNamespace(
        grid_lower=lower, grid_upper=upper, grid_levels=args.levels,
        grid_order_size_usdt=args.size, grid_symbol="BTCUSDT",
        connector="binance", grid_trail_mode="static")
    g = GridStrategy(cfg)
    g.grid.poll_interval = 0.0
    asyncio.run(_drive(g, rows, schema_sql=_GRID_SCHEMA))
    bt = bg._run_engine([r[:5] for r in rows],           # backtest_grid wants 5 cols (no vol)
                        bg.GridParams(range_pct=args.range_pct, levels=args.levels, size=args.size))
    grid_desc = f"[{lower:,.0f}, {upper:,.0f}]  {args.levels} levels  ${args.size}/level"
    return grid_desc, ("cycles", bt.cycles, g.grid.total_cycles,
                       "realized PnL", bt.realized_pnl, g.grid.total_profit_usd)


def _run_swing(rows, center, _args):
    sup = sorted(round(center * (1 + o), 2) for o in bsd._DEF_SUPPORT_OFFSETS)
    res = sorted(round(center * (1 + o), 2) for o in bsd._DEF_RESISTANCE_OFFSETS)
    # max_positions == #supports so BOTH sides arm the SAME level set (else the engine arms
    # only the top-3 at init and never the 4th, adding a level-set confound on top of the
    # SL-re-arm difference we're actually isolating — see docs/backtest-fidelity.md §5).
    n = len(sup)
    p = bsd.SwingParams(support_levels=sup, resistance_levels=res, max_positions=n)
    cfg = {"symbol": "BTCUSDT", "support_levels": sup, "resistance_levels": res,
           "order_size_usdt": p.order_size_usdt, "max_positions": n,
           "sl_pct": p.sl_pct, "tp_pct_fallback": p.tp_pct_fallback,
           "trend_filter_enabled": False, "ema200_filter_enabled": False, "poll_interval": 0.0}
    s = SwingStrategy(types.SimpleNamespace(connector="binance", strategy_cfg=cfg))
    asyncio.run(_drive(s, rows, ensure_schema=SwingStrategy.ensure_schema))
    bt = bsd.run_swing(rows, p)
    desc = f"{len(sup)} support / {len(res)} resistance  ${p.order_size_usdt}/order  (trend filter OFF)"
    return desc, ("trades", bt.n_trades, s.sw.total_trades,
                  "realized PnL", bt.realized_pnl, s.sw.total_pnl)


def main() -> int:
    ap = argparse.ArgumentParser(description="Real engine vs re-implemented backtest.")
    ap.add_argument("db")
    ap.add_argument("--strategy", choices=["grid", "swing"], default="grid")
    ap.add_argument("--range", type=float, default=15.0, dest="range_pct")
    ap.add_argument("--levels", type=int, default=30)
    ap.add_argument("--size", type=float, default=50.0)
    args = ap.parse_args()

    rows = _load_klines(args.db)
    if not rows:
        print("no klines in db", file=sys.stderr)
        return 2
    center = rows[0][1]

    runner = {"grid": _run_grid, "swing": _run_swing}[args.strategy]
    desc, (m1, b1, e1, m2, b2, e2) = runner(rows, center, args)

    print(f"db       : {os.path.basename(args.db)}  ({len(rows)} candles)")
    print(f"strategy : {args.strategy} — {desc}\n")
    print(f"{'':20} {m1:>8} {m2:>16}")
    print(f"{'re-impl backtest':20} {b1:>8} {b2:>15.2f}$")
    print(f"{'real engine':20} {e1:>8} {e2:>15.2f}$")
    print(f"{'Δ (engine − bt)':20} {e1 - b1:>+8} {e2 - b2:>+15.2f}$")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
