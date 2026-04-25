#!/usr/bin/env python3
"""
Backtest trading strategy parameters against historical snapshot data.

The snapshots table records price data every 5 seconds per tracked token.
This script replays those snapshots chronologically, applies the signal logic
with configurable parameters, and produces simulated trade statistics.

Database resolution when no --db / --all flag is given (first match wins):
    1. $TRADINEBOTTE_DIR/live.db  (or ~/tradinebotte/live.db by default)
    2. data/backtest_sample_btc5m_range_2026.db  (bundled sample dataset)

Usage:
    python3 scripts/backtest.py                              # default DB
    python3 scripts/backtest.py --db ~/tradinebotte/live.db  # explicit file
    python3 scripts/backtest.py --db data/session_a.db data/session_b.db
    python3 scripts/backtest.py --db data/*.db               # shell glob
    python3 scripts/backtest.py --all                        # all data/*.db + live.db
    python3 scripts/backtest.py --all --threshold 0.95 --detail
    python3 scripts/backtest.py --sweep                      # grid search
"""

import argparse, os, sqlite3, sys
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from typing import List, Optional, Tuple

INSTALL_DIR = os.path.expanduser(os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte"))
_live_db    = os.path.join(INSTALL_DIR, "live.db")
_sample_db  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "backtest_sample_btc5m_range_2026.db")

_LIVE_DB_MIN_SNAPSHOTS = 100  # below this, fall back to the sample dataset

def _live_db_usable(path: str) -> bool:
    """True if path exists and has enough snapshots to be worth backtesting."""
    if not os.path.exists(path):
        return False
    try:
        c = sqlite3.connect(path)
        count = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        c.close()
        return count >= _LIVE_DB_MIN_SNAPSHOTS
    except Exception:
        return False

DB_PATH = _live_db if _live_db_usable(_live_db) else _sample_db

def _collect_dbs(db_args: Optional[List[str]], scan_all: bool) -> List[str]:
    """
    Resolve the ordered list of database files to replay.

    Priority:
      --all              → all .db files in data/, then live.db if usable
      --db path [path…]  → exactly those paths (shell expands globs)
      (neither)          → default DB_PATH (live.db → sample fallback)
    """
    if scan_all:
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        )
        found = sorted(
            os.path.join(data_dir, f)
            for f in os.listdir(data_dir)
            if f.endswith(".db")
        ) if os.path.isdir(data_dir) else []
        # Prepend live.db when it has enough data so it appears first.
        if _live_db_usable(_live_db) and _live_db not in found:
            found = [_live_db] + found
        return found
    if db_args:
        return list(db_args)
    return [DB_PATH]

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
        if 0 < ask_vol < params.min_ask_vol:                          continue
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


def print_aggregate(results: List[dict]) -> None:
    """
    Print a cross-file summary after replaying multiple databases.

    Each file's capital starts at params.capital_start (independent sessions).
    Win rate is computed over all resolved trades across all files combined.
    Max drawdown shown is the worst single-session drawdown (not global).
    """
    n_files       = len(results)
    total_snaps   = sum(r["n_snapshots"] for r in results)
    total_wins    = sum(r["stats"]["wins"] for r in results)
    total_losses  = sum(r["stats"]["losses"] for r in results)
    total_open    = sum(r["stats"]["open"] for r in results)
    total_pnl     = sum(r["stats"]["total_pnl"] for r in results)
    resolved      = total_wins + total_losses
    win_rate      = (total_wins / resolved * 100) if resolved else 0.0
    worst_dd      = max((r["stats"]["max_drawdown"] for r in results), default=0.0)

    print("=" * 62)
    print(f"  AGGREGATE — {n_files} file(s)  {total_snaps:,} snapshots")
    print("  (capital reset per file — independent sessions)")
    print("=" * 62)
    print(f"  Trades   : {resolved + total_open}")
    print(f"  Wins     : {total_wins}")
    print(f"  Losses   : {total_losses}")
    if total_open:
        print(f"  Open     : {total_open}  (unresolved at end of data)")
    print(f"  Win rate : {win_rate:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    print(f"  Worst DD : ${worst_dd:.2f}  (worst single session)")
    print("=" * 62)


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
        description="Backtest strategy parameters against snapshot databases.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--db", nargs="+", default=None, metavar="PATH",
                        help="one or more .db files to replay (the shell expands globs)")
    parser.add_argument("--all",        action="store_true",
                        help="replay all .db files in data/ and live.db if usable")
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
                        help="show actual bot results alongside backtest (single file only)")
    parser.add_argument("--sweep",      action="store_true",
                        help="grid search over multiple parameter combinations")
    args = parser.parse_args()

    db_paths = _collect_dbs(args.db, args.all)
    if not db_paths:
        print("No database files found. Use --db PATH, --all, or run the bot first.")
        sys.exit(1)

    multi = len(db_paths) > 1

    # Strategy parameters shared across all files.
    p = Params(
        signal_threshold=args.threshold,
        min_secs_remaining=args.min_secs,
        min_ask_vol=args.min_ask,
        obi_reject_thresh=args.obi,
        stake=args.stake,
    )

    file_results: List[dict] = []  # accumulate for aggregate summary

    for db_path in db_paths:
        if not os.path.exists(db_path):
            print(f"WARNING: not found, skipping: {db_path}")
            continue

        tag = "(sample)" if db_path == _sample_db else ("(live)" if db_path == _live_db else "")
        print(f"DB: {db_path} {tag}".rstrip())

        conn        = sqlite3.connect(db_path)
        n_snapshots = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]

        if n_snapshots == 0:
            print("  No snapshots — skipping.")
            conn.close()
            continue

        rows = load_rows(conn)

        if args.sweep:
            # Grid search — run per file; no cross-file aggregate for sweep.
            thresholds = [0.94, 0.95, 0.96, 0.97, 0.98]
            min_secs   = [30,   45,   60]
            min_asks   = [5,    10,   20]
            obi_vals   = [-0.75, -0.50, -0.25]
            combos     = len(thresholds) * len(min_secs) * len(min_asks) * len(obi_vals)
            print(f"Grid search: {len(thresholds)}×{len(min_secs)}×{len(min_asks)}"
                  f"×{len(obi_vals)} = {combos} combinations")
            print(f"Snapshots  : {n_snapshots:,}")

            sweep_results = []
            for thr, secs, ask, obi in product(thresholds, min_secs, min_asks, obi_vals):
                sp = Params(signal_threshold=thr, min_secs_remaining=secs,
                            min_ask_vol=ask, obi_reject_thresh=obi, stake=args.stake)
                trades, capital_final = run_backtest(rows, sp)
                stats = summarize(trades, sp, capital_final)
                sweep_results.append((sp, stats))
            print_sweep_table(sweep_results)

        else:
            trades, capital_final = run_backtest(rows, p)
            stats = summarize(trades, p, capital_final)
            # Show filename in label when replaying multiple files.
            label = f"BACKTEST — {os.path.basename(db_path)}" if multi else "BACKTEST"
            print(_stat_block(label, p, stats, n_snapshots))

            if args.detail:
                print_detail(trades)

            # --compare only makes sense for a single file (needs a trades table).
            if args.compare and not multi:
                print()
                print_actual(conn, p, n_snapshots)

            file_results.append({
                "label":       os.path.basename(db_path),
                "stats":       stats,
                "n_snapshots": n_snapshots,
            })

        conn.close()

    # Print cross-file aggregate when more than one file was replayed.
    if multi and len(file_results) > 1 and not args.sweep:
        print()
        print_aggregate(file_results)


if __name__ == "__main__":
    main()
