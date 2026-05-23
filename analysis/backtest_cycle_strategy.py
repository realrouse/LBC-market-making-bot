#!/usr/bin/env python3
"""
Long-term BTC cycle strategy backtest.

Macro regime logic
------------------
  BULL  target: 90% BTC / 10% cash
  BEAR  target: 10% BTC / 90% cash

Regime transitions
------------------
  → BEAR (top signals, valid only after --min-bull days in BULL):
      · Mayer Multiple (day high / 200DMA) > --top-mm  [default 2.4]
      · Pi Cycle Top: 111DMA crosses above 2×350DMA
      · Close drops below 350DMA (after --min-bull days in bull)

  → BULL (bottom signals, valid only after --min-bear days in BEAR):
      · Mayer Multiple (day low / 200DMA) < --bot-mm   [default 0.8]
      · Day low touches below 200-week SMA (~1400 DMA)

Trade execution
---------------
  BEAR → sell BTC on rebounds : price rises --rebound from recent day low
  BULL → buy  BTC on drawbacks: price falls --drawback from recent day high
  Each trigger trades --tranche of the remaining gap toward the regime target.

Usage
-----
    python3 scripts/backtest_cycle_strategy.py
    python3 scripts/backtest_cycle_strategy.py data/BTCUSDT3197d_20172026.db
    python3 scripts/backtest_cycle_strategy.py --capital 10000 --rebound 0.05 --drawback 0.05
    python3 scripts/backtest_cycle_strategy.py --min-bull 180 --min-bear 60
"""

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# Known BTC halving timestamps (UTC, seconds)
HALVINGS_TS = [
    1354060800,   # 2012-11-28 H1
    1468022400,   # 2016-07-09 H2
    1589155200,   # 2020-05-11 H3
    1713571200,   # 2024-04-20 H4
]


def days_since_last_halving(ts_sec: int) -> int:
    """Days elapsed since the most recent halving at or before ts_sec."""
    past = [h for h in HALVINGS_TS if h <= ts_sec]
    if not past:
        return 0
    return (ts_sec - past[-1]) // 86400


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULTS = dict(
    capital          = 10_000.0,
    btc_alloc_start  = 0.50,      # 50/50 at start
    target_bull      = 0.90,      # BTC fraction target in bull regime
    target_bear      = 0.10,      # BTC fraction target in bear regime
    top_mm_thresh    = 2.4,       # Mayer Multiple (day high) top threshold
    bot_mm_thresh    = 0.8,       # Mayer Multiple (day low) bottom threshold
    rebound_pct      = 0.05,      # sell tranche when price bounces 5% from recent low
    drawback_pct     = 0.05,      # buy  tranche when price dips  5% from recent high
    tranche_frac     = 0.25,      # fraction of remaining gap per tranche
    fee_rate         = 0.001,     # 0.1% per trade
    min_bull_days    = 180,       # min days in BULL before any top signal can flip to BEAR
    min_bear_days    = 60,        # min days in BEAR before any bottom signal can flip to BULL
    # Prudence mode: halving-relative tiers for progressive distribution
    prudence         = False,
    prudence_t1_days   = 400,    # caution tier: days post-halving
    prudence_t1_target = 0.75,   # caution: reduce BTC target to 75%
    prudence_t1_rebound= 0.03,   # caution: tighter rebound trigger
    prudence_t1_tranche= 0.15,   # caution: smaller tranche
    prudence_t2_days   = 480,    # high-risk tier: ~90% of avg 534d halving→top window
    prudence_t2_target = 0.50,   # high-risk: reduce BTC target to 50%
    prudence_t2_rebound= 0.02,   # high-risk: very tight rebound trigger
    prudence_t2_tranche= 0.10,   # high-risk: small tranche
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def _ts_to_date(ts_sec: int) -> str:
    return datetime.fromtimestamp(ts_sec, tz=timezone.utc).strftime("%Y-%m-%d")


def sma(series: list, n: int) -> list:
    out = [None] * (n - 1)
    for i in range(n - 1, len(series)):
        out.append(sum(series[i - n + 1 : i + 1]) / n)
    return out


def load_daily(db_path: str) -> list:
    conn = sqlite3.connect(db_path)
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}

    if "daily" in tables:
        rows = conn.execute(
            "SELECT day_ts, high, low, close FROM daily ORDER BY day_ts"
        ).fetchall()
        conn.close()
        return [{"ts": r[0], "date": _ts_to_date(r[0]),
                 "high": r[1], "low": r[2], "close": r[3]} for r in rows]

    rows = conn.execute("""
        SELECT (ts_ms/86400000)*86400 AS day_ts,
               MAX(high) AS high, MIN(low) AS low,
               MAX(ts_ms) AS last_ms
        FROM klines GROUP BY day_ts ORDER BY day_ts
    """).fetchall()
    phs = ",".join("?" * len(rows))
    close_map = dict(conn.execute(
        f"SELECT ts_ms, close FROM klines WHERE ts_ms IN ({phs})",
        [r[3] for r in rows],
    ).fetchall())
    conn.close()
    result = []
    for r in rows:
        close_px = close_map.get(r[3])
        if close_px is None:
            continue
        result.append({
            "ts":    r[0],
            "date":  _ts_to_date(r[0]),
            "high":  r[1],
            "low":   r[2],
            "close": close_px,
        })
    return result


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------
def run_backtest(daily: list, p: dict) -> dict:
    closes = [d["close"] for d in daily]
    highs  = [d["high"]  for d in daily]
    lows   = [d["low"]   for d in daily]
    dates  = [d["date"]  for d in daily]
    n      = len(closes)

    ma111   = sma(closes, 111)
    ma200d  = sma(closes, 200)
    ma350   = sma(closes, 350)
    ma350x2 = [v * 2 if v is not None else None for v in ma350]
    ma200w  = sma(closes, 200 * 7)

    # Portfolio
    price0 = closes[0]
    usd    = p["capital"] * (1 - p["btc_alloc_start"])
    btc    = p["capital"] * p["btc_alloc_start"] / price0

    fees_paid = 0.0
    trades    = []

    def pv(px):
        return usd + btc * px

    def btc_frac(px):
        v = pv(px)
        return (btc * px) / v if v > 0 else 0.0

    def sell(amt_btc, px, reason, date):
        nonlocal btc, usd, fees_paid
        amt_btc = min(amt_btc, btc)
        if amt_btc < 1e-8:
            return
        proceeds = amt_btc * px
        fee = proceeds * p["fee_rate"]
        usd += proceeds - fee
        btc -= amt_btc
        fees_paid += fee
        trades.append(dict(date=date, action="SELL", btc=amt_btc,
                           price=px, usd=proceeds - fee, fee=fee, reason=reason))

    def buy(amt_usd, px, reason, date):
        nonlocal btc, usd, fees_paid
        amt_usd = min(amt_usd, usd)
        if amt_usd < 0.01 or px <= 0:
            return
        fee = amt_usd * p["fee_rate"]
        got = (amt_usd - fee) / px
        usd -= amt_usd
        btc += got
        fees_paid += fee
        trades.append(dict(date=date, action="BUY", btc=got,
                           price=px, usd=amt_usd - fee, fee=fee, reason=reason))

    def sell_tranche(px, reason, date):
        cur = btc_frac(px)
        gap = cur - p["target_bear"]
        if gap < 0.005:
            return
        sell_btc = (gap * p["tranche_frac"] * pv(px)) / px
        sell(sell_btc, px, reason, date)

    def buy_tranche(px, reason, date):
        cur = btc_frac(px)
        gap = p["target_bull"] - cur
        if gap < 0.005:
            return
        buy_usd = gap * p["tranche_frac"] * pv(px)
        buy(buy_usd, px, reason, date)

    # Regime state
    regime         = "bull"
    days_in_regime = 0
    recent_high    = highs[0]
    recent_low     = lows[0]
    regime_history = []
    pv_series      = []

    for i in range(n):
        px   = closes[i]
        hi   = highs[i]
        lo   = lows[i]
        date = dates[i]

        days_in_regime += 1

        # --- Top signals (only after min_bull_days in bull) ---
        # 350DMA cross-down intentionally excluded: it fires mid-bull-run corrections
        # and causes sell-low / buy-higher sequences.
        top_sig = False
        if regime == "bull" and days_in_regime >= p["min_bull_days"]:
            if ma200d[i] is not None and hi / ma200d[i] >= p["top_mm_thresh"]:
                top_sig = True
            if (i > 0 and ma111[i] is not None and ma350x2[i] is not None
                    and ma111[i-1] is not None and ma350x2[i-1] is not None):
                if ma111[i-1] < ma350x2[i-1] and ma111[i] >= ma350x2[i]:
                    top_sig = True

        # --- Bottom signals (only after min_bear_days in bear) ---
        bot_sig = False
        if regime == "bear" and days_in_regime >= p["min_bear_days"]:
            if ma200d[i] is not None and lo / ma200d[i] <= p["bot_mm_thresh"]:
                bot_sig = True
            if ma200w[i] is not None and lo < ma200w[i]:
                bot_sig = True

        # --- Regime transitions ---
        prev = regime
        if top_sig:
            regime      = "bear"
            recent_low  = lo
            days_in_regime = 0
        elif bot_sig:
            regime      = "bull"
            recent_high = hi
            recent_low  = lo
            days_in_regime = 0

        if regime != prev:
            regime_history.append({"date": date, "regime": regime, "price": px,
                                   "prev": prev})

        # --- Prudence tiers (BULL only, keyed on days since last halving) ---
        # Halving→top historically: 526d / 548d / 529d  (avg 534d)
        # Tier 1 at 400d (~75% of avg window): caution, step BTC target down to 75%
        # Tier 2 at 480d (~90% of avg window): high-risk, step BTC target down to 50%
        eff_target   = p["target_bull"]
        eff_rebound  = p["rebound_pct"]
        eff_drawback = p["drawback_pct"]
        eff_tranche  = p["tranche_frac"]
        prudence_tier = 0
        if p["prudence"] and regime == "bull":
            d_halv = days_since_last_halving(daily[i]["ts"])
            if d_halv >= p["prudence_t2_days"]:
                eff_target    = p["prudence_t2_target"]
                eff_rebound   = p["prudence_t2_rebound"]
                eff_tranche   = p["prudence_t2_tranche"]
                prudence_tier = 2
            elif d_halv >= p["prudence_t1_days"]:
                eff_target    = p["prudence_t1_target"]
                eff_rebound   = p["prudence_t1_rebound"]
                eff_tranche   = p["prudence_t1_tranche"]
                prudence_tier = 1

        # --- Execute trades ---
        if regime == "bear":
            recent_low = min(recent_low, lo)
            if hi >= recent_low * (1 + p["rebound_pct"]):
                sell_tranche(px, f"rebound +{p['rebound_pct']*100:.0f}%", date)
                recent_low = lo

        elif regime == "bull":
            recent_high = max(recent_high, hi)
            recent_low  = min(recent_low, lo)
            cur_frac    = btc_frac(px)

            if prudence_tier > 0 and cur_frac > eff_target + 0.005:
                # Prudence sell: BTC above stepped-down target → distribute on rebounds
                if hi >= recent_low * (1 + eff_rebound):
                    gap      = cur_frac - eff_target
                    sell_btc = (gap * eff_tranche * pv(px)) / px
                    sell(sell_btc, px,
                         f"prudence-T{prudence_tier} rebound +{eff_rebound*100:.0f}%", date)
                    recent_low = lo
            else:
                # Normal BULL: accumulate on drawbacks toward effective target
                if lo <= recent_high * (1 - eff_drawback):
                    gap = eff_target - cur_frac
                    if gap >= 0.005:
                        buy_usd = gap * eff_tranche * pv(px)
                        buy(buy_usd, px, f"drawback -{eff_drawback*100:.0f}%", date)
                    recent_high = hi

        pv_series.append(pv(px))

    final_px    = closes[-1]
    final_pv    = pv(final_px)
    btc_hold_pv = (p["capital"] / closes[0]) * final_px

    def max_drawdown(series):
        peak = series[0]
        mdd  = 0.0
        for v in series:
            peak = max(peak, v)
            mdd  = max(mdd, (peak - v) / peak)
        return mdd

    btc_hold_series = [p["capital"] / closes[0] * c for c in closes]
    years = (n - 1) / 365.25

    return dict(
        final_pv      = final_pv,
        btc_hold_pv   = btc_hold_pv,
        fees_paid     = fees_paid,
        trades        = trades,
        regime_history= regime_history,
        pv_series     = pv_series,
        max_dd        = max_drawdown(pv_series),
        max_dd_hold   = max_drawdown(btc_hold_series),
        years         = years,
        start_date    = dates[0],
        end_date      = dates[-1],
        start_price   = closes[0],
        end_price     = closes[-1],
        final_btc     = btc,
        final_usd     = usd,
        final_btc_frac= btc_frac(final_px),
    )


# ---------------------------------------------------------------------------
# Comparison helper
# ---------------------------------------------------------------------------
def _run_comparison(daily: list, base_p: dict) -> None:
    """Run V1, V2, V2+prudence and print a side-by-side summary table."""
    configs = [
        ("V1 (5%/25%)",         {**base_p, "rebound_pct": 0.05, "drawback_pct": 0.05,
                                  "tranche_frac": 0.25, "prudence": False}),
        ("V2 (4%/20%)",         {**base_p, "rebound_pct": 0.04, "drawback_pct": 0.04,
                                  "tranche_frac": 0.20, "prudence": False}),
        ("V2+prudence",         {**base_p, "rebound_pct": 0.04, "drawback_pct": 0.04,
                                  "tranche_frac": 0.20, "prudence": True}),
    ]
    results = []
    for name, p in configs:
        r = run_backtest(daily, p)
        mult  = r["final_pv"] / p["capital"]
        hold  = r["btc_hold_pv"] / p["capital"]
        cagr  = (mult ** (1 / r["years"]) - 1) * 100
        hcagr = (hold ** (1 / r["years"]) - 1) * 100
        calmar = cagr / (r["max_dd"] * 100) if r["max_dd"] > 0 else 0
        results.append({
            "name": name, "r": r, "p": p,
            "mult": mult, "hold": hold, "cagr": cagr, "hcagr": hcagr,
            "calmar": calmar,
        })

    print("═" * 80)
    print("STRATEGY COMPARISON  (V1 vs V2 vs V2+prudence)")
    print(f"  Period: {daily[0]['date']} → {daily[-1]['date']}"
          f"  ({results[0]['r']['years']:.1f} yr)")
    print("═" * 80)
    print(f"  {'Config':18s}  {'Return':>8s}  {'CAGR':>6s}  {'MaxDD':>7s}  "
          f"{'Calmar':>7s}  {'Trades':>7s}  {'vs B&H':>8s}  {'Fees':>8s}")
    print(f"  {'-'*76}")
    for res in results:
        r   = res["r"]
        vs  = (res["mult"] / res["hold"] - 1) * 100
        print(f"  {res['name']:18s}  ×{res['mult']:>6.1f}   {res['cagr']:>5.1f}%"
              f"  {r['max_dd']*100:>6.1f}%  {res['calmar']:>7.2f}"
              f"  {len(r['trades']):>7}  {vs:>+7.1f}%  ${r['fees_paid']:>7,.0f}")
    print()
    bh_mult = results[0]["r"]["btc_hold_pv"] / results[0]["p"]["capital"]
    bh_cagr = results[0]["hcagr"]
    bh_dd   = results[0]["r"]["max_dd_hold"] * 100
    bh_cal  = bh_cagr / bh_dd if bh_dd > 0 else 0
    print(f"  {'BTC buy&hold':18s}  ×{bh_mult:>6.1f}   {bh_cagr:>5.1f}%"
          f"  {bh_dd:>6.1f}%  {bh_cal:>7.2f}  {'n/a':>7}  {'—':>8s}  {'$0':>8s}")
    print()
    print("  Prudence tiers (BULL only, halving-relative):")
    prd = results[2]["p"]
    print(f"    T0 (<{prd['prudence_t1_days']}d post-halving): 90% BTC target,"
          f" {prd['drawback_pct']*100:.0f}% drawback, {prd['tranche_frac']*100:.0f}% tranche")
    print(f"    T1 ({prd['prudence_t1_days']}-{prd['prudence_t2_days']}d): "
          f"{prd['prudence_t1_target']*100:.0f}% BTC target,"
          f" {prd['prudence_t1_rebound']*100:.0f}% rebound, {prd['prudence_t1_tranche']*100:.0f}% tranche")
    print(f"    T2 (>{prd['prudence_t2_days']}d): "
          f"{prd['prudence_t2_target']*100:.0f}% BTC target,"
          f" {prd['prudence_t2_rebound']*100:.0f}% rebound, {prd['prudence_t2_tranche']*100:.0f}% tranche")
    print(f"    (Halving→top avg: 534d. T1 at 75%, T2 at 90% of expected window)")
    print()
    for res in results:
        r = res["r"]
        sells = [t for t in r["trades"] if t["action"] == "SELL"]
        buys  = [t for t in r["trades"] if t["action"] == "BUY"]
        avg_s = sum(t["price"] for t in sells) / max(len(sells), 1)
        avg_b = sum(t["price"] for t in buys)  / max(len(buys),  1)
        print(f"  {res['name']:18s}  avg sell=${avg_s:>8,.0f}  avg buy=${avg_b:>8,.0f}"
              f"  ({len(sells)} sells / {len(buys)} buys)")
    print()

    print("═" * 80)
    print("REGIME TRANSITIONS (shared across all configs)")
    print("═" * 80)
    for ev in results[0]["r"]["regime_history"]:
        arrow = "↗ → BULL" if ev["regime"] == "bull" else "↘ → BEAR"
        print(f"  {ev['date']}  {ev['prev'].upper():4s} {arrow}  price=${ev['price']:>10,.0f}")
    print()


# ---------------------------------------------------------------------------
# CLI / display
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="BTC cycle strategy backtest",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("db", nargs="?",
                        default=str(Path(__file__).resolve().parent.parent
                                    / "data" / "BTCUSDT3197d_20172026.db"))
    parser.add_argument("--capital",    type=float, default=DEFAULTS["capital"])
    parser.add_argument("--start-btc",  type=float, default=DEFAULTS["btc_alloc_start"],
                        help="Starting BTC allocation fraction (0-1)")
    parser.add_argument("--top-mm",     type=float, default=DEFAULTS["top_mm_thresh"],
                        help="Mayer Multiple (day high) threshold for top signal")
    parser.add_argument("--bot-mm",     type=float, default=DEFAULTS["bot_mm_thresh"],
                        help="Mayer Multiple (day low) threshold for bottom signal")
    parser.add_argument("--rebound",    type=float, default=DEFAULTS["rebound_pct"],
                        help="Sell tranche when price bounces this %% from recent low")
    parser.add_argument("--drawback",   type=float, default=DEFAULTS["drawback_pct"],
                        help="Buy tranche when price dips this %% from recent high")
    parser.add_argument("--tranche",    type=float, default=DEFAULTS["tranche_frac"],
                        help="Fraction of gap to trade per trigger (0-1)")
    parser.add_argument("--min-bull",   type=int,   default=DEFAULTS["min_bull_days"],
                        help="Min days in BULL before any top signal can fire")
    parser.add_argument("--min-bear",   type=int,   default=DEFAULTS["min_bear_days"],
                        help="Min days in BEAR before any bottom signal can fire")
    parser.add_argument("--prudence",   action="store_true",
                        help="Enable halving-relative prudence tiers (progressive distribution near top)")
    parser.add_argument("--compare",    action="store_true",
                        help="Run V1, V2, and V2+prudence side by side and print a summary table")
    args = parser.parse_args()

    p = dict(DEFAULTS)
    p["capital"]       = args.capital
    p["btc_alloc_start"]= args.start_btc
    p["top_mm_thresh"] = args.top_mm
    p["bot_mm_thresh"] = args.bot_mm
    p["rebound_pct"]   = args.rebound
    p["drawback_pct"]  = args.drawback
    p["tranche_frac"]  = args.tranche
    p["min_bull_days"] = args.min_bull
    p["min_bear_days"] = args.min_bear
    p["prudence"]      = args.prudence

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    print(f"Loading daily klines from {args.db} …")
    daily = load_daily(args.db)
    print(f"  {len(daily)} daily bars  ({daily[0]['date']} → {daily[-1]['date']})\n")

    if args.compare:
        _run_comparison(daily, p)
        return

    r = run_backtest(daily, p)

    sells = [t for t in r["trades"] if t["action"] == "SELL"]
    buys  = [t for t in r["trades"] if t["action"] == "BUY"]

    print("═" * 70)
    print("REGIME TRANSITIONS")
    print("═" * 70)
    if not r["regime_history"]:
        print("  (no regime changes)")
    for ev in r["regime_history"]:
        arrow = "↗ → BULL" if ev["regime"] == "bull" else "↘ → BEAR"
        print(f"  {ev['date']}  {ev['prev'].upper():4s} {arrow}"
              f"  price=${ev['price']:>10,.0f}")

    print()
    print("═" * 70)
    print(f"TRADES  ({len(r['trades'])} total | {len(sells)} sells / {len(buys)} buys)")
    print("═" * 70)
    avg_sell = sum(t["price"] for t in sells) / max(len(sells), 1)
    avg_buy  = sum(t["price"] for t in buys)  / max(len(buys),  1)
    print(f"  Avg SELL price : ${avg_sell:>10,.0f}")
    print(f"  Avg BUY  price : ${avg_buy:>10,.0f}")
    print(f"  Fees paid      : ${r['fees_paid']:>10,.2f}")

    head = r["trades"][:4]
    tail = r["trades"][-4:]
    omit = len(r["trades"]) - 8
    for t in head:
        sign = "-" if t["action"] == "SELL" else "+"
        print(f"    {t['date']}  {t['action']:4s}  {sign}{t['btc']:.4f} BTC"
              f"  @ ${t['price']:>10,.0f}  ({t['reason']})")
    if omit > 0:
        print(f"    … {omit} more trades …")
    for t in tail:
        sign = "-" if t["action"] == "SELL" else "+"
        print(f"    {t['date']}  {t['action']:4s}  {sign}{t['btc']:.4f} BTC"
              f"  @ ${t['price']:>10,.0f}  ({t['reason']})")

    strat_mult  = r["final_pv"]    / p["capital"]
    hold_mult   = r["btc_hold_pv"] / p["capital"]
    strat_cagr  = (strat_mult ** (1 / r["years"]) - 1) * 100
    hold_cagr   = (hold_mult  ** (1 / r["years"]) - 1) * 100
    calmar_s    = strat_cagr / (r["max_dd"]      * 100) if r["max_dd"]      > 0 else 0
    calmar_h    = hold_cagr  / (r["max_dd_hold"] * 100) if r["max_dd_hold"] > 0 else 0

    print()
    print("═" * 70)
    print("PERFORMANCE")
    print("═" * 70)
    print(f"  Period         : {r['start_date']} → {r['end_date']}"
          f"  ({r['years']:.1f} yr)")
    print(f"  BTC price      : ${r['start_price']:,.0f} → ${r['end_price']:,.0f}"
          f"  ({(r['end_price']/r['start_price']-1)*100:+.0f}%)")
    print()
    print(f"                     Final value      ×    CAGR   MaxDD  Calmar")
    print(f"  Strategy       : ${r['final_pv']:>12,.0f}   ×{strat_mult:5.1f}"
          f"  {strat_cagr:5.1f}%  {r['max_dd']*100:5.1f}%  {calmar_s:.2f}")
    print(f"  BTC buy&hold   : ${r['btc_hold_pv']:>12,.0f}   ×{hold_mult:5.1f}"
          f"  {hold_cagr:5.1f}%  {r['max_dd_hold']*100:5.1f}%  {calmar_h:.2f}")
    print(f"  USD hold       : ${p['capital']:>12,.0f}   ×{1.0:5.1f}")
    print()
    print(f"  Final position : {r['final_btc']:.4f} BTC + ${r['final_usd']:,.2f} USD"
          f"  ({r['final_btc_frac']*100:.1f}% BTC)")
    vs = (strat_mult / hold_mult - 1) * 100
    print(f"  vs BTC hold    : {vs:+.1f}%")


if __name__ == "__main__":
    main()
