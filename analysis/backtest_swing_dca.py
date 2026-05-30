#!/usr/bin/env python3
"""
Backtest DCA, Swing, and SwingHold CEX strategies against OHLCV SQLite databases.

DCA       — timed buys every interval_h hours, TP/SL exit.
Swing     — limit BUY at support, limit SELL at resistance, pct-based SL.
SwingHold — like Swing but sell sell_fraction of position at each resistance level.

Fill model (all strategies):
  BUY  fills when candle_low  <= limit_buy_price   (conservative: worst open in range)
  SELL fills when candle_high >= limit_sell_price   (conservative: best open in range)
  SL   fills when candle_low  <= sl_price           (market-order proxy)
  SL takes precedence over TP when both trigger on the same candle.

Capital model: realized-PnL-only.
  Available USDT tracks only closed-trade cash; open positions are not marked to market
  for capital allocation purposes.  Portfolio value (displayed at end) = cash + btc×price.

Levels:
  Default: relative % offsets from first candle close.
  --config <json>: absolute levels from strategy JSON (warns when out of DB price range).

Usage:
    python3 analysis/backtest_swing_dca.py                          # 90d DB, default params
    python3 analysis/backtest_swing_dca.py data/BTCUSDT3197d.db     # custom DB
    python3 analysis/backtest_swing_dca.py --all-dbs                # 3 main DBs
    python3 analysis/backtest_swing_dca.py --compare                # all 3 strategies
    python3 analysis/backtest_swing_dca.py --compare --all-dbs
    python3 analysis/backtest_swing_dca.py --strategy swing         # single strategy
    python3 analysis/backtest_swing_dca.py --sweep                  # parameter grid
    python3 analysis/backtest_swing_dca.py --config strategies/swing/swing_BTCUSDT.json
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

FEE_RATE     = 0.001   # 0.1% per side — Binance spot taker
DATA_DIR     = Path(__file__).resolve().parent.parent / "data"
STRATEGY_DIR = Path(__file__).resolve().parent.parent / "tradinebotte-cex" / "strategies"

_DB_90D     = DATA_DIR / "BTCUSDT_1m90d_range_20260208-20260509.db"
_DB_BULLRUN = DATA_DIR / "BTCUSDT_1m92d_bullrun20241015-20250115.db"
_DB_BEAR    = DATA_DIR / "BTCUSDT_1m92d_bearmarket20220501-20220801.db"
_ALL_DBS    = [_DB_90D, _DB_BULLRUN, _DB_BEAR]

DEFAULT_CAPITAL = 3_000.0  # USDT

# Default relative offsets from start price
_DEF_SUPPORT_OFFSETS    = [-0.08, -0.05, -0.03, -0.01]
_DEF_RESISTANCE_OFFSETS = [+0.02, +0.05, +0.08, +0.12]


# ─── Parameter dataclasses ────────────────────────────────────────────────────

@dataclass
class DCAParams:
    capital:       float = DEFAULT_CAPITAL
    interval_h:    float = 4.0
    amount_usdt:   float = 100.0
    max_positions: int   = 5
    tp_pct:        float = 0.03
    sl_pct:        float = 0.0    # 0 = no SL; open positions force-closed at end
    fee_rate:      float = FEE_RATE


@dataclass
class SwingParams:
    capital:           float = DEFAULT_CAPITAL
    support_levels:    List[float] = field(default_factory=list)   # absolute prices
    resistance_levels: List[float] = field(default_factory=list)
    order_size_usdt:   float = 200.0
    max_positions:     int   = 3
    sl_pct:            float = 0.02
    tp_pct_fallback:   float = 0.04
    fee_rate:          float = FEE_RATE


@dataclass
class SwingHoldParams:
    capital:           float = DEFAULT_CAPITAL
    support_levels:    List[float] = field(default_factory=list)
    resistance_levels: List[float] = field(default_factory=list)
    order_size_usdt:   float = 200.0
    max_positions:     int   = 3
    sell_fraction:     float = 0.30
    sl_pct:            float = 0.02
    tp_pct_fallback:   float = 0.04
    fee_rate:          float = FEE_RATE


# ─── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    label:            str
    db_label:         str
    period:           str
    days:             int
    start_price:      float
    end_price:        float
    capital:          float
    n_trades:         int
    n_wins:           int
    n_losses:         int
    realized_pnl:     float
    fees_paid:        float
    final_usdt:       float
    final_btc:        float
    final_btc_val:    float
    max_dd_pct:       float
    buy_and_hold_pct: float

    @property
    def pnl_pct(self) -> float:
        return self.realized_pnl / self.capital * 100 if self.capital else 0.0

    @property
    def ann_pct(self) -> float:
        return self.pnl_pct / self.days * 365 if self.days else 0.0

    @property
    def win_rate(self) -> float:
        t = self.n_wins + self.n_losses
        return self.n_wins / t * 100 if t else 0.0

    @property
    def portfolio_value(self) -> float:
        return self.final_usdt + self.final_btc_val


# ─── Data loading ─────────────────────────────────────────────────────────────

def _load_klines(db_path: Path) -> list:
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT ts_ms, open, high, low, close, volume FROM klines ORDER BY ts_ms"
    ).fetchall()
    conn.close()
    return rows   # tuple: (ts_ms, open, high, low, close, volume)


def _db_label(db_path: Path) -> str:
    return db_path.stem[:45]


def _period_str(rows: list) -> Tuple[str, int]:
    t0   = datetime.fromtimestamp(rows[0][0]  / 1000, tz=timezone.utc)
    t1   = datetime.fromtimestamp(rows[-1][0] / 1000, tz=timezone.utc)
    days = max(1, (t1 - t0).days)
    return f"{t0.strftime('%Y-%m-%d')} → {t1.strftime('%Y-%m-%d')}", days


# ─── Level helpers ────────────────────────────────────────────────────────────

def _derive_levels(start_price: float) -> Tuple[List[float], List[float]]:
    sup = sorted([round(start_price * (1 + o), 2) for o in _DEF_SUPPORT_OFFSETS])
    res = sorted([round(start_price * (1 + o), 2) for o in _DEF_RESISTANCE_OFFSETS])
    return sup, res


def _validate_levels(support: List[float], resistance: List[float],
                     min_price: float, max_price: float, db_label: str) -> None:
    all_levels = support + resistance
    in_range   = sum(1 for p in all_levels if min_price <= p <= max_price)
    pct        = in_range / len(all_levels) * 100 if all_levels else 0
    if pct == 0:
        print(
            f"WARNING [{db_label}]: ALL {len(all_levels)} levels "
            f"({min(all_levels):.0f}–{max(all_levels):.0f}) are outside "
            f"DB price range ({min_price:.0f}–{max_price:.0f}). "
            f"No fills will occur — switch to relative mode.",
            file=sys.stderr,
        )
    elif pct < 50:
        print(
            f"WARNING [{db_label}]: Only {pct:.0f}% of levels are inside "
            f"price range ({min_price:.0f}–{max_price:.0f}). Results may be sparse.",
            file=sys.stderr,
        )


def _find_tp(entry: float, resistance_levels: List[float],
             tp_pct_fallback: float) -> float:
    above = [r for r in resistance_levels if r > entry]
    if above:
        return min(above)
    return round(entry * (1 + tp_pct_fallback), 2)


# ─── DCA Simulator ────────────────────────────────────────────────────────────

def run_dca(rows: list, p: DCAParams, db_label: str = "",
            support: list = None, resistance: list = None) -> BacktestResult:
    """
    Timed DCA: buy at market (candle close) every interval_h hours.
    TP at tp_pct above fill price; SL at sl_pct below (skipped if sl_pct == 0).
    SL check precedes TP on the same candle.
    """
    period, days = _period_str(rows)
    interval_ms  = int(p.interval_h * 60 * 60 * 1000)

    usdt          = p.capital
    btc           = 0.0
    realized_pnl  = 0.0
    fees_paid     = 0.0
    n_wins = n_losses = 0
    peak_val      = p.capital
    max_dd        = 0.0
    open_pos: List[Dict] = []

    next_buy_ms = rows[0][0]   # first candle fires immediately

    for ts_ms, _o, h, l, c, _v in rows:
        # Timer-based buy
        if ts_ms >= next_buy_ms:
            while next_buy_ms <= ts_ms:
                next_buy_ms += interval_ms
            if len(open_pos) < p.max_positions:
                total_cost = p.amount_usdt * (1 + p.fee_rate)
                if usdt >= total_cost:
                    fee  = p.amount_usdt * p.fee_rate
                    qty  = p.amount_usdt / c
                    tp   = round(c * (1 + p.tp_pct), 2)
                    sl   = round(c * (1 - p.sl_pct), 2) if p.sl_pct > 0 else None
                    usdt -= total_cost
                    btc  += qty
                    fees_paid += fee
                    open_pos.append({"qty": qty, "entry": c, "tp": tp,
                                     "sl": sl, "cost": p.amount_usdt})

        # Check exits: SL takes precedence over TP
        still_open = []
        for pos in open_pos:
            if pos["sl"] is not None and l <= pos["sl"]:
                proceeds      = pos["qty"] * pos["sl"] * (1 - p.fee_rate)
                fees_paid    += pos["qty"] * pos["sl"] * p.fee_rate
                realized_pnl += proceeds - pos["cost"]
                usdt         += proceeds
                btc          -= pos["qty"]
                n_losses     += 1
            elif h >= pos["tp"]:
                proceeds      = pos["qty"] * pos["tp"] * (1 - p.fee_rate)
                fees_paid    += pos["qty"] * pos["tp"] * p.fee_rate
                realized_pnl += proceeds - pos["cost"]
                usdt         += proceeds
                btc          -= pos["qty"]
                n_wins       += 1
            else:
                still_open.append(pos)
        open_pos = still_open

        # Drawdown (realized cash + marked open positions)
        val = usdt + btc * c
        if val > peak_val:
            peak_val = val
        dd = (peak_val - val) / peak_val * 100
        if dd > max_dd:
            max_dd = dd

    final_price   = rows[-1][4]
    btc = max(0.0, btc)
    final_btc_val = btc * final_price
    return BacktestResult(
        label="DCA", db_label=db_label, period=period, days=days,
        start_price=rows[0][4], end_price=final_price,
        capital=p.capital,
        n_trades=n_wins + n_losses, n_wins=n_wins, n_losses=n_losses,
        realized_pnl=realized_pnl, fees_paid=fees_paid,
        final_usdt=usdt, final_btc=btc, final_btc_val=final_btc_val,
        max_dd_pct=max_dd,
        buy_and_hold_pct=(final_price / rows[0][4] - 1) * 100,
    )


# ─── Swing Simulator ─────────────────────────────────────────────────────────

def run_swing(rows: list, p: SwingParams, db_label: str = "") -> BacktestResult:
    """
    Swing strategy: limit BUY at each support level, limit SELL at nearest resistance.
    Each support level can have at most one open position at a time.
    SL takes precedence over TP on the same candle.
    """
    period, days = _period_str(rows)
    sup  = sorted(p.support_levels)
    res  = sorted(p.resistance_levels)
    n_sup = len(sup)

    min_p = min(r[3] for r in rows)
    max_p = max(r[2] for r in rows)
    _validate_levels(sup, res, min_p, max_p, db_label)

    usdt         = p.capital
    btc          = 0.0
    realized_pnl = 0.0
    fees_paid    = 0.0
    n_wins = n_losses = 0
    peak_val     = p.capital
    max_dd       = 0.0

    # slot[i]   = None | open-position dict
    # locked[i] = True after a close; cleared only when price recovers above sup[i].
    #             Prevents cascade re-entries during a sharp move through the cluster.
    slot:   List[Optional[Dict]] = [None] * n_sup
    locked: List[bool]           = [False] * n_sup

    for _ts, _o, h, l, c, _v in rows:
        # Unlock levels where price has recovered above the support
        for i, sp in enumerate(sup):
            if locked[i] and h >= sp:
                locked[i] = False

        n_open = sum(1 for s in slot if s is not None)

        # ── Attempt buys at support levels (bottom-up: fill cheapest first)
        for i, sp in enumerate(sup):
            if slot[i] is not None or locked[i]:
                continue
            if l > sp:
                continue
            if n_open >= p.max_positions:
                break
            total_cost = p.order_size_usdt * (1 + p.fee_rate)
            if usdt < total_cost:
                continue
            fee  = p.order_size_usdt * p.fee_rate
            qty  = p.order_size_usdt / sp
            tp   = _find_tp(sp, res, p.tp_pct_fallback)
            sl   = round(sp * (1 - p.sl_pct), 2)
            usdt -= total_cost
            btc  += qty
            fees_paid += fee
            slot[i]    = {"qty": qty, "entry": sp, "tp": tp,
                          "sl": sl, "cost": p.order_size_usdt}
            n_open += 1

        # ── Check exits for open positions
        for i in range(n_sup):
            pos = slot[i]
            if pos is None:
                continue
            if l <= pos["sl"]:
                proceeds      = pos["qty"] * pos["sl"] * (1 - p.fee_rate)
                fees_paid    += pos["qty"] * pos["sl"] * p.fee_rate
                realized_pnl += proceeds - pos["cost"]
                usdt         += proceeds
                btc          -= pos["qty"]
                n_losses     += 1
                slot[i]       = None
                locked[i]     = True   # require price recovery above support before re-entry
            elif h >= pos["tp"]:
                proceeds      = pos["qty"] * pos["tp"] * (1 - p.fee_rate)
                fees_paid    += pos["qty"] * pos["tp"] * p.fee_rate
                realized_pnl += proceeds - pos["cost"]
                usdt         += proceeds
                btc          -= pos["qty"]
                n_wins       += 1
                slot[i]       = None
                locked[i]     = True   # require price recovery above support before re-entry

        val = usdt + btc * c
        if val > peak_val:
            peak_val = val
        dd = (peak_val - val) / peak_val * 100
        if dd > max_dd:
            max_dd = dd

    final_price   = rows[-1][4]
    btc = max(0.0, btc)
    final_btc_val = btc * final_price
    return BacktestResult(
        label="Swing", db_label=db_label, period=period, days=days,
        start_price=rows[0][4], end_price=final_price,
        capital=p.capital,
        n_trades=n_wins + n_losses, n_wins=n_wins, n_losses=n_losses,
        realized_pnl=realized_pnl, fees_paid=fees_paid,
        final_usdt=usdt, final_btc=btc, final_btc_val=final_btc_val,
        max_dd_pct=max_dd,
        buy_and_hold_pct=(final_price / rows[0][4] - 1) * 100,
    )


# ─── SwingHold Simulator ──────────────────────────────────────────────────────

def run_swinghold(rows: list, p: SwingHoldParams, db_label: str = "") -> BacktestResult:
    """
    SwingHold: enters at support levels like Swing, but partially exits at each
    resistance level above entry (sell_fraction of current remaining qty each time).
    Full position exits when SL triggers or no more resistances are above.
    """
    period, days = _period_str(rows)
    sup  = sorted(p.support_levels)
    res  = sorted(p.resistance_levels)
    n_sup = len(sup)

    min_p = min(r[3] for r in rows)
    max_p = max(r[2] for r in rows)
    _validate_levels(sup, res, min_p, max_p, db_label)

    usdt         = p.capital
    btc          = 0.0
    realized_pnl = 0.0
    fees_paid    = 0.0
    n_wins = n_losses = 0
    peak_val     = p.capital
    max_dd       = 0.0

    # slot[i]   = None | open-position dict
    # locked[i] = True after a close; cleared when price recovers above sup[i].
    slot:   List[Optional[Dict]] = [None] * n_sup
    locked: List[bool]           = [False] * n_sup

    for _ts, _o, h, l, c, _v in rows:
        for i, sp in enumerate(sup):
            if locked[i] and h >= sp:
                locked[i] = False

        n_open = sum(1 for s in slot if s is not None)

        # ── Attempt buys
        for i, sp in enumerate(sup):
            if slot[i] is not None or locked[i]:
                continue
            if l > sp:
                continue
            if n_open >= p.max_positions:
                break
            total_cost = p.order_size_usdt * (1 + p.fee_rate)
            if usdt < total_cost:
                continue
            fee              = p.order_size_usdt * p.fee_rate
            qty              = p.order_size_usdt / sp
            above            = [r for r in res if r > sp]
            sl               = round(sp * (1 - p.sl_pct), 2)
            usdt            -= total_cost
            btc             += qty
            fees_paid       += fee
            slot[i]          = {
                "qty":           qty,
                "initial_qty":   qty,
                "entry":         sp,
                "sl":            sl,
                "cost_per_unit": sp,  # cost per BTC unit
                "resistances":   above,
                "res_idx":       0,
                "partial_pnl":   0.0,
            }
            n_open += 1

        # ── Check exits
        for i in range(n_sup):
            pos = slot[i]
            if pos is None:
                continue

            # SL takes precedence
            if l <= pos["sl"]:
                # close entire remaining position at sl price
                proceeds      = pos["qty"] * pos["sl"] * (1 - p.fee_rate)
                fees_paid    += pos["qty"] * pos["sl"] * p.fee_rate
                cost_remain   = pos["qty"] * pos["cost_per_unit"]
                pnl           = pos["partial_pnl"] + (proceeds - cost_remain)
                realized_pnl += pnl
                usdt         += proceeds
                btc          -= pos["qty"]
                n_losses     += 1
                slot[i]       = None
                locked[i]     = True
                continue

            # Partial TP at next resistance (may fire multiple times in one candle)
            while (pos["res_idx"] < len(pos["resistances"]) and
                   h >= pos["resistances"][pos["res_idx"]]):
                rp          = pos["resistances"][pos["res_idx"]]
                sell_qty    = pos["qty"] * p.sell_fraction
                proceeds    = sell_qty * rp * (1 - p.fee_rate)
                fee_here    = sell_qty * rp * p.fee_rate
                cost_here   = sell_qty * pos["cost_per_unit"]
                fees_paid  += fee_here
                usdt       += proceeds
                btc        -= sell_qty
                pos["qty"] -= sell_qty
                pos["partial_pnl"] += proceeds - cost_here
                pos["res_idx"]     += 1

                # If all resistances exhausted, close remainder at this resistance
                if pos["res_idx"] >= len(pos["resistances"]) and pos["qty"] > 1e-9:
                    last_rp      = rp
                    rem_proceeds = pos["qty"] * last_rp * (1 - p.fee_rate)
                    fees_paid   += pos["qty"] * last_rp * p.fee_rate
                    cost_remain  = pos["qty"] * pos["cost_per_unit"]
                    usdt        += rem_proceeds
                    btc         -= pos["qty"]
                    pos["partial_pnl"] += rem_proceeds - cost_remain
                    pos["qty"]   = 0.0
                    break

            if pos["qty"] < 1e-9:
                realized_pnl += pos["partial_pnl"]
                n_wins       += 1
                slot[i]       = None
                locked[i]     = True
            elif pos["res_idx"] == 0 and not pos["resistances"]:
                # no resistances above entry: use fallback TP
                tp = round(pos["entry"] * (1 + p.tp_pct_fallback), 2)
                if h >= tp:
                    proceeds      = pos["qty"] * tp * (1 - p.fee_rate)
                    fees_paid    += pos["qty"] * tp * p.fee_rate
                    cost_remain   = pos["qty"] * pos["cost_per_unit"]
                    realized_pnl += pos["partial_pnl"] + (proceeds - cost_remain)
                    usdt         += proceeds
                    btc          -= pos["qty"]
                    n_wins       += 1
                    slot[i]       = None
                    locked[i]     = True

        val = usdt + btc * c
        if val > peak_val:
            peak_val = val
        dd = (peak_val - val) / peak_val * 100
        if dd > max_dd:
            max_dd = dd

    final_price   = rows[-1][4]
    btc = max(0.0, btc)
    final_btc_val = btc * final_price
    return BacktestResult(
        label="SwingHold", db_label=db_label, period=period, days=days,
        start_price=rows[0][4], end_price=final_price,
        capital=p.capital,
        n_trades=n_wins + n_losses, n_wins=n_wins, n_losses=n_losses,
        realized_pnl=realized_pnl, fees_paid=fees_paid,
        final_usdt=usdt, final_btc=btc, final_btc_val=final_btc_val,
        max_dd_pct=max_dd,
        buy_and_hold_pct=(final_price / rows[0][4] - 1) * 100,
    )


# ─── Parameter builders from JSON config ──────────────────────────────────────

def _params_from_json(cfg_path: Path, capital: float = DEFAULT_CAPITAL):
    """Parse strategy JSON and return (DCAParams | SwingParams | SwingHoldParams)."""
    with open(cfg_path, encoding="utf-8") as f:
        raw = json.load(f)
    sc = raw.get("strategy_cfg", raw)

    if "dca_interval_h" in raw or "interval_h" in raw:
        return DCAParams(
            capital       = capital,
            interval_h    = raw.get("dca_interval_h", raw.get("interval_h", 4.0)),
            amount_usdt   = raw.get("dca_amount_usdt", raw.get("amount_usdt", 100.0)),
            max_positions = raw.get("max_positions", 5),
            tp_pct        = raw.get("tp_pct", 0.03),
            sl_pct        = raw.get("sl_pct", 0.0),
        )

    if "sell_fraction" in sc:
        return SwingHoldParams(
            capital           = capital,
            support_levels    = sc.get("support_levels", []),
            resistance_levels = sc.get("resistance_levels", []),
            order_size_usdt   = sc.get("order_size_usdt", 200.0),
            max_positions     = sc.get("max_positions", 3),
            sell_fraction     = sc.get("sell_fraction", 0.30),
            sl_pct            = sc.get("sl_pct", 0.02),
            tp_pct_fallback   = sc.get("tp_pct_fallback", 0.04),
        )

    return SwingParams(
        capital           = capital,
        support_levels    = sc.get("support_levels", []),
        resistance_levels = sc.get("resistance_levels", []),
        order_size_usdt   = sc.get("order_size_usdt", 200.0),
        max_positions     = sc.get("max_positions", 3),
        sl_pct            = sc.get("sl_pct", 0.02),
        tp_pct_fallback   = sc.get("tp_pct_fallback", 0.04),
    )


# ─── Default parameter builders ───────────────────────────────────────────────

def _default_dca(capital: float = DEFAULT_CAPITAL) -> DCAParams:
    return DCAParams(capital=capital)


def _default_swing(start_price: float, capital: float = DEFAULT_CAPITAL) -> SwingParams:
    sup, res = _derive_levels(start_price)
    return SwingParams(capital=capital, support_levels=sup, resistance_levels=res)


def _default_swinghold(start_price: float, capital: float = DEFAULT_CAPITAL) -> SwingHoldParams:
    sup, res = _derive_levels(start_price)
    return SwingHoldParams(capital=capital, support_levels=sup, resistance_levels=res)


# ─── Output helpers ───────────────────────────────────────────────────────────

def _sign(v: float) -> str:
    return "+" if v >= 0 else ""


def _print_result(r: BacktestResult, show_levels: bool = False) -> None:
    w = 68
    bar = "═" * w
    print()
    print(f"╔{bar}╗")
    print(f"║  {r.label:<20}  {r.db_label:<42}║")
    print(f"╠{bar}╣")
    print(f"║  Period   : {r.period}  ({r.days}d){'':<22}║")
    print(f"║  Start: ${r.start_price:,.0f}   End: ${r.end_price:,.0f}   "
          f"B&H: {_sign(r.buy_and_hold_pct)}{r.buy_and_hold_pct:.1f}%{'':<7}║")
    print(f"╠{bar}╣")
    print(f"║  Trades: {r.n_trades:>4}   Wins: {r.n_wins:>4}   "
          f"Losses: {r.n_losses:>4}   WinRate: {r.win_rate:5.1f}%{'':<8}║")
    pnl_sign = _sign(r.realized_pnl)
    print(f"║  Realized PnL : {pnl_sign}${r.realized_pnl:,.2f}  "
          f"({_sign(r.pnl_pct)}{r.pnl_pct:.1f}%)   "
          f"Ann: {_sign(r.ann_pct)}{r.ann_pct:.1f}%/yr{'':<4}║")
    print(f"║  Fees: ${r.fees_paid:,.2f}   "
          f"MaxDD: {r.max_dd_pct:.1f}%{'':<37}║")
    print(f"║  Portfolio: ${r.portfolio_value:,.2f}  "
          f"(cash ${r.final_usdt:,.0f} + "
          f"{r.final_btc:.4f} BTC × ${r.end_price:,.0f}){'':<5}║")
    print(f"╚{bar}╝")


def _print_comparison(results: List[BacktestResult]) -> None:
    if not results:
        return
    w   = 100
    bar = "─" * w
    r0  = results[0]
    print()
    print("═" * w)
    print("  CEX STRATEGY COMPARISON")
    print(f"  DB: {r0.db_label}   Period: {r0.period}  ({r0.days}d)")
    print("═" * w)
    hdr = (f"  {'Strategy':<12} {'Trades':>7} {'WinRate':>8} {'RealPnL':>10} "
           f"{'PnL%':>7} {'Ann%':>7} {'MaxDD':>7} {'Fees':>8} {'Portfolio':>11}")
    print(hdr)
    print("  " + bar)
    for r in results:
        pnl_s = f"{_sign(r.realized_pnl)}${r.realized_pnl:,.0f}"
        print(
            f"  {r.label:<12} "
            f"{r.n_trades:>7} "
            f"{r.win_rate:>7.1f}% "
            f"{pnl_s:>10} "
            f"{_sign(r.pnl_pct)}{r.pnl_pct:>6.1f}% "
            f"{_sign(r.ann_pct)}{r.ann_pct:>6.1f}% "
            f"{r.max_dd_pct:>6.1f}% "
            f"${r.fees_paid:>7,.0f} "
            f"${r.portfolio_value:>10,.0f}"
        )
    print("═" * w)
    print(f"  B&H reference: {_sign(r0.buy_and_hold_pct)}{r0.buy_and_hold_pct:.1f}%  "
          f"({r0.start_price:,.0f} → {r0.end_price:,.0f})")
    print("═" * w)


def _print_all_dbs_table(all_results: List[List[BacktestResult]]) -> None:
    """Print a summary table: rows = DBs, cols = strategies."""
    if not all_results:
        return
    strats = [r.label for r in all_results[0]]
    w = 110
    print()
    print("═" * w)
    print("  ALL-DBS COMPARISON")
    print("═" * w)
    col_w = 22
    hdr_db = f"  {'Database':<45}"
    hdr_s  = "".join(f"  {s:<{col_w}}" for s in strats)
    print(hdr_db + hdr_s)
    print("  " + "─" * (w - 2))
    for group in all_results:
        db_lbl = group[0].db_label[:43]
        row    = f"  {db_lbl:<45}"
        for r in group:
            cell = (f"{_sign(r.pnl_pct)}{r.pnl_pct:.1f}% "
                    f"ann{_sign(r.ann_pct)}{r.ann_pct:.0f}% "
                    f"dd{r.max_dd_pct:.1f}%")
            row += f"  {cell:<{col_w}}"
        print(row)
    print("═" * w)


# ─── Sweep helpers ────────────────────────────────────────────────────────────

def _run_sweep_dca(rows: list, db_label: str) -> None:
    tp_pcts = [0.02, 0.03, 0.05, 0.08]
    sl_pcts = [0.0, 0.01, 0.02, 0.03]
    w = 80
    print()
    print(f"{'DCA SWEEP':─^{w}}")
    print(f"  DB: {db_label}")
    print(f"  {'tp_pct\\sl_pct':<18}", end="")
    for sl in sl_pcts:
        print(f"  {'sl='+str(sl):<14}", end="")
    print()
    print("  " + "─" * (w - 2))
    for tp in tp_pcts:
        print(f"  tp={tp:<14}", end="")
        for sl in sl_pcts:
            p = DCAParams(tp_pct=tp, sl_pct=sl)
            r = run_dca(rows, p, db_label=db_label)
            cell = f"{_sign(r.pnl_pct)}{r.pnl_pct:.1f}%/{r.win_rate:.0f}%"
            print(f"  {cell:<14}", end="")
        print()
    print("  " + "─" * (w - 2))
    print("  Format: PnL% / WinRate%")


def _run_sweep_swing(rows: list, db_label: str) -> None:
    sl_pcts       = [0.01, 0.015, 0.02, 0.03, 0.05]
    tp_fallbacks  = [0.03, 0.04, 0.06]
    start         = rows[0][4]
    sup_base, res_base = _derive_levels(start)
    w = 80
    print()
    print(f"{'SWING SWEEP':─^{w}}")
    print(f"  DB: {db_label}")
    print(f"  {'sl_pct\\tp_fb':<18}", end="")
    for tf in tp_fallbacks:
        print(f"  {'tp_fb='+str(tf):<14}", end="")
    print()
    print("  " + "─" * (w - 2))
    for sl in sl_pcts:
        print(f"  sl={sl:<15}", end="")
        for tf in tp_fallbacks:
            p = SwingParams(support_levels=sup_base, resistance_levels=res_base,
                            sl_pct=sl, tp_pct_fallback=tf)
            r = run_swing(rows, p, db_label=db_label)
            cell = f"{_sign(r.pnl_pct)}{r.pnl_pct:.1f}%/{r.n_trades}t"
            print(f"  {cell:<14}", end="")
        print()
    print("  " + "─" * (w - 2))
    print("  Format: PnL% / N_trades")


def _run_sweep_swinghold(rows: list, db_label: str) -> None:
    sell_fracs = [0.20, 0.30, 0.40, 0.50]
    sl_pcts    = [0.015, 0.02, 0.03, 0.05]
    start      = rows[0][4]
    sup_base, res_base = _derive_levels(start)
    w = 80
    print()
    print(f"{'SWINGHOLD SWEEP':─^{w}}")
    print(f"  DB: {db_label}")
    print(f"  {'sell_frac\\sl_pct':<18}", end="")
    for sl in sl_pcts:
        print(f"  {'sl='+str(sl):<14}", end="")
    print()
    print("  " + "─" * (w - 2))
    for sf in sell_fracs:
        print(f"  sf={sf:<15}", end="")
        for sl in sl_pcts:
            p = SwingHoldParams(support_levels=sup_base, resistance_levels=res_base,
                                sell_fraction=sf, sl_pct=sl)
            r = run_swinghold(rows, p, db_label=db_label)
            cell = f"{_sign(r.pnl_pct)}{r.pnl_pct:.1f}%/{r.n_trades}t"
            print(f"  {cell:<14}", end="")
        print()
    print("  " + "─" * (w - 2))
    print("  Format: PnL% / N_trades")


# ─── Main ─────────────────────────────────────────────────────────────────────

def _run_one_db(db_path: Path, strategy: str, cfg_path: Optional[Path],
                capital: float) -> List[BacktestResult]:
    """Run one or all strategies on a single DB; return list of results."""
    label  = _db_label(db_path)
    print(f"  Loading {label} …", file=sys.stderr)
    rows   = _load_klines(db_path)
    start  = rows[0][4]

    if cfg_path:
        params = _params_from_json(cfg_path, capital)
        # If absolute levels, validate against DB range
        if hasattr(params, "support_levels") and params.support_levels:
            min_p = min(r[3] for r in rows)
            max_p = max(r[2] for r in rows)
            _validate_levels(params.support_levels, params.resistance_levels,
                             min_p, max_p, label)
        if isinstance(params, DCAParams):
            strategies = ["dca"]
        elif isinstance(params, SwingHoldParams):
            strategies = ["swinghold"]
        else:
            strategies = ["swing"]
    else:
        strategies = [strategy] if strategy != "all" else ["dca", "swing", "swinghold"]
        params     = None

    results = []
    for s in strategies:
        if params and len(strategies) == 1:
            p = params
        else:
            if s == "dca":
                p = _default_dca(capital)
            elif s == "swinghold":
                p = _default_swinghold(start, capital)
            else:
                p = _default_swing(start, capital)

        if isinstance(p, DCAParams) or s == "dca":
            r = run_dca(rows, p if isinstance(p, DCAParams) else _default_dca(capital),
                        db_label=label)
        elif isinstance(p, SwingHoldParams) or s == "swinghold":
            p2 = p if isinstance(p, SwingHoldParams) else _default_swinghold(start, capital)
            r  = run_swinghold(rows, p2, db_label=label)
        else:
            p2 = p if isinstance(p, SwingParams) else _default_swing(start, capital)
            r  = run_swing(rows, p2, db_label=label)
        results.append(r)
    return results


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backtest DCA / Swing / SwingHold CEX strategies"
    )
    ap.add_argument("db", nargs="?", default=str(_DB_90D),
                    help="SQLite DB with 1m klines (default: 90d 2026 range DB)")
    ap.add_argument("--all-dbs", action="store_true",
                    help="Run on bullrun + bearmarket + 90d_2026 DBs")
    ap.add_argument("--compare", action="store_true",
                    help="Run all 3 strategies side-by-side")
    ap.add_argument("--strategy", choices=["dca", "swing", "swinghold"],
                    default="dca",
                    help="Which strategy to run (default: dca; use --compare for all)")
    ap.add_argument("--sweep", action="store_true",
                    help="Parameter sweep: tp_pct×sl_pct (DCA) and sl_pct×tp_fb (Swing)")
    ap.add_argument("--config", metavar="JSON",
                    help="Strategy JSON config file (overrides --strategy)")
    ap.add_argument("--capital", type=float, default=DEFAULT_CAPITAL,
                    help=f"Starting capital in USDT (default: {DEFAULT_CAPITAL:,.0f})")
    args = ap.parse_args()

    cfg_path = Path(args.config) if args.config else None
    strategy = "all" if args.compare else args.strategy

    dbs: List[Path] = _ALL_DBS if args.all_dbs else [Path(args.db)]

    # ── Sweep mode
    if args.sweep:
        db_path = dbs[0]
        print(f"Loading {_db_label(db_path)} …", file=sys.stderr)
        rows    = _load_klines(db_path)
        label   = _db_label(db_path)
        _run_sweep_dca(rows, label)
        _run_sweep_swing(rows, label)
        _run_sweep_swinghold(rows, label)
        return

    # ── Normal / compare / all-dbs mode
    all_groups: List[List[BacktestResult]] = []
    for db_path in dbs:
        group = _run_one_db(db_path, strategy, cfg_path, args.capital)
        all_groups.append(group)

        if args.compare or len(group) > 1:
            _print_comparison(group)
        else:
            _print_result(group[0])

    if args.all_dbs and len(dbs) > 1:
        # Only print the matrix if all groups have the same strategies
        if all(len(g) == len(all_groups[0]) for g in all_groups):
            _print_all_dbs_table(all_groups)


if __name__ == "__main__":
    main()
