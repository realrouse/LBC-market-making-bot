#!/usr/bin/env python3
"""
Binance OBI scalping bot — real-time L2 orderbook signal (contrarian).

Connects to Binance spot and/or perpetual WebSocket depth20 streams (100ms cadence).
Computes OBI (Order Book Imbalance) from the top N bid/ask levels, smoothed with an
exponential moving average. Enters paper trades when the smoothed OBI exceeds a
configurable threshold for a minimum number of consecutive snapshots.

Signal
------
  OBI = (Σ bid_qty[:n] − Σ ask_qty[:n]) / (Σ bid_qty[:n] + Σ ask_qty[:n])
  OBI ∈ [−1, +1]:  +1 = all depth on the bid (bid-heavy)
                   −1 = all depth on the ask (ask-heavy)

  Empirically, high bid depth (OBI > 0) precedes price drops as those bids are
  consumed or cancelled — OBI is contrarian on Binance at sub-minute timeframes.

  Short entry : OBI_ema > +entry_thresh  for ≥ confirm_n consecutive snapshots
  Long entry  : OBI_ema < −entry_thresh  for ≥ confirm_n
  Exit        : OBI_ema crosses exit_thresh  OR  TP / SL / max-hold

  When use_limit_orders=true: entry/exit at mid-price, maker_fee rates, zero slippage.

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
    # StreamHandler only when running interactively — avoids double-write when
    # nohup redirects stdout to the same file as the RotatingFileHandler.
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
    "symbol":             "BTCUSDT",
    "modes":              ["spot", "perp"],
    "capital_spot":       1000.0,
    "capital_perp":       1000.0,
    "stake_frac":         0.10,
    "obi_levels":         10,
    "obi_ema_alpha":      0.15,
    "obi_entry_thresh":   0.30,
    "obi_exit_thresh":    0.05,
    "obi_confirm_n":      3,
    "tp_pct":             0.005,
    "sl_pct":             0.003,
    "max_hold_minutes":   30,
    "fee_spot":           0.001,
    "fee_perp":           0.0005,
    "slippage":           0.0005,
    # Limit-order simulation: entry/exit at mid, no slippage, maker fee rates
    "use_limit_orders":   False,
    "maker_fee_spot":     0.0002,
    "maker_fee_perp":     0.0002,
    "snapshot_every_n":   10,
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
            obi_ema   REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ob_trades (
            id             INTEGER PRIMARY KEY,
            mode           TEXT    NOT NULL,
            direction      TEXT    NOT NULL,
            entry_ts_ms    INTEGER,
            entry_price    REAL,
            entry_obi      REAL,
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
    return conn

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class Position:
    __slots__ = ("mode", "direction", "entry_ts_ms", "entry_price", "entry_obi",
                 "stake", "qty", "fee_entry", "tp", "sl", "capital_before",
                 "max_hold_ms", "db_id")

    def __init__(self, *, mode, direction, entry_ts_ms, entry_price, entry_obi,
                 stake, qty, fee_entry, tp, sl, capital_before, max_hold_minutes):
        self.mode           = mode
        self.direction      = direction
        self.entry_ts_ms    = entry_ts_ms
        self.entry_price    = entry_price
        self.entry_obi      = entry_obi
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
        # obi_ema starts at 0.0 (neutral). The EMA needs ~1/alpha messages
        # (~7 at alpha=0.15) to reflect real order-book imbalance; signals
        # during warm-up are suppressed by obi_confirm_n consecutive checks.
        self.obi_ema       = 0.0
        self.pending_dir   = None   # 'long' | 'short' | None
        self.pending_count = 0
        self.position      = None
        # Capital resets to the configured value on every restart — paper
        # trading only; live_ob.db preserves the snapshot/trade history.
        self.capital       = p[f"capital_{mode}"]
        self.snap_counter  = 0
        self.total_trades  = 0
        self.wins          = 0
        self.losses        = 0
        self.total_pnl     = 0.0


# ---------------------------------------------------------------------------
# OBI
# ---------------------------------------------------------------------------

def compute_obi(bids: list, asks: list, n: int) -> float:
    bid_vol = sum(float(qty) for _, qty in bids[:n])
    ask_vol = sum(float(qty) for _, qty in asks[:n])
    total   = bid_vol + ask_vol
    return (bid_vol - ask_vol) / total if total > 0 else 0.0


# ---------------------------------------------------------------------------
# Paper trade helpers
# ---------------------------------------------------------------------------

def _open_paper(state: StreamState, direction: str, mid: float,
                ts_ms: int, db: sqlite3.Connection) -> None:
    p    = state.p
    slip = p["slippage"]
    if p.get("use_limit_orders"):
        entry_px = mid
        fee_rate = p[f"maker_fee_{state.mode}"]
    else:
        entry_px = mid * (1 + slip) if direction == "long" else mid * (1 - slip)
        fee_rate = p[f"fee_{state.mode}"]
    stake    = state.capital * p["stake_frac"]
    qty      = stake / entry_px
    fee_entry = stake * fee_rate

    if direction == "long":
        tp = entry_px * (1 + p["tp_pct"])
        sl = entry_px * (1 - p["sl_pct"])
    else:
        tp = entry_px * (1 - p["tp_pct"])
        sl = entry_px * (1 + p["sl_pct"])

    pos = Position(
        mode=state.mode, direction=direction, entry_ts_ms=ts_ms,
        entry_price=entry_px, entry_obi=state.obi_ema,
        stake=stake, qty=qty, fee_entry=fee_entry,
        tp=tp, sl=sl, capital_before=state.capital,
        max_hold_minutes=p["max_hold_minutes"],
    )
    state.position = pos

    cur = db.execute("""
        INSERT INTO ob_trades
            (mode, direction, entry_ts_ms, entry_price, entry_obi,
             stake, qty, fee_entry, tp, sl, capital_before)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (pos.mode, pos.direction, ts_ms, entry_px, state.obi_ema,
         stake, qty, fee_entry, tp, sl, state.capital))
    db.commit()
    pos.db_id = cur.lastrowid

    logger.info("[%s] OPEN %s @ %.2f  OBI=%.3f  TP=%.2f  SL=%.2f  stake=%.2f",
                state.mode, direction.upper(), entry_px, state.obi_ema, tp, sl, stake)


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
# Message handler (called on every WebSocket message)
# ---------------------------------------------------------------------------

async def _handle_message(state: StreamState, db: sqlite3.Connection, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    # Spot uses "bids"/"asks"; futures uses "b"/"a"
    bids = msg.get("bids") or msg.get("b")
    asks = msg.get("asks") or msg.get("a")
    if not bids or not asks:
        return

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid      = (best_bid + best_ask) / 2.0
    spread   = (best_ask - best_bid) / mid if mid > 0 else 0.0
    ts_ms    = int(time.time() * 1000)

    p     = state.p
    alpha = p["obi_ema_alpha"]
    obi_raw       = compute_obi(bids, asks, p["obi_levels"])
    state.obi_ema = alpha * obi_raw + (1 - alpha) * state.obi_ema

    # ── Snapshot recording ────────────────────────────────────────────────────
    state.snap_counter += 1
    if state.snap_counter % p["snapshot_every_n"] == 0:
        db.execute("""
            INSERT INTO ob_snapshots (ts_ms, mode, best_bid, best_ask, spread, obi_raw, obi_ema)
            VALUES (?,?,?,?,?,?,?)""",
            (ts_ms, state.mode, best_bid, best_ask, spread, obi_raw, state.obi_ema))
        db.commit()

    # ── Exit check ────────────────────────────────────────────────────────────
    if state.position is not None:
        pos = state.position
        reason = None
        # Contrarian OBI: long entered on OBI < -thresh, short entered on OBI > +thresh.
        # Exit when the triggering imbalance weakens back toward neutral.
        if pos.direction == "long":
            if   mid >= pos.tp:                             reason = "tp"
            elif mid <= pos.sl:                             reason = "sl"
            elif state.obi_ema > -p["obi_exit_thresh"]:    reason = "obi_exit"
            elif ts_ms - pos.entry_ts_ms >= pos.max_hold_ms: reason = "timeout"
        else:
            if   mid <= pos.tp:                             reason = "tp"
            elif mid >= pos.sl:                             reason = "sl"
            elif state.obi_ema < p["obi_exit_thresh"]:     reason = "obi_exit"
            elif ts_ms - pos.entry_ts_ms >= pos.max_hold_ms: reason = "timeout"

        if reason:
            _close_paper(state, mid, reason, ts_ms, db)
        return  # no new entry until next snapshot after a close

    # ── Entry signal ──────────────────────────────────────────────────────────
    entry_thresh = p["obi_entry_thresh"]
    confirm_n    = p["obi_confirm_n"]

    # Contrarian: bid-heavy orderbook → price falls → short; ask-heavy → price rises → long.
    # Spot can short in paper-trading simulation.
    if state.obi_ema > entry_thresh:
        if state.pending_dir == "short":
            state.pending_count += 1
        else:
            state.pending_dir   = "short"
            state.pending_count = 1
    elif state.obi_ema < -entry_thresh:
        if state.pending_dir == "long":
            state.pending_count += 1
        else:
            state.pending_dir   = "long"
            state.pending_count = 1
    else:
        state.pending_dir   = None
        state.pending_count = 0

    if state.pending_count >= confirm_n and state.pending_dir is not None:
        _open_paper(state, state.pending_dir, mid, ts_ms, db)
        state.pending_dir   = None
        state.pending_count = 0


# ---------------------------------------------------------------------------
# WebSocket loop
# ---------------------------------------------------------------------------

def _ws_url(mode: str, symbol: str) -> str:
    sym = symbol.lower()
    if mode == "spot":
        return f"wss://stream.binance.com:9443/ws/{sym}@depth20@100ms"
    return f"wss://fstream.binance.com/ws/{sym}@depth20@100ms"


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
            pos = (f"{st.position.direction.upper()}@{st.position.entry_price:.2f}"
                   if st.position else "flat")
            logger.info("[%s] OBI=%.3f  capital=%.2f  W/T=%s  PnL=%+.4f  %s",
                        st.mode, st.obi_ema, st.capital, wr, st.total_pnl, pos)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(p: dict, db: sqlite3.Connection) -> None:
    modes  = p["modes"]
    states = [StreamState(m, p) for m in modes]

    logger.info("OrderBook bot started — symbol=%s  modes=%s  paper=True",
                p["symbol"], modes)
    logger.info("OBI: entry=%.2f  exit=%.2f  confirm=%d  alpha=%.2f  levels=%d",
                p["obi_entry_thresh"], p["obi_exit_thresh"],
                p["obi_confirm_n"], p["obi_ema_alpha"], p["obi_levels"])
    logger.info("Trade: tp=%.3f%%  sl=%.3f%%  max_hold=%dm  stake=%.0f%%",
                p["tp_pct"] * 100, p["sl_pct"] * 100,
                p["max_hold_minutes"], p["stake_frac"] * 100)

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
