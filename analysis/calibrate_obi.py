#!/usr/bin/env python3
"""
OBI threshold calibration for Piste 3.

Sweeps obi_reject_thresh from -0.75 to -0.25 in steps of 0.05, applying the
Piste 2 base config (bid_alpha=2.0, cap=12%, weekly_stop=$60) with each threshold.
Reports: n_kept, skip_pct, WR, PnL, EV, Sharpe, MaxDD, AvgStk.

Usage:
  python3 analysis/calibrate_obi.py
  python3 analysis/calibrate_obi.py --db data/paper3.db data/liveweek.db
  python3 analysis/calibrate_obi.py --threshold 0.96
"""

import argparse, math, os, sqlite3, sys
from dataclasses import dataclass
from typing import List

BASE_STAKE          = 10.0
SECS_MIN_FACTOR     = 0.3
SIGNAL_THRESHOLD    = 0.95

# Piste 2 base config
BID_ALPHA           = 2.0
SECS_REF            = 45.0
SECS_ALPHA          = 0.0
STAKE_MAX           = 15.0
STAKE_MAX_PCT_CAP   = 0.12
WEEKLY_STOP         = 60.0
CAPITAL_START       = 100.0
GAS_FEE             = 0.03
FEE_RATE            = 0.02

DEFAULT_DBS = [
    os.path.expanduser("~/tradinebotte/tradinebotte/data/paper3.db"),
    os.path.expanduser("~/tradinebotte/tradinebotte/data/liveweek.db"),
]

OBI_THRESHOLDS = [-0.75, -0.70, -0.65, -0.60, -0.55, -0.50, -0.45, -0.40, -0.35, -0.30, -0.25]


@dataclass
class Trade:
    signal_ts_ms: int
    outcome: str
    signal_best_bid: float
    signal_secs_remaining: float
    signal_obi: float
    stake: float        # original stake in DB (for PnL rescaling)
    pnl_net: float      # original pnl_net


def load_trades(db_paths: List[str], threshold: float) -> List[Trade]:
    rows = []
    for path in db_paths:
        if not os.path.exists(path):
            print(f"  [skip] {path} not found", file=sys.stderr)
            continue
        conn = sqlite3.connect(path)
        cur = conn.execute(
            "SELECT signal_ts_ms, outcome, signal_best_bid, signal_secs_remaining,"
            "       signal_obi, stake, pnl_net"
            "  FROM trades"
            " WHERE signal_best_bid >= ? AND resolved = 1"
            " ORDER BY signal_ts_ms",
            (threshold,)
        )
        for r in cur:
            rows.append(Trade(*r))
        conn.close()
    rows.sort(key=lambda t: t.signal_ts_ms)
    return rows


def compute_stake(bid: float, secs: float, capital: float, threshold: float) -> float:
    bid_range  = 1.0 - threshold
    bid_score  = (bid - threshold) / bid_range if bid_range > 0 else 0.0
    bid_boost  = 1.0 + BID_ALPHA * bid_score
    secs_factor = 1.0
    if secs > SECS_REF and SECS_ALPHA > 0:
        excess      = (secs - SECS_REF) / SECS_REF
        secs_factor = max(SECS_MIN_FACTOR, 1.0 - SECS_ALPHA * excess)
    eff_max = min(STAKE_MAX, STAKE_MAX_PCT_CAP * capital) if capital > 0 else STAKE_MAX
    floor   = BASE_STAKE * SECS_MIN_FACTOR
    return min(eff_max, max(floor, BASE_STAKE * bid_boost * secs_factor))


@dataclass
class SimResult:
    obi_thresh: float
    n_total: int
    n_kept: int
    n_loss: int
    wr: float
    pnl: float
    ev: float
    sharpe: float
    max_dd: float
    avg_stake: float


def simulate(trades: List[Trade], obi_thresh: float, threshold: float) -> SimResult:
    capital     = CAPITAL_START
    peak        = CAPITAL_START
    max_dd      = 0.0
    weekly_pnl  = 0.0
    week_idx    = -1

    pnl_list:   List[float] = []
    stakes:     List[float] = []
    n_kept      = 0
    n_loss      = 0

    for t in trades:
        # Weekly reset (7-day periods since Unix epoch)
        w = int(t.signal_ts_ms // (7 * 86400 * 1000))
        if w != week_idx:
            weekly_pnl = 0.0
            week_idx   = w

        # OBI filter
        if t.signal_obi < obi_thresh:
            continue
        # Weekly stop-loss
        if WEEKLY_STOP > 0 and weekly_pnl < -WEEKLY_STOP:
            continue

        n_kept += 1
        stake = compute_stake(t.signal_best_bid, t.signal_secs_remaining, capital, threshold)
        stakes.append(stake)

        # Rescale PnL: pnl_net was computed on original stake; rescale to our stake.
        pnl = (t.pnl_net / t.stake) * stake if t.stake > 0 else 0.0
        pnl = round(pnl, 6)

        # Apply gas fee on losses (fee already in pnl_net for wins via FEE_RATE)
        if t.outcome != "WIN":
            pnl -= GAS_FEE
            n_loss += 1

        pnl_list.append(pnl)
        capital     += pnl
        weekly_pnl  += pnl
        peak         = max(peak, capital)
        max_dd       = max(max_dd, peak - capital)

    n = n_kept
    if n == 0:
        return SimResult(obi_thresh, len(trades), 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    total_pnl = sum(pnl_list)
    ev        = total_pnl / n
    wr        = (n - n_loss) / n * 100

    if n > 1:
        mean  = total_pnl / n
        var   = sum((p - mean) ** 2 for p in pnl_list) / (n - 1)
        std   = math.sqrt(var) if var > 0 else 0.0
        sharpe = (mean / std) * math.sqrt(n) if std > 0 else 0.0
    else:
        sharpe = 0.0

    avg_stake = sum(stakes) / len(stakes) if stakes else 0.0

    return SimResult(
        obi_thresh=obi_thresh,
        n_total=len(trades),
        n_kept=n_kept,
        n_loss=n_loss,
        wr=wr,
        pnl=total_pnl,
        ev=ev,
        sharpe=sharpe,
        max_dd=max_dd,
        avg_stake=avg_stake,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Sweep obi_reject_thresh for Piste 3")
    parser.add_argument("--db", nargs="+", default=DEFAULT_DBS, metavar="PATH")
    parser.add_argument("--threshold", type=float, default=SIGNAL_THRESHOLD)
    args = parser.parse_args()

    trades = load_trades(args.db, args.threshold)
    print(f"Loaded {len(trades)} trades (threshold >= {args.threshold}) from {len(args.db)} DB(s)\n")

    results = [simulate(trades, t, args.threshold) for t in OBI_THRESHOLDS]

    header = (
        f"{'OBI_thresh':>11}  {'n_kept':>7}  {'skip%':>6}  {'losses':>7}"
        f"  {'WR%':>6}  {'PnL':>8}  {'EV':>8}  {'Sharpe':>7}  {'MaxDD':>7}  {'AvgStk':>7}"
    )
    print(header)
    print("─" * len(header))
    for r in results:
        skip = (r.n_total - r.n_kept) / r.n_total * 100 if r.n_total else 0.0
        print(
            f"{r.obi_thresh:>11.2f}  {r.n_kept:>7d}  {skip:>5.1f}%  {r.n_loss:>7d}"
            f"  {r.wr:>5.1f}%  {r.pnl:>+8.2f}  {r.ev:>+8.4f}  {r.sharpe:>7.2f}"
            f"  {r.max_dd:>7.2f}  {r.avg_stake:>7.2f}"
        )

    # Highlight sweet spot: best PnL with MaxDD < piste2's $104
    print("\nSweet spot (MaxDD < $104, PnL maximised):")
    candidates = [r for r in results if r.max_dd < 104.0 and r.n_kept > 0]
    if candidates:
        best = max(candidates, key=lambda r: r.pnl)
        skip = (best.n_total - best.n_kept) / best.n_total * 100
        print(f"  obi_thresh={best.obi_thresh:.2f}  n={best.n_kept}  skip={skip:.1f}%"
              f"  PnL={best.pnl:+.2f}  MaxDD={best.max_dd:.2f}  Sharpe={best.sharpe:.2f}")


if __name__ == "__main__":
    main()
