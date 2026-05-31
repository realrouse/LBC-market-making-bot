#!/usr/bin/env python3
"""
Binance OBI scalping bot — real-time L2 orderbook signal (contrarian).

Consumes pre-computed OBI and TFI values from the shared tradinebotte-indicators
service via ZMQ (streams: btc_scalping_spot, btc_scalping_perp).  No direct
Binance WebSocket connection is opened by this bot.

Signal
------
  OBI_ema published by the indicators service (btc_scalping_spot / perp):
    ask-heavy (OBI < -thresh) → contrarian LONG  (sellers absorbed → price up)
    bid-heavy (OBI > +thresh) → contrarian SHORT  (buyers absorbed → price down)

  TFI gate (optional, tfi_confirm_thresh > 0):
    tfi_gate_mode = "flat" (default):
      entry allowed only when |TFI| < thresh  (taker flow is neutral)
      rationale: high OBI + neutral TFI is the strongest predictive combination;
                 high OBI + strong TFI (either direction) shows mean-reversion reversal
    tfi_gate_mode = "directional" (legacy):
      long  requires TFI > +thresh  (net taker buying)
      short requires TFI < -thresh  (net taker selling)

Usage
-----
    python3 bot/orderbook_bot.py                               # paper, both streams
    python3 bot/orderbook_bot.py --spot-only
    python3 bot/orderbook_bot.py --perp-only
    python3 bot/orderbook_bot.py --strategy strategies/scalping/orderbook_btc.json
    python3 bot/orderbook_bot.py --dir ~/tradinebotte

Refactor history
----------------
  v2.12: Liquidations gate — block long entries when large long liquidation cascades
         are active (liq_long_usd > liq_long_block_usd, default $5M/5min). Mass long
         liquidations signal a falling market where contrarian longs face high adverse
         selection. Consumes btc_liquidations ZMQ stream. Disabled by default
         (liq_gate=false) for minimal disruption to existing deployments.
  v2.11: Funding rate gate — block long entries when the Binance perp funding rate
         exceeds a threshold (default 0.05%/8h). High positive funding means longs
         are crowded and paying carry; the ask-heavy contrarian OBI signal is weaker
         in that regime. Consumes btc_funding ZMQ stream (refreshed every 15min).
         Enabled via funding_gate=true in JSON. Short entries are unaffected (positive
         funding is a tailwind for shorts in the contrarian direction).
  v2.10: Macro OBI gate — block long entries when 1h-of-1m macro taker flow is
         bullish (direction="bullish"); ask-heavy micro OBI in a bullish macro
         is noise rather than genuine exhaustion. Consumes btc_macro_obi ZMQ
         stream (refreshed every 60s). Enabled via macro_obi_gate=true in JSON.
  v2.9: Volume profile gate — block long entries in sell-dominant HVN zones;
        accelerate entry (lower confirm_n) in buy-dominant HVN zones. Consumes
        btc_volume_profile stream from indicators service (hourly, 24h of 5m klines,
        $500 price buckets, top-5 HVN). Enabled via vol_profile_gate=true in JSON.
  v2.8: VWAP context gate — suspend long entries when price is above the 4h VWAP
        (entering into resistance); relax OBI threshold (0.65→0.50) when price is
        ≥1% below VWAP (strong dip context). VWAP published by indicators service
        via btc_vwap_context ZMQ stream. Enabled via vwap_gate=true in JSON.
  v2.7: TFI gate flipped to "flat" mode — require neutral TFI at entry rather than
        directional TFI. Data analysis on ob_snapshots showed OBI>0.6+TFI_flat has
        the strongest 30s predictive signal (+0.035); OBI+strong_TFI reverses.
        Removed orphaned obi_exit_thresh default (obi_exit was eliminated in v2.6).
  v2.6: Removed direct Binance WebSocket connections and local OBI/TFI computation.
        Now subscribes to tradinebotte-indicators via ZMQ, consistent with the
        project rule that all computation lives in the shared indicators service.
        Eliminated: _ws_loop, _poll_tfi_rest, compute_obi, compute_tfi, _update_tfi,
        _fetch_rest_agg_trades, _handle_depth, _handle_message, _ws_url.
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
    # obi_ema_alpha and tfi_window_s are informational — actual values are set
    # in the indicators service config (indicators_all.json).
    "obi_ema_alpha":       0.05,
    "obi_entry_thresh":    0.30,
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
    "tfi_window_s":        30,
    "tfi_confirm_thresh":  0.0,
    "direction":           "long",
    # VWAP context gate
    "vwap_gate":           False,
    "vwap_relax_min_pct":  0.01,   # relax OBI thresh when dip_score >= this value
    "vwap_obi_relax":      0.50,   # relaxed OBI entry threshold (below VWAP dip)
    # Volume profile gate
    "vol_profile_gate":    False,
    "vol_buy_confirm_n":   10,     # obi_confirm_n override inside a buy_hvn (faster entry)
    # Macro OBI gate
    "macro_obi_gate":      False,
    "macro_obi_stream_id": "btc_macro_obi",
    # Funding rate gate
    "funding_gate":        False,
    "funding_gate_thresh": 0.0005,   # 0.05%/8h — typical "crowded longs" threshold
    "funding_stream_id":   "btc_funding",
    # Liquidations gate (v2.12)
    "liq_gate":            False,
    "liq_stream_id":       "btc_liquidations",
    "liq_long_block_usd":  5_000_000,  # block longs during long cascade ($5M/5min)
    # Indicators service (ZMQ)
    "indicators_addr":     "tcp://127.0.0.1:5559",
    "spot_stream_id":      "btc_scalping_spot",
    "perp_stream_id":      "btc_scalping_perp",
    "vwap_stream_id":      "btc_vwap_context",
    "vol_profile_stream_id": "btc_volume_profile",
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
    for table, col in (("ob_snapshots", "tfi"), ("ob_trades", "entry_tfi")):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
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
        self.mode            = mode
        self.p               = p
        self.obi_ema         = 0.0
        self.tfi             = 0.0
        self.vwap_dip_score  = 0.0    # (vwap - price) / vwap; positive = below VWAP
        self.vwap_dip_zone   = "neutral"
        self.vol_price_zone  = "neutral"  # "buy_hvn" | "sell_hvn" | "neutral"
        self.vol_zone_score  = 0.0        # net_vol / total_vol in current bucket
        self.macro_obi       = 0.0        # EMA-smoothed macro OBI (-1 to +1)
        self.macro_obi_dir   = "neutral"  # "bullish" | "neutral" | "bearish"
        self.funding_rate    = 0.0        # Binance perp funding rate (decimal/8h)
        self.liq_long_usd    = 0.0        # long liquidations last 5min (USD)
        self.liq_short_usd   = 0.0        # short liquidations last 5min (USD)
        self.pending_dir     = None
        self.pending_count   = 0
        self.position        = None
        self.capital         = p[f"capital_{mode}"]
        self.snap_counter    = 0
        self.total_trades    = 0
        self.wins            = 0
        self.losses          = 0
        self.total_pnl       = 0.0

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
# VWAP context handler
# ---------------------------------------------------------------------------

def _handle_vwap(state: StreamState, msg: dict) -> None:
    dip_score = msg.get("dip_score")
    dip_zone  = msg.get("dip_zone", "neutral")
    if dip_score is not None:
        state.vwap_dip_score = float(dip_score)
    state.vwap_dip_zone = str(dip_zone)
    logger.debug("[%s] VWAP: dip_score=%.4f zone=%s",
                 state.mode, state.vwap_dip_score, state.vwap_dip_zone)


def _handle_vol_profile(state: StreamState, msg: dict) -> None:
    zone  = msg.get("price_zone", "neutral")
    score = msg.get("zone_score")
    state.vol_price_zone = str(zone)
    if score is not None:
        state.vol_zone_score = float(score)
    logger.debug("[%s] VolProfile: zone=%s score=%.3f",
                 state.mode, state.vol_price_zone, state.vol_zone_score)


def _handle_macro_obi(state: StreamState, msg: dict) -> None:
    val = msg.get("macro_obi")
    drn = msg.get("macro_obi_direction", "neutral")
    if val is not None:
        state.macro_obi = float(val)
    state.macro_obi_dir = str(drn)
    logger.debug("[%s] MacroOBI: obi=%.4f dir=%s",
                 state.mode, state.macro_obi, state.macro_obi_dir)


def _handle_funding(state: StreamState, msg: dict) -> None:
    rate = msg.get("funding_rate")
    if rate is not None:
        state.funding_rate = float(rate)
    logger.debug("[%s] Funding: rate=%.6f (%.4f%%/8h)",
                 state.mode, state.funding_rate, state.funding_rate * 100)


def _handle_liquidations(state: StreamState, msg: dict) -> None:
    liq_long  = msg.get("liq_long_usd")
    liq_short = msg.get("liq_short_usd")
    if liq_long  is not None:
        state.liq_long_usd  = float(liq_long)
    if liq_short is not None:
        state.liq_short_usd = float(liq_short)
    logger.debug("[%s] Liq: long=$%.0f  short=$%.0f",
                 state.mode, state.liq_long_usd, state.liq_short_usd)

# ---------------------------------------------------------------------------
# Indicator message handler
# ---------------------------------------------------------------------------

async def _handle_indicator(state: StreamState, db: sqlite3.Connection,
                             msg: dict, ts_ms: int) -> None:
    mid = msg.get("mid")
    if mid is None:
        return
    mid = float(mid)

    best_bid = float(msg.get("best_bid", mid))
    best_ask = float(msg.get("best_ask", mid))
    spread   = (best_ask - best_bid) / mid if mid > 0 else 0.0

    obi_ema = msg.get("obi_ema")
    if obi_ema is not None:
        state.obi_ema = float(obi_ema)

    tfi = msg.get("tfi")
    state.tfi = float(tfi) if tfi is not None else 0.0

    state.snap_counter += 1
    if state.snap_counter % state.p["snapshot_every_n"] == 0:
        db.execute("""
            INSERT INTO ob_snapshots
                (ts_ms, mode, best_bid, best_ask, spread, obi_raw, obi_ema, tfi)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ts_ms, state.mode, best_bid, best_ask, spread,
             None, state.obi_ema, state.tfi))
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
    p             = state.p
    entry_thresh  = p["obi_entry_thresh"]
    confirm_n     = p["obi_confirm_n"]
    tfi_thresh    = p.get("tfi_confirm_thresh", 0.0)
    tfi_gate_mode = p.get("tfi_gate_mode", "flat")
    direction     = p.get("direction", "long")

    # VWAP gate: suspend longs above VWAP; relax OBI thresh when deep below VWAP
    vwap_gate = p.get("vwap_gate", False)
    if vwap_gate:
        dip = state.vwap_dip_score
        relax_min = p.get("vwap_relax_min_pct", 0.01)
        if dip < 0.0:
            # price above VWAP → entering into resistance, skip longs
            if direction in ("long", "both"):
                logger.debug("[%s] VWAP gate: long blocked (dip_score=%.4f above VWAP)",
                             state.mode, dip)
                state.pending_dir   = None
                state.pending_count = 0
                return
        elif dip >= relax_min:
            # price ≥1% below VWAP → strong dip context, relax OBI threshold
            entry_thresh = p.get("vwap_obi_relax", entry_thresh)

    # Volume profile gate: block entries in sell-dominant HVN; accelerate in buy HVN
    if p.get("vol_profile_gate", False):
        zone = state.vol_price_zone
        if zone == "sell_hvn" and direction in ("long", "both"):
            logger.debug("[%s] VolProfile gate: long blocked (sell_hvn score=%.3f)",
                         state.mode, state.vol_zone_score)
            state.pending_dir   = None
            state.pending_count = 0
            return
        if zone == "buy_hvn":
            confirm_n = p.get("vol_buy_confirm_n", confirm_n)

    # Macro OBI gate: block longs when macro taker flow is bullish
    # (ask-heavy micro OBI in a bullish macro is noise, not exhaustion)
    if p.get("macro_obi_gate", False):
        if state.macro_obi_dir == "bullish" and direction in ("long", "both"):
            logger.debug("[%s] MacroOBI gate: long blocked (macro=%s obi=%.4f)",
                         state.mode, state.macro_obi_dir, state.macro_obi)
            state.pending_dir   = None
            state.pending_count = 0
            return

    # Funding rate gate: block longs when perp funding is highly positive
    # (crowded longs paying carry weaken the contrarian ask-heavy OBI signal)
    if p.get("funding_gate", False):
        thresh = p.get("funding_gate_thresh", 0.0005)
        if state.funding_rate > thresh and direction in ("long", "both"):
            logger.debug("[%s] Funding gate: long blocked (rate=%.6f > thresh=%.6f)",
                         state.mode, state.funding_rate, thresh)
            state.pending_dir   = None
            state.pending_count = 0
            return

    # Liquidations gate: block longs during long liquidation cascades
    # (mass long liquidations signal continued downside — avoid adverse selection)
    if p.get("liq_gate", False):
        liq_thresh = p.get("liq_long_block_usd", 5_000_000)
        if state.liq_long_usd > liq_thresh and direction in ("long", "both"):
            logger.debug("[%s] Liq gate: long blocked (liq_long=$%.0f > $%.0f)",
                         state.mode, state.liq_long_usd, liq_thresh)
            state.pending_dir   = None
            state.pending_count = 0
            return

    if tfi_thresh <= 0.0:
        tfi_ok_long  = True
        tfi_ok_short = True
    elif tfi_gate_mode == "flat":
        # Flat gate: entry only when taker flow is neutral (no exhaustion signal)
        tfi_ok_long  = abs(state.tfi) < tfi_thresh
        tfi_ok_short = abs(state.tfi) < tfi_thresh
    else:
        # Directional gate (legacy): require taker flow in trade direction
        tfi_ok_long  = state.tfi >  tfi_thresh
        tfi_ok_short = state.tfi < -tfi_thresh

    want_long  = (direction in ("long",  "both")) and state.obi_ema < -entry_thresh
    want_short = (direction in ("short", "both")) and state.obi_ema >  entry_thresh

    if want_long:
        if state.pending_dir == "long":
            state.pending_count += 1
        else:
            state.pending_dir, state.pending_count = "long", 1
    elif want_short:
        if state.pending_dir == "short":
            state.pending_count += 1
        else:
            state.pending_dir, state.pending_count = "short", 1
    else:
        state.pending_dir   = None
        state.pending_count = 0

    tfi_ok = tfi_ok_long if state.pending_dir == "long" else tfi_ok_short
    if state.pending_count >= confirm_n and state.pending_dir and tfi_ok:
        _open_paper(state, state.pending_dir, mid, state.tfi, ts_ms, db)
        state.pending_dir   = None
        state.pending_count = 0

# ---------------------------------------------------------------------------
# ZMQ subscription loop
# ---------------------------------------------------------------------------

async def _zmq_loop(states: list, db: sqlite3.Connection, p: dict) -> None:
    try:
        import zmq
        import zmq.asyncio as azmq
    except ImportError:
        logger.error("pyzmq not installed — run: pip install pyzmq")
        return

    addr = p.get("indicators_addr", "tcp://127.0.0.1:5559")
    stream_map = {}
    for st in states:
        sid_key = f"{st.mode}_stream_id"
        sid     = p.get(sid_key, f"btc_scalping_{st.mode}")
        stream_map[sid] = st

    vwap_sid     = p.get("vwap_stream_id",       "btc_vwap_context")   if p.get("vwap_gate")        else None
    vol_sid      = p.get("vol_profile_stream_id","btc_volume_profile") if p.get("vol_profile_gate") else None
    macro_sid    = p.get("macro_obi_stream_id",  "btc_macro_obi")      if p.get("macro_obi_gate")   else None
    funding_sid  = p.get("funding_stream_id",    "btc_funding")         if p.get("funding_gate")     else None
    liq_sid      = p.get("liq_stream_id",        "btc_liquidations")    if p.get("liq_gate")         else None
    all_streams = (list(stream_map.keys())
                   + ([vwap_sid]     if vwap_sid     else [])
                   + ([vol_sid]      if vol_sid      else [])
                   + ([macro_sid]    if macro_sid    else [])
                   + ([funding_sid]  if funding_sid  else [])
                   + ([liq_sid]      if liq_sid      else []))

    ctx = azmq.Context.instance()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(addr)
    logger.info("ZMQ SUB → %s  streams: %s", addr, all_streams)

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
            if sid == vwap_sid and vwap_sid:
                for st in states:
                    _handle_vwap(st, msg)
                continue
            if sid == vol_sid and vol_sid:
                for st in states:
                    _handle_vol_profile(st, msg)
                continue
            if sid == macro_sid and macro_sid:
                for st in states:
                    _handle_macro_obi(st, msg)
                continue
            if sid == funding_sid and funding_sid:
                for st in states:
                    _handle_funding(st, msg)
                continue
            if sid == liq_sid and liq_sid:
                for st in states:
                    _handle_liquidations(st, msg)
                continue

            state = stream_map.get(sid)
            if state is None:
                continue
            ts_ms = int(msg.get("ts", time.time() * 1000))
            await _handle_indicator(state, db, msg, ts_ms)
    finally:
        sub.close()

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
            logger.info("[%s] OBI=%.3f  TFI=%+.3f  capital=%.2f  W/T=%s  PnL=%+.4f  %s",
                        st.mode, st.obi_ema, st.tfi, st.capital, wr, st.total_pnl, pos)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(p: dict, db: sqlite3.Connection) -> None:
    modes  = p["modes"]
    states = [StreamState(m, p) for m in modes]

    tfi_thresh    = p.get("tfi_confirm_thresh", 0.0)
    tfi_gate_mode = p.get("tfi_gate_mode", "flat")
    if tfi_thresh <= 0.0:
        tfi_mode = "TFI logged (no gate)"
    elif tfi_gate_mode == "flat":
        tfi_mode = f"|TFI|<{tfi_thresh:.2f} (flat)"
    else:
        tfi_mode = f"TFI>{tfi_thresh:.2f} long / TFI<-{tfi_thresh:.2f} short (directional)"

    vwap_gate = p.get("vwap_gate", False)
    if vwap_gate:
        vwap_mode = (f"enabled — suspend long above VWAP, "
                     f"relax OBI→{p.get('vwap_obi_relax', 0.50):.2f} "
                     f"when ≥{p.get('vwap_relax_min_pct', 0.01)*100:.0f}% below")
    else:
        vwap_mode = "disabled"

    logger.info("OrderBook bot v2.12 — symbol=%s  modes=%s  direction=%s  paper=True",
                p["symbol"], modes, p.get("direction", "long"))
    logger.info("OBI: entry=%.2f  confirm=%d  alpha=%.2f (indicators)  levels=%d",
                p["obi_entry_thresh"], p["obi_confirm_n"],
                p["obi_ema_alpha"], p["obi_levels"])
    logger.info("Trade: tp=%.3f%%  sl=%.3f%%  max_hold=%dm  stake=%.0f%%",
                p["tp_pct"] * 100, p["sl_pct"] * 100,
                p["max_hold_minutes"], p["stake_frac"] * 100)
    logger.info("TFI: gate=%s", tfi_mode)
    logger.info("VWAP: %s", vwap_mode)
    vol_mode = ("enabled — block sell_hvn longs, confirm_n→%d in buy_hvn" %
                p.get("vol_buy_confirm_n", 10)
                if p.get("vol_profile_gate") else "disabled")
    logger.info("VolProfile: %s", vol_mode)
    macro_mode = ("enabled — block longs when macro direction=bullish"
                  if p.get("macro_obi_gate") else "disabled")
    logger.info("MacroOBI: %s  indicators: %s",
                macro_mode, p.get("indicators_addr", "tcp://127.0.0.1:5559"))
    funding_thresh = p.get("funding_gate_thresh", 0.0005)
    funding_mode = (f"enabled — block longs when funding_rate > {funding_thresh:.4%}/8h"
                    if p.get("funding_gate") else "disabled")
    logger.info("Funding gate: %s", funding_mode)
    liq_thresh = p.get("liq_long_block_usd", 5_000_000)
    liq_mode = (f"enabled — block longs when liq_long > ${liq_thresh:,.0f}/5min"
                if p.get("liq_gate") else "disabled")
    logger.info("Liq gate: %s", liq_mode)

    tasks = [
        asyncio.create_task(_zmq_loop(states, db, p)),
        asyncio.create_task(_stats_loop(states)),
    ]
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
