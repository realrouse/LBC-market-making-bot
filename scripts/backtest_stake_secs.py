#!/usr/bin/env python3
"""
Phase 3 — Grid search: combined bid×secs stake scaling + vol-filter.

Option 2: stake = base_stake × bid_factor(signal_best_bid) × secs_factor(signal_secs_remaining)
Vol-filter complement: skip trades where the rolling bid/OBI volatility exceeds calibrated
thresholds (same logic as backtest_volfilter.py, computed from the snapshots table).

PnL is rescaled from the original stake stored in each trade row so results are comparable
across paper3 ($150/trade) and liveweek ($10/trade).  All output is expressed in $10-unit
equivalents (BASE_STAKE = 10).

Stake formula
─────────────
  bid_score   = (bid − threshold) / (1 − threshold)          ∈ [0, 1]
  bid_boost   = 1 + bid_alpha × bid_score                    ≥ 1
  secs_excess = max(0, (secs − secs_ref) / secs_ref)         ≥ 0
  secs_factor = max(SECS_MIN_FACTOR, 1 − secs_alpha × secs_excess)
  stake       = clip(BASE_STAKE × bid_boost × secs_factor, BASE_STAKE × SECS_MIN_FACTOR, stake_max)

Vol-filter (when enabled, weekday-only by default)
─────────────────────────────────────────────────────
  Computed from the WINDOW snapshots preceding each trade entry (same as live_bot.py):
    vol_bid   = σ(best_bid)     ≤ VOL_BID_MAX
    range_bid = max−min(bid)   ≤ RANGE_BID_MAX
    obi_vol   = σ(obi)          ≤ OBI_VOL_MAX
  If any threshold is exceeded (and enough samples exist), the trade is skipped.

Usage
─────
  python3 scripts/backtest_stake_secs.py                      # paper3 + liveweek
  python3 scripts/backtest_stake_secs.py --db data/paper3.db
  python3 scripts/backtest_stake_secs.py --top 20
  python3 scripts/backtest_stake_secs.py --threshold 0.96
"""

import argparse, math, os, sqlite3, sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import product
from typing import List, Tuple

# ─── Constants ───────────────────────────────────────────────────────────────

BASE_STAKE      = 10.0   # reference stake for normalised output
SECS_MIN_FACTOR = 0.3    # minimum secs multiplier (floor at 30 % of base)
SIGNAL_THRESHOLD = 0.95  # default bid filter; overridden by --threshold

# Vol-filter calibrated thresholds (from backtest_volfilter.py / volstop.txt)
VOL_BID_MAX   = 0.07
RANGE_BID_MAX = 0.30
OBI_VOL_MAX   = 0.40
VOL_WINDOW    = 12       # snapshots (× snapshot_interval seconds)
VOL_MIN_SAMP  = 6

# ─── Grid ────────────────────────────────────────────────────────────────────
# 4 × 3 × 4 × 3 × 2 = 288 combinations; runs in a few seconds

BID_ALPHAS  = [0.0, 0.5, 1.0, 2.0]     # bid-confidence boost
SECS_REFS   = [45.0, 60.0, 90.0]        # safe zone upper bound (s)
SECS_ALPHAS = [0.0, 0.25, 0.5, 1.0]    # secs-penalty intensity
STAKE_MAXS  = [10.0, 15.0]             # stake cap
VOL_MODES   = ["off", "weekday"]        # vol-filter: off | weekday-only

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DEFAULT_DBS = [
    os.path.join(_DATA_DIR, "paper3.db"),
    os.path.join(_DATA_DIR, "liveweek.db"),
]

SEP  = "═" * 90
SEP2 = "─" * 90


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Trade:
    trade_id:   int
    signal_ms:  int
    bid:        float
    secs:       float
    obi:        float
    outcome:    str
    pnl_orig:   float
    stake_orig: float

    @property
    def pnl_per_dollar(self) -> float:
        return self.pnl_orig / self.stake_orig if self.stake_orig else 0.0


@dataclass
class SnapRow:
    ts_ms:     int
    market_id: str
    direction: str
    best_bid:  float
    obi:       float


# ─── Loading ──────────────────────────────────────────────────────────────────

def _load_db(path: str, threshold: float) -> Tuple[List[Trade], List[SnapRow]]:
    conn = sqlite3.connect(path)
    rows = conn.execute(
        """
        SELECT id, signal_ts_ms, signal_best_bid, signal_secs_remaining,
               signal_obi, outcome, pnl_net, stake
        FROM trades
        WHERE resolved=1
          AND signal_best_bid >= ?
          AND signal_secs_remaining IS NOT NULL
          AND pnl_net IS NOT NULL
          AND stake IS NOT NULL AND stake > 0
        ORDER BY signal_ts_ms
        """,
        (threshold,),
    ).fetchall()
    trades = [
        Trade(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7])
        for r in rows
    ]
    snaps = [
        SnapRow(*r)
        for r in conn.execute(
            "SELECT ts_ms, market_id, direction, best_bid, obi FROM snapshots ORDER BY ts_ms"
        ).fetchall()
    ]
    conn.close()
    return trades, snaps


def load_all(paths: List[str], threshold: float) -> Tuple[List[Trade], dict]:
    """
    Load and merge trades from all DBs, attaching vol metrics computed from
    the snapshots table of the same DB (so each DB's snapshots align with
    its own trades).
    """
    all_trades: List[Trade] = []
    vol_at_entry: dict[int, dict] = {}

    for path in paths:
        if not os.path.exists(path):
            print(f"  WARNING: {path} not found — skipped", file=sys.stderr)
            continue
        trades, snaps = _load_db(path, threshold)
        if not trades:
            print(f"  WARNING: {os.path.basename(path)} has no qualifying trades", file=sys.stderr)
            continue
        vol_map = _build_vol_at_entry(snaps, trades)
        all_trades.extend(trades)
        vol_at_entry.update(vol_map)
        print(f"  Loaded  {os.path.basename(path):30s}  {len(trades)} trades  "
              f"{len(snaps):,} snapshots")

    # Sort merged list chronologically so daily-PnL and drawdown are correct.
    all_trades.sort(key=lambda t: t.signal_ms)
    return all_trades, vol_at_entry


# ─── Vol-filter (replicated from backtest_volfilter.py) ───────────────────────

def _build_vol_at_entry(snaps: List[SnapRow], trades: List[Trade]) -> dict:
    """
    For each trade, compute bid/OBI rolling stats from the VOL_WINDOW snapshots
    that precede the trade entry time.  Returns {trade_id: {vol_bid, range_bid,
    vol_obi, n_snaps}}.
    """
    history: dict = defaultdict(list)
    vol_at_entry: dict = {}
    tidx = 0

    for snap in snaps:
        key = (snap.market_id, snap.direction)
        history[key].append(snap)
        if len(history[key]) > VOL_WINDOW:
            history[key].pop(0)

        while tidx < len(trades) and snap.ts_ms >= trades[tidx].signal_ms:
            t = trades[tidx]
            # We need market_id+direction to look up history, but Trade doesn't
            # carry them.  Use signal_ms as a proxy: walk backward through snaps
            # to find the most recent snap for this trade's token.  Since snaps
            # are processed in order, we just use what's already in history.
            # The key lookup will miss if market_id is unknown — fall back to
            # empty stats (filter will not apply for that trade).
            stats: dict = {"vol_bid": 0.0, "range_bid": 0.0, "vol_obi": 0.0, "n_snaps": 0}
            for _, hlist in history.items():
                if hlist and abs(hlist[-1].ts_ms - t.signal_ms) < 10_000:
                    bids = [s.best_bid for s in hlist]
                    obis = [s.obi      for s in hlist]
                    n    = len(bids)
                    if n >= 2:
                        mb = sum(bids) / n
                        mo = sum(obis) / n
                        vb = math.sqrt(sum((b - mb) ** 2 for b in bids) / n)
                        rb = max(bids) - min(bids)
                        ov = math.sqrt(sum((o - mo) ** 2 for o in obis) / n)
                        stats = {"vol_bid": vb, "range_bid": rb, "vol_obi": ov, "n_snaps": n}
                    break
            vol_at_entry[t.trade_id] = stats
            tidx += 1

    return vol_at_entry


def _in_weekend(ts_ms: int) -> bool:
    dt  = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dow, h, m = dt.weekday(), dt.hour, dt.minute
    if dow in (5, 6): return True
    if dow == 4 and h >= 20: return True
    if dow == 0 and (h < 13 or (h == 13 and m < 30)): return True
    return False


def _vol_blocked(trade: Trade, vol_at_entry: dict, vol_mode: str) -> bool:
    """Return True if the vol-filter would block this trade."""
    if vol_mode == "off":
        return False
    v = vol_at_entry.get(trade.trade_id, {})
    n = v.get("n_snaps", 0)
    if n < VOL_MIN_SAMP:
        return False
    # weekday mode: filter inactive on weekends
    if vol_mode == "weekday" and _in_weekend(trade.signal_ms):
        return False
    return (
        v.get("vol_bid",   0.0) > VOL_BID_MAX   or
        v.get("range_bid", 0.0) > RANGE_BID_MAX or
        v.get("vol_obi",   0.0) > OBI_VOL_MAX
    )


# ─── Stake formula ────────────────────────────────────────────────────────────

def compute_stake(bid: float, secs: float, params: dict,
                  threshold: float = SIGNAL_THRESHOLD) -> float:
    bid_range  = 1.0 - threshold
    bid_score  = (bid - threshold) / bid_range if bid_range > 0 else 0.0
    bid_boost  = 1.0 + params["bid_alpha"] * bid_score

    secs_ref   = params["secs_ref"]
    secs_alpha = params["secs_alpha"]
    if secs <= secs_ref:
        secs_factor = 1.0
    else:
        excess      = (secs - secs_ref) / secs_ref
        secs_factor = max(SECS_MIN_FACTOR, 1.0 - secs_alpha * excess)

    raw = BASE_STAKE * bid_boost * secs_factor
    return min(params["stake_max"], max(BASE_STAKE * SECS_MIN_FACTOR, raw))


# ─── Simulation ───────────────────────────────────────────────────────────────

@dataclass
class SimResult:
    params:     dict
    n:          int
    skipped:    int
    wins:       int
    losses:     int
    total_pnl:  float
    sharpe:     float
    max_dd:     float

    @property
    def ev(self) -> float:
        return self.total_pnl / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0


def _simulate(trades: List[Trade], vol_at_entry: dict, params: dict,
              threshold: float) -> SimResult:
    daily: dict[str, float] = defaultdict(float)
    total_pnl = 0.0
    wins = losses = skipped = 0
    peak = cum = 0.0
    max_dd = 0.0

    for t in trades:
        if _vol_blocked(t, vol_at_entry, params["vol_mode"]):
            skipped += 1
            continue

        stake    = compute_stake(t.bid, t.secs, params, threshold)
        pnl      = t.pnl_per_dollar * stake
        day_key  = datetime.fromtimestamp(t.signal_ms / 1000,
                                          tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day_key] += pnl
        total_pnl      += pnl
        cum            += pnl
        peak            = max(peak, cum)
        dd              = peak - cum
        max_dd          = max(max_dd, dd)

        if t.outcome == "WIN":
            wins += 1
        else:
            losses += 1

    # Daily Sharpe (annualised): mean_daily / std_daily × √365
    vals   = list(daily.values())
    sharpe = 0.0
    if len(vals) >= 2:
        mean  = sum(vals) / len(vals)
        std   = math.sqrt(sum((v - mean) ** 2 for v in vals) / len(vals))
        if std > 0:
            sharpe = mean / std * math.sqrt(365)

    return SimResult(
        params=params,
        n=wins + losses,
        skipped=skipped,
        wins=wins,
        losses=losses,
        total_pnl=total_pnl,
        sharpe=sharpe,
        max_dd=max_dd,
    )


# ─── Grid search ──────────────────────────────────────────────────────────────

def run_sweep(trades: List[Trade], vol_at_entry: dict,
              threshold: float, top_n: int) -> List[SimResult]:
    results: List[SimResult] = []

    combos = list(product(BID_ALPHAS, SECS_REFS, SECS_ALPHAS, STAKE_MAXS, VOL_MODES))
    print(f"\n  Running {len(combos)} parameter combinations on {len(trades)} trades …\n")

    for bid_alpha, secs_ref, secs_alpha, stake_max, vol_mode in combos:
        params = {
            "bid_alpha":  bid_alpha,
            "secs_ref":   secs_ref,
            "secs_alpha": secs_alpha,
            "stake_max":  stake_max,
            "vol_mode":   vol_mode,
        }
        results.append(_simulate(trades, vol_at_entry, params, threshold))

    return results


# ─── Baseline rows ────────────────────────────────────────────────────────────

def _baseline(trades: List[Trade], vol_at_entry: dict, threshold: float) -> Tuple:
    flat_off = _simulate(trades, vol_at_entry,
                         {"bid_alpha": 0, "secs_ref": 60, "secs_alpha": 0,
                          "stake_max": 10, "vol_mode": "off"}, threshold)
    flat_wd  = _simulate(trades, vol_at_entry,
                         {"bid_alpha": 0, "secs_ref": 60, "secs_alpha": 0,
                          "stake_max": 10, "vol_mode": "weekday"}, threshold)
    return flat_off, flat_wd


# ─── Output ───────────────────────────────────────────────────────────────────

def _fmt_params(p: dict) -> str:
    return (f"bid_α={p['bid_alpha']:.1f} secs_ref={p['secs_ref']:.0f}s "
            f"secs_α={p['secs_alpha']:.2f} stk_max={p['stake_max']:.0f} "
            f"vol={p['vol_mode']}")


def _fmt_result(label: str, r: SimResult) -> str:
    skip_pct = r.skipped / (r.n + r.skipped) * 100 if (r.n + r.skipped) else 0
    return (
        f"  {label:<34}  n={r.n:>4}  L={r.losses:>3}  "
        f"WR={r.win_rate*100:>5.1f}%  "
        f"PnL=${r.total_pnl:>+8.2f}  EV=${r.ev:>+7.4f}  "
        f"Sharpe={r.sharpe:>+5.2f}  DD=${r.max_dd:>6.2f}  "
        f"skip={r.skipped}({skip_pct:.0f}%)"
    )


def print_report(results: List[SimResult], flat_off: SimResult, flat_wd: SimResult,
                 top_n: int, threshold: float) -> None:
    all_sorted  = sorted(results, key=lambda r: r.ev, reverse=True)
    top_ev      = [r for r in all_sorted if r.n >= 20][:top_n]
    top_sharpe  = sorted(results, key=lambda r: r.sharpe, reverse=True)
    top_sharpe  = [r for r in top_sharpe if r.n >= 20][:top_n]

    print(f"\n{SEP}")
    print(f"  Phase 3 — stake(bid×secs) + vol-filter  "
          f"[threshold={threshold:.2f}  BASE_STAKE=${BASE_STAKE:.0f}  "
          f"SECS_MIN_FACTOR={SECS_MIN_FACTOR}]")
    print(SEP)
    print(f"\n  {'BASELINES':}")
    print(f"  {'-'*88}")
    print(_fmt_result("flat $10 — vol=off", flat_off))
    print(_fmt_result("flat $10 — vol=weekday", flat_wd))

    print(f"\n  TOP {top_n} BY EV/TRADE")
    print(f"  {'-'*88}")
    for rank, r in enumerate(top_ev, 1):
        label = f"#{rank:02d} {_fmt_params(r.params)}"
        delta_ev  = r.ev - flat_off.ev
        delta_pnl = r.total_pnl - flat_off.total_pnl
        line = _fmt_result(label, r)
        line += f"  ΔEV=${delta_ev:+.4f}  ΔPnL=${delta_pnl:+.2f}"
        print(line)

    print(f"\n  TOP {min(top_n, 10)} BY SHARPE")
    print(f"  {'-'*88}")
    for rank, r in enumerate(top_sharpe[:10], 1):
        label = f"#{rank:02d} {_fmt_params(r.params)}"
        delta_ev  = r.ev - flat_off.ev
        print(_fmt_result(label, r) + f"  ΔEV=${delta_ev:+.4f}")

    # Best with vol_mode=weekday only
    with_vol = [r for r in all_sorted if r.params["vol_mode"] == "weekday" and r.n >= 20]
    if with_vol:
        best_vol = with_vol[0]
        print(f"\n  BEST (vol=weekday): {_fmt_params(best_vol.params)}")
        print(f"  {_fmt_result('', best_vol)}")

    # Best with scaling only (no vol filter)
    no_vol = [r for r in all_sorted if r.params["vol_mode"] == "off" and r.n >= 20]
    if no_vol:
        best_nov = no_vol[0]
        print(f"\n  BEST (vol=off): {_fmt_params(best_nov.params)}")
        print(f"  {_fmt_result('', best_nov)}")

    # Summary verdict
    best = top_ev[0] if top_ev else None
    print(f"\n{'─'*90}")
    if best:
        delta_ev  = best.ev - flat_off.ev
        delta_pnl = best.total_pnl - flat_off.total_pnl
        print(f"  BEST OVERALL: {_fmt_params(best.params)}")
        print(f"  EV: ${flat_off.ev:+.4f} → ${best.ev:+.4f}  (Δ{delta_ev:+.4f} per trade)")
        print(f"  PnL: ${flat_off.total_pnl:+.2f} → ${best.total_pnl:+.2f}  (Δ${delta_pnl:+.2f})")
        print(f"  Sharpe: {flat_off.sharpe:+.2f} → {best.sharpe:+.2f}")
        if delta_ev > 0.001:
            print("  VERDICT: bid×secs scaling improves EV — consider Phase 4 implementation.")
        elif delta_ev > 0:
            print("  VERDICT: marginal improvement — validate on independent dataset first.")
        else:
            print("  VERDICT: no clear improvement — flat stake remains optimal.")
    print()


# ─── Entry point ──────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 3: bid×secs stake grid search")
    p.add_argument("--db", nargs="+", metavar="PATH",
                   help="DB file(s) to replay (default: paper3.db + liveweek.db)")
    p.add_argument("--threshold", type=float, default=SIGNAL_THRESHOLD,
                   help=f"Min signal_best_bid (default: {SIGNAL_THRESHOLD})")
    p.add_argument("--top", type=int, default=15, metavar="N",
                   help="Show top N combos (default: 15)")
    return p.parse_args()


def main() -> None:
    args  = _parse_args()
    paths = args.db if args.db else DEFAULT_DBS

    print(f"\n{SEP}")
    print("  Phase 3 — grid search loading databases …")
    print(SEP)

    trades, vol_at_entry = load_all(paths, args.threshold)
    if not trades:
        print("ERROR: no qualifying trades found.", file=sys.stderr)
        sys.exit(1)

    print(f"\n  Total: {len(trades)} trades  threshold≥{args.threshold:.2f}")

    flat_off, flat_wd = _baseline(trades, vol_at_entry, args.threshold)
    results = run_sweep(trades, vol_at_entry, args.threshold, args.top)
    print_report(results, flat_off, flat_wd, args.top, args.threshold)


if __name__ == "__main__":
    main()
