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

import logging
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
                f"Grid bounds invalides: lower={lower}, upper={upper}. "
                "Requis: 0 < lower < upper."
            )
        if n < 2:
            raise ValueError(f"grid_levels doit être >= 2, reçu {n}")
        if size <= 0:
            raise ValueError(f"grid_order_size_usdt doit être > 0, reçu {size}")

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

        logger.info(
            "GridStrategy: %s  %.2f–%.2f  %d niveaux  step=%.2f  taille=$%.2f",
            symbol, lower, upper, n, step, size,
        )

    # ── Public helpers ─────────────────────────────────────────────────────────

    @property
    def levels(self) -> list[GridLevel]:
        return self.grid.levels

    def level_at_price(self, price: float, tolerance: float = 0.5) -> Optional[GridLevel]:
        """Return the GridLevel whose reference price is within `tolerance` of `price`."""
        for lvl in self.grid.levels:
            if abs(lvl.price - price) <= tolerance:
                return lvl
        return None

    # ── Stop-loss ──────────────────────────────────────────────────────────────

    def _check_stop_loss(self, price: float) -> bool:
        """True if price has exited the grid bounds."""
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
        self.grid.halted = True
        logger.warning(
            "GridStrategy [%s] STOP-LOSS — prix=%.2f hors [%.2f, %.2f] "
            "| %d ordres annulés | PnL=$%+.2f",
            self.grid.symbol, self.grid.last_price,
            self.grid.grid_lower, self.grid.grid_upper,
            cancelled, self.grid.total_profit_usd,
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
            "GridStrategy [%s] initialisé: prix=%.2f | %d/%d niveaux actifs",
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
                "GridStrategy [%s] BUY fill %.2f → haut de grille, idle",
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
                "GridStrategy [%s] BUY fill %.2f → échec post_order SELL %.2f",
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
                "GridStrategy [%s] SELL fill %.2f → bas de grille, idle | "
                "PnL total=$%+.2f",
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
                "GridStrategy [%s] SELL fill %.2f → échec post_order BUY %.2f",
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

        if self._check_stop_loss(price):
            await self._cancel_all_orders(state)
            return

        if not self.grid.initialised:
            await self._initialise_grid(state, ts)
            return

        now = time.time()
        if now - self.grid.last_poll_ts >= self.grid.poll_interval:
            self.grid.last_poll_ts = now
            await self._poll_fills(state, ts)
