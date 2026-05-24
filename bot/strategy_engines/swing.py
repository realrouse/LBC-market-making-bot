"""
Swing trading strategy — limit orders at support/resistance levels.

Differences from GridStrategy
------------------------------
  Grid : evenly-spaced levels, trades both sides mechanically, no directional view.
  Swing: levels placed at meaningful S/R; BUY at support, SELL at next resistance;
         optional trend filter from the shared indicators service (RSI, etc.);
         holds for larger moves (1–5 %) rather than the small oscillations grid exploits.

Algorithm
---------
Init (first tick):
  For each support level below current price, place one LIMIT BUY (up to max_positions).
  Resistance levels are not armed at init — they become TP targets after a fill.

BUY fill at support S (entry_price):
  → Find the lowest resistance level above entry_price (or fallback TP = entry * (1+tp_pct))
  → Place LIMIT SELL at that resistance price
  → Record SL = entry_price * (1 − sl_pct)

SL breach (price drops below sl_price while LONG):
  → Simulation: detected in on_book_update, close at current bid
  → Live: TODO — market SELL order via REST

SELL fill (TP hit):
  → Account PnL
  → Re-arm: place new LIMIT BUY at the original support level for the next cycle

Trend filter (optional):
  → Subscribes to the shared indicators PUB socket (ZMQ)
  → Caches latest RSI(14, 4h); skips new BUY entries when RSI > rsi_buy_max

Configuration keys in strategy JSON
-------------------------------------
    "strategy_type":          "swing"
    "connector":              "binance" | "mexc"
    "symbol":                 "BTCUSDT"

    "support_levels":         [70000, 72500, 75000]   ascending list of S levels
    "resistance_levels":      [80000, 82500, 85000]   ascending list of R levels

    "order_size_usdt":        200.0     notional per position
    "max_positions":          3         max simultaneous open longs
    "sl_pct":                 0.02      stop-loss distance below entry (2 %)
    "tp_pct_fallback":        0.04      TP if no resistance found above entry (4 %)

    "trend_filter_enabled":   true
    "rsi_buy_max":            52        skip new BUY when RSI(4h) > this value
    "rsi_stale_secs":         3600      treat RSI as stale (bypass filter) after N s

    "indicators_addr":        "tcp://127.0.0.1:5559"
    "indicators_stream_id":   "btc_4h"

    "poll_interval":          2.0       seconds between REST fill polls (live mode)
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("live")

STRATEGY_TYPE = "swing"

# ─── STATE DATACLASSES ────────────────────────────────────────────────────────

@dataclass
class SwingLevel:
    """One support or resistance price level."""
    price:      float
    level_type: str   # "support" | "resistance"


@dataclass
class SwingPosition:
    """One open long position (BUY placed or filled, waiting for TP or SL)."""
    level_price:   float              # support level that spawned this position
    buy_order_id:  Optional[str]   = None
    buy_price:     Optional[float] = None   # actual fill price
    sell_order_id: Optional[str]   = None
    sell_price:    Optional[float] = None   # TP target
    sl_price:      Optional[float] = None   # stop-loss trigger
    opened_at:     float           = 0.0
    status:        str             = "buy_placed"  # buy_placed | long | tp_placed | closed
    db_id:         Optional[int]   = None


@dataclass
class SwingState:
    """Runtime state for one active swing strategy instance."""
    symbol:            str
    support:           list[SwingLevel]
    resistance:        list[SwingLevel]
    order_size_usdt:   float
    max_positions:     int
    sl_pct:            float
    tp_pct_fallback:   float
    positions:         list[SwingPosition] = field(default_factory=list)
    total_pnl:         float               = 0.0
    total_trades:      int                 = 0
    last_price:        float               = 0.0
    initialised:       bool                = False
    halted:            bool                = False
    # Indicators from shared service (RSI, EMA50/200, ATR)
    last_rsi:          Optional[float]     = None
    last_rsi_ts:       float               = 0.0
    last_ema50:        Optional[float]     = None
    last_ema200:       Optional[float]     = None
    last_atr:          Optional[float]     = None
    last_ind_ts:       float               = 0.0
    # REST poll throttle
    last_poll_ts:      float               = 0.0


# ─── STRATEGY ─────────────────────────────────────────────────────────────────

class SwingStrategy:
    """
    Swing trading strategy: limit BUYs at support, limit SELLs at resistance.

    Instantiation validates config and builds the level list.
    Call on_book_update() on every price tick from the bot's WebSocket feed.
    """

    STRATEGY_TYPE = STRATEGY_TYPE

    def __init__(self, config: Any) -> None:
        cfg     = getattr(config, "strategy_cfg", {})
        symbol  = str(cfg.get("symbol", "BTCUSDT"))
        sup_raw = list(cfg.get("support_levels",    []))
        res_raw = list(cfg.get("resistance_levels", []))

        if not sup_raw:
            raise ValueError("SwingStrategy: 'support_levels' must contain at least one level")
        if not res_raw:
            raise ValueError("SwingStrategy: 'resistance_levels' must contain at least one level")

        size    = float(cfg.get("order_size_usdt",  200.0))
        max_pos = int(cfg.get("max_positions",         3))
        sl_pct  = float(cfg.get("sl_pct",             0.02))
        tp_fall = float(cfg.get("tp_pct_fallback",    0.04))

        if size <= 0:
            raise ValueError(f"order_size_usdt must be > 0, got {size}")
        if max_pos < 1:
            raise ValueError(f"max_positions must be >= 1, got {max_pos}")
        if not 0 < sl_pct < 1:
            raise ValueError(f"sl_pct must be in (0, 1), got {sl_pct}")

        support    = [SwingLevel(float(p), "support")    for p in sorted(sup_raw)]
        resistance = [SwingLevel(float(p), "resistance") for p in sorted(res_raw)]

        self.sw = SwingState(
            symbol=symbol,
            support=support,
            resistance=resistance,
            order_size_usdt=size,
            max_positions=max_pos,
            sl_pct=sl_pct,
            tp_pct_fallback=tp_fall,
        )

        from connectors import load as _load_conn
        self._api  = _load_conn(config.connector)

        self._trend_filter    = bool(cfg.get("trend_filter_enabled",  True))
        self._rsi_buy_max     = float(cfg.get("rsi_buy_max",          52.0))
        self._rsi_stale_secs  = float(cfg.get("rsi_stale_secs",     3600.0))
        self._ema200_filter   = bool(cfg.get("ema200_filter_enabled", True))
        self._atr_sl_mult     = float(cfg.get("atr_sl_multiplier",    1.5))
        self._indicators_addr = str(cfg.get("indicators_addr",
                                    getattr(config, "indicators_addr", "tcp://127.0.0.1:5559")))
        self._indicators_sid  = str(cfg.get("indicators_stream_id",   "btc_4h"))
        self._poll_interval   = float(cfg.get("poll_interval",          2.0))

        self._ind_task: Optional[asyncio.Task] = None

        logger.info(
            "SwingStrategy [%s]: %d support + %d resistance levels | "
            "size=$%.0f  max=%d  SL_fallback=%.1f%%  TP_fallback=%.1f%%  "
            "RSI_filter=%s  EMA200_filter=%s  ATR_SL_mult=%.1f",
            symbol,
            len(support), len(resistance),
            size, max_pos, sl_pct * 100, tp_fall * 100,
            "RSI≤%.0f" % self._rsi_buy_max if self._trend_filter else "off",
            "on" if self._ema200_filter else "off",
            self._atr_sl_mult,
        )
        for lvl in support:
            logger.info("  support    $%.2f", lvl.price)
        for lvl in resistance:
            logger.info("  resistance $%.2f", lvl.price)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _open_positions(self) -> list[SwingPosition]:
        """Positions not yet closed."""
        return [p for p in self.sw.positions if p.status != "closed"]

    def _armed_levels(self) -> set[float]:
        """Support prices that already have a live BUY or open long."""
        return {p.level_price for p in self._open_positions()}

    def _find_tp(self, entry_price: float) -> float:
        """Return the lowest resistance level strictly above entry, else fallback TP."""
        above = [r.price for r in self.sw.resistance if r.price > entry_price]
        if above:
            return min(above)
        return round(entry_price * (1.0 + self.sw.tp_pct_fallback), 2)

    def _trend_ok(self, price: float = 0.0) -> bool:
        """True when all trend filters permit a new BUY entry.

        EMA200: price must be above EMA200 (bull structure).
        RSI:    RSI(14, 4h) must be <= rsi_buy_max (not overbought).
        Stale indicators bypass the filter to avoid blocking entries indefinitely.
        """
        now = time.time()
        age = now - self.sw.last_ind_ts

        # EMA200 directional filter — skip BUY when price < EMA200
        if self._ema200_filter and self.sw.last_ema200 is not None:
            if age <= self._rsi_stale_secs and price > 0 and price < self.sw.last_ema200:
                return False

        # RSI overbought filter
        if self._trend_filter and self.sw.last_rsi is not None:
            if age <= self._rsi_stale_secs and self.sw.last_rsi > self._rsi_buy_max:
                return False

        return True

    # ── Indicators subscription ────────────────────────────────────────────────

    def _ensure_indicators_task(self) -> None:
        """Start the ZMQ SUB background task on first call (lazy init)."""
        if not self._trend_filter:
            return
        if self._ind_task is None or self._ind_task.done():
            self._ind_task = asyncio.create_task(
                self._indicators_loop(), name="swing_indicators_sub"
            )

    async def _indicators_loop(self) -> None:
        """Subscribe to the shared indicators PUB socket; update last_rsi."""
        try:
            import zmq
            import zmq.asyncio as azmq
        except ImportError:
            logger.warning("SwingStrategy: pyzmq not installed — trend filter disabled")
            self._trend_filter = False
            return

        ctx = azmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.connect(self._indicators_addr)
        logger.info("SwingStrategy [%s] indicators SUB → %s",
                    self.sw.symbol, self._indicators_addr)
        try:
            while True:
                try:
                    msg = await sub.recv_json()
                except Exception as exc:
                    logger.warning("SwingStrategy indicators recv error: %s", exc)
                    await asyncio.sleep(5)
                    continue
                if msg.get("stream_id") != self._indicators_sid:
                    continue
                now = time.time()
                changed = False
                rsi_key = next((k for k in msg if k.startswith("rsi_")), None)
                if rsi_key and msg[rsi_key] is not None:
                    self.sw.last_rsi = float(msg[rsi_key])
                    changed = True
                if msg.get("ema_50") is not None:
                    self.sw.last_ema50 = float(msg["ema_50"])
                    changed = True
                if msg.get("ema_200") is not None:
                    self.sw.last_ema200 = float(msg["ema_200"])
                    changed = True
                if msg.get("atr_14") is not None:
                    self.sw.last_atr = float(msg["atr_14"])
                    changed = True
                if changed:
                    self.sw.last_ind_ts = now
                    self.sw.last_rsi_ts = now
                    logger.debug(
                        "SwingStrategy indicators: RSI=%.1f EMA50=%.0f EMA200=%.0f ATR=%.0f",
                        self.sw.last_rsi   or 0,
                        self.sw.last_ema50  or 0,
                        self.sw.last_ema200 or 0,
                        self.sw.last_atr    or 0,
                    )
        finally:
            sub.close()

    # ── DB persistence ─────────────────────────────────────────────────────────

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        """Create swing tables if they do not exist."""
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS swing_state (
                symbol          TEXT PRIMARY KEY,
                total_pnl       REAL    DEFAULT 0,
                total_trades    INTEGER DEFAULT 0,
                initialised     INTEGER DEFAULT 0,
                updated_at      REAL
            );
            CREATE TABLE IF NOT EXISTS swing_orders (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                level_price     REAL    NOT NULL,
                buy_order_id    TEXT,
                buy_price       REAL,
                sell_order_id   TEXT,
                sell_price      REAL,
                sl_price        REAL,
                status          TEXT    NOT NULL DEFAULT 'buy_placed',
                pnl_net         REAL,
                opened_at       REAL,
                closed_at       REAL
            );
        """)
        conn.commit()

    def _save_state(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO swing_state (symbol, total_pnl, total_trades, initialised, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                total_pnl    = excluded.total_pnl,
                total_trades = excluded.total_trades,
                initialised  = excluded.initialised,
                updated_at   = excluded.updated_at
            """,
            (self.sw.symbol, self.sw.total_pnl,
             self.sw.total_trades, int(self.sw.initialised), time.time()),
        )
        for pos in self.sw.positions:
            if pos.db_id is None:
                cur = conn.execute(
                    """
                    INSERT INTO swing_orders
                        (symbol, level_price, buy_order_id, buy_price,
                         sell_order_id, sell_price, sl_price,
                         status, opened_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (self.sw.symbol, pos.level_price, pos.buy_order_id,
                     pos.buy_price, pos.sell_order_id, pos.sell_price,
                     pos.sl_price, pos.status, pos.opened_at),
                )
                pos.db_id = cur.lastrowid
            else:
                conn.execute(
                    """
                    UPDATE swing_orders SET
                        buy_order_id=?, buy_price=?,
                        sell_order_id=?, sell_price=?, sl_price=?,
                        status=?, closed_at=?
                    WHERE id=?
                    """,
                    (pos.buy_order_id, pos.buy_price,
                     pos.sell_order_id, pos.sell_price, pos.sl_price,
                     pos.status,
                     time.time() if pos.status == "closed" else None,
                     pos.db_id),
                )
        conn.commit()

    # ── DB restore ─────────────────────────────────────────────────────────────

    async def restore_from_db(self, state: Any) -> bool:
        """Reload saved state on restart; reconcile open orders with the exchange.

        Returns True when state was restored, False on first start (no saved row).
        """
        conn = state.conn
        self.ensure_schema(conn)

        row = conn.execute(
            "SELECT total_pnl, total_trades, initialised FROM swing_state WHERE symbol = ?",
            (self.sw.symbol,),
        ).fetchone()

        if row is None:
            logger.info("SwingStrategy [%s] — no saved state, normal initialization",
                        self.sw.symbol)
            return False

        self.sw.total_pnl    = row[0]
        self.sw.total_trades = row[1]
        self.sw.initialised  = bool(row[2])

        order_rows = conn.execute(
            """
            SELECT id, level_price, buy_order_id, buy_price,
                   sell_order_id, sell_price, sl_price, status
            FROM swing_orders
            WHERE symbol = ? AND status != 'closed'
            ORDER BY id
            """,
            (self.sw.symbol,),
        ).fetchall()

        for r in order_rows:
            pos = SwingPosition(
                db_id        = r[0],
                level_price  = r[1],
                buy_order_id = r[2],
                buy_price    = r[3],
                sell_order_id= r[4],
                sell_price   = r[5],
                sl_price     = r[6],
                status       = r[7],
            )
            self.sw.positions.append(pos)

        logger.info(
            "SwingStrategy [%s] — restored: %d open positions | "
            "total_trades=%d total_pnl=$%+.2f",
            self.sw.symbol, len(self.sw.positions),
            self.sw.total_trades, self.sw.total_pnl,
        )

        if not self.sw.initialised or not self.sw.positions:
            return True

        # Reconcile: orders that filled while the bot was offline.
        logger.info("SwingStrategy [%s] — reconciling with exchange...", self.sw.symbol)
        try:
            open_orders = await self._api.get_open_orders(state.session, self.sw.symbol)
            open_ids    = {str(o["order_id"]) for o in (open_orders or [])}
        except Exception as exc:
            logger.warning("SwingStrategy reconcile: get_open_orders failed: %s — skipping", exc)
            return True

        for pos in list(self.sw.positions):
            if pos.status == "buy_placed" and pos.buy_order_id:
                if str(pos.buy_order_id) not in open_ids:
                    logger.info("SwingStrategy [%s] reconcile: BUY %s filled offline",
                                self.sw.symbol, pos.buy_order_id)
                    await self._on_buy_filled(state, pos, pos.buy_price or pos.level_price)
            elif pos.status == "tp_placed" and pos.sell_order_id:
                if str(pos.sell_order_id) not in open_ids:
                    logger.info("SwingStrategy [%s] reconcile: TP SELL %s filled offline",
                                self.sw.symbol, pos.sell_order_id)
                    await self._on_sell_filled(state, pos, pos.sell_price or self.sw.last_price)

        self._save_state(conn)
        return True

    # ── Initialisation ─────────────────────────────────────────────────────────

    async def _initialise(self, state: Any, price: float) -> None:
        """Place LIMIT BUY at every support level below current price (up to max_positions)."""
        armed = self._armed_levels()
        slots = self.sw.max_positions - len(self._open_positions())

        placed = 0
        for lvl in reversed(self.sw.support):   # nearest first (highest support)
            if lvl.price >= price:
                continue
            if lvl.price in armed:
                continue
            if slots <= 0:
                break
            if not self._trend_ok(price):
                logger.info(
                    "SwingStrategy [%s] trend filter: skip BUY at %.2f "
                    "(RSI=%.1f EMA50=%.0f EMA200=%.0f price=%.0f)",
                    self.sw.symbol, lvl.price,
                    self.sw.last_rsi    or 0,
                    self.sw.last_ema50  or 0,
                    self.sw.last_ema200 or 0,
                    price,
                )
                continue

            oid = await self._api.post_order(
                state.session, self.sw.symbol,
                lvl.price, self.sw.order_size_usdt, side="BUY",
            )
            if oid:
                pos = SwingPosition(
                    level_price=lvl.price,
                    buy_order_id=oid,
                    buy_price=lvl.price,
                    status="buy_placed",
                    opened_at=time.time(),
                )
                self.sw.positions.append(pos)
                armed.add(lvl.price)
                slots -= 1
                placed += 1
                logger.info("SwingStrategy [%s] BUY %.2f → order %s",
                            self.sw.symbol, lvl.price, oid)

        self.sw.initialised = True
        logger.info("SwingStrategy [%s] init: price=%.2f | %d BUY orders placed",
                    self.sw.symbol, price, placed)

    # ── Fill handlers ──────────────────────────────────────────────────────────

    def _compute_sl(self, entry_price: float) -> float:
        """ATR-based SL when available, static percentage as fallback."""
        if self.sw.last_atr is not None and self.sw.last_atr > 0:
            sl = round(entry_price - self.sw.last_atr * self._atr_sl_mult, 2)
            logger.debug("SwingStrategy SL via ATR: entry=%.2f ATR=%.2f mult=%.1f → SL=%.2f",
                         entry_price, self.sw.last_atr, self._atr_sl_mult, sl)
            return sl
        return round(entry_price * (1.0 - self.sw.sl_pct), 2)

    async def _on_buy_filled(self, state: Any, pos: SwingPosition,
                              fill_price: float) -> None:
        """BUY filled → place TP SELL at next resistance, set ATR-based SL."""
        pos.buy_price    = fill_price
        pos.sl_price     = self._compute_sl(fill_price)
        pos.sell_price   = self._find_tp(fill_price)
        pos.buy_order_id = None
        pos.status       = "long"

        oid = await self._api.post_order(
            state.session, self.sw.symbol,
            pos.sell_price, self.sw.order_size_usdt, side="SELL",
        )
        if oid:
            pos.sell_order_id = oid
            pos.status        = "tp_placed"
            logger.info(
                "SwingStrategy [%s] BUY fill %.2f → TP SELL %.2f  SL %.2f  [%s]",
                self.sw.symbol, fill_price, pos.sell_price, pos.sl_price, oid,
            )
        else:
            logger.error("SwingStrategy [%s] BUY fill %.2f → post_order SELL failed",
                         self.sw.symbol, fill_price)

    async def _on_sell_filled(self, state: Any, pos: SwingPosition,
                               fill_price: float) -> None:
        """TP SELL filled → account PnL, re-arm the support level."""
        entry = pos.buy_price or pos.level_price
        qty   = self.sw.order_size_usdt / entry
        fee_b = self._api.compute_fee(entry,      qty)
        fee_s = self._api.compute_fee(fill_price, qty)
        pnl   = (fill_price - entry) * qty - fee_b - fee_s

        self.sw.total_pnl    += pnl
        self.sw.total_trades += 1

        logger.info(
            "SwingStrategy [%s] TP fill %.2f | entry=%.2f | pnl=$%+.4f | "
            "total_trades=%d total_pnl=$%+.2f",
            self.sw.symbol, fill_price, entry, pnl,
            self.sw.total_trades, self.sw.total_pnl,
        )

        pos.sell_order_id = None
        pos.status        = "closed"

        # Re-arm: place a new BUY at the same support level for the next cycle
        if len(self._open_positions()) < self.sw.max_positions and self._trend_ok(self.sw.last_price):
            new_oid = await self._api.post_order(
                state.session, self.sw.symbol,
                pos.level_price, self.sw.order_size_usdt, side="BUY",
            )
            if new_oid:
                new_pos = SwingPosition(
                    level_price=pos.level_price,
                    buy_order_id=new_oid,
                    buy_price=pos.level_price,
                    status="buy_placed",
                    opened_at=time.time(),
                )
                self.sw.positions.append(new_pos)
                logger.info("SwingStrategy [%s] re-armed support %.2f → BUY %s",
                            self.sw.symbol, pos.level_price, new_oid)

    async def _close_sl(self, state: Any, pos: SwingPosition, price: float) -> None:
        """Stop-loss triggered: cancel TP order and close at current price."""
        entry = pos.buy_price or pos.level_price
        qty   = self.sw.order_size_usdt / entry
        pnl   = (price - entry) * qty   # approximate, fees omitted on emergency close

        self.sw.total_pnl    += pnl
        self.sw.total_trades += 1

        if pos.sell_order_id:
            await self._api.cancel_order(state.session, self.sw.symbol, pos.sell_order_id)

        pos.status = "closed"

        logger.warning(
            "SwingStrategy [%s] SL hit @ %.2f | entry=%.2f | pnl≈$%+.4f | "
            "total_pnl=$%+.2f",
            self.sw.symbol, price, entry, pnl, self.sw.total_pnl,
        )

    # ── Fill detection ─────────────────────────────────────────────────────────

    async def _poll_fills(self, state: Any, ts: Any) -> None:
        """
        Detect filled orders.

        Simulation path (sim_ IDs): price-crossing check, no REST.
        Live path: GET /openOrders, absent IDs = filled.
        """
        open_pos = self._open_positions()
        if not open_pos:
            return

        is_sim = any(
            (p.buy_order_id  or "").startswith("sim_") or
            (p.sell_order_id or "").startswith("sim_")
            for p in open_pos
        )

        if is_sim:
            for pos in list(open_pos):
                if pos.status == "buy_placed" and pos.buy_price is not None:
                    if ts.best_ask <= pos.buy_price:
                        await self._on_buy_filled(state, pos, pos.buy_price)
                elif pos.status == "tp_placed" and pos.sell_price is not None:
                    if ts.best_bid >= pos.sell_price:
                        await self._on_sell_filled(state, pos, pos.sell_price)
        else:
            open_orders = await self._api.get_open_orders(state.session, self.sw.symbol)
            open_ids    = {str(o["order_id"]) for o in (open_orders or [])}
            for pos in list(open_pos):
                if pos.status == "buy_placed" and pos.buy_order_id:
                    if str(pos.buy_order_id) not in open_ids:
                        fill_px = pos.buy_price or pos.level_price
                        await self._on_buy_filled(state, pos, fill_px)
                elif pos.status == "tp_placed" and pos.sell_order_id:
                    if str(pos.sell_order_id) not in open_ids:
                        fill_px = pos.sell_price or self.sw.last_price
                        await self._on_sell_filled(state, pos, fill_px)

    # ── Main entry point ───────────────────────────────────────────────────────

    async def on_book_update(
        self,
        state: Any,
        ts: Any,
        _t_ws: Optional[float] = None,
    ) -> None:
        """Called for every order-book update from the WebSocket feed."""
        price = ts.best_bid
        self.sw.last_price = price

        if self.sw.halted:
            return

        self._ensure_indicators_task()

        if not self.sw.initialised:
            self.ensure_schema(state.conn)
            await self._initialise(state, price)
            self._save_state(state.conn)
            return

        # Stop-loss check — before fill poll so we cancel TP orders first
        changed = False
        for pos in self._open_positions():
            if pos.status in ("long", "tp_placed") and pos.sl_price is not None:
                if price <= pos.sl_price:
                    await self._close_sl(state, pos, price)
                    changed = True

        # Fill detection (throttled for live REST calls)
        now = time.time()
        if now - self.sw.last_poll_ts >= self._poll_interval:
            self.sw.last_poll_ts = now
            prev_trades = self.sw.total_trades
            await self._poll_fills(state, ts)
            if self.sw.total_trades != prev_trades:
                changed = True

        if changed:
            self._save_state(state.conn)
