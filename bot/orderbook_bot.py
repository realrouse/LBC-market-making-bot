#!/usr/bin/env python3
"""
Binance OBI scalping bot — real-time L2 orderbook signal (contrarian).

Connects to Binance combined WebSocket streams (depth20@100ms + aggTrade) for
spot and/or perpetual markets.  Computes OBI (Order Book Imbalance) from the
top N bid/ask levels, smoothed with an EMA, and TFI (Trade Flow Imbalance)
over a rolling time window from aggTrade events.

Signal
------
  OBI = (Σ bid_qty[:n] − Σ ask_qty[:n]) / (Σ bid_qty[:n] + Σ ask_qty[:n])
  OBI ∈ [−1, +1]:  +1 = all depth on the bid (bid-heavy)
                   −1 = all depth on the ask (ask-heavy)

  TFI = (buy_vol − sell_vol) / (buy_vol + sell_vol)  over tfi_window_s seconds
  TFI ∈ [−1, +1]:  −1 = all taker selling (net sellers)
                   +1 = all taker buying (net buyers)
  (m=True in aggTrade = buyer is maker → taker SOLD)

  Empirically, high bid depth (OBI > 0) precedes price drops as those bids are
  consumed or cancelled — OBI is contrarian on Binance at sub-minute timeframes.

  Short entry : OBI_ema > +entry_thresh  for ≥ confirm_n consecutive snapshots
                AND TFI < −tfi_confirm_thresh  (when tfi_confirm_thresh > 0)
  Exit        : TP / SL / max-hold  (obi_exit disabled)

  TFI is always recorded at entry for later analysis even when tfi_confirm_thresh=0.

Usage
-----
    python3 bot/orderbook_bot.py                               # paper, both streams
    python3 bot/orderbook_bot.py --spot-only
    python3 bot/orderbook_bot.py --perp-only
    python3 bot/orderbook_bot.py --strategy strategies/scalping/orderbook_btc.json
    python3 bot/orderbook_bot.py --dir ~/tradinebotte
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import sqlite3
import sys
import time
from collections import deque
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

logger = logging.getLogger("orderbook_bot")

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULTS = {
    "symbol":              "BTCUSDT",
    "modes":               ["spot", "perp"],
    "capital_spot":        1000.0,
    "capital_perp":        1000.0,
    "stake_frac":          0.10,
    "obi_levels":          10,
    "obi_ema_alpha":       0.15,
    "obi_entry_thresh":    0.30,
    "obi_exit_thresh":     0.05,
    "obi_confirm_n":       3,
    "tp_pct":              0.005,
    "sl_pct":              0.003,
    "max_hold_minutes":    30,
    "fee_spot":            0.001,
    "fee_perp":            0.0005,
    "slippage":            0.0005,
    "use_limit_orders":    False,
    "maker_fee_spot":      0.0002,
    "maker_fee_perp":      0.0002,
    "snapshot_every_n":    10,
    # TFI parameters
    "tfi_window_s":        30,     # rolling window for trade flow
    "tfi_confirm_thresh":  0.0,    # 0 = log only (no gating); >0 = require TFI < -thresh
}

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ob_snapshots (
            id        INTEGER PRIMARY KEY,
            ts_ms     INTEGER NOT NULL,
            mode      TEXT    NOT NULL,
            best_bid  REAL,
            best_ask  REAL,
            spread    REAL,
            obi_raw   REAL,
            obi_ema   REAL,
            tfi       REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ob_trades (
            id             INTEGER PRIMARY KEY,
            mode           TEXT    NOT NULL,
            direction      TEXT    NOT NULL,
            entry_ts_ms    INTEGER,
            entry_price    REAL,
            entry_obi      REAL,
            entry_tfi      REAL,
            stake          REAL,
            qty            REAL,
            fee_entry      REAL,
            tp             REAL,
            sl             REAL,
            exit_ts_ms     INTEGER,
            exit_price     REAL,
            exit_reason    TEXT,
            pnl_net        REAL,
            capital_before REAL,
            capital_after  REAL
        )""")
    conn.commit()
    # Migrations for existing DBs
    for col, definition in [("tfi", "REAL"), ("entry_tfi", "REAL")]:
        for table in (("ob_snapshots", "tfi"), ("ob_trades", "entry_tfi")):
            try:
                conn.execute(f"ALTER TABLE {table[0]} ADD COLUMN {table[1]} REAL")
                conn.commit()
            except sqlite3.OperationalError:
                pass
    return conn

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class Position:
    __slots__ = ("mode", "direction", "entry_ts_ms", "entry_price", "entry_obi",
                 "entry_tfi", "stake", "qty", "fee_entry", "tp", "sl",
                 "capital_before", "max_hold_ms", "db_id")

    def __init__(self, *, mode, direction, entry_ts_ms, entry_price, entry_obi,
                 entry_tfi, stake, qty, fee_entry, tp, sl, capital_before,
                 max_hold_minutes):
        self.mode           = mode
        self.direction      = direction
        self.entry_ts_ms    = entry_ts_ms
        self.entry_price    = entry_price
        self.entry_obi      = entry_obi
        self.entry_tfi      = entry_tfi
        self.stake          = stake
        self.qty            = qty
        self.fee_entry      = fee_entry
        self.tp             = tp
        self.sl             = sl
        self.capital_before = capital_before
        self.max_hold_ms    = max_hold_minutes * 60_000
        self.db_id          = None


class StreamState:
    def __init__(self, mode: str, p: dict):
        self.mode          = mode
        self.p             = p
        self.obi_ema       = 0.0
        self.pending_dir   = None
        self.pending_count = 0
        self.position      = None
        self.capital       = p[f"capital_{mode}"]
        self.snap_counter  = 0
        self.total_trades  = 0
        self.wins          = 0
        self.losses        = 0
        self.total_pnl     = 0.0
        # TFI rolling window: deque of (ts_ms, is_buy, qty)
        self.tfi_trades:   deque = deque()
        self.tfi_buy_vol:  float = 0.0
        self.tfi_sell_vol: float = 0.0

# ---------------------------------------------------------------------------
# OBI / TFI
# ---------------------------------------------------------------------------

def compute_obi(bids: list, asks: list, n: int) -> float:
    bid_vol = sum(float(qty) for _, qty in bids[:n])
    ask_vol = sum(float(qty) for _, qty in asks[:n])
    total   = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0


def compute_tfi(buy_vol: float, sell_vol: float) -> float:
    total = buy_vol + sell_vol
    return (buy_vol - sell_vol) / total if total > 0 else 0.0


def _update_tfi(state: StreamState, is_buy: bool, qty: float, ts_ms: int) -> None:
    """Add one aggTrade to the rolling TFI window and evict stale entries."""
    window_ms = state.p.get("tfi_window_s", 30) * 1000
    state.tfi_trades.append((ts_ms, is_buy, qty))
    if is_buy:
        state.tfi_buy_vol += qty
    else:
        state.tfi_sell_vol += qty
    while state.tfi_trades and ts_ms - state.tfi_trades[0][0] > window_ms:
        old_ts, old_is_buy, old_qty = state.tfi_trades.popleft()
        if old_is_buy:
            state.tfi_buy_vol = max(0.0, state.tfi_buy_vol - old_qty)
        else:
            state.tfi_sell_vol = max(0.0, state.tfi_sell_vol - old_qty)

# ---------------------------------------------------------------------------
# Paper trade helpers
# ---------------------------------------------------------------------------

def _open_paper(state: StreamState, direction: str, mid: float,
                tfi: float, ts_ms: int, db: sqlite3.Connection) -> None:
    p    = state.p
    slip = p["slippage"]
    if p.get("use_limit_orders"):
        entry_px = mid
        fee_rate = p[f"maker_fee_{state.mode}"]
    else:
        entry_px = mid * (1 + slip) if direction == "long" else mid * (1 - slip)
        fee_rate = p[f"fee_{state.mode}"]
    stake     = state.capital * p["stake_frac"]
    qty       = stake / entry_px
    fee_entry = stake * fee_rate

    if direction == "long":
        tp = entry_px * (1 + p["tp_pct"])
        sl = entry_px * (1 - p["sl_pct"])
    else:
        tp = entry_px * (1 - p["tp_pct"])
        sl = entry_px * (1 + p["sl_pct"])

    pos = Position(
        mode=state.mode, direction=direction, entry_ts_ms=ts_ms,
        entry_price=entry_px, entry_obi=state.obi_ema, entry_tfi=tfi,
        stake=stake, qty=qty, fee_entry=fee_entry,
        tp=tp, sl=sl, capital_before=state.capital,
        max_hold_minutes=p["max_hold_minutes"],
    )
    state.position = pos

    cur = db.execute("""
        INSERT INTO ob_trades
            (mode, direction, entry_ts_ms, entry_price, entry_obi, entry_tfi,
             stake, qty, fee_entry, tp, sl, capital_before)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (pos.mode, pos.direction, ts_ms, entry_px, state.obi_ema, tfi,
         stake, qty, fee_entry, tp, sl, state.capital))
    db.commit()
    pos.db_id = cur.lastrowid

    logger.info("[%s] OPEN %s @ %.2f  OBI=%.3f  TFI=%+.3f  TP=%.2f  SL=%.2f  stake=%.2f",
                state.mode, direction.upper(), entry_px,
                state.obi_ema, tfi, tp, sl, stake)


def _close_paper(state: StreamState, mid: float, reason: str,
                 ts_ms: int, db: sqlite3.Connection) -> None:
    pos = state.position
    if pos is None:
        return
    p    = state.p
    slip = p["slippage"]
    if p.get("use_limit_orders"):
        fee_rate = p[f"maker_fee_{state.mode}"]
        exit_px  = mid
    else:
        fee_rate = p[f"fee_{state.mode}"]
        exit_px  = mid * (1 - slip) if pos.direction == "long" else mid * (1 + slip)

    if pos.direction == "long":
        exit_val = pos.qty * exit_px
        fee_exit = exit_val * fee_rate
        pnl_net  = exit_val - fee_exit - pos.stake - pos.fee_entry
    else:
        exit_val = pos.qty * exit_px
        fee_exit = exit_val * fee_rate
        pnl_net  = pos.stake - exit_val - fee_exit - pos.fee_entry

    state.capital = pos.capital_before + pnl_net
    state.total_trades += 1
    state.total_pnl    += pnl_net
    if pnl_net > 0:
        state.wins += 1
    else:
        state.losses += 1

    db.execute("""
        UPDATE ob_trades
        SET exit_ts_ms=?, exit_price=?, exit_reason=?, pnl_net=?, capital_after=?
        WHERE id=?""",
        (ts_ms, exit_px, reason, pnl_net, state.capital, pos.db_id))
    db.commit()

    logger.info("[%s] CLOSE %s @ %.2f  reason=%-10s  PnL=%+.4f  capital=%.2f",
                state.mode, pos.direction.upper(), exit_px, reason, pnl_net, state.capital)
    state.position = None

# ---------------------------------------------------------------------------
# Message handlers
# ---------------------------------------------------------------------------

async def _handle_depth(state: StreamState, db: sqlite3.Connection,
                        data: dict, ts_ms: int) -> None:
    bids = data.get("bids") or data.get("b")
    asks = data.get("asks") or data.get("a")
    if not bids or not asks:
        return

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid      = (best_bid + best_ask) / 2.0
    spread   = (best_ask - best_bid) / mid if mid > 0 else 0.0

    p     = state.p
    alpha = p["obi_ema_alpha"]
    obi_raw       = compute_obi(bids, asks, p["obi_levels"])
    state.obi_ema = alpha * obi_raw + (1 - alpha) * state.obi_ema
    tfi           = compute_tfi(state.tfi_buy_vol, state.tfi_sell_vol)

    # Snapshot recording
    state.snap_counter += 1
    if state.snap_counter % p["snapshot_every_n"] == 0:
        db.execute("""
            INSERT INTO ob_snapshots
                (ts_ms, mode, best_bid, best_ask, spread, obi_raw, obi_ema, tfi)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ts_ms, state.mode, best_bid, best_ask, spread, obi_raw, state.obi_ema, tfi))
        db.commit()

    # Exit check
    if state.position is not None:
        pos    = state.position
        reason = None
        if pos.direction == "long":
            if   mid >= pos.tp:                               reason = "tp"
            elif mid <= pos.sl:                               reason = "sl"
            elif ts_ms - pos.entry_ts_ms >= pos.max_hold_ms: reason = "timeout"
        else:
            if   mid <= pos.tp:                               reason = "tp"
            elif mid >= pos.sl:                               reason = "sl"
            elif ts_ms - pos.entry_ts_ms >= pos.max_hold_ms: reason = "timeout"
        if reason:
            _close_paper(state, mid, reason, ts_ms, db)
        return

    # Entry signal
    entry_thresh  = p["obi_entry_thresh"]
    confirm_n     = p["obi_confirm_n"]
    tfi_thresh    = p.get("tfi_confirm_thresh", 0.0)
    # TFI gate: when tfi_thresh > 0, require net selling (TFI < -thresh)
    tfi_ok = (tfi_thresh <= 0.0) or (tfi < -tfi_thresh)

    if state.obi_ema > entry_thresh:
        if state.pending_dir == "short":
            state.pending_count += 1
        else:
            state.pending_dir   = "short"
            state.pending_count = 1
    else:
        state.pending_dir   = None
        state.pending_count = 0

    if state.pending_count >= confirm_n and state.pending_dir and tfi_ok:
        _open_paper(state, state.pending_dir, mid, tfi, ts_ms, db)
        state.pending_dir   = None
        state.pending_count = 0


async def _handle_message(state: StreamState, db: sqlite3.Connection,
                          raw: str) -> None:
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    ts_ms = int(time.time() * 1000)

    # Combined stream: {"stream": "...", "data": {...}}
    data        = msg.get("data", msg)
    event_type  = data.get("e", "")

    if event_type == "aggTrade":
        # m=True: buyer is maker → taker SOLD → is_buy=False
        is_buy = not data.get("m", True)
        qty    = float(data.get("q", 0))
        _update_tfi(state, is_buy, qty, ts_ms)
    else:
        await _handle_depth(state, db, data, ts_ms)

# ---------------------------------------------------------------------------
# WebSocket loop
# ---------------------------------------------------------------------------

def _ws_url(mode: str, symbol: str) -> str:
    sym = symbol.lower()
    if mode == "spot":
        base = "wss://stream.binance.com:9443/stream"
    else:
        base = "wss://fstream.binance.com/stream"
    return f"{base}?streams={sym}@depth20@100ms/{sym}@aggTrade"


async def _ws_loop(state: StreamState, db: sqlite3.Connection) -> None:
    url     = _ws_url(state.mode, state.p["symbol"])
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("[%s] connected", state.mode)
                backoff = 1.0
                async for raw in ws:
                    await _handle_message(state, db, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[%s] disconnected (%s) — retry in %.0fs",
                           state.mode, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

# ---------------------------------------------------------------------------
# Stats display (every 60s)
# ---------------------------------------------------------------------------

async def _stats_loop(states: list) -> None:
    while True:
        await asyncio.sleep(60)
        for st in states:
            wr  = f"{st.wins}/{st.total_trades}" if st.total_trades else "0/0"
            tfi = compute_tfi(st.tfi_buy_vol, st.tfi_sell_vol)
            pos = (f"{st.position.direction.upper()}@{st.position.entry_price:.2f}"
                   if st.position else "flat")
            logger.info("[%s] OBI=%.3f  TFI=%+.3f  capital=%.2f  W/T=%s  PnL=%+.4f  %s",
                        st.mode, st.obi_ema, tfi, st.capital, wr, st.total_pnl, pos)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(p: dict, db: sqlite3.Connection) -> None:
    modes  = p["modes"]
    states = [StreamState(m, p) for m in modes]

    tfi_thresh = p.get("tfi_confirm_thresh", 0.0)
    tfi_mode   = f"TFI<-{tfi_thresh:.2f}" if tfi_thresh > 0 else "TFI logged (no gate)"

    logger.info("OrderBook bot started — symbol=%s  modes=%s  paper=True",
                p["symbol"], modes)
    logger.info("OBI: entry=%.2f  exit=%.2f  confirm=%d  alpha=%.2f  levels=%d",
                p["obi_entry_thresh"], p["obi_exit_thresh"],
                p["obi_confirm_n"], p["obi_ema_alpha"], p["obi_levels"])
    logger.info("Trade: tp=%.3f%%  sl=%.3f%%  max_hold=%dm  stake=%.0f%%",
                p["tp_pct"] * 100, p["sl_pct"] * 100,
                p["max_hold_minutes"], p["stake_frac"] * 100)
    logger.info("TFI: window=%ds  gate=%s",
                p.get("tfi_window_s", 30), tfi_mode)

    tasks = [asyncio.create_task(_ws_loop(st, db)) for st in states]
    tasks.append(asyncio.create_task(_stats_loop(states)))

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        ts_ms = int(time.time() * 1000)
        for st in states:
            if st.position is not None:
                db.execute("""
                    UPDATE ob_trades SET exit_ts_ms=?, exit_reason='shutdown'
                    WHERE id=? AND exit_ts_ms IS NULL""",
                    (ts_ms, st.position.db_id))
        db.commit()
        logger.info("Shutdown complete")


def main() -> None:
    ap = argparse.ArgumentParser(description="Binance OBI scalping bot (paper trading)")
    ap.add_argument("--strategy",  metavar="JSON", help="Strategy config JSON file")
    ap.add_argument("--dir",       default=None,   help="Install directory for DB/log files")
    ap.add_argument("--spot-only", action="store_true")
    ap.add_argument("--perp-only", action="store_true")
    args = ap.parse_args()

    p = dict(DEFAULTS)
    if args.strategy:
        cfg = Path(args.strategy)
        if not cfg.exists():
            print(f"ERROR: config not found: {cfg}", file=sys.stderr)
            sys.exit(1)
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        p.update({k: v for k, v in raw.items() if not k.startswith("_")})

    if args.spot_only:
        p["modes"] = ["spot"]
    elif args.perp_only:
        p["modes"] = ["perp"]

    install_dir = Path(args.dir) if args.dir else Path.home() / "tradinebotte"
    install_dir.mkdir(parents=True, exist_ok=True)

    _setup_logging(install_dir / "orderbook_bot.log")
    db = init_db(install_dir / "live_ob.db")
    logger.info("DB: %s", install_dir / "live_ob.db")

    try:
        asyncio.run(_run(p, db))
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    main()
