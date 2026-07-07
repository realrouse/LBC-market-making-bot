#!/usr/bin/env python3
"""AccumulationStrategy — BTC long-term accumulation as a hosted strategy engine.

Ported from the former standalone accumulation_bot.py (which owned its own main/argparse/
asyncio.run/ZMQ SUB loop/register loop/heartbeat/control/DB). The host now owns the data
plane: live_bot.py builds the BotState + aiohttp session + heartbeat/control loops, and
cex_consumer.indicators_consumer_loop SUBs the indicators service and routes messages:

  primary  btc_scalping_spot  → on_book_update(state, ts)   ts: mid / obi_ema / spread_bps / ts_ms
  gates    btc_vwap_context, btc_macro_obi, fear_greed, btc_liquidations,
           btc_ls_ratio, btc_4h  → on_indicator(state, msg)

This engine owns only strategy state + logic. It is **paper** (no exchange connector — _buy/
_sell self-account exactly like the original standalone bot); botcore.connectors requires no
methods for strategy_type="accumulation". EarnManager is kept wired but is inert without
BINANCE_API_KEY/SECRET (all bots are sim today); the host hands in the aiohttp session.

Behaviour is intended to be byte-identical to the standalone bot on the same stream input —
see tests/test_accumulation_engine.py (parity + gate tests).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:                       # lazy at runtime (see _ensure_earn) — keeps
    from earn_manager import EarnManager  # `import strategy_engines` light for other bots

logger = logging.getLogger("accumulation")

STRATEGY_TYPE = "accumulation"

# ---------------------------------------------------------------------------
# Config defaults (moved verbatim from accumulation_bot.DEFAULTS)
# ---------------------------------------------------------------------------

DEFAULTS: dict = {
    "symbol":                "BTCUSDT",
    "capital_usdt":          1000.0,
    "initial_stake_usdt":    500.0,
    "scale_in_usdt":         100.0,
    "scale_in_dip_factor":   0.5,
    "scale_in_max_mult":     3.0,
    "max_invested_pct":      0.90,
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
    "scalping_stream_id":    "btc_scalping_spot",
    "vwap_gate":             True,
    "vwap_gate_initial":     False,
    "earn_enabled":          True,
    "earn_min_liquid_usdt":  20.0,
    "macro_obi_gate":         True,
    "macro_obi_block_thresh": -0.30,
    "macro_obi_stream_id":    "btc_macro_obi",
    "scale_in_cooldown_min_s":    900,
    "scale_in_obi_strong_thresh": 0.80,
    "rebuy_max_age_days":  30,
    "rebuy_trail_pct":     0.0,
    "fear_greed_gate":          True,
    "fear_greed_stream_id":     "fear_greed",
    "fear_greed_block_thresh":  80,
    "fear_greed_boost_thresh":  25,
    "fear_greed_boost_mult":    1.5,
    "liq_gate":             True,
    "liq_stream_id":        "btc_liquidations",
    "liq_short_block_usd":  10_000_000,
    "liq_long_spike_usd":    5_000_000,
    "liq_long_boost_mult":   1.3,
    "ls_ratio_gate":        True,
    "ls_ratio_stream_id":   "btc_ls_ratio",
    "ls_ratio_block_high":  3.0,
    "rsi4h_gate":           True,
    "rsi4h_stream_id":      "btc_4h",
    "rsi4h_block_high":     70.0,
    "rsi4h_relax_vwap_low": 35.0,
}

# ---------------------------------------------------------------------------
# State (moved verbatim)
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
    last_write_ts:      float = 0.0
    total_realized:     float = 0.0
    peak_holdings_btc:  float = 0.0
    vwap_dip_score:     float = 0.0
    vwap_dip_zone:      str   = "neutral"
    macro_obi:          float = 0.0
    macro_obi_dir:      str   = "neutral"
    fear_greed_val:     int   = 50
    fear_greed_label:   str   = "Neutral"
    liq_long_usd:       float = 0.0
    liq_short_usd:      float = 0.0
    ls_ratio:           float = 1.0
    rsi_4h:             float = 50.0
    earn: EarnManager | None  = field(default=None, repr=False)

    def unrealized_pct(self) -> float:
        if self.avg_entry > 0 and self.last_price > 0:
            return (self.last_price - self.avg_entry) / self.avg_entry * 100.0
        return 0.0

    def max_investable(self) -> float:
        return self.p["capital_usdt"] * self.p["max_invested_pct"]


class AccumulationStrategy:
    """BTC long-term accumulation: OBI dip-buying + partial profit ladder + macro gates.

    Conforms to the botcore.strategy.Strategy protocol (STRATEGY_TYPE + on_book_update) plus
    the CEX-engine extensions the host uses: ensure_schema, restore_from_db, heartbeat_payload,
    and the accumulation-only on_indicator hook (called by indicators_consumer_loop for the
    non-primary gate streams; other engines don't implement it)."""

    STRATEGY_TYPE = STRATEGY_TYPE

    def __init__(self, config: Any) -> None:
        cfg = dict(DEFAULTS)
        cfg.update({k: v for k, v in (getattr(config, "strategy_cfg", {}) or {}).items()
                    if not str(k).startswith("_")})
        self.acc = AccumState(p=cfg, free_usdt=cfg["capital_usdt"])
        # Start the freshness clock at construction so a never-writing restart ages to ⚠data
        # instead of staying silently green (same rationale as the standalone bot's boot set).
        self.acc.last_write_ts = time.time()
        logger.info(
            "AccumulationStrategy [%s] capital=%.0f  initial=%.0f  scale-in=%.0f "
            "(dip_factor=%.1f max_mult=%.1f)  bands=%s%%  earn=%s",
            cfg["symbol"], cfg["capital_usdt"], cfg["initial_stake_usdt"],
            cfg["scale_in_usdt"], cfg.get("scale_in_dip_factor", 0.5),
            cfg.get("scale_in_max_mult", 3.0), cfg.get("profit_bands_pct"),
            "enabled" if cfg.get("earn_enabled", True) else "disabled")

    # ── Schema ────────────────────────────────────────────────────────────────

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
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

    # ── State persistence ──────────────────────────────────────────────────────

    def _save_state(self, conn: sqlite3.Connection) -> None:
        a = self.acc
        rebuys_json = json.dumps([
            {"band_pct": r.band_pct, "sell_price": r.sell_price,
             "qty_btc":  r.qty_btc,  "rebuy_price": r.rebuy_price,
             "ts_ms":    r.ts_ms,    "low_seen":    r.low_seen}
            for r in a.pending_rebuys
        ])
        bands_json = json.dumps(sorted(a.active_bands))
        conn.execute("""
            INSERT OR REPLACE INTO accum_state
                (id, ts_ms, holdings_btc, avg_entry, free_usdt, total_realized,
                 peak_holdings_btc, last_buy_ts, pending_rebuys_json, active_bands_json)
            VALUES (1,?,?,?,?,?,?,?,?,?)""",
            (int(time.time() * 1000), a.holdings_btc, a.avg_entry,
             a.free_usdt, a.total_realized, a.peak_holdings_btc,
             a.last_buy_ts, rebuys_json, bands_json))
        conn.commit()

    def _restore_state(self, conn: sqlite3.Connection) -> bool:
        a = self.acc
        try:
            row = conn.execute("""
                SELECT ts_ms, holdings_btc, avg_entry, free_usdt, total_realized,
                       peak_holdings_btc, last_buy_ts, pending_rebuys_json, active_bands_json
                FROM accum_state WHERE id=1""").fetchone()
        except sqlite3.OperationalError:
            return False
        if row is None:
            return False
        (_, holdings, avg, free, realized,
         peak, last_buy_ts, rebuys_json, bands_json) = row
        a.holdings_btc      = holdings
        a.avg_entry         = avg
        a.free_usdt         = free
        a.total_realized    = realized
        a.peak_holdings_btc = peak
        a.last_buy_ts       = last_buy_ts
        a.initial_done      = True
        if rebuys_json:
            for r in json.loads(rebuys_json):
                a.pending_rebuys.append(PendingRebuy(**r))
        if bands_json:
            a.active_bands = set(json.loads(bands_json))
        logger.info("Restored: %.6f BTC @ avg %.2f  free=%.2f  realized=%+.2f  rebuys=%d  bands=%s",
                    holdings, avg, free, realized,
                    len(a.pending_rebuys), sorted(a.active_bands))
        return True

    async def restore_from_db(self, state: Any) -> bool:
        """Host-called at startup (symmetry with grid/swing). Ensures schema, restores state,
        and wires EarnManager to the host's aiohttp session (inert without credentials)."""
        self.ensure_schema(state.conn)
        self._ensure_earn(state)
        return self._restore_state(state.conn)

    def _ensure_earn(self, state: Any) -> None:
        a = self.acc
        if (a.earn is None and a.p.get("earn_enabled", True)
                and getattr(state, "session", None) is not None):
            from earn_manager import EarnManager  # noqa: PLC0415  pylint: disable=import-outside-toplevel
            a.earn = EarnManager(state.session)

    # ── Heartbeat ───────────────────────────────────────────────────────────────

    def heartbeat_payload(self) -> dict:
        """The status-page payload — same keys the standalone bot pushed, so acct-2/3/4
        render unchanged. pnl_total aliases total_realized (unified cumulative-PnL field)."""
        a = self.acc
        return {
            "bounds_ok":      a.free_usdt > 0,
            "holdings_btc":   round(a.holdings_btc, 6),
            "free_usdt":      round(a.free_usdt, 2),
            "avg_entry":      round(a.avg_entry, 2),
            "total_realized": round(a.total_realized, 2),
            "pnl_total":      round(a.total_realized, 2),
            "last_write_ts":  a.last_write_ts,
        }

    # ── Adaptive helpers ────────────────────────────────────────────────────────

    def _scale_in_amount(self, price: float) -> float:
        a        = self.acc
        p        = a.p
        base     = p.get("scale_in_usdt", 100.0)
        if a.avg_entry <= 0:
            return base
        dip_pct  = (a.avg_entry - price) / a.avg_entry * 100.0
        factor   = p.get("scale_in_dip_factor", 0.5)
        max_mult = p.get("scale_in_max_mult", 3.0)
        mult     = 1.0 + factor * max(dip_pct, 0.0)

        if p.get("fear_greed_gate", True):
            if a.fear_greed_val < p.get("fear_greed_boost_thresh", 25):
                max_mult *= p.get("fear_greed_boost_mult", 1.5)

        if p.get("liq_gate", True):
            if a.liq_long_usd > p.get("liq_long_spike_usd", 5_000_000):
                max_mult = min(max_mult * p.get("liq_long_boost_mult", 1.3),
                               p.get("scale_in_max_mult", 3.0) * 2.0)

        return min(base * mult, base * max_mult)

    def _rebuy_discount(self) -> float:
        a       = self.acc
        p       = a.p
        min_d   = p.get("rebuy_discount_min_pct", 3.0)
        max_d   = p.get("rebuy_discount_max_pct", 10.0)
        mult    = p.get("rebuy_spread_mult", 3.0)
        pct     = max(min_d, min(max_d, a.spread_ema * mult))
        return pct / 100.0

    # ── Trade execution (paper) ─────────────────────────────────────────────────

    async def _buy(self, state: Any, price: float, usdt_amount: float,
                   reason: str, ts_ms: int) -> bool:
        a        = self.acc
        conn     = state.conn
        p        = a.p
        fee_rate = p["maker_fee_spot"] if p.get("use_limit_orders") else p["fee_spot"]
        qty_btc  = usdt_amount / price
        fee      = usdt_amount * fee_rate
        total    = usdt_amount + fee

        if total > a.free_usdt + p.get("buy_dust_tolerance_usdt", 0.01):
            logger.warning("BUY skipped — need %.2f USDT, have %.2f", total, a.free_usdt)
            return False

        earn_keep = p.get("earn_min_liquid_usdt", 20.0)
        if a.earn is not None:
            await a.earn.ensure_liquid(total, keep_liquid=earn_keep)

        if a.holdings_btc > 0 and a.avg_entry > 0:
            total_btc   = a.holdings_btc + qty_btc
            a.avg_entry = (a.holdings_btc * a.avg_entry + qty_btc * price) / total_btc
        else:
            a.avg_entry = price

        a.holdings_btc += qty_btc
        a.free_usdt    -= total
        a.peak_holdings_btc = max(a.peak_holdings_btc, a.holdings_btc)

        if a.earn is not None:
            await a.earn.park_idle(a.free_usdt, keep_liquid=earn_keep)

        conn.execute("""
            INSERT INTO accum_trades
                (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
                 avg_entry_after, holdings_after, free_usdt_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts_ms, "buy", reason, price, qty_btc, usdt_amount, fee,
             a.avg_entry, a.holdings_btc, a.free_usdt))
        conn.commit()
        self._save_state(conn)

        logger.info("BUY  %.6f BTC @ %8.2f  [%-22s]  avg=%8.2f  held=%.6f  free=%7.2f  uPnL=%+.2f%%",
                    qty_btc, price, reason, a.avg_entry,
                    a.holdings_btc, a.free_usdt, a.unrealized_pct())
        return True

    async def _sell(self, state: Any, price: float, qty_btc: float,
                    reason: str, ts_ms: int) -> bool:
        a = self.acc
        if qty_btc <= 0 or a.holdings_btc <= 0:
            return False
        conn     = state.conn
        qty_btc  = min(qty_btc, a.holdings_btc)
        p        = a.p
        fee_rate = p["maker_fee_spot"] if p.get("use_limit_orders") else p["fee_spot"]
        usdt_val = qty_btc * price
        fee      = usdt_val * fee_rate
        realized = usdt_val - fee - qty_btc * (a.avg_entry or price)

        a.holdings_btc   -= qty_btc
        a.free_usdt      += usdt_val - fee
        a.total_realized += realized
        if a.holdings_btc <= 0:
            a.avg_entry = 0.0

        earn_keep = p.get("earn_min_liquid_usdt", 20.0)
        if a.earn is not None:
            await a.earn.park_idle(a.free_usdt, keep_liquid=earn_keep)

        conn.execute("""
            INSERT INTO accum_trades
                (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
                 avg_entry_after, holdings_after, free_usdt_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts_ms, "sell", reason, price, qty_btc, usdt_val, fee,
             a.avg_entry, a.holdings_btc, a.free_usdt))
        conn.commit()
        self._save_state(conn)

        logger.info("SELL %.6f BTC @ %8.2f  [%-22s]  realized=%+7.2f  held=%.6f  free=%7.2f",
                    qty_btc, price, reason, realized, a.holdings_btc, a.free_usdt)
        return True

    # ── Strategy logic ──────────────────────────────────────────────────────────

    async def _check_profit_bands(self, state: Any, price: float, ts_ms: int) -> None:
        a = self.acc
        if a.holdings_btc <= 0 or a.avg_entry <= 0:
            return
        p        = a.p
        bands    = sorted(p.get("profit_bands_pct", []))
        fraction = p.get("sell_fraction", 0.20)
        discount = self._rebuy_discount()

        min_hold_pct = p.get("min_holdings_pct", 0.0)
        floor_btc    = a.peak_holdings_btc * min_hold_pct if a.peak_holdings_btc > 0 else 0.0

        for band_pct in bands:
            if band_pct in a.active_bands:
                continue
            target = a.avg_entry * (1.0 + band_pct / 100.0)
            if price < target:
                break
            qty = a.holdings_btc * fraction
            max_sellable = max(0.0, a.holdings_btc - floor_btc)
            qty = min(qty, max_sellable)
            if qty < 1e-6:
                logger.info("Band +%.1f%% skipped — holdings at floor (%.2f%%)",
                            band_pct, min_hold_pct * 100)
                continue
            if await self._sell(state, price, qty, f"profit+{band_pct:.1f}%", ts_ms):
                rebuy = price * (1.0 - discount)
                a.active_bands.add(band_pct)
                a.pending_rebuys.append(
                    PendingRebuy(band_pct=band_pct, sell_price=price,
                                 qty_btc=qty, rebuy_price=rebuy, ts_ms=ts_ms))
                logger.info("  → rebuy %.6f BTC @ %.2f  (spread=%.4f%% → discount=%.3f%%)",
                            qty, rebuy, a.spread_ema, discount * 100)

    async def _check_rebuys(self, state: Any, price: float, ts_ms: int) -> None:
        a = self.acc
        p       = a.p
        max_age = p.get("rebuy_max_age_days", 30) * 86_400_000
        trail   = p.get("rebuy_trail_pct", 0.0)

        expired = []
        filled  = []
        for rb in a.pending_rebuys:
            if rb.ts_ms > 0 and 0 < max_age < (ts_ms - rb.ts_ms):
                logger.info("Rebuy +%.1f%% expired (age=%dd) — band re-armed",
                            rb.band_pct, (ts_ms - rb.ts_ms) // 86_400_000)
                a.active_bands.discard(rb.band_pct)
                expired.append(rb)
                continue

            if price > rb.rebuy_price:
                continue

            if trail > 0.0:
                rb.low_seen = price if rb.low_seen < 0 else min(rb.low_seen, price)
                if price < rb.low_seen * (1.0 + trail):
                    continue

            usdt_needed = rb.qty_btc * price
            if usdt_needed > a.free_usdt:
                continue
            if await self._buy(state, price, usdt_needed, f"rebuy+{rb.band_pct:.1f}%", ts_ms):
                a.active_bands.discard(rb.band_pct)
                filled.append(rb)

        for rb in expired + filled:
            a.pending_rebuys.remove(rb)

    async def _check_obi_scale_in(self, state: Any, price: float, ts_ms: int) -> None:
        a = self.acc
        p          = a.p
        max_invest = a.max_investable()

        base_iv  = p.get("min_scale_interval_s", 3600)
        floor_iv = p.get("scale_in_cooldown_min_s", base_iv)
        strong   = p.get("scale_in_obi_strong_thresh", 0.80)
        min_iv   = max(floor_iv, base_iv // 2) if abs(a.obi_ema) >= strong else base_iv
        if (ts_ms - a.last_buy_ts) / 1000.0 < min_iv:
            return

        invested = a.holdings_btc * price
        scale_usdt = self._scale_in_amount(price)
        if invested + scale_usdt > max_invest:
            logger.debug("Scale-in skipped — max invested (%.0f%%)", p["max_invested_pct"] * 100)
            return
        if scale_usdt > a.free_usdt:
            return

        if p.get("macro_obi_gate", True):
            thresh = p.get("macro_obi_block_thresh", -0.30)
            if a.macro_obi < thresh:
                logger.debug("Scale-in blocked — macro OBI bearish (%.3f < %.3f)",
                             a.macro_obi, thresh)
                return

        rsi4h_gate = p.get("rsi4h_gate", True)
        if rsi4h_gate and a.rsi_4h > p.get("rsi4h_block_high", 70.0):
            logger.debug("Scale-in blocked — 4h RSI overbought (%.1f)", a.rsi_4h)
            return

        if p.get("fear_greed_gate", True):
            if a.fear_greed_val > p.get("fear_greed_block_thresh", 80):
                logger.debug("Scale-in blocked — extreme greed (F&G=%d %s)",
                             a.fear_greed_val, a.fear_greed_label)
                return

        vwap_blocked = p.get("vwap_gate", True) and a.vwap_dip_score < 0.0
        if vwap_blocked:
            relax_low = p.get("rsi4h_relax_vwap_low", 35.0) if rsi4h_gate else 0.0
            if rsi4h_gate and a.rsi_4h <= relax_low:
                logger.debug("VWAP gate relaxed — 4h RSI oversold (%.1f <= %.1f)",
                             a.rsi_4h, relax_low)
            else:
                logger.debug("Scale-in skipped — price above VWAP (dip_score=%.4f zone=%s)",
                             a.vwap_dip_score, a.vwap_dip_zone)
                return

        if p.get("ls_ratio_gate", True):
            if a.ls_ratio > p.get("ls_ratio_block_high", 3.0):
                logger.debug("Scale-in blocked — extreme long crowding (L/S=%.2f)", a.ls_ratio)
                return

        if p.get("liq_gate", True):
            if a.liq_short_usd > p.get("liq_short_block_usd", 10_000_000):
                logger.debug("Scale-in blocked — short squeeze (liq_short=%.0f USDT)",
                             a.liq_short_usd)
                return

        dip_pct = ((a.avg_entry - price) / a.avg_entry * 100.0
                   if a.avg_entry > 0 else 0.0)
        reason = f"obi_dip({dip_pct:+.1f}%)"
        if await self._buy(state, price, scale_usdt, reason, ts_ms):
            a.last_buy_ts = ts_ms

    # ── Snapshot ─────────────────────────────────────────────────────────────────

    def _record_snapshot(self, conn: sqlite3.Connection, mid: float, ts_ms: int) -> None:
        """One accum_snapshots row + advance the freshness clock (→ status ⚠data). Named,
        tested step (same rationale as the standalone _record_accum_snapshot)."""
        a = self.acc
        invested = a.holdings_btc * a.avg_entry if a.avg_entry > 0 else 0.0
        conn.execute("""
            INSERT INTO accum_snapshots
                (ts_ms, price, holdings_btc, avg_entry, invested_usdt,
                 free_usdt, unrealized_pct, obi_ema)
            VALUES (?,?,?,?,?,?,?,?)""",
            (ts_ms, float(mid), a.holdings_btc, a.avg_entry or 0.0,
             invested, a.free_usdt, a.unrealized_pct(), a.obi_ema))
        conn.commit()
        a.last_write_ts = ts_ms / 1000.0

    # ── Primary tick (scalping stream) ───────────────────────────────────────────

    async def on_book_update(self, state: Any, ts: Any, _t_ws: Optional[float] = None) -> None:
        """Primary tick from the scalping indicator stream. `ts` carries mid / obi_ema /
        spread_bps / ts_ms (built by cex_consumer.indicators_consumer_loop). Body ported
        verbatim from the standalone _handle_indicator."""
        a = self.acc
        mid = getattr(ts, "mid", None)
        if mid is None:
            return
        self._ensure_earn(state)
        conn  = state.conn
        ts_ms = int(getattr(ts, "ts_ms", None) or time.time() * 1000)

        a.last_price = float(mid)

        obi_ema = getattr(ts, "obi_ema", None)
        if obi_ema is not None:
            a.obi_ema = float(obi_ema)

        spread_bps = getattr(ts, "spread_bps", None)
        if spread_bps is not None:
            spread_pct = float(spread_bps) / 100.0
            _alpha = a.p.get("spread_ema_alpha", 0.1)
            a.spread_ema = _alpha * spread_pct + (1 - _alpha) * a.spread_ema

        a.snap_counter += 1
        if a.snap_counter % a.p["snapshot_every_n"] == 0:
            self._record_snapshot(conn, float(mid), ts_ms)

        price = float(mid)

        if not a.initial_done:
            if a.p.get("vwap_gate_initial", False):
                if a.p.get("vwap_gate", True) and a.vwap_dip_score < 0.0:
                    return
            init_usdt = min(a.p["initial_stake_usdt"], a.free_usdt)
            if await self._buy(state, price, init_usdt, "initial", ts_ms):
                a.last_buy_ts = ts_ms
                a.initial_done = True
            return

        await self._check_profit_bands(state, price, ts_ms)
        await self._check_rebuys(state, price, ts_ms)

        thresh = a.p["obi_entry_thresh"]
        if a.obi_ema < -thresh:
            a.pending_count += 1
        else:
            a.pending_count = 0

        if a.pending_count >= a.p["obi_confirm_n"]:
            await self._check_obi_scale_in(state, price, ts_ms)
            a.pending_count = 0

    # ── Gate streams ──────────────────────────────────────────────────────────────

    async def on_indicator(self, _state: Any, msg: dict) -> None:
        """Non-primary gate streams → gate-state setters (routed by the host on stream_id).
        Async to match the host's `await`; setters themselves are synchronous."""
        a   = self.acc
        p   = a.p
        sid = msg.get("stream_id")
        if sid == p.get("macro_obi_stream_id", "btc_macro_obi"):
            val = msg.get("macro_obi")
            if val is not None:
                a.macro_obi = float(val)
            a.macro_obi_dir = str(msg.get("macro_obi_direction", "neutral"))
        elif sid == "btc_vwap_context":
            ds = msg.get("dip_score")
            if ds is not None:
                a.vwap_dip_score = float(ds)
            a.vwap_dip_zone = str(msg.get("dip_zone", "neutral"))
        elif sid == p.get("fear_greed_stream_id", "fear_greed"):
            val = msg.get("fear_greed")
            if val is not None:
                a.fear_greed_val = int(val)
            a.fear_greed_label = str(msg.get("fear_greed_label", "Neutral"))
        elif sid == p.get("liq_stream_id", "btc_liquidations"):
            lo, sh = msg.get("liq_long_usd"), msg.get("liq_short_usd")
            if lo is not None:
                a.liq_long_usd = float(lo)
            if sh is not None:
                a.liq_short_usd = float(sh)
        elif sid == p.get("ls_ratio_stream_id", "btc_ls_ratio"):
            ratio = msg.get("long_short_ratio")
            if ratio is not None:
                a.ls_ratio = float(ratio)
        elif sid == p.get("rsi4h_stream_id", "btc_4h"):
            rsi = msg.get("rsi_14")
            if rsi is not None:
                a.rsi_4h = float(rsi)
