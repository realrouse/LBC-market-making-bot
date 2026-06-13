#!/usr/bin/env python3
"""
Replay backtest for orderbook_bot strategy using ob_snapshots data.

Data source: ob_snapshots table from a live_ob_*.db file.
  columns: ts_ms, mode, best_bid, best_ask, spread, obi_raw, obi_ema, tfi

Entry/exit logic mirrors orderbook_bot._handle_indicator() exactly.
TFI NULL rows are treated as tfi_ok=True (no gate) since they predate
the TFI measurement window.

Usage:
    python analysis/backtest_orderbook.py
    python analysis/backtest_orderbook.py --db data/live_ob_2026-05-26.db
    python analysis/backtest_orderbook.py --mode spot --direction both
    python analysis/backtest_orderbook.py --obi-thresh 0.50 --tfi-thresh 0.30
    python analysis/backtest_orderbook.py --sweep
    python analysis/backtest_orderbook.py --sweep --csv sweep_results.csv
"""

import argparse
import csv
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT   = Path(__file__).resolve().parent.parent
DEFAULT_DB  = REPO_ROOT / "data" / "live_ob_2026-05-26.db"
DEFAULT_CFG = REPO_ROOT / "strategies" / "scalping" / "orderbook_btc.json"


# ---------------------------------------------------------------------------
# Parameters
# ---------------------------------------------------------------------------

@dataclass
class Params:
    mode:              str   = "spot"
    direction:         str   = "long"        # long | short | both
    capital:           float = 1000.0
    stake_frac:        float = 0.10
    obi_entry_thresh:  float = 0.65
    obi_confirm_n:     int   = 15
    tp_pct:            float = 0.0030
    sl_pct:            float = 0.0015
    max_hold_minutes:  int   = 5
    tfi_confirm_thresh: float = 0.30
    tfi_gate_mode:     str   = "flat"
    use_limit_orders:  bool  = True
    fee_taker:         float = 0.001
    fee_maker:         float = 0.0002
    slippage:          float = 0.0005

    @classmethod
    def from_strategy(cls, cfg: dict, mode: str) -> "Params":
        p = cls()
        p.mode             = mode
        p.direction        = cfg.get("direction", "long")
        p.capital          = cfg.get(f"capital_{mode}", 1000.0)
        p.stake_frac       = cfg.get("stake_frac", 0.10)
        p.obi_entry_thresh = cfg.get("obi_entry_thresh", 0.65)
        p.obi_confirm_n    = cfg.get("obi_confirm_n", 15)
        p.tp_pct           = cfg.get("tp_pct", 0.0030)
        p.sl_pct           = cfg.get("sl_pct", 0.0015)
        p.max_hold_minutes = cfg.get("max_hold_minutes", 5)
        p.tfi_confirm_thresh = cfg.get("tfi_confirm_thresh", 0.30)
        p.tfi_gate_mode    = cfg.get("tfi_gate_mode", "flat")
        p.use_limit_orders = cfg.get("use_limit_orders", True)
        p.fee_taker        = cfg.get(f"fee_{mode}", 0.001)
        p.fee_maker        = cfg.get(f"maker_fee_{mode}", 0.0002)
        p.slippage         = cfg.get("slippage", 0.0005)
        return p


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

@dataclass
class Position:
    direction:   str
    entry_ts_ms: int
    entry_price: float
    entry_obi:   float
    entry_tfi:   Optional[float]
    stake:       float
    qty:         float
    fee_entry:   float
    tp:          float
    sl:          float
    max_hold_ms: int


# ---------------------------------------------------------------------------
# Single-run backtest engine
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    mode:        str
    params:      Params
    n_snapshots: int
    n_trades:    int
    wins:        int
    losses:      int
    timeouts:    int
    total_pnl:   float
    final_cap:   float
    win_rate:    float
    trades:      list = field(default_factory=list, repr=False)


def _tfi_ok(tfi: Optional[float], p: Params, direction: str) -> bool:
    if p.tfi_confirm_thresh <= 0.0:
        return True
    if tfi is None:
        return True   # pre-TFI data: no gate
    if p.tfi_gate_mode == "flat":
        return abs(tfi) < p.tfi_confirm_thresh
    # directional (legacy)
    if direction == "long":
        return tfi > p.tfi_confirm_thresh
    return tfi < -p.tfi_confirm_thresh



def _close(pos: Position, mid: float, reason: str, ts_ms: int, p: Params) -> Tuple[float, dict]:
    slip = p.slippage
    if p.use_limit_orders:
        fee_rate = p.fee_maker
        exit_px  = mid
    else:
        fee_rate = p.fee_taker
        exit_px  = mid * (1 - slip) if pos.direction == "long" else mid * (1 + slip)

    if pos.direction == "long":
        exit_val = pos.qty * exit_px
        fee_exit = exit_val * fee_rate
        pnl_net  = exit_val - fee_exit - pos.stake - pos.fee_entry
    else:
        exit_val = pos.qty * exit_px
        fee_exit = exit_val * fee_rate
        pnl_net  = pos.stake - exit_val - fee_exit - pos.fee_entry

    trade = {
        "direction":   pos.direction,
        "entry_ts":    pos.entry_ts_ms // 1000,
        "exit_ts":     ts_ms // 1000,
        "hold_s":      (ts_ms - pos.entry_ts_ms) // 1000,
        "entry_price": round(pos.entry_price, 4),
        "exit_price":  round(exit_px, 4),
        "entry_obi":   round(pos.entry_obi, 4),
        "entry_tfi":   round(pos.entry_tfi, 4) if pos.entry_tfi is not None else None,
        "tp":          round(pos.tp, 4),
        "sl":          round(pos.sl, 4),
        "reason":      reason,
        "pnl_net":     round(pnl_net, 4),
    }
    return pnl_net, trade


def run_backtest_fixed(db_path: Path, p: Params) -> RunResult:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT ts_ms, best_bid, best_ask, obi_ema, tfi "
        "FROM ob_snapshots WHERE mode=? ORDER BY ts_ms",
        (p.mode,)
    ).fetchall()
    conn.close()

    capital       = p.capital
    position      = None
    pending_dir   = None
    pending_count = 0
    stake_frac    = p.stake_frac

    trades   = []
    wins = losses = timeouts = 0

    for ts_ms, best_bid, best_ask, obi_ema, tfi in rows:
        if obi_ema is None:
            continue
        mid = (best_bid + best_ask) / 2.0 if (best_bid and best_ask) else None
        if mid is None:
            continue

        # Exit check
        if position is not None:
            reason = None
            pos = position
            if pos.direction == "long":
                if   mid >= pos.tp:                              reason = "tp"
                elif mid <= pos.sl:                              reason = "sl"
                elif ts_ms - pos.entry_ts_ms >= pos.max_hold_ms: reason = "timeout"
            else:
                if   mid <= pos.tp:                              reason = "tp"
                elif mid >= pos.sl:                              reason = "sl"
                elif ts_ms - pos.entry_ts_ms >= pos.max_hold_ms: reason = "timeout"
            if reason:
                pnl, trade = _close(position, mid, reason, ts_ms, p)
                capital += pnl
                trades.append(trade)
                if reason == "tp":    wins     += 1
                elif reason == "sl":  losses   += 1
                else:                 timeouts += 1
                position      = None
                pending_dir   = None
                pending_count = 0
            continue

        # Entry signal
        thresh = p.obi_entry_thresh

        want_long  = (p.direction in ("long",  "both")) and obi_ema < -thresh
        want_short = (p.direction in ("short", "both")) and obi_ema >  thresh

        if want_long:
            if pending_dir == "long":
                pending_count += 1
            else:
                pending_dir, pending_count = "long", 1
        elif want_short:
            if pending_dir == "short":
                pending_count += 1
            else:
                pending_dir, pending_count = "short", 1
        else:
            pending_dir   = None
            pending_count = 0

        if pending_count >= p.obi_confirm_n and pending_dir:
            d = pending_dir
            if _tfi_ok(tfi, p, d):
                stake     = capital * stake_frac
                slip      = p.slippage
                if p.use_limit_orders:
                    entry_px = mid
                    fee_rate = p.fee_maker
                else:
                    entry_px = mid * (1 + slip) if d == "long" else mid * (1 - slip)
                    fee_rate = p.fee_taker
                qty       = stake / entry_px
                fee_entry = stake * fee_rate
                if d == "long":
                    tp = entry_px * (1 + p.tp_pct)
                    sl = entry_px * (1 - p.sl_pct)
                else:
                    tp = entry_px * (1 - p.tp_pct)
                    sl = entry_px * (1 + p.sl_pct)
                position = Position(
                    direction=d, entry_ts_ms=ts_ms, entry_price=entry_px,
                    entry_obi=obi_ema, entry_tfi=tfi, stake=stake, qty=qty,
                    fee_entry=fee_entry, tp=tp, sl=sl,
                    max_hold_ms=p.max_hold_minutes * 60_000,
                )
                pending_dir   = None
                pending_count = 0

    n_trades  = wins + losses + timeouts
    total_pnl = sum(t["pnl_net"] for t in trades)
    win_rate  = wins / n_trades if n_trades else 0.0

    return RunResult(
        mode=p.mode, params=p,
        n_snapshots=len(rows), n_trades=n_trades,
        wins=wins, losses=losses, timeouts=timeouts,
        total_pnl=round(total_pnl, 4),
        final_cap=round(capital, 4),
        win_rate=win_rate,
        trades=trades,
    )


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

SWEEP_GRID = {
    "obi_entry_thresh":  [0.40, 0.50, 0.55, 0.60, 0.65, 0.70],
    "obi_confirm_n":     [5, 10, 15, 20],
    "tfi_confirm_thresh": [0.0, 0.20, 0.30, 0.45],
    "direction":         ["long", "short", "both"],
}


def sweep(db_path: Path, base_cfg: dict, modes: List[str],
          csv_path: Optional[Path]) -> None:
    keys   = list(SWEEP_GRID.keys())
    combos = list(product(*[SWEEP_GRID[k] for k in keys]))
    total  = len(combos) * len(modes)
    print(f"Sweep: {len(combos)} param combos × {len(modes)} mode(s) = {total} runs")

    rows = []
    for mode in modes:
        for vals in combos:
            cfg = {**base_cfg}
            for k, v in zip(keys, vals):
                cfg[k] = v
            p   = Params.from_strategy(cfg, mode)
            res = run_backtest_fixed(db_path, p)
            rows.append({
                "mode":              mode,
                "obi_thresh":        p.obi_entry_thresh,
                "obi_confirm_n":     p.obi_confirm_n,
                "tfi_thresh":        p.tfi_confirm_thresh,
                "direction":         p.direction,
                "n_trades":          res.n_trades,
                "wins":              res.wins,
                "losses":            res.losses,
                "timeouts":          res.timeouts,
                "win_rate":          round(res.win_rate * 100, 1),
                "total_pnl":         res.total_pnl,
                "final_cap":         res.final_cap,
                "pnl_per_trade":     round(res.total_pnl / res.n_trades, 4) if res.n_trades else 0,
            })

    rows.sort(key=lambda r: r["total_pnl"], reverse=True)

    # Print top 20
    print(f"\n{'Mode':<6} {'OBI':>5} {'N':>3} {'TFI':>5} {'Dir':<6} "
          f"{'Trades':>6} {'W%':>6} {'PnL':>9} {'$/trade':>8}")
    print("-" * 65)
    for r in rows[:20]:
        print(f"{r['mode']:<6} {r['obi_thresh']:>5.2f} {r['obi_confirm_n']:>3} "
              f"{r['tfi_thresh']:>5.2f} {r['direction']:<6} "
              f"{r['n_trades']:>6} {r['win_rate']:>5.1f}% "
              f"{r['total_pnl']:>+9.2f} {r['pnl_per_trade']:>+8.4f}")

    if csv_path:
        with open(csv_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\nFull results → {csv_path}")


# ---------------------------------------------------------------------------
# Single run display
# ---------------------------------------------------------------------------

def print_result(res: RunResult, verbose: bool) -> None:
    p = res.params
    print(f"\n{'='*60}")
    print(f"OBI Scalping Backtest — {res.mode.upper()}  ({res.n_snapshots:,} snapshots)")
    print(f"  direction      : {p.direction}")
    print(f"  obi_thresh     : {p.obi_entry_thresh}  confirm_n={p.obi_confirm_n}")
    print(f"  tfi_thresh     : {p.tfi_confirm_thresh}  gate_mode={p.tfi_gate_mode}")
    print(f"  tp/sl          : {p.tp_pct*100:.2f}% / {p.sl_pct*100:.2f}%")
    print(f"  max_hold       : {p.max_hold_minutes}min")
    print(f"  orders         : {'limit (maker)' if p.use_limit_orders else 'market (taker)'}")
    print(f"  capital        : ${p.capital:.0f}  stake_frac={p.stake_frac}")
    print(f"{'─'*60}")
    n = res.n_trades
    print(f"  Trades         : {n}  (W={res.wins} L={res.losses} T={res.timeouts})")
    wr = f"{res.win_rate*100:.1f}%" if n else "—"
    print(f"  Win rate       : {wr}")
    print(f"  Total PnL      : ${res.total_pnl:+.4f}")
    print(f"  Final capital  : ${res.final_cap:.2f}")
    if n:
        print(f"  PnL / trade    : ${res.total_pnl/n:+.4f}")
    print(f"{'='*60}")

    if verbose and res.trades:
        print("\nTrades:")
        print(f"  {'Dir':<6} {'Entry':>10} {'Exit':>10} {'Hold':>6} "
              f"{'OBI':>6} {'TFI':>6} {'Reason':<9} {'PnL':>9}")
        print(f"  {'─'*70}")
        for t in res.trades:
            tfi_s = f"{t['entry_tfi']:+.3f}" if t["entry_tfi"] is not None else "  None"
            print(f"  {t['direction']:<6} {t['entry_price']:>10.2f} {t['exit_price']:>10.2f} "
                  f"{t['hold_s']:>5}s {t['entry_obi']:>+6.3f} {tfi_s:>6} "
                  f"{t['reason']:<9} {t['pnl_net']:>+9.4f}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db",          default=str(DEFAULT_DB), help="ob_snapshots SQLite db")
    parser.add_argument("--strategy",    default=str(DEFAULT_CFG), help="strategy JSON")
    parser.add_argument("--mode",        choices=["spot", "perp", "both"], default="both")
    parser.add_argument("--direction",   choices=["long", "short", "both"], default=None)
    parser.add_argument("--obi-thresh",  type=float, default=None, dest="obi_entry_thresh")
    parser.add_argument("--confirm-n",   type=int,   default=None, dest="obi_confirm_n")
    parser.add_argument("--tfi-thresh",  type=float, default=None, dest="tfi_confirm_thresh")
    parser.add_argument("--tfi-mode",    choices=["flat", "directional"], default=None)
    parser.add_argument("--tp-pct",      type=float, default=None)
    parser.add_argument("--sl-pct",      type=float, default=None)
    parser.add_argument("--hold",        type=int,   default=None, dest="max_hold_minutes")
    parser.add_argument("--sweep",       action="store_true", help="grid sweep over key params")
    parser.add_argument("--csv",         default=None, help="save sweep results to CSV")
    parser.add_argument("--verbose", "-v", action="store_true", help="print trade list")
    args = parser.parse_args()

    db_path  = Path(args.db)
    cfg_path = Path(args.strategy)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    cfg = {}
    if cfg_path.exists():
        cfg = json.loads(cfg_path.read_text())

    # Apply CLI overrides
    overrides = {
        "direction":           args.direction,
        "obi_entry_thresh":    args.obi_entry_thresh,
        "obi_confirm_n":       args.obi_confirm_n,
        "tfi_confirm_thresh":  args.tfi_confirm_thresh,
        "tfi_gate_mode":       args.tfi_mode,
        "tp_pct":              args.tp_pct,
        "sl_pct":              args.sl_pct,
        "max_hold_minutes":    args.max_hold_minutes,
    }
    for k, v in overrides.items():
        if v is not None:
            cfg[k] = v

    modes = ["spot", "perp"] if args.mode == "both" else [args.mode]

    print(f"DB      : {db_path}")
    print(f"Strategy: {cfg_path}")

    # Show TFI data coverage
    conn = sqlite3.connect(str(db_path))
    for m in modes:
        total, with_tfi = conn.execute(
            "SELECT count(*), sum(tfi IS NOT NULL) FROM ob_snapshots WHERE mode=?", (m,)
        ).fetchone()
        pct = with_tfi / total * 100 if total else 0
        print(f"  {m}: {total:,} snapshots, TFI available: {with_tfi:,} ({pct:.0f}%)")
    conn.close()

    if args.sweep:
        sweep(db_path, cfg, modes, Path(args.csv) if args.csv else None)
        return

    for mode in modes:
        p   = Params.from_strategy(cfg, mode)
        res = run_backtest_fixed(db_path, p)
        print_result(res, args.verbose)


if __name__ == "__main__":
    main()
