#!/usr/bin/env python3
"""
BTC Long-Term Accumulation Bot v1.5

Builds a growing BTC spot position using OBI dip-buying + partial profit ladder.

Changes from v1.4:
  - New: Fear & Greed gate — blocks scale-ins when F&G > fear_greed_block_thresh (80,
    extreme greed). Boosts max scale-in amount during extreme fear (F&G < 25).
    Consumes fear_greed ZMQ stream.
  - New: Liquidations gate — blocks scale-ins during active short squeezes
    (liq_short_usd > liq_short_block_usd). Boosts scale-in amount during long
    liquidation cascades (capitulation: liq_long_usd > liq_long_spike_usd).
    Consumes btc_liquidations ZMQ stream.
  - New: Long/Short ratio gate — blocks scale-ins when longs are extremely crowded
    (long_short_ratio > ls_ratio_block_high, default 3.0).
    Consumes btc_ls_ratio ZMQ stream.
  - New: RSI 4h gate — blocks scale-ins when 4h RSI > rsi4h_block_high (70, overbought).
    Relaxes the VWAP gate when RSI < rsi4h_relax_vwap_low (35, oversold),
    allowing scale-ins even above VWAP during extreme oversold conditions.
    Consumes btc_4h ZMQ stream.

Changes from v1.3:
  - New: Macro OBI gate (P1), VWAP-gated initial buy (P2), adaptive cooldown (P3),
    pending rebuy expiry (P4), trailing rebuy (P5), configurable Earn buffer (P6).

Strategy:
  1. Initial buy at startup (initial_stake_usdt), optionally VWAP-gated
  2. OBI dip signal: OBI_ema < -entry_thresh for confirm_n consecutive ticks
     Gates checked in order: cooldown → max_invest → macro_obi → rsi4h →
     fear_greed → VWAP (with RSI relaxation) → ls_ratio → liq_short_squeeze
  3. Profit ladder: sell sell_fraction at each band, set adaptive rebuy target
  4. Rebuy: fill on price dip (with optional trailing bounce), expire after N days
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

from earn_manager import EarnManager
from tradinetools.logging import setup_root_logger

logger = logging.getLogger("accumulation_bot")

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "symbol":                "BTCUSDT",
    "capital_usdt":          1000.0,
    "initial_stake_usdt":    500.0,
    "scale_in_usdt":         100.0,
    "scale_in_dip_factor":   0.5,
    "scale_in_max_mult":     3.0,
    "max_invested_pct":      0.90,
    # OBI — alpha is informational; actual value set in indicators service config
    "obi_levels":            10,
    "obi_ema_alpha":         0.05,
    "obi_entry_thresh":      0.50,
    "obi_confirm_n":         20,
    "min_scale_interval_s":  3600,
    "profit_bands_pct":      [5.0, 10.0, 20.0, 30.0, 50.0],
    "sell_fraction":         0.15,
    "min_holdings_pct":      0.50,
    "rebuy_discount_min_pct": 3.0,
    "rebuy_discount_max_pct": 10.0,
    "rebuy_spread_mult":      3.0,
    "fee_spot":              0.001,
    "maker_fee_spot":        0.0002,
    "use_limit_orders":      True,
    "snapshot_every_n":      20,
    "spread_ema_alpha":       0.1,
    "buy_dust_tolerance_usdt": 0.01,
    # Indicators service (ZMQ) — None means auto-detect (IPC or set TRADINEBOTTE_INDICATORS_ADDR)
    "indicators_addr":       None,
    "scalping_stream_id":    "btc_scalping_spot",
    # VWAP gate
    "vwap_gate":             True,
    "vwap_gate_initial":     False,
    # Binance Simple Earn
    "earn_enabled":          True,
    "earn_min_liquid_usdt":  20.0,
    # Macro OBI gate (v1.4 P1)
    "macro_obi_gate":         True,
    "macro_obi_block_thresh": -0.30,
    "macro_obi_stream_id":    "btc_macro_obi",
    # Adaptive cooldown (v1.4 P3)
    "scale_in_cooldown_min_s":    900,
    "scale_in_obi_strong_thresh": 0.80,
    # Rebuy improvements (v1.4 P4/P5)
    "rebuy_max_age_days":  30,
    "rebuy_trail_pct":     0.0,
    # ── Fear & Greed gate ──────────────────────────────────────────────────
    "fear_greed_gate":          True,
    "fear_greed_stream_id":     "fear_greed",
    "fear_greed_block_thresh":  80,    # block scale-ins when F&G > 80 (extreme greed)
    "fear_greed_boost_thresh":  25,    # boost scale-in size when F&G < 25 (extreme fear)
    "fear_greed_boost_mult":    1.5,   # multiply scale_in_max_mult in extreme fear zone
    # ── Liquidations gate ─────────────────────────────────────────────────
    "liq_gate":             True,
    "liq_stream_id":        "btc_liquidations",
    "liq_short_block_usd":  10_000_000,  # block scale-ins during short squeeze ($10M/5min)
    "liq_long_spike_usd":    5_000_000,  # boost scale-in during long cascade ($5M/5min)
    "liq_long_boost_mult":   1.3,        # max_mult multiplier during capitulation
    # ── Long/Short ratio gate ─────────────────────────────────────────────
    "ls_ratio_gate":        True,
    "ls_ratio_stream_id":   "btc_ls_ratio",
    "ls_ratio_block_high":  3.0,   # block when ratio > 3 (extreme long crowding)
    # ── RSI 4h gate ───────────────────────────────────────────────────────
    "rsi4h_gate":           True,
    "rsi4h_stream_id":      "btc_4h",
    "rsi4h_block_high":     70.0,  # block scale-ins when 4h RSI > 70 (overbought)
    "rsi4h_relax_vwap_low": 35.0,  # relax VWAP gate when RSI < 35 (oversold override)
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
    ts_ms:       int   = 0
    low_seen:    float = -1.0

@dataclass
class AccumState:
    p:                  dict
    holdings_btc:       float = 0.0
    avg_entry:          float = 0.0
    free_usdt:          float = 0.0
    last_price:         float = 0.0
    obi_ema:            float = 0.0
    spread_ema:         float = 0.002
    pending_count:      int   = 0
    last_buy_ts:        int   = 0
    initial_done:       bool  = False
    pending_rebuys:     list  = field(default_factory=list)
    active_bands:       set   = field(default_factory=set)
    snap_counter:       int   = 0
    total_realized:     float = 0.0
    peak_holdings_btc:  float = 0.0
    # VWAP context
    vwap_dip_score:     float = 0.0
    vwap_dip_zone:      str   = "neutral"
    # Macro OBI
    macro_obi:          float = 0.0
    macro_obi_dir:      str   = "neutral"
    # Fear & Greed (default 50 = neutral)
    fear_greed_val:     int   = 50
    fear_greed_label:   str   = "Neutral"
    # Liquidations (5-min windows from indicators service)
    liq_long_usd:       float = 0.0
    liq_short_usd:      float = 0.0
    # Long/Short ratio (default 1.0 = balanced)
    ls_ratio:           float = 1.0
    # RSI 4h (default 50 = neutral)
    rsi_4h:             float = 50.0
    earn: EarnManager | None  = field(default=None, repr=False)

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
         "qty_btc":  r.qty_btc,  "rebuy_price": r.rebuy_price,
         "ts_ms":    r.ts_ms,    "low_seen":    r.low_seen}
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
    (_, holdings, avg, free, realized,
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
    p        = state.p
    base     = p.get("scale_in_usdt", 100.0)
    if state.avg_entry <= 0:
        return base
    dip_pct  = (state.avg_entry - price) / state.avg_entry * 100.0
    factor   = p.get("scale_in_dip_factor", 0.5)
    max_mult = p.get("scale_in_max_mult", 3.0)
    mult     = 1.0 + factor * max(dip_pct, 0.0)

    # Fear & Greed boost: more capital during extreme fear (capitulation zone)
    if p.get("fear_greed_gate", True):
        if state.fear_greed_val < p.get("fear_greed_boost_thresh", 25):
            max_mult *= p.get("fear_greed_boost_mult", 1.5)

    # Liquidations capitulation boost: long cascade = forced selling = buy more
    if p.get("liq_gate", True):
        if state.liq_long_usd > p.get("liq_long_spike_usd", 5_000_000):
            max_mult = min(max_mult * p.get("liq_long_boost_mult", 1.3),
                           p.get("scale_in_max_mult", 3.0) * 2.0)

    return min(base * mult, base * max_mult)


def _rebuy_discount(state: AccumState) -> float:
    p       = state.p
    min_d   = p.get("rebuy_discount_min_pct", 3.0)
    max_d   = p.get("rebuy_discount_max_pct", 10.0)
    mult    = p.get("rebuy_spread_mult", 3.0)
    pct     = max(min_d, min(max_d, state.spread_ema * mult))
    return pct / 100.0

# ---------------------------------------------------------------------------
# Trade execution (paper)
# ---------------------------------------------------------------------------

async def _buy(state: AccumState, price: float, usdt_amount: float,
               reason: str, ts_ms: int, db: sqlite3.Connection) -> bool:
    p        = state.p
    fee_rate = p["maker_fee_spot"] if p.get("use_limit_orders") else p["fee_spot"]
    qty_btc  = usdt_amount / price
    fee      = usdt_amount * fee_rate
    total    = usdt_amount + fee

    if total > state.free_usdt + p.get("buy_dust_tolerance_usdt", 0.01):
        logger.warning("BUY skipped — need %.2f USDT, have %.2f", total, state.free_usdt)
        return False

    earn_keep = p.get("earn_min_liquid_usdt", 20.0)
    if state.earn is not None:
        await state.earn.ensure_liquid(total, keep_liquid=earn_keep)

    if state.holdings_btc > 0 and state.avg_entry > 0:
        total_btc       = state.holdings_btc + qty_btc
        state.avg_entry = (state.holdings_btc * state.avg_entry + qty_btc * price) / total_btc
    else:
        state.avg_entry = price

    state.holdings_btc += qty_btc
    state.free_usdt    -= total
    state.peak_holdings_btc = max(state.peak_holdings_btc, state.holdings_btc)

    if state.earn is not None:
        await state.earn.park_idle(state.free_usdt, keep_liquid=earn_keep)

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


async def _sell(state: AccumState, price: float, qty_btc: float,
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
    if state.holdings_btc <= 0:
        state.avg_entry = 0.0

    earn_keep = p.get("earn_min_liquid_usdt", 20.0)
    if state.earn is not None:
        await state.earn.park_idle(state.free_usdt, keep_liquid=earn_keep)

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

async def _check_profit_bands(state: AccumState, price: float,
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
        if await _sell(state, price, qty, f"profit+{band_pct:.1f}%", ts_ms, db):
            rebuy = price * (1.0 - discount)
            state.active_bands.add(band_pct)
            state.pending_rebuys.append(
                PendingRebuy(band_pct=band_pct, sell_price=price,
                             qty_btc=qty, rebuy_price=rebuy, ts_ms=ts_ms))
            logger.info("  → rebuy %.6f BTC @ %.2f  (spread=%.4f%% → discount=%.3f%%)",
                        qty, rebuy, state.spread_ema, discount * 100)


async def _check_rebuys(state: AccumState, price: float,
                        ts_ms: int, db: sqlite3.Connection) -> None:
    p       = state.p
    max_age = p.get("rebuy_max_age_days", 30) * 86_400_000
    trail   = p.get("rebuy_trail_pct", 0.0)

    expired = []
    filled  = []
    for rb in state.pending_rebuys:
        if rb.ts_ms > 0 and 0 < max_age < (ts_ms - rb.ts_ms):
            logger.info("Rebuy +%.1f%% expired (age=%dd) — band re-armed",
                        rb.band_pct, (ts_ms - rb.ts_ms) // 86_400_000)
            state.active_bands.discard(rb.band_pct)
            expired.append(rb)
            continue

        if price > rb.rebuy_price:
            continue

        if trail > 0.0:
            rb.low_seen = price if rb.low_seen < 0 else min(rb.low_seen, price)
            if price < rb.low_seen * (1.0 + trail):
                continue

        usdt_needed = rb.qty_btc * price
        if usdt_needed > state.free_usdt:
            continue
        if await _buy(state, price, usdt_needed, f"rebuy+{rb.band_pct:.1f}%", ts_ms, db):
            state.active_bands.discard(rb.band_pct)
            filled.append(rb)

    for rb in expired + filled:
        state.pending_rebuys.remove(rb)


async def _check_obi_scale_in(state: AccumState, price: float,
                               ts_ms: int, db: sqlite3.Connection) -> None:
    p          = state.p
    max_invest = state.max_investable()

    # Adaptive cooldown: halve wait when OBI signal is very strong.
    # floor_iv is the hard minimum — never go below it even with halving.
    base_iv  = p.get("min_scale_interval_s", 3600)
    floor_iv = p.get("scale_in_cooldown_min_s", base_iv)
    strong   = p.get("scale_in_obi_strong_thresh", 0.80)
    min_iv   = max(floor_iv, base_iv // 2) if abs(state.obi_ema) >= strong else base_iv
    if (ts_ms - state.last_buy_ts) / 1000.0 < min_iv:
        return

    invested = state.holdings_btc * price
    scale_usdt = _scale_in_amount(state, price)
    if invested + scale_usdt > max_invest:
        logger.debug("Scale-in skipped — max invested (%.0f%%)", p["max_invested_pct"] * 100)
        return
    if scale_usdt > state.free_usdt:
        return

    # Macro OBI gate: block during macro bearish regimes
    if p.get("macro_obi_gate", True):
        thresh = p.get("macro_obi_block_thresh", -0.30)
        if state.macro_obi < thresh:
            logger.debug("Scale-in blocked — macro OBI bearish (%.3f < %.3f)",
                         state.macro_obi, thresh)
            return

    # RSI 4h gate: block when overbought
    rsi4h_gate = p.get("rsi4h_gate", True)
    if rsi4h_gate and state.rsi_4h > p.get("rsi4h_block_high", 70.0):
        logger.debug("Scale-in blocked — 4h RSI overbought (%.1f)", state.rsi_4h)
        return

    # Fear & Greed gate: block when extreme greed
    if p.get("fear_greed_gate", True):
        if state.fear_greed_val > p.get("fear_greed_block_thresh", 80):
            logger.debug("Scale-in blocked — extreme greed (F&G=%d %s)",
                         state.fear_greed_val, state.fear_greed_label)
            return

    # VWAP gate with RSI oversold relaxation
    vwap_blocked = p.get("vwap_gate", True) and state.vwap_dip_score < 0.0
    if vwap_blocked:
        relax_low = p.get("rsi4h_relax_vwap_low", 35.0) if rsi4h_gate else 0.0
        if rsi4h_gate and state.rsi_4h <= relax_low:
            logger.debug("VWAP gate relaxed — 4h RSI oversold (%.1f <= %.1f)",
                         state.rsi_4h, relax_low)
        else:
            logger.debug("Scale-in skipped — price above VWAP (dip_score=%.4f zone=%s)",
                         state.vwap_dip_score, state.vwap_dip_zone)
            return

    # L/S ratio gate: block when extreme long crowding
    if p.get("ls_ratio_gate", True):
        if state.ls_ratio > p.get("ls_ratio_block_high", 3.0):
            logger.debug("Scale-in blocked — extreme long crowding (L/S=%.2f)", state.ls_ratio)
            return

    # Liquidations gate: block during active short squeeze
    if p.get("liq_gate", True):
        if state.liq_short_usd > p.get("liq_short_block_usd", 10_000_000):
            logger.debug("Scale-in blocked — short squeeze (liq_short=%.0f USDT)",
                         state.liq_short_usd)
            return

    dip_pct = ((state.avg_entry - price) / state.avg_entry * 100.0
               if state.avg_entry > 0 else 0.0)
    reason = f"obi_dip({dip_pct:+.1f}%)"
    if await _buy(state, price, scale_usdt, reason, ts_ms, db):
        state.last_buy_ts = ts_ms

# ---------------------------------------------------------------------------
# Indicators ZMQ handlers
# ---------------------------------------------------------------------------

def _handle_vwap(state: AccumState, msg: dict) -> None:
    dip_score = msg.get("dip_score")
    dip_zone  = msg.get("dip_zone", "neutral")
    if dip_score is not None:
        state.vwap_dip_score = float(dip_score)
    state.vwap_dip_zone = str(dip_zone)
    logger.debug("VWAP: dip_score=%.4f zone=%s", state.vwap_dip_score, state.vwap_dip_zone)


def _handle_macro_obi(state: AccumState, msg: dict) -> None:
    val = msg.get("macro_obi")
    drn = msg.get("macro_obi_direction", "neutral")
    if val is not None:
        state.macro_obi = float(val)
    state.macro_obi_dir = str(drn)
    logger.debug("MacroOBI: %.3f (%s)", state.macro_obi, state.macro_obi_dir)


def _handle_fear_greed(state: AccumState, msg: dict) -> None:
    val   = msg.get("fear_greed")
    label = msg.get("fear_greed_label", "Neutral")
    if val is not None:
        state.fear_greed_val = int(val)
    state.fear_greed_label = str(label)
    logger.debug("F&G: %d (%s)", state.fear_greed_val, state.fear_greed_label)


def _handle_liquidations(state: AccumState, msg: dict) -> None:
    long_usd  = msg.get("liq_long_usd")
    short_usd = msg.get("liq_short_usd")
    if long_usd  is not None:
        state.liq_long_usd  = float(long_usd)
    if short_usd is not None:
        state.liq_short_usd = float(short_usd)
    logger.debug("Liq: long=%.0f  short=%.0f  USDT",
                 state.liq_long_usd, state.liq_short_usd)


def _handle_ls_ratio(state: AccumState, msg: dict) -> None:
    ratio = msg.get("long_short_ratio")
    if ratio is not None:
        state.ls_ratio = float(ratio)
    logger.debug("L/S ratio: %.3f", state.ls_ratio)


def _handle_4h(state: AccumState, msg: dict) -> None:
    rsi = msg.get("rsi_14")
    if rsi is not None:
        state.rsi_4h = float(rsi)
    logger.debug("4h RSI: %.1f", state.rsi_4h)


async def _handle_indicator(state: AccumState, db: sqlite3.Connection,
                             msg: dict, ts_ms: int) -> None:
    mid = msg.get("mid")
    if mid is None:
        return

    state.last_price = float(mid)

    obi_ema = msg.get("obi_ema")
    if obi_ema is not None:
        state.obi_ema = float(obi_ema)

    spread_bps = msg.get("spread_bps")
    if spread_bps is not None:
        spread_pct = float(spread_bps) / 100.0
        _alpha = state.p.get("spread_ema_alpha", 0.1)
        state.spread_ema = _alpha * spread_pct + (1 - _alpha) * state.spread_ema

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
        if state.p.get("vwap_gate_initial", False):
            if state.p.get("vwap_gate", True) and state.vwap_dip_score < 0.0:
                return
        init_usdt = min(state.p["initial_stake_usdt"], state.free_usdt)
        if await _buy(state, price, init_usdt, "initial", ts_ms, db):
            state.last_buy_ts = ts_ms
            state.initial_done = True
        return

    await _check_profit_bands(state, price, ts_ms, db)
    await _check_rebuys(state, price, ts_ms, db)

    thresh = state.p["obi_entry_thresh"]
    if state.obi_ema < -thresh:
        state.pending_count += 1
    else:
        state.pending_count = 0

    if state.pending_count >= state.p["obi_confirm_n"]:
        await _check_obi_scale_in(state, price, ts_ms, db)
        state.pending_count = 0

# ---------------------------------------------------------------------------
# ZMQ subscription loop
# ---------------------------------------------------------------------------

async def _zmq_loop(state: AccumState, db: sqlite3.Connection) -> None:
    try:
        import os as _os
        import zmq.asyncio as azmq
        from tradinetools.zmq import make_sub, default_ipc_addr
    except ImportError:
        logger.error("pyzmq not installed — run: pip install pyzmq")
        return

    p    = state.p
    addr = (p.get("indicators_addr")
            or _os.environ.get("TRADINEBOTTE_INDICATORS_ADDR")
            or default_ipc_addr("tradinebotte-indicators"))
    scalping_id = p.get("scalping_stream_id",  "btc_scalping_spot")
    macro_id = (p.get("macro_obi_stream_id", "btc_macro_obi")
                if p.get("macro_obi_gate",   True) else None)
    fg_id    = (p.get("fear_greed_stream_id", "fear_greed")
                if p.get("fear_greed_gate",  True) else None)
    liq_id   = (p.get("liq_stream_id",  "btc_liquidations")
                if p.get("liq_gate",         True) else None)
    ls_id    = (p.get("ls_ratio_stream_id", "btc_ls_ratio")
                if p.get("ls_ratio_gate",    True) else None)
    rsi4h_id = (p.get("rsi4h_stream_id", "btc_4h")
                if p.get("rsi4h_gate",       True) else None)

    ctx = azmq.Context.instance()
    sub = make_sub(ctx, addr)
    streams = ", ".join(s for s in
                        [scalping_id, "btc_vwap_context", macro_id,
                         fg_id, liq_id, ls_id, rsi4h_id] if s)
    logger.info("ZMQ SUB → %s  (streams: %s)", addr, streams)

    try:
        while True:
            try:
                msg = await sub.recv_json()
            except asyncio.CancelledError:  # pylint: disable=try-except-raise
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                logger.warning("ZMQ recv error: %s — retrying in 5s", exc)
                await asyncio.sleep(5)
                continue

            sid = msg.get("stream_id")
            if sid == scalping_id:
                ts_ms = int(msg.get("ts", time.time() * 1000))
                await _handle_indicator(state, db, msg, ts_ms)
            elif sid == "btc_vwap_context":
                _handle_vwap(state, msg)
            elif macro_id and sid == macro_id:
                _handle_macro_obi(state, msg)
            elif fg_id and sid == fg_id:
                _handle_fear_greed(state, msg)
            elif liq_id and sid == liq_id:
                _handle_liquidations(state, msg)
            elif ls_id and sid == ls_id:
                _handle_ls_ratio(state, msg)
            elif rsi4h_id and sid == rsi4h_id:
                _handle_4h(state, msg)
    finally:
        sub.close()

# ---------------------------------------------------------------------------
# Stats loop (every 60s)
# ---------------------------------------------------------------------------

async def _stats_loop(state: AccumState) -> None:
    while True:
        await asyncio.sleep(60)
        cooldown_s = max(0, state.p.get("min_scale_interval_s", 3600) -
                         (int(time.time() * 1000) - state.last_buy_ts) / 1000)
        logger.debug(
            "HOLD %.6f BTC @ avg %.2f  price=%.2f  uPnL=%+.2f%%  "
            "free=%.2f  realized=%+.2f  spread=%.4f%%  bands=%s  rebuys=%d  "
            "dip_in=%ds  obi=%.3f  macro=%.3f(%s)  "
            "fg=%d  ls=%.2f  rsi4h=%.1f  liq_l=%.0f  liq_s=%.0f  vwap=%s",
            state.holdings_btc, state.avg_entry or 0.0, state.last_price,
            state.unrealized_pct(), state.free_usdt, state.total_realized,
            state.spread_ema, sorted(state.active_bands),
            len(state.pending_rebuys), int(cooldown_s),
            state.obi_ema, state.macro_obi, state.macro_obi_dir,
            state.fear_greed_val, state.ls_ratio, state.rsi_4h,
            state.liq_long_usd, state.liq_short_usd, state.vwap_dip_zone)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _run(p: dict, db: sqlite3.Connection, install_dir: str = "") -> None:
    state = AccumState(p=p, free_usdt=p["capital_usdt"])
    restored = _restore_state(state, db)

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=10)) as http_session:
        if p.get("earn_enabled", True):
            state.earn = EarnManager(http_session)

        logger.info("Accumulation bot v1.5 — %s  capital=%.0f USDT  paper=True%s  earn=%s",
                    p["symbol"], p["capital_usdt"],
                    "  [RESTORED]" if restored else "",
                    "enabled" if state.earn is not None else "disabled")
        logger.info("Initial %.0f USDT  scale-in %.0f-%.0f USDT  "
                    "every %dmin (dip_factor=%.1f  max_mult=%.1f×)",
                    p["initial_stake_usdt"],
                    p["scale_in_usdt"],
                    p["scale_in_usdt"] * p.get("scale_in_max_mult", 3.0),
                    p.get("min_scale_interval_s", 3600) // 60,
                    p.get("scale_in_dip_factor", 0.5),
                    p.get("scale_in_max_mult", 3.0))
        logger.info("Profit bands: %s%%  sell %.0f%%  min_hold %.0f%%  "
                    "rebuy discount %.2f–%.2f%% (spread×%.1f)",
                    p.get("profit_bands_pct"),
                    p.get("sell_fraction", 0.20) * 100,
                    p.get("min_holdings_pct", 0.0) * 100,
                    p.get("rebuy_discount_min_pct", 3.0),
                    p.get("rebuy_discount_max_pct", 10.0),
                    p.get("rebuy_spread_mult", 3.0))
        logger.info("VWAP gate: %s%s  Macro OBI: %s (thresh=%.2f)",
                    "enabled" if p.get("vwap_gate", True) else "disabled",
                    " (initial=gated)" if p.get("vwap_gate_initial", False) else "",
                    "enabled" if p.get("macro_obi_gate", True) else "disabled",
                    p.get("macro_obi_block_thresh", -0.30))
        logger.info("Gates: F&G block>%d boost<%d×%.1f | "
                    "Liq short>$%.0fM block, long>$%.0fM boost×%.1f | "
                    "L/S>%.1f block | RSI4h>%.0f block / <%.0f relax-VWAP",
                    p.get("fear_greed_block_thresh", 80),
                    p.get("fear_greed_boost_thresh", 25),
                    p.get("fear_greed_boost_mult", 1.5),
                    p.get("liq_short_block_usd", 10_000_000) / 1e6,
                    p.get("liq_long_spike_usd",   5_000_000) / 1e6,
                    p.get("liq_long_boost_mult", 1.3),
                    p.get("ls_ratio_block_high", 3.0),
                    p.get("rsi4h_block_high", 70.0),
                    p.get("rsi4h_relax_vwap_low", 35.0))
        logger.info("Rebuy: trail=%.1f%%  expiry=%dd  cooldown=%ds (min=%ds  strong=%.2f)",
                    p.get("rebuy_trail_pct", 0.0) * 100,
                    p.get("rebuy_max_age_days", 30),
                    p.get("min_scale_interval_s", 3600),
                    p.get("scale_in_cooldown_min_s", p.get("min_scale_interval_s", 3600)),
                    p.get("scale_in_obi_strong_thresh", 0.80))

        from tradinetools import heartbeat_loop
        tasks = [
            asyncio.create_task(_zmq_loop(state, db)),
            asyncio.create_task(_stats_loop(state)),
            asyncio.create_task(
                heartbeat_loop(
                    "accumulation_bot",
                    install_dir or None,
                    lambda: {
                        "bounds_ok":      state.free_usdt > 0,
                        "holdings_btc":   round(state.holdings_btc, 6),
                        "free_usdt":      round(state.free_usdt, 2),
                        "avg_entry":      round(state.avg_entry, 2),
                        "total_realized": round(state.total_realized, 2),
                    },
                    mode="sim",  # paper trading only — no real exchange connector
                )
            ),
        ]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Shutdown")


def main() -> None:
    ap = argparse.ArgumentParser(description="BTC long-term accumulation bot v1.5")
    ap.add_argument("--strategy", metavar="JSON")
    ap.add_argument("--dir",      default=None)
    args = ap.parse_args()

    install_dir = Path(args.dir).expanduser() if args.dir else Path.home() / "tradinebotte"
    install_dir.mkdir(parents=True, exist_ok=True)

    setup_root_logger(install_dir / "accumulation_bot.log")
    try:
        from version import __version__ as _ver  # pylint: disable=import-outside-toplevel
    except ImportError:
        _ver = "?"
    logger.info("tradinebotte v%s accumulation_bot PID=%d starting — dir=%s",
                _ver, os.getpid(), install_dir)

    p = dict(DEFAULTS)
    if args.strategy:
        strat = Path(args.strategy)
        if not strat.is_absolute():
            strat = install_dir / strat
        with open(strat, encoding="utf-8") as f:
            p.update({k: v for k, v in json.load(f).items()
                      if not k.startswith("_")})

    db = init_db(install_dir / "live_accum.db")
    try:
        asyncio.run(_run(p, db, str(install_dir)))
    except KeyboardInterrupt:
        logger.info("Interrupted")
    finally:
        db.close()


if __name__ == "__main__":
    main()
