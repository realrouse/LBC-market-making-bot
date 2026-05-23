#!/usr/bin/env python3
"""
Backtest static and trailing grid trading strategies against OHLCV SQLite databases.

Static grid  : stop when price exits [grid_lower, grid_upper].
Trailing grid: re-center grid on current price when the boundary is breached,
               instead of stopping.  Two bias modes:
   --trail bear : re-center only when price falls below grid_lower
                  (follow price DOWN — suited for bear markets)
   --trail bull : re-center only when price rises above grid_upper
                  (follow price UP — suited for bull runs)
   --trail both : re-center in either direction (market-neutral trailing)

Fill model: price-touch on candle [low, high] range.
Capital    : n_levels × order_size  (worst-case, all levels filled).
At stop    : remaining BTC liquidated at candle close price.

Usage:
    python analysis/backtest_grid.py --all
    python analysis/backtest_grid.py --all --sweep
    python analysis/backtest_grid.py --all --trail bear          # bear-adapted
    python analysis/backtest_grid.py --all --trail bull          # bull-adapted
    python analysis/backtest_grid.py --all --trail bear --compare   # static vs trailing
    python analysis/backtest_grid.py --all --range 15 --levels 30 --trail bear
"""

import argparse
import sqlite3
import time as _time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FEE_RATE = 0.001   # 0.1 % — Binance spot taker/maker
DATA_DIR  = Path(__file__).resolve().parent.parent / "data"


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class GridParams:
    range_pct:     float = 15.0   # ±% from start price (or re-center price)
    levels:        int   = 30
    size:          float = 50.0   # USDT per order
    fee_rate:      float = FEE_RATE
    trail_mode:    str   = "off"  # "off" | "bear" | "bull" | "both"
    max_recenters: int   = 10


@dataclass
class GridResult:
    label:          str
    period:         str
    days:           int
    start_price:    float
    grid_lower:     float
    grid_upper:     float
    grid_step:      float
    levels:         int
    size:           float
    capital:        float
    cycles:         int
    gross_pnl:      float          # realized gross from completed BUY→SELL cycles
    fees:           float
    net_pnl:        float          # (final USDT + final BTC × final_price) − capital
    realized_pnl:   float          # gross_pnl − fees
    unrealized_pnl: float          # btc × final_price − btc_cost_basis
    max_dd_pct:     float
    time_pct:       float          # % of candles processed before stop
    stop_reason:    str            # "completed" | "exit_low" | "exit_high" | "recenters_exhausted"
    final_btc:      float
    final_price:    float
    params:         GridParams
    recenters:      int            = 0
    recenter_prices: List[float]   = field(default_factory=list)

    @property
    def pnl_pct(self) -> float:
        return self.net_pnl / self.capital * 100

    @property
    def ann_pnl_pct(self) -> float:
        return self.pnl_pct / self.days * 365 if self.days else 0.0

    @property
    def calmar(self) -> float:
        return self.pnl_pct / self.max_dd_pct if self.max_dd_pct > 0 else 99.9


# ─── Shared candle-processing helpers ─────────────────────────────────────────

def _compute_grid(center: float, params: GridParams) -> Tuple[float, float, float, List[float]]:
    lower  = center * (1 - params.range_pct / 100)
    upper  = center * (1 + params.range_pct / 100)
    step   = (upper - lower) / (params.levels - 1)
    prices = [lower + i * step for i in range(params.levels)]
    return lower, upper, step, prices


def _init_buys(prices: List[float], center: float, usdt: float,
               size: float, fee_rate: float) -> List[bool]:
    """Activate buy orders from closest to center downward, within available USDT."""
    buy_active = [False] * len(prices)
    budget = usdt
    for i in sorted(range(len(prices)), key=lambda j: prices[j], reverse=True):
        if prices[i] < center:
            cost = size * (1 + fee_rate)
            if budget >= cost:
                buy_active[i] = True
                budget -= cost
    return buy_active


# ─── Core engine ──────────────────────────────────────────────────────────────

def _run_engine(rows: list, params: GridParams) -> GridResult:
    """
    Shared simulation engine used by both static and trailing modes.
    """
    first_open = rows[0][1]
    lower, upper, step, prices = _compute_grid(first_open, params)
    capital = params.levels * params.size

    usdt = capital
    btc  = 0.0
    btc_cost = 0.0          # total USDT spent buying BTC (for unrealized P/L)
    buy_active:  List[bool]       = _init_buys(prices, first_open, usdt,
                                                params.size, params.fee_rate)
    sell_active: Dict[int, float] = {}

    cycles    = 0
    gross_pnl = 0.0
    total_fee = 0.0
    peak_val  = capital
    max_dd    = 0.0
    n_in_grid = 0
    stop_reason    = "completed"
    final_price    = rows[-1][4]
    recenters      = 0
    recenter_prices: List[float] = []

    trail_mode    = params.trail_mode
    max_recenters = params.max_recenters

    for _ts, _open, high, low, close in rows:
        n_in_grid += 1

        # ── BUY fills ─────────────────────────────────────────────────────
        for i, bp in enumerate(prices):
            if buy_active[i] and low <= bp:
                qty   = params.size / bp
                fee   = bp * qty * params.fee_rate
                usdt -= bp * qty + fee
                btc  += qty
                btc_cost  += bp * qty + fee
                total_fee += fee
                buy_active[i] = False
                sp = bp + step
                if sp <= upper + 1e-6:
                    sell_active[i] = sp

        # ── SELL fills ────────────────────────────────────────────────────
        sold = []
        for i, sp in list(sell_active.items()):
            if high >= sp:
                bp    = prices[i]
                qty   = params.size / bp
                fee   = sp * qty * params.fee_rate
                usdt += sp * qty - fee
                btc   = max(0.0, btc - qty)
                btc_cost  = max(0.0, btc_cost - params.size)
                total_fee += fee
                gross_pnl += (sp - bp) * qty
                cycles    += 1
                sold.append(i)
                if bp >= lower - 1e-6:
                    buy_active[i] = True
        for i in sold:
            del sell_active[i]

        # ── Portfolio & drawdown ───────────────────────────────────────────
        pv = usdt + btc * close
        peak_val = max(peak_val, pv)
        dd = (peak_val - pv) / peak_val * 100 if peak_val > 0 else 0.0
        max_dd = max(max_dd, dd)

        # ── Boundary check ────────────────────────────────────────────────
        hit_low  = low  < lower
        hit_high = high > upper

        should_trail_down = hit_low  and trail_mode in ("bear", "both")
        should_trail_up   = hit_high and trail_mode in ("bull", "both")

        if should_trail_down or should_trail_up:
            if recenters < max_recenters:
                # Re-center grid on current close price
                lower, upper, step, prices = _compute_grid(close, params)
                buy_active  = _init_buys(prices, close, usdt,
                                         params.size, params.fee_rate)
                sell_active = {}
                recenters += 1
                recenter_prices.append(round(close, 0))
            else:
                stop_reason = "recenters_exhausted"
                final_price = close
                break

        elif hit_low or hit_high:
            stop_reason = "exit_low" if hit_low else "exit_high"
            final_price = close
            break

    unrealized = btc * final_price - btc_cost
    net_pnl    = (usdt + btc * final_price) - capital

    days   = max(1, round(len(rows) / 1440))
    ts0    = rows[0][0]  / 1000
    ts1    = rows[-1][0] / 1000
    period = (f"{_time.strftime('%Y-%m-%d', _time.gmtime(ts0))}"
              f" → {_time.strftime('%Y-%m-%d', _time.gmtime(ts1))}")

    return GridResult(
        label          = "",     # filled by caller
        period         = period,
        days           = days,
        start_price    = first_open,
        grid_lower     = rows[0][1] * (1 - params.range_pct / 100),  # initial lower
        grid_upper     = rows[0][1] * (1 + params.range_pct / 100),  # initial upper
        grid_step      = (rows[0][1] * params.range_pct / 100 * 2) / (params.levels - 1),
        levels         = params.levels,
        size           = params.size,
        capital        = capital,
        cycles         = cycles,
        gross_pnl      = gross_pnl,
        fees           = total_fee,
        net_pnl        = net_pnl,
        realized_pnl   = gross_pnl - total_fee,
        unrealized_pnl = unrealized,
        max_dd_pct     = max_dd,
        time_pct       = n_in_grid / len(rows) * 100,
        stop_reason    = stop_reason,
        final_btc      = btc,
        final_price    = final_price,
        params         = params,
        recenters      = recenters,
        recenter_prices = recenter_prices,
    )


def run_backtest(db_path: str, params: GridParams) -> GridResult:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ts_ms, open, high, low, close FROM klines ORDER BY ts_ms"
    ).fetchall()
    conn.close()
    if not rows:
        raise ValueError(f"Empty: {db_path}")
    r = _run_engine(rows, params)
    r.label = Path(db_path).stem
    return r


# ─── Display ──────────────────────────────────────────────────────────────────

_STOP = {
    "completed":           "✓ ok",
    "exit_low":            "↓ exit_low",
    "exit_high":           "↑ exit_high",
    "recenters_exhausted": "⊘ recenters_exhausted",
}
_TRAIL_LABEL = {"off": "STATIC", "bear": "TRAILING ↓ bear", "bull": "TRAILING ↑ bull", "both": "TRAILING ↕ both"}
_W = 68


def print_result(r: GridResult) -> None:
    sep  = "═" * _W
    mode = _TRAIL_LABEL.get(r.params.trail_mode, r.params.trail_mode)
    print(f"\n╔{sep}╗")
    title = f"  {mode} — {r.label}"
    print(f"║{title:<{_W}}║")
    print(f"╠{sep}╣")
    print(f"║  {'Period     : ' + r.period + '  (' + str(r.days) + 'd)':<{_W}}║")
    l2 = (f"  Start : ${r.start_price:,.0f}  "
          f"Grid₀: [${r.grid_lower:,.0f}, ${r.grid_upper:,.0f}]  step ${r.grid_step:,.0f}")
    print(f"║{l2:<{_W}}║")
    l3 = f"  Levels: {r.levels}  Order: ${r.size:.0f}/level  Capital: ${r.capital:,.0f}"
    print(f"║{l3:<{_W}}║")
    if r.recenters:
        prices_s = ", ".join(f"${p:,.0f}" for p in r.recenter_prices)
        l_rc = f"  Re-centers: {r.recenters}  @  [{prices_s}]"
        print(f"║{l_rc:<{_W}}║")
    print(f"╠{sep}╣")
    sign = "+" if r.net_pnl >= 0 else ""
    print(f"║  Cycles     : {r.cycles:>6,}{'':<{_W - 22}}║")
    l4 = f"  Net PnL     : {sign}${r.net_pnl:>8,.2f}  ({sign}{r.pnl_pct:.1f}%)  ann {sign}{r.ann_pnl_pct:.0f}%/yr"
    print(f"║{l4:<{_W}}║")
    sr = "+" if r.realized_pnl >= 0 else ""
    su = "+" if r.unrealized_pnl >= 0 else ""
    l5 = (f"  Realized    :  {sr}${r.realized_pnl:>7,.2f}  "
          f"Unrealized: {su}${r.unrealized_pnl:>7,.2f}  "
          f"Fees: -${r.fees:,.2f}")
    print(f"║{l5:<{_W}}║")
    calmar_s = f"{r.calmar:.2f}" if r.max_dd_pct > 0 else "N/A"
    l6 = f"  MaxDD: {r.max_dd_pct:>5.1f}%  Calmar: {calmar_s}  Time in grid: {r.time_pct:.1f}%"
    print(f"║{l6:<{_W}}║")
    stop_s = _STOP.get(r.stop_reason, r.stop_reason)
    l7 = f"  Stop: {stop_s}"
    if r.final_btc > 1e-8:
        l7 += f"  ({r.final_btc:.6f} BTC @ ${r.final_price:,.0f})"
    print(f"║{l7:<{_W}}║")
    print(f"╚{sep}╝")


def print_compare(static: GridResult, trailing: GridResult) -> None:
    """Side-by-side comparison of static vs trailing for the same DB."""
    W   = 34
    sep = "─" * (W * 2 + 7)
    print(f"\n  {static.label}")
    print(sep)
    print(f"  {'Metric':<20}  {'STATIC':>{W}}  {'TRAILING':>{W}}")
    print(sep)
    rows_cmp = [
        ("Cycles",       f"{static.cycles:,}",              f"{trailing.cycles:,}"),
        ("Net PnL",      f"{'+' if static.net_pnl>=0 else ''}${static.net_pnl:,.2f}  ({static.pnl_pct:+.1f}%)",
                         f"{'+' if trailing.net_pnl>=0 else ''}${trailing.net_pnl:,.2f}  ({trailing.pnl_pct:+.1f}%)"),
        ("Realized",     f"{'+' if static.realized_pnl>=0 else ''}${static.realized_pnl:,.2f}",
                         f"{'+' if trailing.realized_pnl>=0 else ''}${trailing.realized_pnl:,.2f}"),
        ("Unrealized",   f"{'+' if static.unrealized_pnl>=0 else ''}${static.unrealized_pnl:,.2f}",
                         f"{'+' if trailing.unrealized_pnl>=0 else ''}${trailing.unrealized_pnl:,.2f}"),
        ("Annualized",   f"{static.ann_pnl_pct:+.0f}%/yr",
                         f"{trailing.ann_pnl_pct:+.0f}%/yr"),
        ("Max Drawdown", f"{static.max_dd_pct:.1f}%",      f"{trailing.max_dd_pct:.1f}%"),
        ("Calmar",       f"{static.calmar:.2f}" if static.max_dd_pct else "N/A",
                         f"{trailing.calmar:.2f}" if trailing.max_dd_pct else "N/A"),
        ("Time in grid", f"{static.time_pct:.1f}%",        f"{trailing.time_pct:.1f}%"),
        ("Re-centers",   "—",                               str(trailing.recenters)),
        ("Stop",         _STOP.get(static.stop_reason, static.stop_reason)[:W],
                         _STOP.get(trailing.stop_reason, trailing.stop_reason)[:W]),
    ]
    for label, sv, tv in rows_cmp:
        print(f"  {label:<20}  {sv:>{W}}  {tv:>{W}}")
    print(sep)


def print_summary_table(results: List[GridResult], params: GridParams) -> None:
    mode = _TRAIL_LABEL.get(params.trail_mode, params.trail_mode)
    print(f"\n{'─'*110}")
    print(f"  {mode} — ±{params.range_pct:.0f}%  levels={params.levels}  "
          f"size=${params.size:.0f}  max_recenters={params.max_recenters}")
    print(f"{'─'*110}")
    hdr = (f"  {'Database':<38} {'d':>3} {'cyc':>5} {'RC':>3} "
           f"{'NetPnL':>9} {'PnL%':>6} {'Ann%':>6} {'Real':>8} {'Unreal':>8} "
           f"{'MaxDD':>6} {'T%':>5}  Stop")
    print(hdr)
    print(f"{'─'*110}")
    for r in results:
        s  = "+" if r.net_pnl >= 0 else ""
        sr = "+" if r.realized_pnl >= 0 else ""
        su = "+" if r.unrealized_pnl >= 0 else ""
        print(f"  {r.label[:38]:<38} {r.days:>3} {r.cycles:>5} {r.recenters:>3} "
              f"{s}${r.net_pnl:>7,.0f} {s}{r.pnl_pct:>5.1f}% {s}{r.ann_pnl_pct:>5.0f}% "
              f"{sr}${r.realized_pnl:>6,.0f} {su}${r.unrealized_pnl:>6,.0f} "
              f"{r.max_dd_pct:>5.1f}% {r.time_pct:>4.0f}%  "
              f"{_STOP.get(r.stop_reason, r.stop_reason)}")
    print(f"{'─'*110}")


def print_sweep_table(
    sweep:   List[tuple],
    dbs:     List[str],
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

    sub_hdr = "  ".join(f"{'PnL%':>5} {'DD':>4} {'T%':>3}" for _ in labels)
    sep = "─" * (18 + len(labels) * 17 + 18)
    mode = _TRAIL_LABEL.get(scored[0][0].trail_mode if scored else "off", "")
    print(f"\n  Sweep ({mode}) — size=${scored[0][0].size:.0f}, fee={FEE_RATE*100:.2f}%")
    print("  Columns per DB: PnL%  MaxDD%  Time%\n")
    db_hdr = "  ".join(f"{lb[:14]:>14}" for lb in labels)
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
    if scored:
        best = scored[0][0]
        print(f"\n  Best (avg Calmar): ±{best.range_pct:.0f}%  {best.levels} levels  "
              f"${best.size:.0f}/order  trail={best.trail_mode}")
        print(f"  Reproduce: python analysis/backtest_grid.py --all "
              f"--range {best.range_pct:.0f} --levels {best.levels} "
              f"--size {best.size:.0f} --trail {best.trail_mode}")


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
        description="Static and trailing grid backtest on OHLCV SQLite databases",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("dbs",      nargs="*", help="SQLite database path(s)")
    ap.add_argument("--all",    action="store_true",
                    help="Use all BTCUSDT_1m*.db in data/")
    ap.add_argument("--range",  type=float, default=15.0, dest="range_pct",
                    help="Grid ±%% from start/re-center price")
    ap.add_argument("--levels", type=int,   default=30)
    ap.add_argument("--size",   type=float, default=50.0,
                    help="USDT per order  (capital = levels × size)")
    ap.add_argument("--fee",    type=float, default=FEE_RATE * 100,
                    help="Fee rate %%")
    ap.add_argument("--trail",  default="off",
                    choices=["off", "bear", "bull", "both"],
                    help="Trailing mode: bear (follow down), bull (follow up), both")
    ap.add_argument("--max-recenters", type=int, default=10, dest="max_recenters",
                    help="Max re-centers before treating as stop-loss")
    ap.add_argument("--compare", action="store_true",
                    help="Run static AND trailing side-by-side for each DB")
    ap.add_argument("--sweep",  action="store_true",
                    help="Sweep range_pct × levels (5×3=15 combos)")
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
                p = GridParams(range_pct=rp, levels=lv, size=args.size,
                               fee_rate=fee, trail_mode=args.trail,
                               max_recenters=args.max_recenters)
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

    params_trailing = GridParams(
        range_pct=args.range_pct, levels=args.levels, size=args.size,
        fee_rate=fee, trail_mode=args.trail, max_recenters=args.max_recenters,
    )
    params_static = GridParams(
        range_pct=args.range_pct, levels=args.levels, size=args.size,
        fee_rate=fee, trail_mode="off",
    )

    trailing_results = []
    for db in dbs:
        try:
            if args.compare:
                r_static   = run_backtest(db, params_static)
                r_trailing = run_backtest(db, params_trailing)
                print_result(r_static)
                print_result(r_trailing)
                print_compare(r_static, r_trailing)
                trailing_results.append(r_trailing)
            else:
                r = run_backtest(db, params_trailing)
                trailing_results.append(r)
                print_result(r)
        except Exception as exc:
            print(f"  ERROR: {db}: {exc}")

    if len(trailing_results) > 1:
        print_summary_table(trailing_results, params_trailing)


if __name__ == "__main__":
    main()
