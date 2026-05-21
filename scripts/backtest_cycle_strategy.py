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
            days_in_regime = 0

        if regime != prev:
            regime_history.append({"date": date, "regime": regime, "price": px,
                                   "prev": prev})

        # --- Execute trades ---
        if regime == "bear":
            recent_low = min(recent_low, lo)
            if hi >= recent_low * (1 + p["rebound_pct"]):
                sell_tranche(px, f"rebound +{p['rebound_pct']*100:.0f}%", date)
                recent_low = lo

        elif regime == "bull":
            recent_high = max(recent_high, hi)
            if lo <= recent_high * (1 - p["drawback_pct"]):
                buy_tranche(px, f"drawback -{p['drawback_pct']*100:.0f}%", date)
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

    if not Path(args.db).exists():
        sys.exit(f"DB not found: {args.db}")

    print(f"Loading daily klines from {args.db} …")
    daily = load_daily(args.db)
    print(f"  {len(daily)} daily bars  ({daily[0]['date']} → {daily[-1]['date']})\n")

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
