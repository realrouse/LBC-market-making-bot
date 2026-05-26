#!/usr/bin/env python3
"""
BTC Long-Term Accumulation Bot v1.2

Builds a growing BTC spot position using OBI dip-buying + partial profit ladder.

Changes from v1.1:
  - Refactor: consumes OBI/price data from the shared indicators service (ZMQ)
    instead of opening a redundant Binance WebSocket connection directly.
    Subscribes to btc_scalping_spot (OBI, price) and btc_vwap_context (VWAP).
  - New: VWAP gate — scale-ins are suspended when price is above the 4h VWAP
    (dip_score < 0). Enabled by default via vwap_gate=true in config.

Changes from v1.0:
  - Fix: last_buy_ts only updated on OBI dip scale-ins (not on rebuys)
  - Fix: OBI scale-in no longer clears pending_rebuys / active_bands
  - New: state persistence — restores position on restart from accum_state table
  - New: adaptive scale-in — buys more USDT when deeper below avg_entry
  - New: min_holdings_pct — never sell below X% of peak BTC holdings
  - New: adaptive rebuy discount — scales with spread EMA (volatility proxy)

Strategy:
  1. Initial buy at startup (initial_stake_usdt)
  2. OBI dip signal: OBI_ema < -entry_thresh for confirm_n consecutive ticks
     from the shared indicators service (btc_scalping_spot stream)
     VWAP gate: skip scale-in when price > 4h VWAP (dip_score < 0)
     → scale in (adaptive amount), cooldown between scale-ins
  3. Profit ladder: when price >= avg_entry * (1 + band_pct), sell sell_fraction
     of current holdings; set rebuy target at adaptive discount below sell price
  4. Rebuy: when price reaches rebuy_target, buy back the sold quantity

Usage:
    python3 bot/accumulation_bot.py
    python3 bot/accumulation_bot.py --strategy strategies/accumulation/btc_accumulation.json
    python3 bot/accumulation_bot.py --dir ~/tradinebotte
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _setup_logging(log_path: Path) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    if sys.stdout.isatty():
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

logger = logging.getLogger("accumulation_bot")

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "symbol":                "BTCUSDT",
    "capital_usdt":          1000.0,
    "initial_stake_usdt":    500.0,   # deploy 50% upfront at startup
    "scale_in_usdt":         100.0,
    "scale_in_dip_factor":   0.5,
    "scale_in_max_mult":     3.0,
    "max_invested_pct":      0.90,
    # OBI parameters — obi_ema_alpha is informational when using ZMQ mode;
    # the actual alpha is set in the indicators service config.
    "obi_levels":            10,
    "obi_ema_alpha":         0.05,
    "obi_entry_thresh":      0.50,
    "obi_confirm_n":         20,
    "min_scale_interval_s":  3600,    # 1h cooldown between OBI scale-ins
    "profit_bands_pct":      [5.0, 10.0, 20.0, 30.0, 50.0],
    "sell_fraction":         0.15,    # sell 15% at each band
    "min_holdings_pct":      0.50,    # never sell below 50% of peak holdings
    "rebuy_discount_min_pct": 3.0,
    "rebuy_discount_max_pct": 10.0,
    "rebuy_spread_mult":      3.0,
    "fee_spot":              0.001,
    "maker_fee_spot":        0.0002,
    "use_limit_orders":      True,
    "snapshot_every_n":      20,
    # Indicators service (ZMQ)
    "indicators_addr":       "tcp://127.0.0.1:5559",
    "scalping_stream_id":    "btc_scalping_spot",
    # VWAP gate: when True, scale-ins are suspended when price is above the 4h VWAP
    "vwap_gate":             True,
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accum_trades (
            id              INTEGER PRIMARY KEY,
            ts_ms           INTEGER NOT NULL,
            side            TEXT    NOT NULL,
            reason          TEXT    NOT NULL,
            price           REAL    NOT NULL,
            qty_btc         REAL    NOT NULL,
            usdt_value      REAL    NOT NULL,
            fee_usdt        REAL    NOT NULL,
            avg_entry_after REAL,
            holdings_after  REAL    NOT NULL,
            free_usdt_after REAL    NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accum_snapshots (
            id              INTEGER PRIMARY KEY,
            ts_ms           INTEGER NOT NULL,
            price           REAL    NOT NULL,
            holdings_btc    REAL    NOT NULL,
            avg_entry       REAL,
            invested_usdt   REAL    NOT NULL,
            free_usdt       REAL    NOT NULL,
            unrealized_pct  REAL    NOT NULL,
            obi_ema         REAL    NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS accum_state (
            id                  INTEGER PRIMARY KEY,
            ts_ms               INTEGER NOT NULL,
            holdings_btc        REAL    NOT NULL,
            avg_entry           REAL    NOT NULL,
            free_usdt           REAL    NOT NULL,
            total_realized      REAL    NOT NULL,
            peak_holdings_btc   REAL    NOT NULL,
            last_buy_ts         INTEGER NOT NULL,
            pending_rebuys_json TEXT,
            active_bands_json   TEXT
        )""")
    conn.commit()
    logger.info("DB: %s", db_path)
    return conn

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class PendingRebuy:
    band_pct:    float
    sell_price:  float
    qty_btc:     float
    rebuy_price: float

@dataclass
class AccumState:
    p:                  dict
    holdings_btc:       float = 0.0
    avg_entry:          float = 0.0
    free_usdt:          float = 0.0
    last_price:         float = 0.0
    obi_ema:            float = 0.0
    spread_ema:         float = 0.002  # typical Binance spot spread ~0.002%
    pending_count:      int   = 0
    last_buy_ts:        int   = 0
    initial_done:       bool  = False
    pending_rebuys:     list  = field(default_factory=list)
    active_bands:       set   = field(default_factory=set)
    snap_counter:       int   = 0
    total_realized:     float = 0.0
    peak_holdings_btc:  float = 0.0
    # VWAP context — updated from btc_vwap_context stream
    vwap_dip_score:     float = 0.0
    vwap_dip_zone:      str   = "neutral"

    def unrealized_pct(self) -> float:
        if self.avg_entry > 0 and self.last_price > 0:
            return (self.last_price - self.avg_entry) / self.avg_entry * 100.0
        return 0.0

    def max_investable(self) -> float:
        return self.p["capital_usdt"] * self.p["max_invested_pct"]

# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _save_state(state: AccumState, db: sqlite3.Connection) -> None:
    rebuys_json = json.dumps([
        {"band_pct": r.band_pct, "sell_price": r.sell_price,
         "qty_btc":  r.qty_btc,  "rebuy_price": r.rebuy_price}
        for r in state.pending_rebuys
    ])
    bands_json = json.dumps(sorted(state.active_bands))
    db.execute("""
        INSERT OR REPLACE INTO accum_state
            (id, ts_ms, holdings_btc, avg_entry, free_usdt, total_realized,
             peak_holdings_btc, last_buy_ts, pending_rebuys_json, active_bands_json)
        VALUES (1,?,?,?,?,?,?,?,?,?)""",
        (int(time.time() * 1000), state.holdings_btc, state.avg_entry,
         state.free_usdt, state.total_realized, state.peak_holdings_btc,
         state.last_buy_ts, rebuys_json, bands_json))
    db.commit()


def _restore_state(state: AccumState, db: sqlite3.Connection) -> bool:
    try:
        row = db.execute("""
            SELECT ts_ms, holdings_btc, avg_entry, free_usdt, total_realized,
                   peak_holdings_btc, last_buy_ts, pending_rebuys_json, active_bands_json
            FROM accum_state WHERE id=1""").fetchone()
    except sqlite3.OperationalError:
        return False
    if row is None:
        return False
    (ts_ms, holdings, avg, free, realized,
     peak, last_buy_ts, rebuys_json, bands_json) = row
    state.holdings_btc      = holdings
    state.avg_entry         = avg
    state.free_usdt         = free
    state.total_realized    = realized
    state.peak_holdings_btc = peak
    state.last_buy_ts       = last_buy_ts
    state.initial_done      = True
    if rebuys_json:
        for r in json.loads(rebuys_json):
            state.pending_rebuys.append(PendingRebuy(**r))
    if bands_json:
        state.active_bands = set(json.loads(bands_json))
    logger.info("Restored: %.6f BTC @ avg %.2f  free=%.2f  realized=%+.2f  "
                "rebuys=%d  bands=%s",
                holdings, avg, free, realized,
                len(state.pending_rebuys), sorted(state.active_bands))
    return True

# ---------------------------------------------------------------------------
# Adaptive helpers
# ---------------------------------------------------------------------------

def _scale_in_amount(state: AccumState, price: float) -> float:
    base = state.p.get("scale_in_usdt", 100.0)
    if state.avg_entry <= 0:
        return base
    dip_pct  = (state.avg_entry - price) / state.avg_entry * 100.0
    factor   = state.p.get("scale_in_dip_factor", 0.5)
    max_mult = state.p.get("scale_in_max_mult", 3.0)
    mult     = 1.0 + factor * max(dip_pct, 0.0)
    return min(base * mult, base * max_mult)


def _rebuy_discount(state: AccumState) -> float:
    p       = state.p
    min_d   = p.get("rebuy_discount_min_pct", 0.15)
    max_d   = p.get("rebuy_discount_max_pct", 1.00)
    mult    = p.get("rebuy_spread_mult", 3.0)
    pct     = max(min_d, min(max_d, state.spread_ema * mult))
    return pct / 100.0

# ---------------------------------------------------------------------------
# Trade execution (paper)
# ---------------------------------------------------------------------------

def _buy(state: AccumState, price: float, usdt_amount: float,
         reason: str, ts_ms: int, db: sqlite3.Connection) -> bool:
    p        = state.p
    fee_rate = p["maker_fee_spot"] if p.get("use_limit_orders") else p["fee_spot"]
    qty_btc  = usdt_amount / price
    fee      = usdt_amount * fee_rate
    total    = usdt_amount + fee

    if total > state.free_usdt + 0.01:
        logger.warning("BUY skipped — need %.2f USDT, have %.2f", total, state.free_usdt)
        return False

    if state.holdings_btc > 0 and state.avg_entry > 0:
        total_btc       = state.holdings_btc + qty_btc
        state.avg_entry = (state.holdings_btc * state.avg_entry + qty_btc * price) / total_btc
    else:
        state.avg_entry = price

    state.holdings_btc += qty_btc
    state.free_usdt    -= total
    if state.holdings_btc > state.peak_holdings_btc:
        state.peak_holdings_btc = state.holdings_btc

    db.execute("""
        INSERT INTO accum_trades
            (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
             avg_entry_after, holdings_after, free_usdt_after)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, "buy", reason, price, qty_btc, usdt_amount, fee,
         state.avg_entry, state.holdings_btc, state.free_usdt))
    db.commit()
    _save_state(state, db)

    logger.info("BUY  %.6f BTC @ %8.2f  [%-22s]  avg=%8.2f  held=%.6f  free=%7.2f  uPnL=%+.2f%%",
                qty_btc, price, reason, state.avg_entry,
                state.holdings_btc, state.free_usdt, state.unrealized_pct())
    return True


def _sell(state: AccumState, price: float, qty_btc: float,
          reason: str, ts_ms: int, db: sqlite3.Connection) -> bool:
    if qty_btc <= 0 or state.holdings_btc <= 0:
        return False
    qty_btc  = min(qty_btc, state.holdings_btc)
    p        = state.p
    fee_rate = p["maker_fee_spot"] if p.get("use_limit_orders") else p["fee_spot"]
    usdt_val = qty_btc * price
    fee      = usdt_val * fee_rate
    realized = usdt_val - fee - qty_btc * (state.avg_entry or price)

    state.holdings_btc   -= qty_btc
    state.free_usdt      += usdt_val - fee
    state.total_realized += realized

    db.execute("""
        INSERT INTO accum_trades
            (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
             avg_entry_after, holdings_after, free_usdt_after)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, "sell", reason, price, qty_btc, usdt_val, fee,
         state.avg_entry, state.holdings_btc, state.free_usdt))
    db.commit()
    _save_state(state, db)

    logger.info("SELL %.6f BTC @ %8.2f  [%-22s]  realized=%+7.2f  held=%.6f  free=%7.2f",
                qty_btc, price, reason, realized,
                state.holdings_btc, state.free_usdt)
    return True

# ---------------------------------------------------------------------------
# Strategy logic
# ---------------------------------------------------------------------------

def _check_profit_bands(state: AccumState, price: float,
                        ts_ms: int, db: sqlite3.Connection) -> None:
    if state.holdings_btc <= 0 or state.avg_entry <= 0:
        return
    p        = state.p
    bands    = sorted(p.get("profit_bands_pct", []))
    fraction = p.get("sell_fraction", 0.20)
    discount = _rebuy_discount(state)

    min_hold_pct = p.get("min_holdings_pct", 0.0)
    floor_btc    = state.peak_holdings_btc * min_hold_pct if state.peak_holdings_btc > 0 else 0.0

    for band_pct in bands:
        if band_pct in state.active_bands:
            continue
        target = state.avg_entry * (1.0 + band_pct / 100.0)
        if price < target:
            break
        qty = state.holdings_btc * fraction
        max_sellable = max(0.0, state.holdings_btc - floor_btc)
        qty = min(qty, max_sellable)
        if qty < 1e-6:
            logger.info("Band +%.1f%% skipped — holdings at floor (%.2f%%)",
                        band_pct, min_hold_pct * 100)
            continue
        if _sell(state, price, qty, f"profit+{band_pct:.1f}%", ts_ms, db):
            rebuy = price * (1.0 - discount)
            state.active_bands.add(band_pct)
            state.pending_rebuys.append(
                PendingRebuy(band_pct=band_pct, sell_price=price,
                             qty_btc=qty, rebuy_price=rebuy))
            logger.info("  → rebuy %.6f BTC @ %.2f  (spread=%.4f%% → discount=%.3f%%)",
                        qty, rebuy, state.spread_ema, discount * 100)


def _check_rebuys(state: AccumState, price: float,
                  ts_ms: int, db: sqlite3.Connection) -> None:
    filled = []
    for rb in state.pending_rebuys:
        if price > rb.rebuy_price:
            continue
        usdt_needed = rb.qty_btc * price
        if usdt_needed > state.free_usdt:
            continue
        if _buy(state, price, usdt_needed, f"rebuy+{rb.band_pct:.1f}%", ts_ms, db):
            state.active_bands.discard(rb.band_pct)
            filled.append(rb)
    for rb in filled:
        state.pending_rebuys.remove(rb)


def _check_obi_scale_in(state: AccumState, price: float,
                         ts_ms: int, db: sqlite3.Connection) -> None:
    p          = state.p
    min_iv     = p.get("min_scale_interval_s", 1800)
    max_invest = state.max_investable()

    if (ts_ms - state.last_buy_ts) / 1000.0 < min_iv:
        return

    # VWAP gate: skip scale-ins when price is above the 4h VWAP
    if p.get("vwap_gate", True) and state.vwap_dip_score < 0.0:
        logger.debug("Scale-in skipped — price above 4h VWAP (dip_score=%.4f zone=%s)",
                     state.vwap_dip_score, state.vwap_dip_zone)
        return

    invested = state.holdings_btc * price
    scale_usdt = _scale_in_amount(state, price)
    if invested + scale_usdt > max_invest:
        logger.debug("Scale-in skipped — max invested (%.0f%%)", p["max_invested_pct"] * 100)
        return
    if scale_usdt > state.free_usdt:
        return

    dip_pct = ((state.avg_entry - price) / state.avg_entry * 100.0
               if state.avg_entry > 0 else 0.0)
    reason = f"obi_dip({dip_pct:+.1f}%)"
    if _buy(state, price, scale_usdt, reason, ts_ms, db):
        state.last_buy_ts = ts_ms

# ---------------------------------------------------------------------------
# Indicators ZMQ handler
# ---------------------------------------------------------------------------

def _handle_vwap(state: AccumState, msg: dict) -> None:
    dip_score = msg.get("dip_score")
    dip_zone  = msg.get("dip_zone", "neutral")
    if dip_score is not None:
        state.vwap_dip_score = float(dip_score)
    state.vwap_dip_zone = str(dip_zone)
    logger.debug("VWAP: dip_score=%.4f zone=%s", state.vwap_dip_score, state.vwap_dip_zone)


async def _handle_indicator(state: AccumState, db: sqlite3.Connection,
                             msg: dict, ts_ms: int) -> None:
    mid = msg.get("mid")
    if mid is None:
        return

    state.last_price = float(mid)

    obi_ema = msg.get("obi_ema")
    if obi_ema is not None:
        state.obi_ema = float(obi_ema)

    # Spread EMA: convert spread_bps to pct for adaptive rebuy discount
    spread_bps = msg.get("spread_bps")
    if spread_bps is not None:
        spread_pct = float(spread_bps) / 100.0
        state.spread_ema = 0.1 * spread_pct + 0.9 * state.spread_ema

    state.snap_counter += 1
    if state.snap_counter % state.p["snapshot_every_n"] == 0:
        invested = state.holdings_btc * state.avg_entry if state.avg_entry > 0 else 0.0
        db.execute("""
            INSERT INTO accum_snapshots
                (ts_ms, price, holdings_btc, avg_entry, invested_usdt,
                 free_usdt, unrealized_pct, obi_ema)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ts_ms, float(mid), state.holdings_btc, state.avg_entry or 0.0,
             invested, state.free_usdt, state.unrealized_pct(), state.obi_ema))
        db.commit()

    price = float(mid)

    if not state.initial_done:
        init_usdt = min(state.p["initial_stake_usdt"], state.free_usdt)
        if _buy(state, price, init_usdt, "initial", ts_ms, db):
            state.last_buy_ts = ts_ms
            state.initial_done = True
        return

    _check_profit_bands(state, price, ts_ms, db)
    _check_rebuys(state, price, ts_ms, db)

    thresh = state.p["obi_entry_thresh"]
    if state.obi_ema < -thresh:
        state.pending_count += 1
    else:
        state.pending_count = 0

    if state.pending_count >= state.p["obi_confirm_n"]:
        _check_obi_scale_in(state, price, ts_ms, db)
        state.pending_count = 0

# ---------------------------------------------------------------------------
# ZMQ subscription loop
# ---------------------------------------------------------------------------

async def _zmq_loop(state: AccumState, db: sqlite3.Connection) -> None:
    try:
        import zmq
        import zmq.asyncio as azmq
    except ImportError:
        logger.error("pyzmq not installed — run: pip install pyzmq")
        return

    addr        = state.p.get("indicators_addr", "tcp://127.0.0.1:5559")
    scalping_id = state.p.get("scalping_stream_id", "btc_scalping_spot")

    ctx = azmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(addr)
    logger.info("ZMQ SUB → %s  (streams: %s, btc_vwap_context)", addr, scalping_id)

    try:
        while True:
            try:
                msg = await sub.recv_json()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("ZMQ recv error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)
                continue

            sid = msg.get("stream_id")
            if sid == scalping_id:
                ts_ms = int(msg.get("ts", time.time()) * 1000)
                await _handle_indicator(state, db, msg, ts_ms)
            elif sid == "btc_vwap_context":
                _handle_vwap(state, msg)
    finally:
        sub.close()

# ---------------------------------------------------------------------------
# Stats loop (every 60s)
# ---------------------------------------------------------------------------

async def _stats_loop(state: AccumState) -> None:
    while True:
        await asyncio.sleep(60)
        cooldown_s = max(0, state.p.get("min_scale_interval_s", 1800) -
                         (int(time.time() * 1000) - state.last_buy_ts) / 1000)
        logger.info(
            "HOLD %.6f BTC @ avg %.2f  price=%.2f  uPnL=%+.2f%%  "
            "free=%.2f  realized=%+.2f  spread=%.4f%%  bands=%s  rebuys=%d  "
            "dip_in=%ds  obi=%.3f  vwap_dip=%.4f(%s)",
            state.holdings_btc, state.avg_entry or 0.0, state.last_price,
            state.unrealized_pct(), state.free_usdt, state.total_realized,
            state.spread_ema, sorted(state.active_bands),
            len(state.pending_rebuys), int(cooldown_s),
            state.obi_ema, state.vwap_dip_score, state.vwap_dip_zone)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(p: dict, db: sqlite3.Connection) -> None:
    state = AccumState(p=p, free_usdt=p["capital_usdt"])

    restored = _restore_state(state, db)

    logger.info("Accumulation bot v1.2 — %s  capital=%.0f USDT  paper=True%s",
                p["symbol"], p["capital_usdt"],
                "  [RESTORED]" if restored else "")
    logger.info("Initial %.0f USDT  scale-in %.0f-%.0f USDT  "
                "every %dmin (dip_factor=%.1f  max_mult=%.1f×)",
                p["initial_stake_usdt"],
                p["scale_in_usdt"],
                p["scale_in_usdt"] * p.get("scale_in_max_mult", 3.0),
                p.get("min_scale_interval_s", 1800) // 60,
                p.get("scale_in_dip_factor", 0.5),
                p.get("scale_in_max_mult", 3.0))
    logger.info("Profit bands: %s%%  sell %.0f%%  min_hold %.0f%%  "
                "rebuy discount %.2f–%.2f%% (spread×%.1f)",
                p.get("profit_bands_pct"),
                p.get("sell_fraction", 0.20) * 100,
                p.get("min_holdings_pct", 0.0) * 100,
                p.get("rebuy_discount_min_pct", 0.15),
                p.get("rebuy_discount_max_pct", 1.00),
                p.get("rebuy_spread_mult", 3.0))
    logger.info("VWAP gate: %s  indicators: %s",
                "enabled" if p.get("vwap_gate", True) else "disabled",
                p.get("indicators_addr", "tcp://127.0.0.1:5559"))

    tasks = [
        asyncio.create_task(_zmq_loop(state, db)),
        asyncio.create_task(_stats_loop(state)),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown")


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC long-term accumulation bot v1.2")
    ap.add_argument("--strategy", metavar="JSON")
    ap.add_argument("--dir",      default=None)
    args = ap.parse_args()

    install_dir = Path(args.dir).expanduser() if args.dir else Path.home() / "tradinebotte"
    install_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(install_dir / "accumulation_bot.log")

    p = dict(DEFAULTS)
    if args.strategy:
        strat = Path(args.strategy)
        if not strat.is_absolute():
            strat = install_dir / strat
        with open(strat) as f:
            p.update({k: v for k, v in json.load(f).items()
                      if not k.startswith("_")})

    db = init_db(install_dir / "live_accum.db")
    try:
        asyncio.run(_run(p, db))
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        db.close()


if __name__ == "__main__":
    main()
