#!/usr/bin/env python3
"""
backtest_accumulation.py — backtest the BTC accumulation strategy
on historical Binance OHLCV data (1h klines with taker-buy volume).

OBI proxy: (taker_buy_vol / total_vol - 0.5) * 2  → [-1, +1]
  < -0.50 sustained → scale-in signal (same threshold as live bot)

Profit bands / rebuys: checked against candle high/low.
  Bullish candle  (close > open): assume low first, then high
  Bearish candle  (close < open): assume high first, then low

Usage:
  python3 scripts/backtest_accumulation.py
  python3 scripts/backtest_accumulation.py --start 2024-09-01 --end 2025-12-31
  python3 scripts/backtest_accumulation.py --start 2024-09-01 --capital 1000 --tf 1h
  python3 scripts/backtest_accumulation.py --trades   # show trade log
"""

import argparse, json, math, sys, time, urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── CLI ────────────────────────────────────────────────────────────────────
ap = argparse.ArgumentParser(description="Accumulation strategy backtest")
ap.add_argument("--start",   default="2024-09-01", help="Start date YYYY-MM-DD")
ap.add_argument("--end",     default=None,          help="End date YYYY-MM-DD (default: today)")
ap.add_argument("--tf",      default="1h",          help="Kline timeframe (default 1h)")
ap.add_argument("--capital", type=float, default=None, help="Override capital_usdt")
ap.add_argument("--trades",  action="store_true",   help="Print full trade log")
ap.add_argument("--strategy", default=None,         help="Path to btc_accumulation.json")
ap.add_argument("--proxy",   default="dip",         choices=["obi","dip"],
                help="Scale-in signal proxy: obi=taker-buy EMA, dip=price drop from N-candle high (default: dip)")
ap.add_argument("--dip-pct",      type=float, default=4.0,  help="Dip proxy: pct drop from recent high to trigger (default 4.0)")
ap.add_argument("--dip-lookback", type=int,   default=72,   help="Dip proxy: rolling high lookback in candles (default 72)")
args = ap.parse_args()

# ── Load strategy params ───────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR   = SCRIPT_DIR.parent
DEFAULT_CFG = REPO_DIR / "strategies" / "accumulation" / "btc_accumulation.json"
cfg_path = Path(args.strategy) if args.strategy else DEFAULT_CFG

with open(cfg_path) as f:
    P = json.load(f)

if args.capital:
    P["capital_usdt"] = args.capital

CAPITAL        = P["capital_usdt"]
INIT_USDT      = P["initial_stake_usdt"]
SCALE_USDT     = P["scale_in_usdt"]
DIP_FACTOR     = P.get("scale_in_dip_factor", 0.5)
MAX_MULT       = P.get("scale_in_max_mult", 3.0)
MAX_INV_PCT    = P["max_invested_pct"]
OBI_ALPHA      = P["obi_ema_alpha"]
OBI_THRESH     = P["obi_entry_thresh"]
MIN_IV_S       = P["min_scale_interval_s"]
BANDS          = sorted(P["profit_bands_pct"])
SELL_FRAC      = P["sell_fraction"]
MIN_HOLD_PCT   = P.get("min_holdings_pct", 0.30)
RB_MIN         = P.get("rebuy_discount_min_pct", 0.15) / 100
RB_MAX         = P.get("rebuy_discount_max_pct", 1.00) / 100
RB_MULT        = P.get("rebuy_spread_mult", 3.0)
FEE              = P["fee_spot"]
SYMBOL           = P["symbol"]
MAX_AVG_ENTRY_MULT = P.get("max_avg_entry_mult", 1.20)
REBUY_MAX_AGE_S    = int(P.get("rebuy_max_age_days", 60) * 86400)

# ── Binance kline fetch ────────────────────────────────────────────────────
BASE = "https://api.binance.com/api/v3/klines"

def ts_ms(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int):
    rows = []
    cur  = start_ms
    print(f"Fetching {symbol} {interval} klines from Binance…", file=sys.stderr)
    while cur < end_ms:
        url = f"{BASE}?symbol={symbol}&interval={interval}&startTime={cur}&endTime={end_ms}&limit=1000"
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                batch = json.loads(r.read())
        except Exception as e:
            print(f"  fetch error: {e}", file=sys.stderr); time.sleep(2); continue
        if not batch:
            break
        rows.extend(batch)
        cur = batch[-1][6] + 1   # next open after last close_time
        sys.stderr.write(f"  …{len(rows)} candles, last={datetime.utcfromtimestamp(batch[-1][0]/1000).strftime('%Y-%m-%d')}\r")
        time.sleep(0.12)         # stay well under rate limit
    print(f"\n  total: {len(rows)} candles", file=sys.stderr)
    return rows

start_ms = ts_ms(args.start)
end_ms   = ts_ms(args.end) if args.end else int(datetime.now(timezone.utc).timestamp() * 1000)
raw = fetch_klines(SYMBOL, args.tf, start_ms, end_ms)

# ── State ──────────────────────────────────────────────────────────────────
@dataclass
class State:
    holdings_btc:     float = 0.0
    avg_entry:        float = 0.0
    free_usdt:        float = CAPITAL
    obi_ema:          float = 0.0
    spread_ema:       float = 0.002
    last_buy_ts_s:    int   = 0
    initial_done:     bool  = False
    peak_holdings:    float = 0.0
    total_realized:   float = 0.0
    active_bands:     set   = field(default_factory=set)
    pending_rebuys:   list  = field(default_factory=list)  # (rebuy_price, qty_btc, band_pct)

@dataclass
class Trade:
    ts: str; side: str; qty: float; price: float; usdt: float; reason: str

state  = State()
trades: list[Trade] = []
equity_curve = []  # (ts_str, price, portfolio_usdt)

# ── Helpers ────────────────────────────────────────────────────────────────
def portfolio_value(price: float) -> float:
    return state.free_usdt + state.holdings_btc * price

def _scale_in_amount(price: float) -> float:
    if state.avg_entry <= 0:
        return SCALE_USDT
    dip_pct = max(0.0, (state.avg_entry - price) / state.avg_entry)
    mult = min(1.0 + DIP_FACTOR * (dip_pct * 100), MAX_MULT)
    return SCALE_USDT * mult

def _rebuy_discount() -> float:
    d = state.spread_ema * RB_MULT
    return max(RB_MIN, min(RB_MAX, d))

def _buy(price: float, usdt: float, reason: str, ts_s: int) -> bool:
    usdt = min(usdt, state.free_usdt)
    if usdt < 5.0:
        return False
    fee     = usdt * FEE
    net     = usdt - fee
    qty     = net / price
    old_val = state.holdings_btc * state.avg_entry
    state.holdings_btc += qty
    state.avg_entry     = (old_val + net) / state.holdings_btc
    state.free_usdt    -= usdt
    if state.holdings_btc > state.peak_holdings:
        state.peak_holdings = state.holdings_btc
    trades.append(Trade(
        ts=datetime.utcfromtimestamp(ts_s).strftime("%Y-%m-%d %H:%M"),
        side="BUY", qty=qty, price=price, usdt=usdt, reason=reason))
    return True

def _sell(price: float, qty: float, reason: str, ts_s: int) -> float:
    qty = min(qty, state.holdings_btc)
    if qty < 1e-6:
        return 0.0
    gross = qty * price
    fee   = gross * FEE
    net   = gross - fee
    # realized PnL vs avg_entry
    cost  = qty * state.avg_entry
    pnl   = net - cost
    state.total_realized += pnl
    state.holdings_btc   -= qty
    if state.holdings_btc < 1e-8:
        state.holdings_btc = 0.0
        state.avg_entry    = 0.0
        state.active_bands.clear()
        state.pending_rebuys.clear()
    state.free_usdt += net
    trades.append(Trade(
        ts=datetime.utcfromtimestamp(ts_s).strftime("%Y-%m-%d %H:%M"),
        side="SELL", qty=qty, price=price, usdt=net, reason=reason))
    return net

# ── Profit band check (uses candle high) ──────────────────────────────────
def check_profit_bands(high: float, low: float, ts_s: int):
    if state.holdings_btc < 1e-6 or state.avg_entry <= 0:
        return
    floor_btc = state.peak_holdings * MIN_HOLD_PCT

    for band_pct in BANDS:
        if band_pct in state.active_bands:
            continue
        target = state.avg_entry * (1 + band_pct / 100)
        if high >= target:
            max_sell = max(0.0, state.holdings_btc - floor_btc)
            qty      = state.holdings_btc * SELL_FRAC
            qty      = min(qty, max_sell)
            if qty < 1e-6:
                state.active_bands.add(band_pct)
                continue
            sell_price = target
            _sell(sell_price, qty, f"band+{band_pct}%", ts_s)
            state.active_bands.add(band_pct)
            disc     = _rebuy_discount()
            rb_price = sell_price * (1 - disc)
            state.pending_rebuys.append((rb_price, qty, band_pct, ts_s))

# ── Rebuy check (uses candle low) ─────────────────────────────────────────
def check_rebuys(low: float, ts_s: int):
    expired = [r for r in state.pending_rebuys if (ts_s - r[3]) > REBUY_MAX_AGE_S]
    for item in expired:
        state.pending_rebuys.remove(item)
        state.active_bands.discard(item[2])

    filled = []
    for (rb_price, qty, band_pct, created_ts) in state.pending_rebuys:
        if low <= rb_price:
            invested = state.holdings_btc * state.avg_entry if state.avg_entry > 0 else 0
            if (invested + qty * rb_price) / CAPITAL > MAX_INV_PCT:
                continue
            if _buy(rb_price, qty * rb_price, f"rebuy@{band_pct}%", ts_s):
                filled.append((rb_price, qty, band_pct, created_ts))
                state.active_bands.discard(band_pct)
    for item in filled:
        state.pending_rebuys.remove(item)

# ── OBI scale-in check ────────────────────────────────────────────────────
def check_scale_in(price: float, ts_s: int):
    if state.obi_ema >= -OBI_THRESH:
        return
    if (ts_s - state.last_buy_ts_s) < MIN_IV_S:
        return
    if state.avg_entry > 0 and price > state.avg_entry * MAX_AVG_ENTRY_MULT:
        return
    invested = state.holdings_btc * state.avg_entry if state.avg_entry > 0 else 0
    amount   = _scale_in_amount(price)
    if (invested + amount) / CAPITAL > MAX_INV_PCT:
        return
    if _buy(price, amount, "obi_dip", ts_s):
        state.last_buy_ts_s = ts_s
        # Reset bands for new avg_entry but keep pending rebuys
        state.active_bands.clear()

# ── Main candle loop ───────────────────────────────────────────────────────
TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
candle_s = TF_SECONDS.get(args.tf, 3600)

DIP_PCT      = args.dip_pct / 100
DIP_LOOKBACK = args.dip_lookback
recent_highs: list[float] = []   # rolling window for dip proxy

for k in raw:
    open_t_ms    = int(k[0])
    o, h, l, c   = float(k[1]), float(k[2]), float(k[3]), float(k[4])
    volume        = float(k[5])
    taker_buy_vol = float(k[9])
    ts_s          = open_t_ms // 1000

    # OBI proxy: either taker-buy EMA or price-dip from recent high
    if args.proxy == "obi":
        if volume > 0:
            buy_ratio = taker_buy_vol / volume
            obi_raw   = (buy_ratio - 0.5) * 2
        else:
            obi_raw = 0.0
        state.obi_ema = state.obi_ema + OBI_ALPHA * (obi_raw - state.obi_ema)
    else:   # dip proxy
        recent_highs.append(h)
        if len(recent_highs) > DIP_LOOKBACK:
            recent_highs.pop(0)
        rolling_high = max(recent_highs)
        drop_pct = (rolling_high - c) / rolling_high if rolling_high > 0 else 0.0
        # map drop_pct to OBI-like signal: 2% drop → OBI = -1.0
        state.obi_ema = -(drop_pct / DIP_PCT)   # raw (no EMA needed, already smooth)

    # Spread proxy: (high-low)/close
    spread = (h - l) / c if c > 0 else 0.002
    state.spread_ema += 0.05 * (spread - state.spread_ema)

    # Initial buy
    if not state.initial_done:
        usdt = min(INIT_USDT, state.free_usdt)
        if _buy(o, usdt, "initial", ts_s):
            state.last_buy_ts_s = ts_s
            state.initial_done  = True

    # Intracandle path heuristic: bullish → low first then high; bearish → high first then low
    if c >= o:   # bullish candle
        check_rebuys(l, ts_s)
        check_profit_bands(h, l, ts_s)
    else:        # bearish candle
        check_profit_bands(h, l, ts_s)
        check_rebuys(l, ts_s)

    # OBI scale-in on candle open price
    check_scale_in(o, ts_s)

    # Equity snapshot (hourly)
    pv = portfolio_value(c)
    equity_curve.append((datetime.utcfromtimestamp(ts_s).strftime("%Y-%m-%d"), c, pv))

# ── Results ────────────────────────────────────────────────────────────────
if not raw:
    print("No data fetched."); sys.exit(1)

first_price = float(raw[0][4])
last_price  = float(raw[-1][4])
pv_now      = portfolio_value(last_price)
buys        = [t for t in trades if t.side == "BUY"]
sells       = [t for t in trades if t.side == "SELL"]
obi_buys    = [t for t in buys if t.reason == "obi_dip"]
rebuys      = [t for t in buys if t.reason.startswith("rebuy")]
band_sells  = [t for t in sells if t.reason.startswith("band")]

hodl_return = (last_price - first_price) / first_price * 100
strat_return = (pv_now - CAPITAL) / CAPITAL * 100
btc_value    = state.holdings_btc * last_price

print(f"""
╔══════════════════════════════════════════════════════════════════╗
║       ACCUMULATION BACKTEST  {args.start} → {args.end or 'today'}  [{args.proxy}]
╠══════════════════════════════════════════════════════════════════╣
║  PARAMS
║    max_invested_pct : {MAX_INV_PCT:.0%}   max_avg_entry_mult: ×{MAX_AVG_ENTRY_MULT:.2f}
║    dip_pct          : {args.dip_pct:.1f}%  dip_lookback: {args.dip_lookback}h
║    sell_fraction    : {SELL_FRAC:.0%}   rebuy_max_age: {REBUY_MAX_AGE_S//86400}d
╠══════════════════════════════════════════════════════════════════╣
║  MARKET
║    BTC start  : ${first_price:>10,.2f}
║    BTC end    : ${last_price:>10,.2f}
║    Market ret : {hodl_return:>+8.1f}%  (HODL $1000 → ${1000*(1+hodl_return/100):,.0f})
╠══════════════════════════════════════════════════════════════════╣
║  PORTFOLIO
║    Capital    : ${CAPITAL:>10,.2f}
║    Final value: ${pv_now:>10,.2f}  ({strat_return:>+.1f}%)
║    Free USDT  : ${state.free_usdt:>10,.2f}
║    BTC held   : {state.holdings_btc:>12.6f} BTC  (${btc_value:,.2f})
║    Avg entry  : ${state.avg_entry:>10,.2f}
║    Unrealized : {(last_price/state.avg_entry-1)*100 if state.avg_entry>0 else 0:>+8.2f}%
║    Realized P&L: ${state.total_realized:>+9.2f}
╠══════════════════════════════════════════════════════════════════╣
║  TRADES
║    Total buys       : {len(buys):>4}  (${sum(t.usdt for t in buys):,.0f} deployed)
║      Initial buy    : {len(buys)-len(obi_buys)-len(rebuys):>4}
║      OBI scale-ins  : {len(obi_buys):>4}
║      Rebuys         : {len(rebuys):>4}
║    Total sells      : {len(sells):>4}  (${sum(t.usdt for t in sells):,.0f} received)
║      Band sells     : {len(band_sells):>4}
║    Pending rebuys   : {len(state.pending_rebuys):>4}
╠══════════════════════════════════════════════════════════════════╣
║  PERFORMANCE vs HODL
║    Strategy  : ${pv_now:>10,.2f}  ({strat_return:>+.1f}%)
║    HODL      : ${1000*(1+hodl_return/100):>10,.2f}  ({hodl_return:>+.1f}%)
║    Alpha     : {strat_return-hodl_return:>+8.1f}%
╚══════════════════════════════════════════════════════════════════╝
""")

# Monthly equity
if equity_curve:
    print("Monthly snapshot (end of month portfolio value):")
    seen = set()
    for ts_str, price, pv in equity_curve:
        ym = ts_str[:7]
        if ym not in seen:
            seen.add(ym)
    for ym in sorted(seen):
        rows_m = [(p, v) for d, p, v in equity_curve if d[:7] == ym]
        if rows_m:
            price_m, pv_m = rows_m[-1]
            ret_m = (pv_m - CAPITAL) / CAPITAL * 100
            bar = "█" * min(int(max(ret_m, 0) / 2), 40)
            print(f"  {ym}  BTC=${price_m:>8,.0f}  portfolio=${pv_m:>9,.0f}  {ret_m:>+7.1f}%  {bar}")

# Trade log
if args.trades:
    print(f"\n{'─'*80}")
    print(f"{'DATE':<18} {'SIDE':<5} {'QTY BTC':>12} {'PRICE':>10} {'USDT':>10}  REASON")
    print(f"{'─'*80}")
    for t in trades:
        print(f"  {t.ts:<16} {t.side:<5} {t.qty:>12.6f} ${t.price:>9,.2f} ${t.usdt:>9,.2f}  {t.reason}")
