#!/usr/bin/env python3
"""
Simulate the volatility filter on one or more datasets.

Comparison mode (default):
    Displays baseline vs calibrated filter for each DB and in aggregate.

Calibration mode (--sweep):
    Scans the full threshold grid on a single DB to find
    the best configuration. Results saved to volstop.txt.

Usage:
    python3 scripts/backtest_volfilter.py
    python3 scripts/backtest_volfilter.py --db data/liveweek.db data/basicsunday.db
    python3 scripts/backtest_volfilter.py --sweep [--db PATH]
"""
import sqlite3, sys, os, math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone

# ─── Calibrated thresholds (see volstop.txt) ─────────────────────────────────
VOL_BID_MAX   = 0.07   # std dev best_bid
RANGE_BID_MAX = 0.30   # max-min amplitude
OBI_VOL_MAX   = 0.40   # std dev OBI
WINDOW        = 12     # samples × 5s = 60s
MIN_SAMPLES   = 6      # minimum to activate the filter

DEFAULT_DBS = [
    "data/liveweek.db",
    "data/basicsunday.db",
    "data/calmsaturday.db",
]

# Grilles pour le mode --sweep
VOL_BID_THRESHOLDS   = [0.02, 0.03, 0.04, 0.05, 0.07, 0.10, 999]
RANGE_BID_THRESHOLDS = [0.05, 0.08, 0.10, 0.15, 0.20, 0.30, 999]
OBI_VOL_THRESHOLDS   = [0.10, 0.15, 0.20, 0.30, 0.40, 0.60, 999]

SEP  = "=" * 72
SEP2 = "─" * 72


def _in_weekend_session(ts_ms: int) -> bool:
    """Fri >= 20:00 UTC  ->  Mon < 13:30 UTC (same definition as live_bot.py)."""
    dt  = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    dow, hour, minute = dt.weekday(), dt.hour, dt.minute
    if dow in (5, 6):
        return True
    if dow == 4 and hour >= 20:
        return True
    if dow == 0 and (hour < 13 or (hour == 13 and minute < 30)):
        return True
    return False


# ─── Data structures ─────────────────────────────────────────────────────────

@dataclass
class Row:
    ts_ms:     int
    market_id: str
    direction: str
    best_bid:  float
    obi:       float


@dataclass
class TradeRecord:
    id:          int
    market_id:   str
    direction:   str
    entry_price: float
    outcome:     str
    created_at:  int
    pnl_net:     float


# ─── Loading ──────────────────────────────────────────────────────────────────

def load_data(db_path: str) -> tuple[list[Row], list[TradeRecord]]:
    conn = sqlite3.connect(db_path)
    rows = [Row(*r) for r in conn.execute(
        "SELECT ts_ms, market_id, direction, best_bid, obi "
        "FROM snapshots ORDER BY ts_ms"
    ).fetchall()]
    trades = [TradeRecord(*r) for r in conn.execute(
        "SELECT id, market_id, direction, entry_price, outcome, created_at, pnl_net "
        "FROM trades WHERE resolved=1 ORDER BY id"
    ).fetchall()]
    conn.close()
    return rows, trades


# ─── Indicator calculation at entry time ──────────────────────────────────────

def build_vol_at_entry(rows: list[Row], trades: list[TradeRecord]) -> dict[int, dict]:
    """Compute vol_bid, range_bid, obi_vol over the window preceding each trade."""
    history: dict[tuple, list[Row]] = defaultdict(list)
    trade_idx = 0
    vol_at_entry: dict[int, dict] = {}

    for row in rows:
        key = (row.market_id, row.direction)
        history[key].append(row)
        if len(history[key]) > WINDOW:
            history[key].pop(0)

        while trade_idx < len(trades) and row.ts_ms >= trades[trade_idx].created_at:
            t = trades[trade_idx]
            snap = history.get((t.market_id, t.direction), [])
            bids = [s.best_bid for s in snap]
            obis = [s.obi for s in snap]
            n = len(bids)
            if n >= 2:
                mb = sum(bids) / n
                mo = sum(obis) / n
                vb = math.sqrt(sum((b - mb) ** 2 for b in bids) / n)
                rb = max(bids) - min(bids)
                ov = math.sqrt(sum((o - mo) ** 2 for o in obis) / n)
            else:
                vb = rb = ov = 0.0
            vol_at_entry[t.id] = {
                "vol_bid": vb, "range_bid": rb, "vol_obi": ov, "n_snaps": n
            }
            trade_idx += 1

    return vol_at_entry


# ─── Simulation ───────────────────────────────────────────────────────────────

def simulate(trades: list[TradeRecord], vol_at_entry: dict[int, dict],
             vb_max: float, rb_max: float, ov_max: float,
             weekday_only: bool = False) -> dict:
    """Simulate the volatility filter.

    weekday_only=True: suspends the filter during the weekend session
    (Fri 20:00 UTC -> Mon 13:30 UTC), identical to VOL_FILTER_WEEKDAY_ONLY.
    """
    total = wins = losses = skipped = skipped_wd = skipped_we = 0
    pnl_total = 0.0
    for t in trades:
        v = vol_at_entry.get(t.id, {})
        n = v.get("n_snaps", 0)
        is_we = _in_weekend_session(t.created_at)
        # filter inactive if: window too short, or weekday_only mode + weekend entry
        filter_active = n >= MIN_SAMPLES and not (weekday_only and is_we)
        if filter_active and (
            v.get("vol_bid", 0)   > vb_max or
            v.get("range_bid", 0) > rb_max or
            v.get("vol_obi", 0)   > ov_max
        ):
            skipped += 1
            if is_we:
                skipped_we += 1
            else:
                skipped_wd += 1
            continue
        total += 1
        pnl_total += t.pnl_net
        wins   += t.outcome == "WIN"
        losses += t.outcome == "LOSS"
    wr = wins / total if total else 0.0
    ev = pnl_total / total if total else 0.0
    return {"trades": total, "wins": wins, "losses": losses,
            "skipped": skipped, "skipped_wd": skipped_wd, "skipped_we": skipped_we,
            "pnl": pnl_total, "wr": wr, "ev": ev}


# ─── Multi-DB comparison ──────────────────────────────────────────────────────

def _fmt_row(label: str, tag: str, r: dict, extra: str = "") -> str:
    skip_tot = r['skipped']
    skip_pct = skip_tot / (r['trades'] + skip_tot) * 100 if (r['trades'] + skip_tot) else 0
    s = (f"  {label:<28}  {tag:>8}  {r['trades']:>7}  {r['losses']:>7}  "
         f"{r['wr']*100:>6.1f}%  {r['pnl']:>+9.2f}  {r['ev']:>+8.4f}")
    if skip_tot:
        s += f"  skip={skip_tot}({skip_pct:.0f}%)"
        if r.get('skipped_wd') and r.get('skipped_we'):
            s += f" [wd={r['skipped_wd']} we={r['skipped_we']}]"
    if extra:
        s += f"  {extra}"
    return s


def run_comparison(db_paths: list[str]) -> None:
    print(f"\n{SEP}")
    print("  COMPARISON — 3 SCENARIOS")
    print(f"  Thresholds: vol_bid<={VOL_BID_MAX}  range<={RANGE_BID_MAX}  obi_vol<={OBI_VOL_MAX}  "
          f"window {WINDOW}x5s={WINDOW*5}s")
    print("  BASE       : no filter")
    print("  FILT-ALL   : filter active 7d/7  (VOL_FILTER_WEEKDAY_ONLY=False)")
    print("  FILT-WD    : filter active weekdays only  (VOL_FILTER_WEEKDAY_ONLY=True)")
    print(SEP)

    hdr = (f"\n  {'Dataset':<28}  {'Scenario':>8}  {'trades':>7}  {'losses':>7}  "
           f"{'WR':>7}  {'PnL':>9}  {'EV':>8}")
    print(hdr)
    print(f"  {'-'*28}  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*9}  {'-'*8}")

    keys = ("trades", "wins", "losses", "skipped", "skipped_wd", "skipped_we")
    agg: dict[str, dict] = {sc: {k: 0 for k in keys} | {"pnl": 0.0}
                             for sc in ("base", "fall", "fwd")}

    for db_path in db_paths:
        if not os.path.exists(db_path):
            print(f"  {os.path.basename(db_path):<28}  (file not found)")
            continue
        rows, trades = load_data(db_path)
        if not trades:
            print(f"  {os.path.basename(db_path):<28}  (no resolved trades)")
            continue
        vol  = build_vol_at_entry(rows, trades)
        base = simulate(trades, vol, 999,         999,          999)
        fall = simulate(trades, vol, VOL_BID_MAX, RANGE_BID_MAX, OBI_VOL_MAX, weekday_only=False)
        fwd  = simulate(trades, vol, VOL_BID_MAX, RANGE_BID_MAX, OBI_VOL_MAX, weekday_only=True)
        name = os.path.basename(db_path)

        # Count weekend trades in this dataset
        we_trades  = sum(1 for t in trades if _in_weekend_session(t.created_at))
        we_losses  = sum(1 for t in trades if t.outcome == "LOSS" and _in_weekend_session(t.created_at))

        print(_fmt_row(name,       "BASE",     base))
        print(_fmt_row("",         "FILT-ALL", fall))
        print(_fmt_row("",         "FILT-WD",  fwd,
                       f"(we: {we_trades}t/{we_losses}L unfiltered)"))
        print()

        for sc, r in (("base", base), ("fall", fall), ("fwd", fwd)):
            for k in keys:
                agg[sc][k] += r.get(k, 0)
            agg[sc]["pnl"] += r["pnl"]

    # Compute aggregated WR/EV
    for d in agg.values():
        d["wr"] = d["wins"] / d["trades"] if d["trades"] else 0.0
        d["ev"] = d["pnl"]  / d["trades"] if d["trades"] else 0.0

    label = f"AGGREGATE ({len(db_paths)} files)"
    print(f"  {SEP2}")
    print(_fmt_row(label, "BASE",     agg["base"]))
    print(_fmt_row("",    "FILT-ALL", agg["fall"]))
    print(_fmt_row("",    "FILT-WD",  agg["fwd"]))

    b, fa, fw = agg["base"], agg["fall"], agg["fwd"]
    print(f"\n  EV/trade  :  BASE {b['ev']:+.4f}"
          f"  →  FILT-ALL {fa['ev']:+.4f}  (Δ{fa['ev']-b['ev']:+.4f})"
          f"  →  FILT-WD  {fw['ev']:+.4f}  (Δ{fw['ev']-b['ev']:+.4f})")
    print(f"  Losses    :  BASE {b['losses']}"
          f"  →  FILT-ALL {fa['losses']}"
          f"  →  FILT-WD  {fw['losses']}")
    extra_trades = fw['trades'] - fa['trades']
    print(f"  Extra trades FILT-WD vs FILT-ALL: {extra_trades:+d} "
          f"(weekend trades reintroduced)\n")


# ─── Sweep calibration (--sweep mode) ────────────────────────────────────────

def run_sweep(db_path: str) -> None:

    print(f"\n{SEP}")
    print(f"  CALIBRATION — FULL SWEEP — {os.path.basename(db_path)}")
    print(SEP)

    rows, trades = load_data(db_path)
    vol = build_vol_at_entry(rows, trades)
    base = simulate(trades, vol, 999, 999, 999)

    print(f"\n  {len(rows):,} snapshots, {len(trades)} trades  |  "
          f"Window {WINDOW}x5s={WINDOW*5}s")
    print(f"  Baseline : {base['trades']} trades, {base['losses']} losses, "
          f"WR={base['wr']*100:.1f}%, PnL={base['pnl']:+.2f}, EV={base['ev']:+.4f}")

    # Indicators at the time of losses
    print(f"\n{SEP2}")
    print("  INDICATORS AT LOSS ENTRIES")
    print(f"{SEP2}")
    print(f"  {'ID':>4}  {'date':<20}  {'vol_bid':>8}  {'range':>8}  {'obi_vol':>8}  {'n':>4}")
    print(f"  {'-'*4}  {'-'*20}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*4}")
    for t in trades:
        if t.outcome == "LOSS":
            v = vol.get(t.id, {})
            dt = datetime.utcfromtimestamp(t.created_at / 1000).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  {t.id:>4}  {dt}  {v.get('vol_bid',0):>8.4f}  "
                  f"{v.get('range_bid',0):>8.4f}  {v.get('vol_obi',0):>8.4f}  "
                  f"{v.get('n_snaps',0):>4}")

    # Distribution WINS vs LOSSES
    def pct(lst: list, p: int) -> float:
        s = sorted(lst)
        return s[int(len(s) * p / 100)] if s else 0.0

    wins_vb = [vol[t.id]["vol_bid"]   for t in trades if t.outcome == "WIN"  and t.id in vol]
    wins_rb = [vol[t.id]["range_bid"] for t in trades if t.outcome == "WIN"  and t.id in vol]
    loss_vb = [vol[t.id]["vol_bid"]   for t in trades if t.outcome == "LOSS" and t.id in vol]
    loss_rb = [vol[t.id]["range_bid"] for t in trades if t.outcome == "LOSS" and t.id in vol]

    print(f"\n{SEP2}")
    print("  DISTRIBUTION vol_bid / range_bid: WINS vs LOSSES")
    print(f"{SEP2}")
    print(f"  {'':12} {'median':>10}  {'p75':>8}  {'p90':>8}  {'p95':>8}  {'max':>8}")
    print(f"  {'-'*12} {'-'*10}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")
    for lbl, lst in [("vol_bid WIN", wins_vb), ("vol_bid LOSS", loss_vb),
                     ("range WIN",   wins_rb), ("range LOSS",  loss_rb)]:
        print(f"  {lbl:<12} {pct(lst,50):>10.4f}  {pct(lst,75):>8.4f}  "
              f"{pct(lst,90):>8.4f}  {pct(lst,95):>8.4f}  {max(lst or [0]):>8.4f}")

    # Sweep vol_bid
    for metric, sweep_fn in [
        ("vol_bid",   lambda thr: simulate(trades, vol, thr,  999,  999)),
        ("range_bid", lambda thr: simulate(trades, vol, 999,  thr,  999)),
    ]:
        print(f"\n{SEP2}")
        print(f"  SWEEP {metric}")
        print(f"{SEP2}")
        print(f"  {'threshold':>8}  {'trades':>7}  {'losses':>7}  {'skip':>6}  "
              f"{'WR':>7}  {'PnL':>9}  {'EV':>8}")
        thresholds = VOL_BID_THRESHOLDS if metric == "vol_bid" else RANGE_BID_THRESHOLDS
        for thr in thresholds:
            r = sweep_fn(thr)
            lbl = f"{thr:.2f}" if thr < 999 else "∞ (OFF)"
            print(f"  {lbl:>8}  {r['trades']:>7}  {r['losses']:>7}  {r['skipped']:>6}  "
                  f"{r['wr']*100:>6.1f}%  {r['pnl']:>+9.2f}  {r['ev']:>+8.4f}")

    # Top 10 combinations
    print(f"\n{SEP2}")
    print("  TOP 10 COMBINATIONS (vol_bid x range_bid x obi_vol)")
    print(f"{SEP2}")
    results = []
    for vb in VOL_BID_THRESHOLDS[:-1]:
        for rb in RANGE_BID_THRESHOLDS[:-1]:
            for ov in OBI_VOL_THRESHOLDS[:-1]:
                r = simulate(trades, vol, vb, rb, ov)
                if r["trades"] >= 10:
                    results.append((r["ev"], vb, rb, ov, r))
    results.sort(reverse=True)
    print(f"  {'vol_bid':>8}  {'range':>7}  {'obi_v':>7}  "
          f"{'trades':>7}  {'losses':>7}  {'skip':>6}  {'WR':>7}  {'PnL':>9}  {'EV':>8}")
    print(f"  {'-'*8}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*6}  {'-'*7}  {'-'*9}  {'-'*8}")
    for ev, vb, rb, ov, r in results[:10]:
        print(f"  {vb:>8.2f}  {rb:>7.2f}  {ov:>7.2f}  "
              f"{r['trades']:>7}  {r['losses']:>7}  {r['skipped']:>6}  "
              f"{r['wr']*100:>6.1f}%  {r['pnl']:>+9.2f}  {ev:>+8.4f}")

    if results:
        ev, vb, rb, ov, best = results[0]
        print(f"\n  ★  vol_bid<={vb}  range<={rb}  obi_vol<={ov}")
        print(f"     Baseline -> Filter: "
              f"{base['trades']}t/{base['losses']}L/WR{base['wr']*100:.1f}%/PnL{base['pnl']:+.2f}/EV{base['ev']:+.4f}"
              f"  →  "
              f"{best['trades']}t/{best['losses']}L/WR{best['wr']*100:.1f}%/PnL{best['pnl']:+.2f}/EV{ev:+.4f}")
    print()


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sweep_mode = "--sweep" in sys.argv
    db_args: list[str] = []
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--db":
            i += 1
            while i < len(args) and not args[i].startswith("--"):
                db_args.append(args[i])
                i += 1
        else:
            i += 1

    if sweep_mode:
        run_sweep(db_args[0] if db_args else DEFAULT_DBS[0])
    else:
        run_comparison(db_args if db_args else DEFAULT_DBS)
