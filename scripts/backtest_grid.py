#!/usr/bin/env python3
"""
Backtest a static grid trading strategy against OHLCV SQLite databases.

Fill model  : price-touch on candle [low, high] range.
Order model : BUY limit orders placed at levels below start price;
              after each BUY fill a SELL is placed one step above;
              after each SELL fill a BUY is placed one step below.
Capital     : n_levels × order_size  (worst-case: all levels filled at once).
Stop-loss   : triggered when candle low < grid_lower or high > grid_upper;
              remaining BTC liquidated at candle close price.

Usage:
    python scripts/backtest_grid.py --all
    python scripts/backtest_grid.py data/BTCUSDT_1m90d_range_*.db
    python scripts/backtest_grid.py --all --range 20 --levels 40 --size 50
    python scripts/backtest_grid.py --all --sweep
    python scripts/backtest_grid.py --all --sweep --sort pnl
"""

import argparse
import sqlite3
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

FEE_RATE = 0.001   # 0.1 % — Binance spot taker/maker
DATA_DIR  = Path(__file__).resolve().parent.parent / "data"


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class GridParams:
    range_pct: float = 15.0   # ±% from start price
    levels:    int   = 30
    size:      float = 50.0   # USDT per order
    fee_rate:  float = FEE_RATE


@dataclass
class GridResult:
    label:       str
    period:      str
    days:        int
    start_price: float
    grid_lower:  float
    grid_upper:  float
    grid_step:   float
    levels:      int
    size:        float
    capital:     float
    cycles:      int
    gross_pnl:   float
    fees:        float
    net_pnl:     float
    max_dd_pct:  float
    time_pct:    float         # % of candles before stop
    stop_reason: str           # "completed" | "exit_low" | "exit_high"
    final_btc:   float
    final_price: float
    params:      GridParams

    @property
    def pnl_pct(self) -> float:
        return self.net_pnl / self.capital * 100

    @property
    def ann_pnl_pct(self) -> float:
        return self.pnl_pct / self.days * 365 if self.days else 0.0

    @property
    def calmar(self) -> float:
        return self.pnl_pct / self.max_dd_pct if self.max_dd_pct > 0 else 99.9


# ─── Engine ───────────────────────────────────────────────────────────────────

def run_backtest(db_path: str, params: GridParams) -> GridResult:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ts_ms, open, high, low, close FROM klines ORDER BY ts_ms"
    ).fetchall()
    conn.close()

    if not rows:
        raise ValueError(f"Empty database: {db_path}")

    first_open = rows[0][1]
    lower  = first_open * (1 - params.range_pct / 100)
    upper  = first_open * (1 + params.range_pct / 100)
    n      = params.levels
    step   = (upper - lower) / (n - 1)
    prices = [lower + i * step for i in range(n)]
    capital = n * params.size

    usdt = capital
    btc  = 0.0
    # buy_active[i]  : BUY order standing at prices[i]
    # sell_active[i] : SELL order standing at prices[i] + step
    buy_active:  List[bool]        = [p < first_open for p in prices]
    sell_active: Dict[int, float]  = {}

    cycles    = 0
    gross_pnl = 0.0
    total_fee = 0.0
    peak_val  = capital
    max_dd    = 0.0
    n_in_grid = 0
    stop_reason = "completed"
    final_price = rows[-1][4]

    for _ts, _open, high, low, close in rows:
        n_in_grid += 1

        # ── BUY fills (candle low touches the level) ──────────────────────
        for i, bp in enumerate(prices):
            if buy_active[i] and low <= bp:
                qty   = params.size / bp
                fee   = bp * qty * params.fee_rate
                usdt -= bp * qty + fee
                btc  += qty
                total_fee    += fee
                buy_active[i] = False
                sp = bp + step
                if sp <= upper + 1e-6:
                    sell_active[i] = sp

        # ── SELL fills (candle high reaches the level) ────────────────────
        sold = []
        for i, sp in list(sell_active.items()):
            if high >= sp:
                bp    = prices[i]
                qty   = params.size / bp
                fee   = sp * qty * params.fee_rate
                usdt += sp * qty - fee
                btc   = max(0.0, btc - qty)
                total_fee += fee
                gross_pnl += (sp - bp) * qty
                cycles    += 1
                sold.append(i)
                if bp >= lower - 1e-6:
                    buy_active[i] = True
        for i in sold:
            del sell_active[i]

        # ── Portfolio value & drawdown ────────────────────────────────────
        pv = usdt + btc * close
        if pv > peak_val:
            peak_val = pv
        dd = (peak_val - pv) / peak_val * 100 if peak_val > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

        # ── Stop-loss ─────────────────────────────────────────────────────
        if low < lower or high > upper:
            stop_reason = "exit_low" if low < lower else "exit_high"
            final_price = close
            break

    net_pnl = (usdt + btc * final_price) - capital
    days    = max(1, round(len(rows) / 1440))
    ts0     = rows[0][0]  / 1000
    ts1     = rows[-1][0] / 1000
    period  = (f"{_time.strftime('%Y-%m-%d', _time.gmtime(ts0))}"
               f" → {_time.strftime('%Y-%m-%d', _time.gmtime(ts1))}")

    return GridResult(
        label       = Path(db_path).stem,
        period      = period,
        days        = days,
        start_price = first_open,
        grid_lower  = lower,
        grid_upper  = upper,
        grid_step   = step,
        levels      = n,
        size        = params.size,
        capital     = capital,
        cycles      = cycles,
        gross_pnl   = gross_pnl,
        fees        = total_fee,
        net_pnl     = net_pnl,
        max_dd_pct  = max_dd,
        time_pct    = n_in_grid / len(rows) * 100,
        stop_reason = stop_reason,
        final_btc   = btc,
        final_price = final_price,
        params      = params,
    )


# ─── Display ──────────────────────────────────────────────────────────────────

_STOP = {"completed": "✓ ok", "exit_low": "↓ exit_low", "exit_high": "↑ exit_high"}
_W    = 68


def _fmt(val: float, prefix: str = "") -> str:
    sign = "+" if val >= 0 else ""
    return f"{prefix}{sign}${val:,.2f}" if prefix else f"{sign}{val:.1f}%"


def print_result(r: GridResult) -> None:
    sep = "═" * _W
    print(f"\n╔{sep}╗")
    print(f"║  GRID BACKTEST — {r.label:<{_W - 19}}║")
    print(f"╠{sep}╣")
    l1 = f"  Period     : {r.period}  ({r.days}d)"
    print(f"║{l1:<{_W}}║")
    l2 = f"  Start : ${r.start_price:,.0f}  Grid: [${r.grid_lower:,.0f}, ${r.grid_upper:,.0f}]  step ${r.grid_step:,.0f}"
    print(f"║{l2:<{_W}}║")
    l3 = f"  Levels: {r.levels}  Order: ${r.size:.0f}/level  Capital: ${r.capital:,.0f}"
    print(f"║{l3:<{_W}}║")
    print(f"╠{sep}╣")
    sign = "+" if r.net_pnl >= 0 else ""
    print(f"║  Cycles        : {r.cycles:>6,}{'':<{_W - 26}}║")
    l4 = f"  Net PnL        : {sign}${r.net_pnl:>8,.2f}  ({sign}{r.pnl_pct:.1f}%)  ann {sign}{r.ann_pnl_pct:.0f}%/yr"
    print(f"║{l4:<{_W}}║")
    l5 = f"  Gross PnL      :  ${r.gross_pnl:>8,.2f}  fees: -${r.fees:,.2f}"
    print(f"║{l5:<{_W}}║")
    calmar_s = f"{r.calmar:.2f}" if r.max_dd_pct > 0 else "N/A"
    l6 = f"  Max drawdown   : {r.max_dd_pct:>6.1f}%   Calmar: {calmar_s}   time in grid: {r.time_pct:.1f}%"
    print(f"║{l6:<{_W}}║")
    stop_s = _STOP.get(r.stop_reason, r.stop_reason)
    l7 = f"  Stop reason    : {stop_s}"
    if r.final_btc > 1e-8:
        l7 += f"  ({r.final_btc:.6f} BTC @ ${r.final_price:,.0f})"
    print(f"║{l7:<{_W}}║")
    print(f"╚{sep}╝")


def print_summary_table(results: List[GridResult], params: GridParams) -> None:
    print(f"\n{'─'*100}")
    print(f"  Summary — ±{params.range_pct:.0f}%  levels={params.levels}  "
          f"size=${params.size:.0f}  capital=${results[0].capital:,.0f}")
    print(f"{'─'*100}")
    hdr = (f"  {'Database':<38} {'d':>3} {'cyc':>5} "
           f"{'NetPnL':>9} {'PnL%':>6} {'Ann%':>6} "
           f"{'MaxDD':>6} {'Calmar':>7} {'T%':>5}  Stop")
    print(hdr)
    print(f"{'─'*100}")
    for r in results:
        s = "+" if r.net_pnl >= 0 else ""
        cs = f"{r.calmar:.1f}" if r.max_dd_pct > 0 else " N/A"
        print(f"  {r.label[:38]:<38} {r.days:>3} {r.cycles:>5} "
              f"{s}${r.net_pnl:>7,.0f} {s}{r.pnl_pct:>5.1f}% {s}{r.ann_pnl_pct:>5.0f}% "
              f"{r.max_dd_pct:>5.1f}% {cs:>7} {r.time_pct:>4.0f}%  "
              f"{_STOP.get(r.stop_reason, r.stop_reason)}")
    print(f"{'─'*100}")


def print_sweep_table(
    sweep: List[tuple],   # (GridParams, List[GridResult|None])
    dbs:   List[str],
    sort_by: str = "calmar",
) -> None:
    labels = []
    for d in dbs:
        s = Path(d).stem
        for kw in ("bullrun", "bearmarket", "range"):
            if kw in s:
                labels.append(s[s.index(kw):][:14])
                break
        else:
            labels.append(s[:14])

    # Score each combo
    scored = []
    for params, res_list in sweep:
        valid = [r for r in res_list if r is not None]
        if not valid:
            continue
        avg_cal = sum(r.calmar  for r in valid) / len(valid)
        avg_pnl = sum(r.pnl_pct for r in valid) / len(valid)
        scored.append((params, res_list, avg_cal, avg_pnl))

    key = (lambda x: x[2]) if sort_by == "calmar" else (lambda x: x[3])
    scored.sort(key=key, reverse=True)

    # Build header
    db_hdr = "  ".join(f"{lb[:14]:>14}" for lb in labels)
    sub_hdr = "  ".join(f"{'PnL%':>5} {'DD':>4} {'T%':>3}" for _ in labels)
    sep = "─" * (16 + len(labels) * 17 + 18)
    print(f"\n  Sweep: range_pct × levels  (size=${scored[0][0].size:.0f}, fee={FEE_RATE*100:.2f}%)")
    print(f"  Columns per DB: PnL%  MaxDD%  Time%\n")
    print(f"  {'±Rng':>4} {'Lvl':>4}  {db_hdr}  {'AvgCal':>7} {'AvgPnL%':>8}")
    print(f"  {'':>4} {'':>4}  {sub_hdr}  {'':>7} {'':>8}")
    print(sep)

    for params, res_list, avg_cal, avg_pnl in scored:
        cols = ""
        for r in res_list:
            if r is None:
                cols += f"  {'N/A':>5} {'N/A':>4} {'N/A':>3}"
            else:
                s = "+" if r.pnl_pct >= 0 else ""
                cols += f"  {s}{r.pnl_pct:>4.1f}% {r.max_dd_pct:>3.0f}% {r.time_pct:>3.0f}%"
        s = "+" if avg_pnl >= 0 else ""
        print(f"  {params.range_pct:>3.0f}%  {params.levels:>4}  {cols}  "
              f"{avg_cal:>7.2f}  {s}{avg_pnl:>6.1f}%")
    print(sep)

    best = scored[0][0] if scored else None
    if best:
        print(f"\n  Best (avg Calmar): ±{best.range_pct:.0f}%  {best.levels} levels  ${best.size:.0f}/order")
        print(f"  Reproduce: python scripts/backtest_grid.py --all "
              f"--range {best.range_pct:.0f} --levels {best.levels} --size {best.size:.0f}")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _collect_dbs(paths: List[str], use_all: bool) -> List[str]:
    if use_all:
        found = sorted(DATA_DIR.glob("BTCUSDT_1m*.db"))
        if not found:
            raise SystemExit(f"No BTCUSDT_1m*.db files in {DATA_DIR}")
        return [str(p) for p in found]
    return paths


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Grid trading backtest on OHLCV SQLite databases",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("dbs",     nargs="*", help="SQLite database path(s)")
    ap.add_argument("--all",   action="store_true",
                    help="Use all BTCUSDT_1m*.db in data/")
    ap.add_argument("--range", type=float, default=15.0, dest="range_pct",
                    help="Grid ±%% from start price")
    ap.add_argument("--levels", type=int,   default=30)
    ap.add_argument("--size",   type=float, default=50.0,
                    help="USDT per order (capital = levels × size)")
    ap.add_argument("--fee",    type=float, default=FEE_RATE * 100,
                    help="Fee rate %%")
    ap.add_argument("--sweep",  action="store_true",
                    help="Sweep range_pct × levels")
    ap.add_argument("--sort",   default="calmar", choices=["calmar", "pnl"])
    args = ap.parse_args()

    dbs = _collect_dbs(args.dbs, args.all)
    if not dbs:
        ap.print_help()
        return

    fee = args.fee / 100

    if args.sweep:
        RANGES = [10, 15, 20, 25, 30]
        LEVELS = [20, 30, 40]
        sweep_data = []
        for rp in RANGES:
            for lv in LEVELS:
                p = GridParams(range_pct=rp, levels=lv, size=args.size, fee_rate=fee)
                res_list: List[Optional[GridResult]] = []
                for db in dbs:
                    try:
                        res_list.append(run_backtest(db, p))
                    except Exception as exc:
                        print(f"  WARN: {db}: {exc}")
                        res_list.append(None)
                sweep_data.append((p, res_list))
        print_sweep_table(sweep_data, dbs, sort_by=args.sort)
        return

    params = GridParams(range_pct=args.range_pct, levels=args.levels,
                        size=args.size, fee_rate=fee)
    results = []
    for db in dbs:
        try:
            r = run_backtest(db, params)
            results.append(r)
            print_result(r)
        except Exception as exc:
            print(f"  ERROR: {db}: {exc}")

    if len(results) > 1:
        print_summary_table(results, params)


if __name__ == "__main__":
    main()
