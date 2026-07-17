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

from api_common import decimals_for_price

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
    # Ichimoku daily-cloud trend gate — blocks scale-in when price is BELOW the cloud
    # (bearish structure). DEFAULT OFF: this fights the deep-dip accumulation thesis
    # (cf. rsi4h_relax_vwap_low, which buys MORE when oversold). Wired but opt-in.
    # Fail-open — inert until a cloud value arrives or if it goes stale.
    "ichimoku_gate":        False,
    "ichimoku_stream_id":   "btc_4h_ichimoku",   # 4h cloud (more responsive than daily)
    "ichi_stale_secs":      172800.0,   # 2 days — fail-open staleness guard, not a freshness need

    # ── Live maker execution (Option B) — OFF by default so every existing accum bot stays
    # pure paper. When live_execution=true the engine places REAL maker BUY orders (buy-only:
    # profit-band sells + rebuys are skipped) and reconciles holdings from actual fills.
    "live_execution":       False,
    "shadow":               False,   # live_execution + shadow → log intended orders, place NOTHING
    "maker_bid_offset_pct":  0.5,     # rest the bid this % below mid (maker, no cross; buys the dip)
    "rebid_stale_pct":       2.0,     # cancel & re-bid if price rises this % above our resting bid
    "rebid_max_age_s":       3600,    # …or if the bid has rested longer than this
    # ── Live resting SELL ladder (the ratchet). Empty = buy-only, the default. ──
    "sell_ladder":           [],      # [{band_pct, fraction}] off avg_entry, e.g. 30% @ +5%
    "sell_ceiling_price":    0.0,     # 0 = none; no NEW sells at/above this price (let it run)
    "sell_rearm_tol_pct":    0.5,     # re-price a resting sell only past this drift (hysteresis)
    "sell_resize_tol_pct":   10.0,    # …and re-SIZE it past this qty drift (looser: partials nibble)
    "sell_min_notional_usdt": 1.1,    # skip dust bands (MEXC rejects < 1 USDT, code 30002)
    "sell_breakout_gate":    False,   # block NEW sells while ripping away from basis
    "sell_breakout_pct":     25.0,    # …meaning price > avg_entry * (1 + this%)
    "drift_check_every_s":   300,     # how often to reconcile books vs the real balance
    "drift_tolerance":       0.01,    # base-asset units of slack before halting
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
    # live maker execution: the ONE resting bid in flight, or None (buy-only, Option B).
    # {order_id, price, orig_qty, executed_qty_seen, quote_spent_seen, placed_ts}
    pending_buy:        Optional[dict] = None
    # The resting SELL ladder: order_id -> record (same shape + band_pct). Separate from
    # pending_buy because the lifecycles genuinely differ — bids are one-at-a-time and
    # staleness-driven, sells are a declarative ladder re-armed against avg_entry.
    open_sells:         dict  = field(default_factory=dict)
    # Tripped when internal books over-claim vs the exchange: blocks ALL placement.
    halted:             bool  = False
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
    ichi_cloud_top:     Optional[float] = None
    ichi_cloud_bottom:  Optional[float] = None
    ichi_ts:            float = 0.0
    earn: EarnManager | None  = field(default=None, repr=False)

    def unrealized_pct(self) -> float:
        if self.avg_entry > 0 and self.last_price > 0:
            return (self.last_price - self.avg_entry) / self.avg_entry * 100.0
        return 0.0

    def max_investable(self) -> float:
        return self.p["capital_usdt"] * self.p["max_invested_pct"]


# ── Live maker-buy reconciliation (PURE — no I/O, exhaustively unit-tested) ──────────
# The money-critical path: given the current resting-bid record and a fresh get_order()
# result, decide what changed. Kept pure + deterministic because shadow mode can NEVER
# exercise it (a resting maker bid may not fill for days), so correctness rests on tests,
# not on a live dry-run. Caller (the async wrapper) applies the deltas + issues any cancel.
# Placement-failure backoff: 2s → 4s → 8s … capped. Keeps a persistent rejection from
# retrying at book-tick rate (~2s) against an IP-whitelisted real-money key.
_PLACE_BACKOFF_BASE_S = 2.0
_PLACE_BACKOFF_MAX_S  = 300.0


def reconcile_order(tracked: dict, order: "dict | None") -> tuple:
    """(new_tracked, qty_delta, quote_delta, action) — side-agnostic fill accounting.

    The money-critical primitive, shared by BUY bids and the resting SELL ladder: given a
    tracked order record and a fresh exchange view of it, report what NEWLY filled.

    tracked : {order_id, price, orig_qty, executed_qty_seen, quote_spent_seen, placed_ts, …}
    order   : {status, executed_qty, cummulative_quote_qty} from get_order()/get_open_orders(),
              or None (API error — assume NOTHING, retry next tick).
    Returns:
      new_tracked  — updated record, or None once the order is done (filled/canceled/gone)
      qty_delta    — base qty NEWLY filled since last seen (≥0; a DELTA, never cumulative)
      quote_delta  — quote NEWLY moved since last seen (≥0; a DELTA), tracked independently
                     of qty so multi-partial fills at different prices don't drift the basis
      action       — 'noop'|'hold'|'partial'|'filled'|'canceled'

    Deliberately knows nothing about staleness or side: those are policy, decided by
    bid_is_stale() / plan_sell_ladder(). Crediting must happen before any re-arm, or a fill
    landing between ticks is lost when its order is cancelled.
    """
    if order is None:
        return tracked, 0.0, 0.0, "noop"        # API error: change nothing, retry
    dqty   = max(0.0, order["executed_qty"] - tracked["executed_qty_seen"])
    dquote = max(0.0, order["cummulative_quote_qty"] - tracked["quote_spent_seen"])
    if dqty > 0 or dquote > 0:                   # advance the seen counters (both)
        tracked = {**tracked,
                   "executed_qty_seen": order["executed_qty"],
                   "quote_spent_seen":  order["cummulative_quote_qty"]}
    status = order["status"]
    if status == "FILLED":
        return None, dqty, dquote, "filled"
    if status in ("CANCELED", "EXPIRED", "REJECTED"):
        return None, dqty, dquote, "canceled"
    if status == "PARTIALLY_FILLED":
        return tracked, dqty, dquote, "partial"
    return tracked, dqty, dquote, "hold"


def bid_is_stale(tracked: dict, *, now_ts: float, price: float,
                 stale_pct: float, max_age_s: float) -> bool:
    """Should a resting BUY bid be cancelled and re-placed? True when the market has run
    away above it, or it has rested too long. Buy-side policy only — sells are re-armed
    declaratively by plan_sell_ladder(), not by staleness."""
    return (price > tracked["price"] * (1.0 + stale_pct)
            or (now_ts - tracked["placed_ts"]) > max_age_s)


def reconcile_pending_buy(pending: dict, order: "dict | None", *, now_ts: float,
                          price: float, stale_pct: float, max_age_s: float) -> tuple:
    """Buy-side composition of reconcile_order() + bid_is_stale().

    Same contract as reconcile_order, plus one extra action: 'cancel' = still resting but
    stale, so the caller should cancel_order() and re-bid.
    """
    tracked, dqty, dquote, action = reconcile_order(pending, order)
    if action == "hold" and bid_is_stale(tracked, now_ts=now_ts, price=price,
                                         stale_pct=stale_pct, max_age_s=max_age_s):
        return tracked, dqty, dquote, "cancel"
    return tracked, dqty, dquote, action


def plan_sell_ladder(*, holdings: float, avg_entry: float, peak_holdings: float,
                     best_ask: float, price: float, params: dict,
                     gate_open: bool = True) -> list:
    """The DESIRED resting sell ladder, as pure policy: [{band_pct, price, qty}, …].

    Anchored to avg_entry (the WHOLE position's cost basis), never to individual tranches.
    Per-tranche anchoring sells the cheapest coins first — a dip-bought tranche's +5% fills
    on any bounce while an expensive tranche's never does — which ratchets the cost basis
    UP over time. Anchoring here means we only ever sell when the whole book is genuinely
    up N%, so every fill is profitable against real basis.

    Guarantees, in order:
      • floor      — never plan below peak_holdings * min_holdings_pct (the ratchet: a core
                     position that is never sold, whatever the ladder does)
      • ceiling    — no NEW sells at/above sell_ceiling_price: past it the breakout thesis
                     wins and we stop trimming the stake
      • maker      — clamp each price to >= best_ask so the order always rests instead of
                     crossing. If avg_entry*(1+band) is already below the market we are past
                     the band, so rest at the ask rather than sell into the bid
      • gate       — gate_open=False blocks NEW placements only; callers must NOT cancel
                     already-resting sells on the gate (a sell resting above market is
                     already trimming into strength — pulling it is backwards)
      • never oversell — the ladder totals <= sellable, so Σ resting qty can't exceed what
                     we own above the floor even if every band fills at once
    """
    if holdings <= 0 or avg_entry <= 0 or best_ask <= 0 or not gate_open:
        return []
    ceiling = params.get("sell_ceiling_price", 0.0)
    if ceiling > 0 and price >= ceiling:
        return []
    floor_btc = peak_holdings * params.get("min_holdings_pct", 0.0)
    sellable  = max(0.0, holdings - floor_btc)
    if sellable <= 0:
        return []
    plan, allocated = [], 0.0
    for step in params.get("sell_ladder", []):
        band_pct = float(step["band_pct"])
        target   = avg_entry * (1.0 + band_pct / 100.0)
        px       = max(target, best_ask)          # never cross → always maker
        if ceiling > 0 and px >= ceiling:
            continue
        qty = min(holdings * float(step["fraction"]), sellable - allocated)
        if qty <= 0:
            continue
        # Don't plan dust. The exchange rejects anything under its min notional (MEXC:
        # 1 USDT, code 30002), and a planned-but-unplaceable band is worse than no band —
        # it retries forever. Seen live: after a fill re-sized the ladder, the tail band
        # came to $0.99 and every tick re-attempted it. The margin absorbs the gap between
        # planning and placement (lot-step flooring, price drift).
        if qty * px < params.get("sell_min_notional_usdt", 1.1):
            continue
        plan.append({"band_pct": band_pct, "price": px, "qty": qty})
        allocated += qty
    return plan


def diff_sell_ladder(desired: list, tracked: list, *, tol_pct: float,
                     qty_tol_pct: float = 0.10) -> tuple:
    """(to_cancel, to_place) — declarative re-arm of the resting ladder.

    Matches by band_pct and replaces an order whose price has drifted past tol_pct OR whose
    size has drifted past qty_tol_pct. Both hysteresis bands matter:
      • price — without it, every buy nudges avg_entry, re-prices every band and churns the
        whole ladder, burning rate limit and queue position for nothing.
      • QTY — a resting order is sized from the holdings of the moment it was placed. When a
        sibling band fills, holdings drop and that order is left over-sized, still claiming
        coins the new plan wants to spread across the ladder. Observed live: after the +5%
        band filled, the surviving +10% order still held the ENTIRE sellable amount, so
        every re-arm asked for a +5% that could not fit and the oversell invariant refused
        it on every tick. Comparing price alone never sees this.
    qty_tol is deliberately looser than price tol: partial fills nibble the size constantly
    and re-arming on each one would churn for no benefit.

    tracked entries are order records carrying band_pct + price + orig_qty. Any tracked band
    absent from `desired` is cancelled (floor/ceiling reached, or holdings fell).
    """
    by_band = {t["band_pct"]: t for t in tracked}
    to_cancel, to_place = [], []
    for d in desired:
        t = by_band.pop(d["band_pct"], None)
        if t is None:
            to_place.append(d)
            continue
        remaining = t["orig_qty"] - t.get("executed_qty_seen", 0.0)
        if (abs(d["price"] - t["price"]) > t["price"] * tol_pct
                or abs(d["qty"] - remaining) > max(remaining, d["qty"]) * qty_tol_pct):
            to_cancel.append(t)                   # re-price/re-size: cancel, then re-place
            to_place.append(d)
    to_cancel.extend(by_band.values())            # tracked bands no longer wanted
    return to_cancel, to_place


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

        # ── Live maker execution (Option B, opt-in) ──────────────────────────────
        self.symbol   = cfg["symbol"]
        self.live     = bool(cfg.get("live_execution", False))
        self.shadow   = bool(cfg.get("shadow", False))
        self._api     = None
        self._adopted = False
        # Placement circuit breaker. A rejected order leaves no pending bid, so the next
        # book tick retries immediately — a persistent rejection (filter change, permission
        # flip, exchange maintenance) becomes a hot retry loop at tick rate against an
        # IP-whitelisted key. Observed for real: 20 rejects in 50s during the Content-Type
        # bug. Back off exponentially instead of hammering our way to a ban.
        self._fail_streak = 0
        self._retry_after = 0.0
        self._last_drift_ts = 0.0
        self._drift_ok      = None    # None = never checked (vs True = checked and clean)
        self._oversell_warn_ts = 0.0
        self._sell_fail_streak = 0
        self._sell_retry_after = 0.0
        if self.live:
            try:
                from connectors import load as _load_conn  # noqa: PLC0415  pylint: disable=import-outside-toplevel
                self._api = _load_conn(getattr(config, "connector", cfg.get("connector", "mexc")))
                logger.warning("AccumulationStrategy LIVE execution ENABLED [%s %s]%s — buy-only, "
                               "real maker BUY orders (sells disabled)", cfg.get("connector", "mexc"),
                               self.symbol, "  [SHADOW: places nothing]" if self.shadow else "")
            except Exception as e:  # pylint: disable=broad-exception-caught  # fail-safe → paper
                logger.error("LIVE execution requested but connector load failed — staying PAPER: %s", e)
                self.live = False

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
        # Live maker execution only: the single resting bid in flight (id=1) or empty.
        # A separate table so paper bots never touch it (they simply never write a row).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accum_pending_order (
                id              INTEGER PRIMARY KEY,
                pending_buy_json TEXT
            )""")
        # The resting SELL ladder, persisted as one JSON blob (id=1) so it is written
        # atomically. It MUST survive restart: adoption matches live open orders against
        # this to credit fills that landed while we were down, and to tell our own resting
        # sells apart from genuine orphans. Losing it would cancel a valid ladder on every
        # restart — churning the book and missing the wicks the ladder exists to catch.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accum_open_sells (
                id              INTEGER PRIMARY KEY,
                open_sells_json TEXT
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

    def _save_pending(self, conn: sqlite3.Connection) -> None:
        """Persist the single resting bid (live only) so a restart resumes/reconciles it."""
        conn.execute("INSERT OR REPLACE INTO accum_pending_order (id, pending_buy_json) VALUES (1,?)",
                     (json.dumps(self.acc.pending_buy) if self.acc.pending_buy else None,))
        conn.commit()

    def _save_sells(self, conn: sqlite3.Connection) -> None:
        """Persist the resting sell ladder (live only) so a restart can adopt it."""
        conn.execute("INSERT OR REPLACE INTO accum_open_sells (id, open_sells_json) VALUES (1,?)",
                     (json.dumps(list(self.acc.open_sells.values())),))
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
        try:
            prow = conn.execute(
                "SELECT pending_buy_json FROM accum_pending_order WHERE id=1").fetchone()
            if prow and prow[0]:
                a.pending_buy = json.loads(prow[0])
                logger.info("Restored resting bid: %s", a.pending_buy)
        except sqlite3.OperationalError:
            pass
        try:
            srow = conn.execute(
                "SELECT open_sells_json FROM accum_open_sells WHERE id=1").fetchone()
            if srow and srow[0]:
                a.open_sells = {s["order_id"]: s for s in json.loads(srow[0])}
                if a.open_sells:
                    logger.info("Restored %d resting sell(s): %s", len(a.open_sells),
                                [(s["band_pct"], s["price"]) for s in a.open_sells.values()])
        except sqlite3.OperationalError:
            pass
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
            "avg_entry":      round(a.avg_entry, decimals_for_price(a.avg_entry)),
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

    # ── Trade execution ─────────────────────────────────────────────────────────

    async def _buy(self, state: Any, price: float, usdt_amount: float,
                   reason: str, ts_ms: int) -> bool:
        """Dispatch a buy decision: LIVE (place a real maker bid, credit on fill) or PAPER
        (instant self-accounted fill). Call sites are unchanged; only the effect differs."""
        if self.live:
            return await self._place_live_buy(state, price, usdt_amount, reason, ts_ms)
        return await self._buy_paper(state, price, usdt_amount, reason, ts_ms)

    # ── Live maker execution (Option B — buy-only) ───────────────────────────────

    async def _place_live_buy(self, state: Any, price: float, usdt_amount: float,
                              reason: str, ts_ms: int) -> bool:
        """Place ONE resting maker bid below mid (0-cross, no impact). Holdings are NOT
        credited here — only on fill, in _reconcile_live_buy. One bid at a time caps the
        committed budget at a single clip. Shadow → log intent, place nothing, keep paper
        accounting so the strategy trajectory still runs for validation."""
        a = self.acc
        if not self._adopted:
            return False                              # no placement until startup adoption ran
        if a.pending_buy is not None:
            return False                              # one resting bid at a time
        if time.time() < self._retry_after:
            return False                              # backing off after placement failures
        if usdt_amount > a.free_usdt + a.p.get("buy_dust_tolerance_usdt", 0.01):
            logger.info("live BUY skipped — need %.2f USDT, budget %.2f", usdt_amount, a.free_usdt)
            return False
        offset = a.p.get("maker_bid_offset_pct", 0.5) / 100.0
        bid    = price * (1.0 - offset)
        pd     = decimals_for_price(bid)
        if self.shadow:
            logger.info("SHADOW would place maker BUY %.2f USDT @ %.*f (mid %.*f) [%s] — placing nothing",
                        usdt_amount, pd, bid, pd, price, reason)
            return await self._buy_paper(state, bid, usdt_amount, reason + "|shadow", ts_ms)
        oid = await self._api.post_order(state.session, self.symbol, bid, usdt_amount, side="BUY")
        if not oid or str(oid).startswith("sim_"):
            self._fail_streak += 1
            backoff = min(_PLACE_BACKOFF_BASE_S * 2 ** (self._fail_streak - 1), _PLACE_BACKOFF_MAX_S)
            self._retry_after = time.time() + backoff
            logger.error("live BUY placement failed (oid=%s) — key/precision/permission? "
                         "Not tracking. Retry #%d backing off %.0fs.", oid, self._fail_streak, backoff)
            return False
        self._fail_streak = 0
        self._retry_after = 0.0
        a.pending_buy = {"order_id": str(oid), "price": bid, "orig_qty": usdt_amount / bid,
                         "executed_qty_seen": 0.0, "quote_spent_seen": 0.0,
                         # `reason` links a rebuy bid back to its obligation, which is
                         # discharged on FILL (in _credit_fill) — never on placement.
                         "reason": reason, "placed_ts": time.time()}
        self._save_pending(state.conn)
        logger.info("LIVE maker BUY placed id=%s  %.2f USDT @ %.*f  [%s]", oid, usdt_amount, pd, bid, reason)
        return True

    def _credit_fill(self, state: Any, dqty: float, dquote: float, ts_ms: int) -> None:
        """Apply a REAL fill delta to holdings/free/avg_entry from actual filled base + quote
        spent (not an assumed price), and record the accum_trades row + freshness clock."""
        a = self.acc
        fill_price = dquote / dqty if dqty > 0 else 0.0
        if a.holdings_btc > 0 and a.avg_entry > 0:
            tot = a.holdings_btc + dqty
            a.avg_entry = (a.holdings_btc * a.avg_entry + dqty * fill_price) / tot
        else:
            a.avg_entry = fill_price
        a.holdings_btc += dqty
        a.free_usdt    -= dquote                      # real quote spent (maker fee ≈ 0)
        a.peak_holdings_btc = max(a.peak_holdings_btc, a.holdings_btc)
        # A real fill means we now own the tranche — this, not the placement, is what
        # completes the initial buy (see the live note in on_book_update).
        a.initial_done = True
        a.last_buy_ts  = ts_ms
        # Discharge a rebuy obligation only once its bid actually FILLS. Removing it at
        # placement (as the paper instant-fill path can) would drop the obligation whenever
        # the bid is later cancelled unfilled — we would have sold coins and quietly
        # abandoned the plan to buy them back cheaper, which is the whole ratchet.
        reason = (a.pending_buy or {}).get("reason", "")
        if reason.startswith("rebuy+"):
            for rb in list(a.pending_rebuys):
                if f"rebuy+{rb.band_pct:.1f}%" == reason:
                    a.pending_rebuys.remove(rb)
                    a.active_bands.discard(rb.band_pct)
                    logger.info("  rebuy obligation +%.1f%% discharged on fill", rb.band_pct)
                    break
        state.conn.execute("""
            INSERT INTO accum_trades
                (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
                 avg_entry_after, holdings_after, free_usdt_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts_ms, "buy", "live-fill", fill_price, dqty, dquote, 0.0,
             a.avg_entry, a.holdings_btc, a.free_usdt))
        a.last_write_ts = time.time()
        self._save_state(state.conn)
        pd = decimals_for_price(fill_price)
        logger.info("LIVE FILL +%.4f @ %.*f  spent=%.2f  held=%.4f  free=%.2f  avg=%.*f",
                    dqty, pd, fill_price, dquote, a.holdings_btc, a.free_usdt, pd, a.avg_entry)

    async def _reconcile_live_buy(self, state: Any, price: float, ts_ms: int) -> None:
        """Poll the resting bid, credit any new fill, and cancel it if it has gone stale.
        Pure decision in reconcile_pending_buy(); this only does the I/O + applies deltas."""
        a = self.acc
        if not a.pending_buy:
            return
        oid = a.pending_buy["order_id"]
        order = await self._api.get_order(state.session, self.symbol, oid)
        new_pending, dqty, dquote, action = reconcile_pending_buy(
            a.pending_buy, order, now_ts=time.time(), price=price,
            stale_pct=a.p.get("rebid_stale_pct", 2.0) / 100.0,
            max_age_s=a.p.get("rebid_max_age_s", 3600))
        if dqty > 0:
            self._credit_fill(state, dqty, dquote, ts_ms)
        if action == "cancel":
            # NEW but stale — cancel; next tick sees CANCELED and clears it (catching any last fill).
            logger.info("resting bid %s stale (mid %.6f vs bid %.6f) — canceling to re-bid",
                        oid, price, a.pending_buy["price"])
            await self._api.cancel_order(state.session, self.symbol, oid)
        a.pending_buy = new_pending
        self._save_pending(state.conn)

    # ── Live resting SELL ladder (the ratchet) ──────────────────────────────────

    def _credit_sell_fill(self, state: Any, rec: dict, dqty: float, dquote: float,
                          ts_ms: int) -> None:
        """Apply a REAL sell-fill delta: holdings down, USDT up, and EXTEND the rebuy
        obligation by exactly this delta at this fill's price.

        The rebuy obligation is minted per FILL DELTA, not per placed order: a resting sell
        fills in pieces over time, and minting the whole slice up-front (as the paper
        instant-fill path does) would leave an obligation to rebuy coins we never sold if
        the remainder is cancelled."""
        a = self.acc
        fill_price = dquote / dqty if dqty > 0 else 0.0
        realized   = dquote - dqty * (a.avg_entry or fill_price)
        a.holdings_btc   -= dqty
        a.free_usdt      += dquote                    # maker → fee 0, proceeds are exact
        a.total_realized += realized
        if a.holdings_btc <= 1e-12:
            a.avg_entry = 0.0                         # flat: no basis to carry
        # Coin-positive by construction: rebuy MORE than we sold, at a discount to the
        # actual fill price. This is the engine of the ratchet — the sell only exists to
        # fund a cheaper rebuy.
        discount = self._rebuy_discount()
        band     = rec.get("band_pct", 0.0)
        a.pending_rebuys.append(
            PendingRebuy(band_pct=band, sell_price=fill_price, qty_btc=dqty,
                         rebuy_price=fill_price * (1.0 - discount), ts_ms=ts_ms))
        state.conn.execute("""
            INSERT INTO accum_trades
                (ts_ms, side, reason, price, qty_btc, usdt_value, fee_usdt,
                 avg_entry_after, holdings_after, free_usdt_after)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (ts_ms, "sell", f"ladder+{band:.1f}%", fill_price, dqty, dquote, 0.0,
             a.avg_entry, a.holdings_btc, a.free_usdt))
        a.last_write_ts = time.time()
        self._save_state(state.conn)
        pd = decimals_for_price(fill_price)
        logger.info("LIVE SELL FILL -%.4f @ %.*f  got=%.2f  realized=%+.2f  held=%.4f  "
                    "free=%.2f  → rebuy %.4f @ %.*f (-%.1f%%)",
                    dqty, pd, fill_price, dquote, realized, a.holdings_btc, a.free_usdt,
                    dqty, pd, fill_price * (1.0 - discount), discount * 100)

    async def _reconcile_live_sells(self, state: Any, ts_ms: int) -> None:
        """Credit fills on every resting sell. Runs BEFORE any re-arm: cancelling an order
        without first crediting a fill that landed since the last tick loses real coins.

        One get_open_orders() call covers every still-open order (including partial-fill
        progress); orders that vanished from the book need a get_order each to learn their
        final state — normally none."""
        a = self.acc
        if not a.open_sells:
            return
        oo = await self._api.get_open_orders(state.session, self.symbol)
        if oo is None:
            return                                    # transient error: assume nothing
        live = {o["order_id"]: o for o in oo}
        for oid, rec in list(a.open_sells.items()):
            view = live.get(oid)
            if view is None:                          # gone from the book → settle it
                view = await self._api.get_order(state.session, self.symbol, oid)
            new_rec, dqty, dquote, action = reconcile_order(rec, view)
            if dqty > 0:
                self._credit_sell_fill(state, rec, dqty, dquote, ts_ms)
            if new_rec is None:
                a.open_sells.pop(oid, None)
            else:
                a.open_sells[oid] = new_rec
        self._save_sells(state.conn)

    def _breakout_gate_open(self, price: float) -> bool:
        """False while price is breaking out — blocks NEW sells only, never cancels resting
        ones. Deliberately the dumbest thing that works: the floor and ceiling are the real
        guardrails, and a clever gate that fights the re-arm logic just churns the book."""
        a = self.acc
        if not a.p.get("sell_breakout_gate", False):
            return True
        ref = a.avg_entry
        thresh = a.p.get("sell_breakout_pct", 25.0) / 100.0
        if ref > 0 and price > ref * (1.0 + thresh):
            return False                              # ripping away from basis → let it run
        return True

    async def _rearm_sell_ladder(self, state: Any, price: float, best_ask: float,
                                 ts_ms: int) -> None:
        """Reconcile the resting ladder toward the plan: cancel what no longer belongs,
        place what is missing. Declarative — the plan is a pure function of state, so this
        is idempotent and self-correcting."""
        a = self.acc
        if a.halted or not a.p.get("sell_ladder"):
            return
        desired = plan_sell_ladder(
            holdings=a.holdings_btc, avg_entry=a.avg_entry,
            peak_holdings=a.peak_holdings_btc, best_ask=best_ask, price=price,
            params=a.p, gate_open=self._breakout_gate_open(price))
        to_cancel, to_place = diff_sell_ladder(
            desired, list(a.open_sells.values()),
            tol_pct=a.p.get("sell_rearm_tol_pct", 0.5) / 100.0,
            qty_tol_pct=a.p.get("sell_resize_tol_pct", 10.0) / 100.0)
        for rec in to_cancel:
            logger.info("ladder: canceling +%.1f%% sell %s @ %.6f (re-arm)",
                        rec["band_pct"], rec["order_id"], rec["price"])
            await self._api.cancel_order(state.session, self.symbol, rec["order_id"])
            a.open_sells.pop(rec["order_id"], None)
        for d in to_place:
            await self._place_live_sell(state, d, ts_ms)
        if to_cancel or to_place:
            self._save_sells(state.conn)

    async def _place_live_sell(self, state: Any, plan: dict, ts_ms: int) -> bool:
        """Place ONE resting post-only sell. Holdings are NOT debited here — only on fill."""
        a = self.acc
        if not self._adopted or a.halted:
            return False
        pd = decimals_for_price(plan["price"])
        if self.shadow:
            logger.info("SHADOW would place maker SELL %.4f @ %.*f [+%.1f%%] — placing nothing",
                        plan["qty"], pd, plan["price"], plan["band_pct"])
            return False
        # Hard invariant: we can only ever sell coins we actually hold above the floor.
        resting = sum(r["orig_qty"] - r["executed_qty_seen"] for r in a.open_sells.values())
        floor   = a.peak_holdings_btc * a.p.get("min_holdings_pct", 0.0)
        if resting + plan["qty"] > max(0.0, a.holdings_btc - floor) + 1e-9:
            # Throttled: this fires per book tick, and a planner/reality mismatch would
            # otherwise bury the log at ~2s intervals (it did — see the qty-drift bug in
            # diff_sell_ladder). The refusal itself is the invariant doing its job.
            if time.time() - self._oversell_warn_ts > 60:
                self._oversell_warn_ts = time.time()
                logger.error("ladder: REFUSED +%.1f%% — would oversell (resting=%.4f + %.4f > "
                             "sellable=%.4f). Plan disagrees with the book; re-arm should "
                             "resize.", plan["band_pct"], resting, plan["qty"],
                             max(0.0, a.holdings_btc - floor))
            return False
        if time.time() < self._sell_retry_after:
            return False                              # backing off after placement failures
        oid = await self._api.post_order(
            state.session, self.symbol, plan["price"], quantity=plan["qty"],
            side="SELL", order_type="LIMIT_MAKER")
        if not oid or str(oid).startswith("sim_"):
            # Same hazard as the buy path, which already had this: a rejected sell leaves no
            # tracked order, so the next tick retries at book rate against an IP-whitelisted
            # key. Seen live at ~2s intervals on a sub-min-notional band. Sells needed their
            # own breaker — a dust rejection must not also block buying.
            self._sell_fail_streak += 1
            backoff = min(_PLACE_BACKOFF_BASE_S * 2 ** (self._sell_fail_streak - 1),
                          _PLACE_BACKOFF_MAX_S)
            self._sell_retry_after = time.time() + backoff
            logger.error("ladder: SELL +%.1f%% placement failed (oid=%s, qty=%.4f @ %.*f "
                         "= %.2f USDT). Retry #%d backing off %.0fs.",
                         plan["band_pct"], oid, plan["qty"], pd, plan["price"],
                         plan["qty"] * plan["price"], self._sell_fail_streak, backoff)
            return False
        self._sell_fail_streak = 0
        self._sell_retry_after = 0.0
        # LIMIT_MAKER returns an id even when it auto-cancels for crossing — confirm it rests.
        view = await self._api.get_order(state.session, self.symbol, str(oid))
        if view is not None and view.get("status") in ("CANCELED", "EXPIRED", "REJECTED"):
            logger.info("ladder: +%.1f%% sell @ %.*f auto-canceled (post-only would cross) — "
                        "re-planning next tick", plan["band_pct"], pd, plan["price"])
            return False                              # market moved, not an error → no backoff
        a.open_sells[str(oid)] = {
            "order_id": str(oid), "band_pct": plan["band_pct"], "price": plan["price"],
            "orig_qty": plan["qty"], "executed_qty_seen": 0.0, "quote_spent_seen": 0.0,
            "placed_ts": time.time()}
        logger.info("LADDER maker SELL placed id=%s  %.4f @ %.*f  [+%.1f%%]",
                    oid, plan["qty"], pd, plan["price"], plan["band_pct"])
        return True

    async def _check_drift(self, state: Any) -> None:
        """Halt if our books over-claim what the exchange says we own.

        Compares internal holdings against free + LOCKED: a resting sell moves coins from
        free to locked, so a free-only check would 'drift' by exactly the resting quantity
        the moment the ladder goes up. Asymmetric on purpose — only over-claiming is a
        safety problem (it can oversell). Extra coins in the account (a deposit) are
        harmless and must not halt the bot."""
        a = self.acc
        every = a.p.get("drift_check_every_s", 300)
        now   = time.time()
        if now - self._last_drift_ts < every:
            return
        self._last_drift_ts = now
        acct = await self._api.get_account(state.session)
        if not acct:
            return                                    # unknown ≠ mismatch
        base = self.symbol.replace("USDT", "")
        bal  = acct["balances"].get(base)
        if not bal:
            return
        real = bal["free"] + bal["locked"]
        tol  = a.p.get("drift_tolerance", 0.01)
        if a.holdings_btc > real + tol:
            if not a.halted:
                a.halted = True
                logger.error("HALT — books over-claim the exchange: internal=%.6f > real=%.6f "
                             "(free=%.6f + locked=%.6f). No further orders until resolved.",
                             a.holdings_btc, real, bal["free"], bal["locked"])
            return
        # Report the FIRST clean check, and any transition back to clean, so a silent guard
        # can be told apart from a guard that never ran. Steady state stays quiet.
        if self._drift_ok is not True:
            self._drift_ok = True
            logger.info("drift check OK — internal=%.6f  real=%.6f (free=%.6f + locked=%.6f "
                        "in resting orders)  surplus=%+.6f",
                        a.holdings_btc, real, bal["free"], bal["locked"], real - a.holdings_btc)
        elif real - a.holdings_btc > tol:
            # Not dangerous (only over-claiming can oversell) but it means coins arrived that
            # our books never credited — a deposit, or a fill we missed. Worth knowing.
            logger.warning("drift: exchange holds %.6f MORE than our books (internal=%.6f "
                           "real=%.6f) — deposit, or an uncredited fill?",
                           real - a.holdings_btc, a.holdings_btc, real)

    async def _adopt_open_orders(self, state: Any) -> None:
        """Startup, BEFORE any placement: reconcile the persisted resting bid AND adopt the
        persisted sell ladder, then cancel only genuine orphans.

        Resting sells MUST be adopted, not cancelled: they are the ladder, and dropping them
        on every restart would churn the book and miss exactly the wicks they exist to
        catch. An untracked order is still an orphan (crash-after-place) and gets cleaned."""
        oo = await self._api.get_open_orders(state.session, self.symbol)
        if oo is None:
            logger.warning("adopt: get_open_orders error — deferring (no placement until it succeeds)")
            return
        a = self.acc
        live_ids = {o["order_id"] for o in oo}
        tracked_buy = a.pending_buy["order_id"] if a.pending_buy else None
        if tracked_buy and tracked_buy not in live_ids:
            # our tracked bid is gone from the book → it filled/canceled while we were down
            await self._reconcile_live_buy(state, a.last_price or 0.0, int(time.time() * 1000))
        if a.open_sells:
            # credit anything that filled while we were down, and drop what no longer rests
            await self._reconcile_live_sells(state, int(time.time() * 1000))
            adopted = [oid for oid in a.open_sells if oid in live_ids]
            if adopted:
                logger.info("adopt: keeping %d resting ladder sell(s): %s", len(adopted),
                            [(a.open_sells[o]["band_pct"], a.open_sells[o]["price"])
                             for o in adopted])
        known = set(a.open_sells) | ({tracked_buy} if tracked_buy else set())
        for o in oo:
            if o["order_id"] not in known:
                logger.warning("adopt: canceling orphan %s (%s qty=%.2f @ %.6f)",
                               o["order_id"], o.get("side"), o.get("qty", 0), o.get("price", 0))
                await self._api.cancel_order(state.session, self.symbol, o["order_id"])
        self._adopted = True

    async def _buy_paper(self, state: Any, price: float, usdt_amount: float,
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

        pd = decimals_for_price(price)
        logger.info("BUY  %.6f @ %.*f  [%-22s]  avg=%.*f  held=%.6f  free=%7.2f  uPnL=%+.2f%%",
                    qty_btc, pd, price, reason, pd, a.avg_entry,
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

        logger.info("SELL %.6f @ %.*f  [%-22s]  realized=%+7.2f  held=%.6f  free=%7.2f",
                    qty_btc, decimals_for_price(price), price, reason, realized,
                    a.holdings_btc, a.free_usdt)
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
                logger.info("  → rebuy %.6f @ %.*f  (spread=%.4f%% → discount=%.3f%%)",
                            qty, decimals_for_price(rebuy), rebuy, a.spread_ema, discount * 100)

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
            placed = await self._buy(state, price, usdt_needed, f"rebuy+{rb.band_pct:.1f}%", ts_ms)
            if placed and not self.live:
                a.active_bands.discard(rb.band_pct)   # paper fills instantly → done
                filled.append(rb)
            elif placed:
                break   # live: the bid only RESTS. The obligation is discharged in
                        # _credit_fill when it fills; one bid in flight, so stop here.

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

        # Ichimoku daily-cloud trend gate (opt-in). Fail-open: only acts when a fresh
        # cloud bottom is known. Blocks scale-in when price is below the cloud (bearish).
        if p.get("ichimoku_gate", False) and a.ichi_cloud_bottom is not None:
            if (time.time() - a.ichi_ts) <= p.get("ichi_stale_secs", 172800.0):
                if 0 < price < a.ichi_cloud_bottom:
                    logger.debug("Scale-in blocked — price %.0f below Ichimoku cloud (%.0f)",
                                 price, a.ichi_cloud_bottom)
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

        # LIVE: reconcile the resting bid every tick (credit fills, cancel-if-stale). Adopt
        # open orders ONCE before any placement so a crash-orphaned bid can't breach the cap.
        if self.live:
            if not self._adopted:
                await self._adopt_open_orders(state)
            await self._reconcile_live_buy(state, price, ts_ms)
            # Credit sell fills BEFORE any re-arm: cancelling a resting sell without first
            # taking its fill delta would lose real coins.
            await self._reconcile_live_sells(state, ts_ms)
            await self._check_drift(state)

        if not a.initial_done:
            if a.p.get("vwap_gate_initial", False):
                if a.p.get("vwap_gate", True) and a.vwap_dip_score < 0.0:
                    return
            init_usdt = min(a.p["initial_stake_usdt"], a.free_usdt)
            if await self._buy(state, price, init_usdt, "initial", ts_ms):
                a.last_buy_ts = ts_ms
                # PAPER fills instantly, so placing == owning. LIVE only *places* a resting
                # bid: mark the initial done on the real FILL (in _credit_fill), never here.
                # Otherwise a bid that is canceled unfilled (stale) consumes the initial
                # opportunity while holding nothing, and only the dip-gated scale-in path
                # can ever buy again — the bot idles with the full budget. Until it fills,
                # the one-bid guard makes this a no-op, so re-bidding costs nothing.
                if not self.live:
                    a.initial_done = True
            return

        if self.live:
            # RESTING ladder instead of the paper path's reactive band checks: a limit order
            # sitting on the book catches a wick that a polling bot sleeps through, and on a
            # book this thin the wicks are the opportunity. best_ask is derived from mid +
            # half the spread (the tick carries no book side); an approximation is safe
            # because LIMIT_MAKER refuses to cross, so a bad estimate just re-plans.
            half_spread = float(spread_bps or 0.0) / 20_000.0
            await self._rearm_sell_ladder(state, price, price * (1.0 + half_spread), ts_ms)
            await self._check_rebuys(state, price, ts_ms)
        else:
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
        elif sid == p.get("ichimoku_stream_id", "btc_1d_ichimoku"):
            ct, cb = msg.get("ichi_cloud_top"), msg.get("ichi_cloud_bottom")
            if ct is not None and cb is not None:
                a.ichi_cloud_top    = float(ct)
                a.ichi_cloud_bottom = float(cb)
                a.ichi_ts           = time.time()
