"""
Grid trading strategy for continuous CEX spot markets (Binance, MEXC).

Algorithm
---------
The grid covers [grid_lower, grid_upper] divided into `grid_levels` evenly-
spaced price points.  grid_step = (upper - lower) / (levels - 1).

Initialisation (first tick):
  Levels below current ask price → LIMIT BUY at level.price
  Levels above current ask price → LIMIT SELL at level.price

BUY fill at level L (buy_price = L.price):
  → Place LIMIT SELL at L.price + grid_step
  → Store sell_price on the same GridLevel object

SELL fill at level L (sell_price = L.sell_price):
  → If buy_price is known: profit = (sell_price - buy_price) × qty − fees
  → Place LIMIT BUY at sell_price − grid_step
  → Update GridLevel for next cycle

Stop-loss (price exits [grid_lower, grid_upper]):
  → Cancel all open orders via REST
  → Set grid.halted = True; on_book_update becomes a no-op

Fill detection
  Simulated orders (order_id starts with "sim_"):
    BUY  fill detected when best_ask <= buy_price
    SELL fill detected when best_bid >= sell_price
  Real orders:
    Single call to get_open_orders(symbol) per poll cycle.
    Orders not present in the response are assumed FILLED.
    Throttled to one call per poll_interval seconds (default 2 s).

Configuration keys in strategy JSON
-------------------------------------
    "strategy_type":         "grid"
    "connector":             "binance" | "mexc"
    "grid_symbol":           "BTCUSDT"
    "grid_lower":            90000.0
    "grid_upper":            110000.0
    "grid_levels":           20           (>= 2)
    "grid_order_size_usdt":  50.0         (> 0)
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("live")

STRATEGY_TYPE = "grid"


# ─── STATE DATACLASSES ────────────────────────────────────────────────────────

@dataclass
class GridLevel:
    """One price slot in the grid.

    `price` is the reference level (= buy price at init or after a SELL cycle).
    `buy_price` and `sell_price` track the actual prices of placed orders,
    which may differ from `price` when a counter-order shifts the slot.
    """
    price:          float
    buy_order_id:   Optional[str]   = None
    sell_order_id:  Optional[str]   = None
    buy_price:      Optional[float] = None   # actual price of the active BUY order
    sell_price:     Optional[float] = None   # actual price of the active SELL order
    # idle | buy_placed | sell_placed
    status:         str             = "idle"
    filled_at_ts:   Optional[float] = None

    @property
    def is_active(self) -> bool:
        """True when a BUY or SELL order is outstanding (not idle)."""
        return self.status != "idle"


@dataclass
class GridState:
    """Runtime state for one active grid."""
    symbol:           str
    grid_lower:       float
    grid_upper:       float
    grid_step:        float
    order_size_usdt:  float
    levels:           list[GridLevel] = field(default_factory=list)
    total_cycles:     int             = 0
    total_profit_usd: float           = 0.0
    last_price:       float           = 0.0
    initialised:      bool            = False
    halted:           bool            = False
    poll_interval:    float           = 2.0   # seconds between REST fill polls
    last_poll_ts:     float           = 0.0


# ─── STRATEGY ─────────────────────────────────────────────────────────────────

class GridStrategy:
    """
    Grid trading strategy.

    Instantiation validates config and computes price levels.
    Call on_book_update() on every price tick from the WebSocket feed.
    """

    STRATEGY_TYPE = STRATEGY_TYPE

    def __init__(self, config: Any) -> None:
        lower  = float(config.grid_lower)
        upper  = float(config.grid_upper)
        n      = int(config.grid_levels)
        size   = float(config.grid_order_size_usdt)
        symbol = str(config.grid_symbol)

        if lower <= 0 or upper <= lower:
            raise ValueError(
                f"Grid bounds invalid: lower={lower}, upper={upper}. "
                "Required: 0 < lower < upper."
            )
        if n < 2:
            raise ValueError(f"grid_levels must be >= 2, got {n}")
        if size <= 0:
            raise ValueError(f"grid_order_size_usdt must be > 0, got {size}")

        step   = (upper - lower) / (n - 1)
        levels = [GridLevel(price=round(lower + i * step, 2)) for i in range(n)]

        self.grid = GridState(
            symbol=symbol,
            grid_lower=lower,
            grid_upper=upper,
            grid_step=step,
            order_size_usdt=size,
            levels=levels,
        )

        from connectors import load as _load_conn
        self._api = _load_conn(config.connector)
        self._trail_mode: str = str(getattr(config, "grid_trail_mode", "static"))

        # User data stream state (real-time fills via WebSocket).
        self._user_stream_task: Optional[asyncio.Task] = None
        self._user_ws_connected: bool = False
        self._no_credentials: bool = False  # set True once; stops task re-spawn when no API key

        logger.info(
            "GridStrategy: %s  %.2f–%.2f  %d levels  step=%.2f  size=$%.2f  trail=%s",
            symbol, lower, upper, n, step, size, self._trail_mode,
        )

    # ── Public helpers ─────────────────────────────────────────────────────────

    @property
    def levels(self) -> list[GridLevel]:
        """Convenience accessor for the active grid's level list."""
        return self.grid.levels

    def level_at_price(self, price: float, tolerance: float = 0.5) -> Optional[GridLevel]:
        """Return the GridLevel whose reference price is within `tolerance` of `price`."""
        for lvl in self.grid.levels:
            if abs(lvl.price - price) <= tolerance:
                return lvl
        return None

    # ── Persistence ────────────────────────────────────────────────────────────

    def _save_state(self, conn: sqlite3.Connection) -> None:
        """Upsert grid metadata and all level states to the DB."""
        now = time.time()
        conn.execute(
            """
            INSERT INTO grid_state
                (symbol, grid_lower, grid_upper, grid_step, order_size_usdt,
                 total_cycles, total_profit_usd, initialised, halted, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                total_cycles     = excluded.total_cycles,
                total_profit_usd = excluded.total_profit_usd,
                initialised      = excluded.initialised,
                halted           = excluded.halted,
                updated_at       = excluded.updated_at
            """,
            (
                self.grid.symbol,
                self.grid.grid_lower, self.grid.grid_upper,
                self.grid.grid_step,  self.grid.order_size_usdt,
                self.grid.total_cycles, self.grid.total_profit_usd,
                int(self.grid.initialised), int(self.grid.halted), now,
            ),
        )
        for lvl in self.grid.levels:
            conn.execute(
                """
                INSERT INTO grid_levels
                    (symbol, level_price, buy_order_id, sell_order_id,
                     buy_price, sell_price, status, filled_at_ts, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, level_price) DO UPDATE SET
                    buy_order_id  = excluded.buy_order_id,
                    sell_order_id = excluded.sell_order_id,
                    buy_price     = excluded.buy_price,
                    sell_price    = excluded.sell_price,
                    status        = excluded.status,
                    filled_at_ts  = excluded.filled_at_ts,
                    updated_at    = excluded.updated_at
                """,
                (
                    self.grid.symbol, lvl.price,
                    lvl.buy_order_id, lvl.sell_order_id,
                    lvl.buy_price,    lvl.sell_price,
                    lvl.status,       lvl.filled_at_ts, now,
                ),
            )
        conn.commit()

    async def restore_from_db(self, state: Any) -> bool:
        """Load saved grid state from DB and reconcile fills with the exchange.

        Returns True when state was restored, False when no saved state exists
        or the grid config has changed (different bounds / step / order size).
        Reconciliation detects orders that filled while the bot was offline and
        immediately places the appropriate counter-orders.
        """
        conn = state.conn
        row = conn.execute(
            """
            SELECT grid_lower, grid_upper, grid_step, order_size_usdt,
                   total_cycles, total_profit_usd, initialised, halted
            FROM grid_state WHERE symbol = ?
            """,
            (self.grid.symbol,),
        ).fetchone()

        if row is None:
            logger.info(
                "GridStrategy [%s] — no saved state, normal initialization",
                self.grid.symbol,
            )
            return False

        (saved_lower, saved_upper, saved_step, saved_size,
         total_cycles, total_profit, initialised, halted) = row

        tol = 0.01
        bounds_changed = (
            abs(saved_lower - self.grid.grid_lower) > tol or
            abs(saved_upper - self.grid.grid_upper) > tol or
            abs(saved_step  - self.grid.grid_step)  > tol
        )
        size_changed = abs(saved_size - self.grid.order_size_usdt) > tol

        if size_changed:
            logger.warning(
                "GridStrategy [%s] — order_size changed, "
                "saved state discarded and grid re-initialized",
                self.grid.symbol,
            )
            return False

        if bounds_changed and self._trail_mode == "static":
            logger.warning(
                "GridStrategy [%s] — bounds/step changed, "
                "saved state discarded and grid re-initialized",
                self.grid.symbol,
            )
            return False

        if bounds_changed:
            # Trail mode: saved bounds reflect a previous re-center; restore them.
            n = len(self.grid.levels)
            self.grid.grid_lower = saved_lower
            self.grid.grid_upper = saved_upper
            self.grid.grid_step  = saved_step
            self.grid.levels     = [
                GridLevel(price=round(saved_lower + i * saved_step, 2)) for i in range(n)
            ]
            logger.info(
                "GridStrategy [%s] — trail re-center detected: restoring saved bounds "
                "[%.2f, %.2f]",
                self.grid.symbol, saved_lower, saved_upper,
            )

        self.grid.total_cycles     = total_cycles
        self.grid.total_profit_usd = total_profit
        self.grid.initialised      = bool(initialised)
        self.grid.halted           = bool(halted)

        level_rows = conn.execute(
            """
            SELECT level_price, buy_order_id, sell_order_id,
                   buy_price, sell_price, status, filled_at_ts
            FROM grid_levels WHERE symbol = ?
            ORDER BY level_price
            """,
            (self.grid.symbol,),
        ).fetchall()

        saved = {round(r[0], 2): r for r in level_rows}
        for lvl in self.grid.levels:
            r = saved.get(lvl.price)
            if r is None:
                continue
            (_, lvl.buy_order_id, lvl.sell_order_id,
             lvl.buy_price, lvl.sell_price,
             lvl.status, lvl.filled_at_ts) = r

        if self.grid.halted:
            logger.warning(
                "GridStrategy [%s] — HALTED state restored from DB | "
                "total PnL=$%+.2f",
                self.grid.symbol, self.grid.total_profit_usd,
            )
            return True

        if not self.grid.initialised:
            logger.info(
                "GridStrategy [%s] — state restored (not initialized), "
                "will initialize on next tick",
                self.grid.symbol,
            )
            return True

        # Reconcile: detect fills that occurred while the bot was offline.
        logger.info(
            "GridStrategy [%s] — reconciling with exchange...",
            self.grid.symbol,
        )
        open_orders = await self._api.get_open_orders(
            state.session, self.grid.symbol)
        open_ids = {str(o["order_id"]) for o in (open_orders or [])}

        filled = 0
        for lvl in self.grid.levels:
            if lvl.status == "buy_placed" and lvl.buy_order_id:
                if str(lvl.buy_order_id) not in open_ids:
                    await self._on_buy_filled(state, lvl)
                    filled += 1
            elif lvl.status == "sell_placed" and lvl.sell_order_id:
                if str(lvl.sell_order_id) not in open_ids:
                    await self._on_sell_filled(state, lvl)
                    filled += 1

        self._save_state(conn)
        logger.info(
            "GridStrategy [%s] — restored: %d active levels | "
            "%d missed fills | cycles=%d | total PnL=$%+.2f",
            self.grid.symbol,
            sum(1 for l in self.grid.levels if l.is_active),
            filled, self.grid.total_cycles, self.grid.total_profit_usd,
        )
        return True

    # ── User data stream ───────────────────────────────────────────────────────

    async def _user_stream_loop(self, state: Any) -> None:
        """
        Background task: subscribe to the exchange user data stream for real-time
        fill notifications.

        Lifecycle:
          1. Call get_listen_key() to obtain a time-limited key (60-min TTL).
          2. Open a WebSocket to make_user_stream_url(listen_key).
          3. Parse every frame with parse_user_stream_msg(); dispatch fills to
             _on_user_stream_fill().
          4. Renew the listenKey every KEEPALIVE_SECS (30 min) to prevent expiry.
          5. On disconnect, reconnect with exponential back-off (5 s → 60 s cap).
          6. Exit when the grid is halted or after MAX_KEY_FAILURES consecutive
             failures to obtain a listenKey (no credentials / API unreachable).

        While the stream is active (_user_ws_connected = True), on_book_update
        skips the REST poll — the stream already provides sub-second fill events.
        """
        KEEPALIVE_SECS  = 1800    # renew listenKey every 30 min (TTL = 60 min)
        MAX_KEY_FAILURES = 3
        backoff = 5.0
        key_failures = 0

        while not self.grid.halted:
            listen_key = await self._api.get_listen_key(state.session)
            if not listen_key:
                key_failures += 1
                if key_failures >= MAX_KEY_FAILURES:
                    logger.warning(
                        "GridStrategy [%s] user stream: giving up after %d consecutive "
                        "failures (no credentials?)",
                        self.grid.symbol, key_failures,
                    )
                    self._no_credentials = True
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue

            key_failures = 0
            backoff = 5.0
            ws_url = self._api.make_user_stream_url(listen_key)

            try:
                keepalive_ts = time.time()
                async with state.session.ws_connect(ws_url, heartbeat=20) as ws:
                    self._user_ws_connected = True
                    logger.info(
                        "GridStrategy [%s] user stream connected (real-time fills)",
                        self.grid.symbol,
                    )
                    async for msg in ws:
                        if self.grid.halted:
                            break
                        if time.time() - keepalive_ts >= KEEPALIVE_SECS:
                            await self._api.keepalive_listen_key(
                                state.session, listen_key)
                            keepalive_ts = time.time()
                        try:
                            data = json.loads(msg.data)
                        except Exception:
                            continue
                        fill = self._api.parse_user_stream_msg(data)
                        if fill:
                            await self._on_user_stream_fill(state, fill)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(
                    "GridStrategy [%s] user stream disconnected: %s",
                    self.grid.symbol, e,
                )
            finally:
                self._user_ws_connected = False

            if not self.grid.halted:
                logger.info(
                    "GridStrategy [%s] user stream: reconnecting in %.0fs",
                    self.grid.symbol, backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def _on_user_stream_fill(self, state: Any, fill: dict) -> None:
        """
        Dispatch a fill event received from the user data stream.

        Matches the order_id to the active grid level and calls the appropriate
        fill handler.  Saves state to DB after the counter-order is placed.
        Unknown order IDs (orders placed outside the grid) are silently ignored.
        """
        order_id = fill.get("order_id", "")
        if not order_id:
            return
        for lvl in self.grid.levels:
            if lvl.status == "buy_placed" and str(lvl.buy_order_id) == order_id:
                logger.debug(
                    "GridStrategy [%s] user stream BUY fill %s",
                    self.grid.symbol, order_id,
                )
                await self._on_buy_filled(state, lvl)
                self._save_state(state.conn)
                return
            if lvl.status == "sell_placed" and str(lvl.sell_order_id) == order_id:
                logger.debug(
                    "GridStrategy [%s] user stream SELL fill %s",
                    self.grid.symbol, order_id,
                )
                await self._on_sell_filled(state, lvl)
                self._save_state(state.conn)
                return
        logger.debug(
            "GridStrategy [%s] user stream: unknown order %s (outside grid)",
            self.grid.symbol, order_id,
        )

    # ── Stop-loss ──────────────────────────────────────────────────────────────

    def _check_stop_loss(self, price: float) -> bool:
        """True if price exited the bounds that trigger a stop for the current trail_mode."""
        if self._trail_mode == "bull":
            return price < self.grid.grid_lower
        if self._trail_mode == "bear":
            return price > self.grid.grid_upper
        return price < self.grid.grid_lower or price > self.grid.grid_upper

    async def _cancel_all_orders(self, state: Any) -> None:
        """Cancel every open order on the exchange and mark the grid halted."""
        cancelled = 0
        for lvl in self.grid.levels:
            for oid in (lvl.buy_order_id, lvl.sell_order_id):
                if oid:
                    ok = await self._api.cancel_order(state.session, self.grid.symbol, oid)
                    if ok:
                        cancelled += 1
            lvl.buy_order_id  = None
            lvl.sell_order_id = None
            lvl.buy_price     = None
            lvl.sell_price    = None
            lvl.status        = "idle"
        if self._user_stream_task is not None and not self._user_stream_task.done():
            self._user_stream_task.cancel()
            self._user_stream_task = None
        self.grid.halted = True
        logger.warning(
            "GridStrategy [%s] STOP-LOSS — price=%.2f outside [%.2f, %.2f] "
            "| %d orders cancelled | PnL=$%+.2f",
            self.grid.symbol, self.grid.last_price,
            self.grid.grid_lower, self.grid.grid_upper,
            cancelled, self.grid.total_profit_usd,
        )

    async def _recenter_grid(self, state: Any, price: float) -> None:
        """Cancel all orders and shift grid bounds so price lands at the midpoint."""
        half_range = (self.grid.grid_upper - self.grid.grid_lower) / 2
        new_lower  = round(price - half_range, 2)
        new_upper  = round(price + half_range, 2)
        old_lower, old_upper = self.grid.grid_lower, self.grid.grid_upper

        cancelled = 0
        for lvl in self.grid.levels:
            for oid in (lvl.buy_order_id, lvl.sell_order_id):
                if oid:
                    ok = await self._api.cancel_order(state.session, self.grid.symbol, oid)
                    if ok:
                        cancelled += 1
            lvl.buy_order_id  = None
            lvl.sell_order_id = None
            lvl.buy_price     = None
            lvl.sell_price    = None
            lvl.status        = "idle"

        n    = len(self.grid.levels)
        step = (new_upper - new_lower) / (n - 1) if n > 1 else 0.0
        self.grid.grid_lower  = new_lower
        self.grid.grid_upper  = new_upper
        self.grid.grid_step   = step
        self.grid.levels      = [GridLevel(price=round(new_lower + i * step, 2)) for i in range(n)]
        self.grid.initialised = False

        logger.info(
            "GridStrategy [%s] TRAIL re-center: [%.2f, %.2f] → [%.2f, %.2f] "
            "| %d orders cancelled",
            self.grid.symbol, old_lower, old_upper, new_lower, new_upper, cancelled,
        )

    # ── Initialisation ─────────────────────────────────────────────────────────

    async def _initialise_grid(self, state: Any, ts: Any) -> None:
        """Place the initial BUY/SELL orders across all grid levels."""
        current = ts.best_ask   # use ask as reference: buy orders must be below ask
        placed  = 0
        for lvl in self.grid.levels:
            if lvl.price < current:
                oid = await self._api.post_order(
                    state.session, self.grid.symbol,
                    lvl.price, self.grid.order_size_usdt, side="BUY",
                )
                if oid:
                    lvl.buy_order_id = oid
                    lvl.buy_price    = lvl.price
                    lvl.status       = "buy_placed"
                    placed += 1
                    logger.debug("GridStrategy BUY %.2f → %s", lvl.price, oid)
            elif lvl.price > current:
                oid = await self._api.post_order(
                    state.session, self.grid.symbol,
                    lvl.price, self.grid.order_size_usdt, side="SELL",
                )
                if oid:
                    lvl.sell_order_id = oid
                    lvl.sell_price    = lvl.price
                    lvl.status        = "sell_placed"
                    placed += 1
                    logger.debug("GridStrategy SELL %.2f → %s", lvl.price, oid)
            # Level at exact current price: skip to avoid crossing the spread

        self.grid.initialised = True
        logger.info(
            "GridStrategy [%s] initialized: price=%.2f | %d/%d levels active",
            self.grid.symbol, current, placed, len(self.grid.levels),
        )

    # ── Fill detection ─────────────────────────────────────────────────────────

    async def _poll_fills(self, state: Any, ts: Any) -> None:
        """
        Detect filled orders and trigger counter-orders.

        Simulation path (sim_ order IDs): price-crossing check, no REST call.
        Live path: one GET /openOrders call; absent order IDs = filled.
        """
        active = [l for l in self.grid.levels if l.is_active]
        if not active:
            return

        is_sim = any(
            (l.buy_order_id  or "").startswith("sim_") or
            (l.sell_order_id or "").startswith("sim_")
            for l in active
        )

        if is_sim:
            for lvl in active:
                if lvl.status == "buy_placed" and lvl.buy_price is not None:
                    if ts.best_ask <= lvl.buy_price:
                        await self._on_buy_filled(state, lvl)
                elif lvl.status == "sell_placed" and lvl.sell_price is not None:
                    if ts.best_bid >= lvl.sell_price:
                        await self._on_sell_filled(state, lvl)
        else:
            open_orders = await self._api.get_open_orders(
                state.session, self.grid.symbol)
            open_ids = {str(o["order_id"]) for o in (open_orders or [])}
            for lvl in active:
                if lvl.status == "buy_placed" and lvl.buy_order_id:
                    if str(lvl.buy_order_id) not in open_ids:
                        await self._on_buy_filled(state, lvl)
                elif lvl.status == "sell_placed" and lvl.sell_order_id:
                    if str(lvl.sell_order_id) not in open_ids:
                        await self._on_sell_filled(state, lvl)

    # ── Fill handlers ──────────────────────────────────────────────────────────

    async def _on_buy_filled(self, state: Any, lvl: GridLevel) -> None:
        """BUY at lvl.buy_price filled → place SELL at buy_price + grid_step."""
        buy_p  = lvl.buy_price or lvl.price
        sell_p = round(buy_p + self.grid.grid_step, 2)

        lvl.buy_order_id = None
        lvl.buy_price    = None
        lvl.filled_at_ts = time.time()

        if sell_p > self.grid.grid_upper:
            # Top of grid: no SELL counter-order, mark idle
            lvl.status = "idle"
            logger.info(
                "GridStrategy [%s] BUY fill %.2f → top of grid, idle",
                self.grid.symbol, buy_p,
            )
            return

        oid = await self._api.post_order(
            state.session, self.grid.symbol,
            sell_p, self.grid.order_size_usdt, side="SELL",
        )
        if oid:
            lvl.sell_order_id = oid
            lvl.sell_price    = sell_p
            lvl.status        = "sell_placed"
            logger.info(
                "GridStrategy [%s] BUY fill %.2f → SELL %.2f [%s]",
                self.grid.symbol, buy_p, sell_p, oid,
            )
        else:
            lvl.status = "idle"
            logger.error(
                "GridStrategy [%s] BUY fill %.2f → post_order SELL %.2f failed",
                self.grid.symbol, buy_p, sell_p,
            )

    async def _on_sell_filled(self, state: Any, lvl: GridLevel) -> None:
        """SELL at lvl.sell_price filled → account PnL, place BUY at sell_price − grid_step."""
        sell_p   = lvl.sell_price or lvl.price
        buy_p    = lvl.buy_price                        # None for init-placed SELLs
        new_buy  = round(sell_p - self.grid.grid_step, 2)

        # PnL only counted when a full BUY→SELL cycle completes
        profit = 0.0
        if buy_p is not None and new_buy > 0:
            qty      = self.grid.order_size_usdt / buy_p
            fee_buy  = self._api.compute_fee(buy_p, qty)
            fee_sell = self._api.compute_fee(sell_p, qty)
            profit   = (sell_p - buy_p) * qty - fee_buy - fee_sell
            self.grid.total_profit_usd += profit
            self.grid.total_cycles     += 1

        lvl.sell_order_id = None
        lvl.sell_price    = None
        lvl.buy_price     = None
        lvl.filled_at_ts  = time.time()

        if new_buy < self.grid.grid_lower:
            # Bottom of grid: no BUY counter-order, mark idle
            lvl.status = "idle"
            logger.info(
                "GridStrategy [%s] SELL fill %.2f → grid bottom, idle | "
                "total PnL=$%+.2f",
                self.grid.symbol, sell_p, self.grid.total_profit_usd,
            )
            return

        oid = await self._api.post_order(
            state.session, self.grid.symbol,
            new_buy, self.grid.order_size_usdt, side="BUY",
        )
        if oid:
            lvl.buy_order_id = oid
            lvl.buy_price    = new_buy
            lvl.status       = "buy_placed"
            logger.info(
                "GridStrategy [%s] SELL fill %.2f → BUY %.2f [%s] | "
                "cycle #%d profit=$%+.4f total=$%+.2f",
                self.grid.symbol, sell_p, new_buy, oid,
                self.grid.total_cycles, profit, self.grid.total_profit_usd,
            )
        else:
            lvl.status = "idle"
            logger.error(
                "GridStrategy [%s] SELL fill %.2f → post_order BUY %.2f failed",
                self.grid.symbol, sell_p, new_buy,
            )

    # ── Main entry point ───────────────────────────────────────────────────────

    async def on_book_update(
        self,
        state: Any,
        ts: Any,
        _t_ws: Optional[float] = None,
    ) -> None:
        """Called for every order-book update from the WebSocket feed."""
        price = ts.best_bid
        self.grid.last_price = price

        if self.grid.halted:
            return

        if self._trail_mode == "bull" and price > self.grid.grid_upper:
            await self._recenter_grid(state, price)
            self._save_state(state.conn)
            return

        if self._trail_mode == "bear" and price < self.grid.grid_lower:
            await self._recenter_grid(state, price)
            self._save_state(state.conn)
            return

        if self._check_stop_loss(price):
            await self._cancel_all_orders(state)
            self._save_state(state.conn)
            return

        if not self.grid.initialised:
            await self._initialise_grid(state, ts)
            self._save_state(state.conn)
            return

        # Start the user data stream on the first tick after init (non-sim mode).
        # Re-create the task if it exited unexpectedly (e.g. no credentials).
        if (not self._no_credentials and
                (self._user_stream_task is None or self._user_stream_task.done())):
            # Only start for real orders — sim_ IDs have no matching exchange stream.
            active = [l for l in self.grid.levels if l.is_active]
            is_sim = any(
                (l.buy_order_id  or "").startswith("sim_") or
                (l.sell_order_id or "").startswith("sim_")
                for l in active
            )
            if not is_sim:
                self._user_stream_task = asyncio.create_task(
                    self._user_stream_loop(state)
                )

        # REST polling is the fallback: skip when the user stream is active.
        if not self._user_ws_connected:
            now = time.time()
            if now - self.grid.last_poll_ts >= self.grid.poll_interval:
                self.grid.last_poll_ts = now
                prev_state = (self.grid.total_cycles,
                              tuple((l.buy_order_id, l.sell_order_id)
                                    for l in self.grid.levels))
                await self._poll_fills(state, ts)
                new_state = (self.grid.total_cycles,
                             tuple((l.buy_order_id, l.sell_order_id)
                                   for l in self.grid.levels))
                if prev_state != new_state:
                    self._save_state(state.conn)
