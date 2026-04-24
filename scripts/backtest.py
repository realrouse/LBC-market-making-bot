#!/usr/bin/env python3
"""
Backtest trading strategy parameters against historical snapshot data in live.db.

The snapshots table records price data every 5 seconds per tracked token.
This script replays those snapshots chronologically, applies the signal logic
with configurable parameters, and produces simulated trade statistics.

Usage:
    python3 scripts/backtest.py                          # default parameters
    python3 scripts/backtest.py --threshold 0.95         # custom threshold
    python3 scripts/backtest.py --threshold 0.95 --min-secs 30 --detail
    python3 scripts/backtest.py --sweep                  # grid search
    POLYMARKET_DIR=~/polymarket python3 scripts/backtest.py
"""

import argparse, os, sqlite3, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from typing import List, Optional, Tuple

INSTALL_DIR = os.environ.get("POLYMARKET_DIR", "/opt/polymarket-live")
DB_PATH     = os.path.join(INSTALL_DIR, "live.db")

FEE_RATE    = 0.02    # Polymarket taker fee rate
GAS_FEE_USD = 0.03    # estimated gas cost per order


# ─── Parameter set ────────────────────────────────────────────────────────────

@dataclass
class Params:
    """All tunable parameters for one backtest run."""
    signal_threshold:   float = 0.96
    entry_max:          float = 0.998
    min_secs_remaining: float = 45.0
    min_ask_vol:        float = 10.0
    win_threshold:      float = 0.99
    loss_threshold:     float = 0.01
    obi_reject_thresh:  float = -0.50
    stake:              float = 10.0
    daily_stop_loss:    float = 30.0
    capital_start:      float = 100.0


# ─── Simulated trade record ───────────────────────────────────────────────────

@dataclass
class SimTrade:
    market_id:            str
    direction:            str
    entry_ts_ms:          int
    entry_price:          float
    tokens:               float
    fee:                  float
    entry_bid:            float
    entry_secs_remaining: float
    # filled at resolution
    outcome:     Optional[str]   = None   # "WIN", "LOSS", or "OPEN"
    exit_bid:    Optional[float] = None
    exit_ts_ms:  Optional[int]   = None
    pnl_net:     Optional[float] = None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fee(price: float, tokens: float) -> float:
    return FEE_RATE * min(price, 1.0 - price) * tokens


def _date(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# ─── Core replay engine ───────────────────────────────────────────────────────

def run_backtest(rows: list, params: Params) -> Tuple[List[SimTrade], float]:
    """
    Replay snapshot rows chronologically and simulate trades.

    Each row is: (ts_ms, market_id, direction, secs_remaining,
                  best_bid, best_ask, ask_vol, obi)

    Returns (list_of_sim_trades, final_capital).
    """
    open_trades: dict  = {}   # market_id → SimTrade (one active trade per market)
    signalled:   set   = set()  # market_ids that have already been entered
    daily_pnl:   dict  = {}   # "YYYY-MM-DD" → cumulative net PnL that day
    all_trades:  list  = []
    capital:     float = params.capital_start

    for (ts_ms, market_id, direction,
         secs_remaining, best_bid, best_ask, ask_vol, obi) in rows:

        # ── Resolution check for any open trade on this market ────────────────
        if market_id in open_trades:
            trade = open_trades[market_id]

            # Only the token we entered can resolve the trade.
            if trade.direction == direction:
                outcome = None
                if best_bid >= params.win_threshold:
                    outcome = "WIN"
                elif best_bid <= params.loss_threshold:
                    outcome = "LOSS"
                elif secs_remaining <= 0:
                    outcome = "WIN" if best_bid >= 0.50 else "LOSS"

                if outcome:
                    won = (outcome == "WIN")
                    pg  = (trade.tokens - params.stake) if won else -params.stake
                    pn  = pg - trade.fee - GAS_FEE_USD
                    trade.outcome   = outcome
                    trade.exit_bid  = best_bid
                    trade.exit_ts_ms = ts_ms
                    trade.pnl_net   = pn
                    capital        += pn
                    daily_pnl[_date(ts_ms)] = daily_pnl.get(_date(ts_ms), 0.0) + pn
                    all_trades.append(trade)
                    del open_trades[market_id]

            # Don't fall through to the signal check — this market is taken.
            continue

        # ── Signal check ─────────────────────────────────────────────────────
        if market_id in signalled:                                   continue
        if secs_remaining <= 0:                                      continue
        if best_bid < params.signal_threshold:                       continue
        if best_bid > params.entry_max:                              continue
        if best_ask >= 1.0:                                          continue
        if best_ask > params.entry_max:                              continue
        if ask_vol > 0 and ask_vol < params.min_ask_vol:             continue
        if secs_remaining < params.min_secs_remaining:               continue
        if obi < params.obi_reject_thresh:                           continue
        if capital - len(open_trades) * params.stake < params.stake: continue
        if daily_pnl.get(_date(ts_ms), 0.0) < -params.daily_stop_loss: continue

        # Signal fires — open a simulated trade at current best_ask.
        ep     = best_ask
        tokens = params.stake / ep if ep > 0 else 0
        fee    = _fee(ep, tokens)
        open_trades[market_id] = SimTrade(
            market_id=market_id, direction=direction,
            entry_ts_ms=ts_ms, entry_price=ep, tokens=tokens, fee=fee,
            entry_bid=best_bid, entry_secs_remaining=secs_remaining,
        )
        signalled.add(market_id)

    # Trades still open when the snapshot data ends.
    for trade in open_trades.values():
        trade.outcome = "OPEN"
        all_trades.append(trade)

    return all_trades, capital


# ─── Statistics ───────────────────────────────────────────────────────────────

def summarize(trades: List[SimTrade], params: Params, capital_final: float) -> dict:
    """Compute aggregate statistics for a list of simulated trades."""
    resolved = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    wins     = sum(1 for t in resolved if t.outcome == "WIN")
    losses   = len(resolved) - wins
    open_t   = sum(1 for t in trades if t.outcome == "OPEN")
    pnls     = [t.pnl_net for t in resolved]
    total_pnl = sum(pnls)
    win_rate  = (wins / len(resolved) * 100) if resolved else 0.0

    # Maximum drawdown: largest peak-to-trough drop in cumulative PnL.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in pnls:
        cum += pnl
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)

    return {
        "total":         len(resolved) + open_t,
        "wins":          wins,
        "losses":        losses,
        "open":          open_t,
        "win_rate":      win_rate,
        "total_pnl":     total_pnl,
        "max_drawdown":  max_dd,
        "capital_final": capital_final,
    }


# ─── Output helpers ───────────────────────────────────────────────────────────

def _stat_block(label: str, params: Params, stats: dict, n_snapshots: int) -> str:
    lines = [
        "=" * 62,
        f"  {label}",
        f"  signal={params.signal_threshold}  min_secs={params.min_secs_remaining:.0f}"
        f"  min_ask={params.min_ask_vol:.0f}  obi={params.obi_reject_thresh}",
        f"  Snapshots: {n_snapshots:,}",
        "=" * 62,
        f"  Trades   : {stats['total']}",
        f"  Wins     : {stats['wins']}",
        f"  Losses   : {stats['losses']}",
    ]
    if stats["open"]:
        lines.append(f"  Open     : {stats['open']}  (unresolved at end of data)")
    lines += [
        f"  Win rate : {stats['win_rate']:.1f}%",
        f"  Total PnL: ${stats['total_pnl']:+.2f}",
        f"  Max DD   : ${stats['max_drawdown']:.2f}",
        f"  Capital  : ${stats['capital_final']:.2f}"
        f"  (start: ${params.capital_start:.2f})",
        "=" * 62,
    ]
    return "\n".join(lines)


def print_detail(trades: List[SimTrade]) -> None:
    """Print a table of individual simulated trades."""
    resolved = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    if not resolved:
        print("  (no resolved trades)")
        return
    print(f"\n  {'#':>3}  {'Direction':<5}  {'Entry':>6}  {'Entry bid':>9}"
          f"  {'Secs':>4}  {'Outcome':<5}  {'Exit bid':>8}  {'PnL':>7}")
    print("  " + "-" * 68)
    for i, t in enumerate(resolved, 1):
        print(f"  {i:>3}  {t.direction:<5}  {t.entry_price:>6.4f}  {t.entry_bid:>9.4f}"
              f"  {t.entry_secs_remaining:>4.0f}  {t.outcome:<5}  {t.exit_bid:>8.4f}"
              f"  ${t.pnl_net:>+6.2f}")


def print_actual(conn: sqlite3.Connection, params: Params, n_snapshots: int) -> None:
    """Print actual bot performance from the trades table for comparison."""
    row = conn.execute(
        "SELECT COUNT(*), "
        "  SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), "
        "  SUM(CASE WHEN outcome='LOSS' THEN 1 ELSE 0 END), "
        "  COALESCE(SUM(pnl_net),0) "
        "FROM trades WHERE resolved=1"
    ).fetchone()
    total, wins, losses, pnl = row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0.0
    wr = (wins / total * 100) if total else 0.0
    print("=" * 62)
    print("  ACTUAL BOT RESULTS (for comparison)")
    print("=" * 62)
    print(f"  Trades   : {total}")
    print(f"  Wins     : {wins}")
    print(f"  Losses   : {losses}")
    print(f"  Win rate : {wr:.1f}%")
    print(f"  Total PnL: ${pnl:+.2f}")
    print("=" * 62)


def print_sweep_table(results: list) -> None:
    """Print a comparison table of all parameter combinations."""
    header = (f"{'threshold':>9} | {'min_secs':>8} | {'min_ask':>7} | {'obi':>6} | "
              f"{'trades':>6} | {'wins':>5} | {'WR%':>6} | {'PnL':>8} | {'MaxDD':>7}")
    sep = "-" * len(header)
    print(f"\n{header}\n{sep}")
    for params, stats in sorted(results, key=lambda x: -x[1]["win_rate"]):
        print(
            f"  {params.signal_threshold:>7.2f} | {params.min_secs_remaining:>8.0f} |"
            f" {params.min_ask_vol:>7.0f} | {params.obi_reject_thresh:>6.2f} |"
            f" {stats['total']:>6} | {stats['wins']:>5} |"
            f" {stats['win_rate']:>5.1f}% | ${stats['total_pnl']:>+7.2f} |"
            f" ${stats['max_drawdown']:>6.2f}"
        )


# ─── DB loading ───────────────────────────────────────────────────────────────

def load_rows(conn: sqlite3.Connection) -> list:
    """Load all snapshot rows needed for the backtest, ordered by time."""
    return conn.execute(
        "SELECT ts_ms, market_id, direction, secs_remaining, "
        "       best_bid, best_ask, ask_vol, obi "
        "FROM snapshots "
        "ORDER BY ts_ms ASC"
    ).fetchall()


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest strategy parameters against live.db snapshots.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db",         default=DB_PATH,  metavar="PATH",
                        help="path to live.db")
    parser.add_argument("--threshold",  type=float, default=0.96,
                        help="entry signal threshold (best_bid >= X)")
    parser.add_argument("--min-secs",   type=float, default=45.0,
                        help="minimum seconds remaining at entry")
    parser.add_argument("--min-ask",    type=float, default=10.0,
                        help="minimum ask-side volume in USD")
    parser.add_argument("--obi",        type=float, default=-0.50,
                        help="OBI reject threshold (below = no entry)")
    parser.add_argument("--stake",      type=float, default=10.0,
                        help="USD stake per trade")
    parser.add_argument("--detail",     action="store_true",
                        help="print individual simulated trade table")
    parser.add_argument("--compare",    action="store_true",
                        help="show actual bot results alongside backtest")
    parser.add_argument("--sweep",      action="store_true",
                        help="grid search over multiple parameter combinations")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"ERROR: database not found: {args.db}")
        print("The bot must run at least one session to populate snapshots.")
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    n_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

    if n_snapshots == 0:
        print("No snapshots in database. Run the bot first to collect data.")
        conn.close()
        sys.exit(1)

    rows = load_rows(conn)

    if args.sweep:
        # Grid search over the four most impactful parameters.
        thresholds = [0.94, 0.95, 0.96, 0.97, 0.98]
        min_secs   = [30,   45,   60]
        min_asks   = [5,    10,   20]
        obi_vals   = [-0.75, -0.50, -0.25]
        combos     = len(thresholds) * len(min_secs) * len(min_asks) * len(obi_vals)

        print(f"Grid search: {len(thresholds)}×{len(min_secs)}×{len(min_asks)}"
              f"×{len(obi_vals)} = {combos} combinations")
        print(f"Snapshots  : {n_snapshots:,}")

        results = []
        for thr, secs, ask, obi in product(thresholds, min_secs, min_asks, obi_vals):
            p = Params(signal_threshold=thr, min_secs_remaining=secs,
                       min_ask_vol=ask, obi_reject_thresh=obi, stake=args.stake)
            trades, capital_final = run_backtest(rows, p)
            stats = summarize(trades, p, capital_final)
            results.append((p, stats))

        print_sweep_table(results)

        if args.compare:
            print()
            print_actual(conn, Params(), n_snapshots)

    else:
        p = Params(
            signal_threshold=args.threshold,
            min_secs_remaining=args.min_secs,
            min_ask_vol=args.min_ask,
            obi_reject_thresh=args.obi,
            stake=args.stake,
        )
        trades, capital_final = run_backtest(rows, p)
        stats = summarize(trades, p, capital_final)
        print(_stat_block("BACKTEST", p, stats, n_snapshots))

        if args.detail:
            print_detail(trades)

        if args.compare:
            print()
            print_actual(conn, p, n_snapshots)

    conn.close()


if __name__ == "__main__":
    main()
