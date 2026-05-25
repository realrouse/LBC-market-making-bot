#!/usr/bin/env python3
"""
BTC Long-Term Accumulation Bot

Builds a growing BTC spot position using OBI dip-buying + partial profit ladder.

Strategy:
  1. Initial buy at startup (initial_stake_usdt)
  2. OBI dip signal: OBI_ema < -entry_thresh for confirm_n consecutive ticks
     → scale in (scale_in_usdt), with min_scale_interval_s cooldown
  3. Profit ladder: when price >= avg_entry * (1 + band_pct), sell sell_fraction
     of current holdings and set a rebuy target below the sell price
  4. Rebuy: when price drops to rebuy_target, buy back the sold quantity

Ratchet effect: extract profit on every meaningful rise, rebuy the dip,
always maintaining a growing base BTC position. Never fully exits.

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

try:
    import websockets
except ImportError:
    print("ERROR: websockets not installed — run: pip install websockets", file=sys.stderr)
    sys.exit(1)

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
    "symbol":               "BTCUSDT",
    "capital_usdt":         1000.0,
    "initial_stake_usdt":   200.0,
    "scale_in_usdt":        100.0,
    "max_invested_pct":     0.90,
    "obi_levels":           10,
    "obi_ema_alpha":        0.05,
    "obi_entry_thresh":     0.50,
    "obi_confirm_n":        20,
    "min_scale_interval_s": 1800,
    "profit_bands_pct":     [0.5, 1.0, 2.0, 3.0, 5.0],
    "sell_fraction":        0.20,
    "rebuy_discount_pct":   0.30,
    "fee_spot":             0.001,
    "maker_fee_spot":       0.0002,
    "use_limit_orders":     True,
    "snapshot_every_n":     20,
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
    p:              dict
    holdings_btc:   float = 0.0
    avg_entry:      float = 0.0
    free_usdt:      float = 0.0
    last_price:     float = 0.0
    obi_ema:        float = 0.0
    pending_count:  int   = 0
    last_buy_ts:    int   = 0
    initial_done:   bool  = False
    pending_rebuys: list  = field(default_factory=list)
    active_bands:   set   = field(default_factory=set)
    snap_counter:   int   = 0
    total_realized: float = 0.0

    def unrealized_pct(self) -> float:
        if self.avg_entry > 0 and self.last_price > 0:
            return (self.last_price - self.avg_entry) / self.avg_entry * 100.0
        return 0.0

    def max_investable(self) -> float:
        return self.p["capital_usdt"] * self.p["max_invested_pct"]

# ---------------------------------------------------------------------------
# OBI
# ---------------------------------------------------------------------------

def compute_obi(bids: list, asks: list, levels: int) -> float:
    bid_vol = sum(float(b[1]) for b in bids[:levels])
    ask_vol = sum(float(a[1]) for a in asks[:levels])
    total   = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0

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

    # Weighted average entry update
    if state.holdings_btc > 0 and state.avg_entry > 0:
        total_btc     = state.holdings_btc + qty_btc
        state.avg_entry = (state.holdings_btc * state.avg_entry + qty_btc * price) / total_btc
    else:
        state.avg_entry = price

    state.holdings_btc += qty_btc
    state.free_usdt    -= total
    state.last_buy_ts   = ts_ms

    db.execute("""
        INSERT INTO accum_trades
            (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
             avg_entry_after, holdings_after, free_usdt_after)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, "buy", reason, price, qty_btc, usdt_amount, fee,
         state.avg_entry, state.holdings_btc, state.free_usdt))
    db.commit()

    upnl = state.unrealized_pct()
    logger.info("BUY  %.6f BTC @ %8.2f  [%-20s]  avg=%8.2f  held=%.6f  free=%7.2f  uPnL=%+.2f%%",
                qty_btc, price, reason, state.avg_entry,
                state.holdings_btc, state.free_usdt, upnl)
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

    state.holdings_btc  -= qty_btc
    state.free_usdt     += usdt_val - fee
    state.total_realized += realized

    db.execute("""
        INSERT INTO accum_trades
            (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
             avg_entry_after, holdings_after, free_usdt_after)
        VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (ts_ms, "sell", reason, price, qty_btc, usdt_val, fee,
         state.avg_entry, state.holdings_btc, state.free_usdt))
    db.commit()

    logger.info("SELL %.6f BTC @ %8.2f  [%-20s]  realized=%+7.2f  held=%.6f  free=%7.2f",
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
    discount = p.get("rebuy_discount_pct", 0.30) / 100.0

    for band_pct in bands:
        if band_pct in state.active_bands:
            continue
        target = state.avg_entry * (1.0 + band_pct / 100.0)
        if price < target:
            break  # bands are sorted ascending — no point checking higher ones
        qty = state.holdings_btc * fraction
        if qty < 1e-6:
            continue
        if _sell(state, price, qty, f"profit+{band_pct:.1f}%", ts_ms, db):
            rebuy = price * (1.0 - discount)
            state.active_bands.add(band_pct)
            state.pending_rebuys.append(
                PendingRebuy(band_pct=band_pct, sell_price=price,
                             qty_btc=qty, rebuy_price=rebuy))
            logger.info("  → rebuy %.6f BTC target @ %.2f (-%.2f%%)",
                        qty, rebuy, discount * 100)


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
    p           = state.p
    min_iv      = p.get("min_scale_interval_s", 1800)
    scale_usdt  = p.get("scale_in_usdt", 100.0)
    max_invest  = state.max_investable()

    if (ts_ms - state.last_buy_ts) / 1000.0 < min_iv:
        return
    invested = state.holdings_btc * price
    if invested + scale_usdt > max_invest:
        logger.debug("Scale-in skipped — max invested (%.0f%%)", p["max_invested_pct"] * 100)
        return
    if scale_usdt > state.free_usdt:
        return

    if _buy(state, price, scale_usdt, "obi_dip", ts_ms, db):
        # Fresh scale-in: reset band tracking so bands re-evaluate vs new avg_entry
        state.active_bands.clear()
        state.pending_rebuys.clear()

# ---------------------------------------------------------------------------
# WebSocket handler
# ---------------------------------------------------------------------------

async def _handle_depth(state: AccumState, db: sqlite3.Connection,
                        data: dict, ts_ms: int) -> None:
    bids = data.get("bids") or data.get("b")
    asks = data.get("asks") or data.get("a")
    if not bids or not asks:
        return

    best_bid     = float(bids[0][0])
    best_ask     = float(asks[0][0])
    mid          = (best_bid + best_ask) / 2.0
    state.last_price = mid

    p         = state.p
    obi_raw   = compute_obi(bids, asks, p["obi_levels"])
    state.obi_ema = p["obi_ema_alpha"] * obi_raw + (1 - p["obi_ema_alpha"]) * state.obi_ema

    # Periodic snapshot
    state.snap_counter += 1
    if state.snap_counter % p["snapshot_every_n"] == 0:
        invested = state.holdings_btc * state.avg_entry if state.avg_entry > 0 else 0.0
        db.execute("""
            INSERT INTO accum_snapshots
                (ts_ms, price, holdings_btc, avg_entry, invested_usdt,
                 free_usdt, unrealized_pct, obi_ema)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ts_ms, mid, state.holdings_btc, state.avg_entry or 0.0,
             invested, state.free_usdt, state.unrealized_pct(), state.obi_ema))
        db.commit()

    # Initial buy
    if not state.initial_done:
        init_usdt = min(p["initial_stake_usdt"], state.free_usdt)
        _buy(state, mid, init_usdt, "initial", ts_ms, db)
        state.initial_done = True
        return

    # Check profit bands (sell partial on rises)
    _check_profit_bands(state, mid, ts_ms, db)

    # Check pending rebuys (buy back after profit takes)
    _check_rebuys(state, mid, ts_ms, db)

    # OBI dip signal → scale in
    thresh = p["obi_entry_thresh"]
    if state.obi_ema < -thresh:
        state.pending_count += 1
    else:
        state.pending_count = 0

    if state.pending_count >= p["obi_confirm_n"]:
        _check_obi_scale_in(state, mid, ts_ms, db)
        state.pending_count = 0

# ---------------------------------------------------------------------------
# WebSocket loop
# ---------------------------------------------------------------------------

async def _ws_loop(state: AccumState, db: sqlite3.Connection) -> None:
    sym = state.p["symbol"].lower()
    url = f"wss://stream.binance.com:9443/ws/{sym}@depth20@100ms"
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("connected — %s", url)
                backoff = 1.0
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    await _handle_depth(state, db, msg, int(time.time() * 1000))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("WS disconnected (%s) — retry in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ---------------------------------------------------------------------------
# Stats loop (every 60s)
# ---------------------------------------------------------------------------

async def _stats_loop(state: AccumState) -> None:
    while True:
        await asyncio.sleep(60)
        price   = state.last_price
        held    = state.holdings_btc
        avg     = state.avg_entry or 0.0
        upnl    = state.unrealized_pct()
        real    = state.total_realized
        rebuys  = len(state.pending_rebuys)
        bands   = sorted(state.active_bands)
        cooldown_s = max(0, state.p.get("min_scale_interval_s", 1800) -
                         (int(time.time() * 1000) - state.last_buy_ts) / 1000)
        logger.info(
            "HOLD %.6f BTC @ avg %.2f  price=%.2f  uPnL=%+.2f%%  "
            "free=%.2f  realized=%+.2f  bands=%s  rebuys=%d  next_dip_in=%ds",
            held, avg, price, upnl,
            state.free_usdt, real, bands, rebuys, int(cooldown_s))

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(p: dict, db: sqlite3.Connection) -> None:
    state = AccumState(p=p, free_usdt=p["capital_usdt"])

    bands  = p.get("profit_bands_pct", [])
    frac   = p.get("sell_fraction", 0.20)
    disc   = p.get("rebuy_discount_pct", 0.30)
    min_iv = p.get("min_scale_interval_s", 1800)

    logger.info("Accumulation bot — %s  capital=%.0f USDT  paper=True",
                p["symbol"], p["capital_usdt"])
    logger.info("Initial %.0f USDT  scale-in %.0f USDT every %dmin (max %.0f%%)",
                p["initial_stake_usdt"], p["scale_in_usdt"],
                min_iv // 60, p["max_invested_pct"] * 100)
    logger.info("Profit bands: %s%%  sell %.0f%%  rebuy -%.2f%% below sell",
                bands, frac * 100, disc)

    tasks = [
        asyncio.create_task(_ws_loop(state, db)),
        asyncio.create_task(_stats_loop(state)),
    ]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Shutdown")


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC long-term accumulation bot (paper trading)")
    ap.add_argument("--strategy", metavar="JSON", help="Strategy config JSON")
    ap.add_argument("--dir",      default=None,   help="Install/data directory")
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
