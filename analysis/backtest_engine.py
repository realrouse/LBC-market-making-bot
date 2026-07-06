#!/usr/bin/env python3
"""backtest_engine.py — Phase-1 PoC: drive the REAL strategy engine in sim mode on replayed
klines, instead of a re-implemented backtest.

Why: the standalone backtests (backtest_grid.py, …) re-implement the strategy logic and can
drift from the live strategy_engines. The engines already run a self-simulated fill path in
sim mode (no API key → orders get "sim_" ids; fills detected from the price stream inside
_poll_fills, driven by on_book_update). So a faithful backtest is just: feed historical
prices to the real engine's on_book_update and read its own cycle/PnL accounting.

This PoC covers GridStrategy only, and prints the engine-driven result alongside
backtest_grid.py's re-implemented result on the SAME db + grid — the delta IS the drift.

Scope/caveats (see docs/backtest-fidelity.md):
  * Intra-candle order: each candle is replayed as a low tick then a high tick (buys check
    best_ask≤price, sells check best_bid≥price), the same approximation backtest_grid makes.
  * poll_interval is forced to 0 so fills are checked every tick (the live 2s gate uses
    wall-clock, which a fast replay would skip).

Usage:
    python3 analysis/backtest_engine.py data/BTCUSDT_1m90d_range_...db
    python3 analysis/backtest_engine.py <db> --range 15 --levels 30 --size 50
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sqlite3
import sys
import types
from unittest.mock import MagicMock

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "tradinebotte-cex"))    # strategy_engines, connectors, api_*
sys.path.insert(0, os.path.join(_REPO, "analysis"))
# Force sim mode: no credentials → the connector's post_order returns "sim_" ids, no HTTP.
os.environ.pop("BINANCE_API_KEY", None)
os.environ.pop("BINANCE_API_SECRET", None)

from strategy_engines.grid import GridStrategy          # noqa: E402
import backtest_grid as bg                              # noqa: E402  (the re-implemented one)

# grid schema (mirrors live_bot.py migrations) so the engine's _save_state upsert works.
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
    """Return [(ts, open, high, low, close), …] ordered by time."""
    con = sqlite3.connect(db_path)
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(klines)")]
        tcol = "ts_open" if "ts_open" in cols else cols[0]
        rows = con.execute(
            f"SELECT {tcol}, open, high, low, close FROM klines ORDER BY {tcol}"
        ).fetchall()
    finally:
        con.close()
    return [(int(t), float(o), float(h), float(l), float(c)) for t, o, h, l, c in rows]


def _tick(bid: float, ask: float):
    ts = types.SimpleNamespace()
    ts.best_bid = bid
    ts.best_ask = ask
    return ts


async def _run_engine(rows: list[tuple], lower: float, upper: float,
                      levels: int, size: float) -> tuple[int, float]:
    """Drive the REAL GridStrategy over the klines; return (cycles, realized_pnl)."""
    cfg = types.SimpleNamespace(
        grid_lower=lower, grid_upper=upper, grid_levels=levels,
        grid_order_size_usdt=size, grid_symbol="BTCUSDT",
        connector="binance", grid_trail_mode="static",
    )
    g = GridStrategy(cfg)
    g.grid.poll_interval = 0.0                       # check fills every tick (see caveat)

    conn = sqlite3.connect(":memory:")
    conn.executescript(_GRID_SCHEMA)
    state = types.SimpleNamespace(conn=conn, session=MagicMock())

    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):     # hush the connector's sim-order chatter
        for _ts, _o, high, low, _c in rows:
            # low tick first (arms/fills BUYs at/below low + stop-loss-low), then high tick
            # (fills SELLs at/above high + stop-loss-high) — matches backtest_grid ordering.
            await g.on_book_update(state, _tick(bid=low, ask=low), None)
            await g.on_book_update(state, _tick(bid=high, ask=high), None)
            if g.grid.halted:
                break
    return g.grid.total_cycles, g.grid.total_profit_usd


def main() -> int:
    ap = argparse.ArgumentParser(description="PoC: real grid engine vs re-implemented backtest_grid.")
    ap.add_argument("db")
    ap.add_argument("--range", type=float, default=15.0, dest="range_pct")
    ap.add_argument("--levels", type=int, default=30)
    ap.add_argument("--size", type=float, default=50.0)
    args = ap.parse_args()

    rows = _load_klines(args.db)
    if not rows:
        print("no klines in db", file=sys.stderr)
        return 2
    center = rows[0][1]                                  # first open
    lower = center * (1 - args.range_pct / 100)
    upper = center * (1 + args.range_pct / 100)

    # ── Re-implemented backtest (backtest_grid._run_engine) on the same grid ──
    params = bg.GridParams(range_pct=args.range_pct, levels=args.levels, size=args.size)
    bt = bg._run_engine(rows, params)

    # ── Real engine driven over the same klines ──
    cyc_e, pnl_e = asyncio.run(_run_engine(rows, lower, upper, args.levels, args.size))

    print(f"db           : {os.path.basename(args.db)}  ({len(rows)} candles)")
    print(f"grid         : [{lower:,.0f}, {upper:,.0f}]  {args.levels} levels  ${args.size}/level\n")
    print(f"{'':20} {'cycles':>8} {'realized PnL':>14}")
    print(f"{'backtest_grid.py':20} {bt.cycles:>8} {bt.realized_pnl:>13.2f}$")
    print(f"{'real grid engine':20} {cyc_e:>8} {pnl_e:>13.2f}$")
    d_cyc = cyc_e - bt.cycles
    d_pnl = pnl_e - bt.realized_pnl
    print(f"{'Δ (engine − bt)':20} {d_cyc:>+8} {d_pnl:>+13.2f}$")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
