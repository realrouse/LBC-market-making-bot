#!/usr/bin/env python3
"""
Phase 1 — Time-scaled stake sizing: validate the win-rate vs secs_remaining hypothesis.

Groups resolved trades by signal_secs_remaining bucket and reports win rate, average EV,
and trade count.  Filters to signal_best_bid >= threshold so sweep-data at lower thresholds
does not dilute the analysis.

Usage:
    python3 analysis/analyze_stake_secs.py                         # default DB resolution
    python3 analysis/analyze_stake_secs.py --db data/paper3.db
    python3 analysis/analyze_stake_secs.py --db data/liveweek.db --threshold 0.95
    python3 analysis/analyze_stake_secs.py --db data/paper3.db data/liveweek.db
"""

import argparse, os, sqlite3, sys
from dataclasses import dataclass
from typing import List, Optional, Tuple

INSTALL_DIR = os.path.expanduser(os.environ.get("TRADINEBOTTE_DIR", "~/tradinebotte"))
_live_db   = os.path.join(INSTALL_DIR, "live.db")
_data_dir  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_paper3_db = os.path.join(_data_dir, "paper3.db")

# Bucket edges in seconds (right edge is exclusive except for the last bucket)
BUCKETS: List[Tuple[Optional[float], Optional[float], str]] = [
    (None, 30,   "< 30s"),
    (30,   45,   "30–45s"),
    (45,   60,   "45–60s"),
    (60,   90,   "60–90s"),
    (90,   120,  "90–120s"),
    (120,  180,  "120–180s"),
    (180,  None, "180s+"),
]


@dataclass
class BucketStats:
    label:    str
    n:        int
    wins:     int
    losses:   int
    total_pnl: float
    avg_stake: float
    avg_bid:  float
    avg_secs: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def avg_ev(self) -> float:
        return self.total_pnl / self.n if self.n else 0.0


def _collect_dbs(db_args: Optional[List[str]]) -> List[str]:
    if db_args:
        return list(db_args)
    defaults = []
    if os.path.exists(_live_db):
        try:
            c = sqlite3.connect(_live_db)
            n = c.execute("SELECT COUNT(*) FROM trades WHERE resolved=1").fetchone()[0]
            c.close()
            if n >= 10:
                defaults.append(_live_db)
        except Exception:
            pass
    if os.path.exists(_paper3_db):
        defaults.append(_paper3_db)
    return defaults


def _load_trades(paths: List[str], threshold: float) -> List[dict]:
    rows: List[dict] = []
    for path in paths:
        if not os.path.exists(path):
            print(f"WARNING: {path} not found — skipped", file=sys.stderr)
            continue
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            """
            SELECT signal_secs_remaining, signal_best_bid, outcome, pnl_net, stake
            FROM trades
            WHERE resolved=1
              AND signal_best_bid >= ?
              AND signal_secs_remaining IS NOT NULL
            ORDER BY signal_ts_ms
            """,
            (threshold,),
        )
        rows.extend(dict(r) for r in cur.fetchall())
        conn.close()
    return rows


def _bucket(secs: float) -> int:
    for i, (lo, hi, _) in enumerate(BUCKETS):
        if (lo is None or secs >= lo) and (hi is None or secs < hi):
            return i
    return len(BUCKETS) - 1


def _analyse(trades: List[dict]) -> List[BucketStats]:
    buckets: List[List[dict]] = [[] for _ in BUCKETS]
    for t in trades:
        buckets[_bucket(t["signal_secs_remaining"])].append(t)

    stats: List[BucketStats] = []
    for i, (_, _, label) in enumerate(BUCKETS):
        grp = buckets[i]
        if not grp:
            stats.append(BucketStats(label, 0, 0, 0, 0.0, 0.0, 0.0, 0.0))
            continue
        wins   = sum(1 for t in grp if t["outcome"] == "WIN")
        losses = len(grp) - wins
        stats.append(BucketStats(
            label    = label,
            n        = len(grp),
            wins     = wins,
            losses   = losses,
            total_pnl = sum(t["pnl_net"] for t in grp),
            avg_stake = sum(t["stake"]   for t in grp) / len(grp),
            avg_bid  = sum(t["signal_best_bid"] for t in grp) / len(grp),
            avg_secs = sum(t["signal_secs_remaining"] for t in grp) / len(grp),
        ))
    return stats


def _print_table(stats: List[BucketStats], threshold: float) -> None:
    total_n   = sum(s.n for s in stats)
    total_pnl = sum(s.total_pnl for s in stats)

    print(f"\n{'═'*78}")
    print(f"  Win-rate vs secs_remaining  (signal_best_bid >= {threshold:.2f}  |  n={total_n})")
    print(f"{'═'*78}")
    hdr = f"  {'Bucket':<12}  {'N':>5}  {'WR%':>6}  {'AvgEV':>7}  {'TotalPnL':>9}  {'AvgBid':>7}  {'AvgSecs':>8}"
    print(hdr)
    print(f"  {'-'*12}  {'-'*5}  {'-'*6}  {'-'*7}  {'-'*9}  {'-'*7}  {'-'*8}")

    max_wr = max((s.win_rate for s in stats if s.n), default=0.0)

    for s in stats:
        if s.n == 0:
            continue
        marker = " ◀ max" if s.win_rate == max_wr else ""
        print(
            f"  {s.label:<12}  {s.n:>5}  {s.win_rate*100:>5.1f}%  "
            f"${s.avg_ev:>+6.4f}  ${s.total_pnl:>+8.2f}  "
            f"{s.avg_bid:>7.4f}  {s.avg_secs:>8.1f}s{marker}"
        )

    print(f"  {'─'*12}  {'─'*5}  {'─'*6}  {'─'*7}  {'─'*9}  {'─'*7}  {'─'*8}")
    all_wr = sum(s.wins for s in stats) / total_n if total_n else 0.0
    all_ev = total_pnl / total_n if total_n else 0.0
    print(f"  {'TOTAL':<12}  {total_n:>5}  {all_wr*100:>5.1f}%  ${all_ev:>+6.4f}  ${total_pnl:>+8.2f}")
    print(f"{'═'*78}\n")


def _print_verdict(stats: List[BucketStats]) -> None:
    active = [s for s in stats if s.n >= 10]
    if len(active) < 2:
        print("Not enough data to determine a trend.\n")
        return

    max_wr = max(s.win_rate for s in active)
    min_wr = min(s.win_rate for s in active)
    spread = max_wr - min_wr

    print("VERDICT")
    print("-------")
    if spread < 0.01:
        print(f"  Win rate spread across buckets: {spread*100:.2f} pp  →  FLAT")
        print("  Win rate does not vary meaningfully with secs_remaining.")
        print("  Time-scaled stake sizing is NOT justified by the data.\n")
    elif spread < 0.03:
        print(f"  Win rate spread across buckets: {spread*100:.2f} pp  →  MARGINAL")
        print("  Small trend visible — proceed to Phase 2 with caution.")
        print("  Validate on an independent dataset before deploying.\n")
    else:
        print(f"  Win rate spread across buckets: {spread*100:.2f} pp  →  SIGNIFICANT")
        print("  Clear trend: higher win rate at lower secs_remaining.")
        print("  Proceed to Phase 2 (stake curve candidates) and Phase 3 (grid search).\n")

    # Check monotonicity (higher secs → lower WR?)
    buckets_asc = list(active)
    wr_vals     = [s.win_rate for s in buckets_asc]
    mono_ok     = all(wr_vals[i] >= wr_vals[i+1] for i in range(len(wr_vals)-1))
    if mono_ok:
        print("  Monotonicity: YES — win rate decreases consistently as secs_remaining grows.")
    else:
        print("  Monotonicity: NO — win rate is not strictly monotone across buckets.")
        print("  Consider merging small buckets or reviewing outlier trades.")
    print()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Analyse win rate per secs_remaining bucket")
    p.add_argument("--db", nargs="+", metavar="PATH",
                   help="SQLite DB file(s) to analyse (default: paper3.db or live.db)")
    p.add_argument("--threshold", type=float, default=0.95, metavar="THRESH",
                   help="Minimum signal_best_bid filter (default: 0.95)")
    return p.parse_args()


def main() -> None:
    args  = _parse_args()
    paths = _collect_dbs(args.db)

    if not paths:
        print("ERROR: no database found. Specify --db path/to/file.db", file=sys.stderr)
        sys.exit(1)

    print(f"\nDatabase(s): {', '.join(paths)}")
    trades = _load_trades(paths, args.threshold)

    if not trades:
        print(f"No resolved trades with signal_best_bid >= {args.threshold}", file=sys.stderr)
        sys.exit(1)

    stats = _analyse(trades)
    _print_table(stats, args.threshold)
    _print_verdict(stats)


if __name__ == "__main__":
    main()
