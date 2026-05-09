"""
Grid trading strategy for continuous CEX spot markets (Binance, MEXC).

Algorithm
---------
1. Divide [grid_lower, grid_upper] into `grid_levels` evenly-spaced price levels.
   grid_step = (upper - lower) / (levels - 1)

2. On startup: place LIMIT BUY at every level below current price,
               place LIMIT SELL at every level above current price.

3. On BUY fill at level L:
     - Record fill; place LIMIT SELL at L + grid_step.
     - Estimated profit per cycle: grid_step * quantity_btc (before fees).

4. On SELL fill at level L:
     - Record fill; place LIMIT BUY at L - grid_step.
     - Increment cycle counter; accumulate realised profit.

5. Stop-loss: if price exits [grid_lower, grid_upper], cancel all open orders
   and halt the grid.

Configuration keys in strategy JSON
-------------------------------------
    "strategy_type":         "grid"
    "connector":             "binance" | "mexc"
    "grid_symbol":           "BTCUSDT"
    "grid_lower":            90000.0      (USDT lower bound)
    "grid_upper":            110000.0     (USDT upper bound)
    "grid_levels":           20           (number of price levels, >= 2)
    "grid_order_size_usdt":  50.0         (USDT notional per grid order)

Notes
-----
- Prices are absolute USDT values, not the 0–1 probability scale used by
  the Polymarket threshold strategy.  Recalibrate all thresholds accordingly.
- Fill detection requires either (a) a WebSocket order-status stream or
  (b) periodic REST polling — neither is implemented in this stub yet.
  See TODO markers below for the extension points.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("live")

STRATEGY_TYPE = "grid"


# ─── GRID STATE ───────────────────────────────────────────────────────────────

@dataclass
class GridLevel:
    """One price level in the grid."""
    price:         float
    buy_order_id:  Optional[str] = None
    sell_order_id: Optional[str] = None
    # idle | buy_placed | sell_placed | cycle_complete
    status:       str            = "idle"
    filled_at_ts: Optional[float] = None

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
    initialised:      bool            = False   # True once initial orders are placed


# ─── STRATEGY CLASS ───────────────────────────────────────────────────────────

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
        logger.info(
            "GridStrategy: %s  %.2f–%.2f  %d niveaux  step=%.2f  taille=$%.2f",
            symbol, lower, upper, n, step, size,
        )

    # ── public helpers ─────────────────────────────────────────────────────────

    @property
    def levels(self) -> list[GridLevel]:
        return self.grid.levels

    def level_at_price(self, price: float, tolerance: float = 0.5) -> Optional[GridLevel]:
        """Return the GridLevel whose price is within `tolerance` of `price`."""
        for lvl in self.grid.levels:
            if abs(lvl.price - price) <= tolerance:
                return lvl
        return None

    # ── main entry point ───────────────────────────────────────────────────────

    async def on_book_update(
        self,
        state: Any,
        ts: Any,
        _t_ws: Optional[float] = None,
    ) -> None:
        """
        React to a new price tick.

        Current status: STUB — logs the tick and updates grid.last_price.
        Full implementation steps (TODO):

        1. _initialise_grid(state, ts):
               On the first tick, place BUY orders below current price and
               SELL orders above.  Set grid.initialised = True.

        2. _poll_fills(state):
               REST-poll open order IDs (or consume a WebSocket order stream)
               to detect fills.  On BUY fill: call _on_buy_filled(state, level).
               On SELL fill: call _on_sell_filled(state, level).

        3. _check_stop_loss(state, ts):
               If ts.best_bid < grid.grid_lower or ts.best_bid > grid.grid_upper:
               cancel all open orders, log, halt grid.

        4. _on_buy_filled(state, level):
               Place SELL at level.price + grid.grid_step.
               Update level.status = "sell_placed".

        5. _on_sell_filled(state, level):
               Place BUY at level.price - grid.grid_step.
               Update level.status = "buy_placed".
               grid.total_cycles += 1.
               grid.total_profit_usd += grid_step * qty - fees.
        """
        price = ts.best_bid
        self.grid.last_price = price

        if not self.grid.initialised:
            logger.info(
                "GridStrategy [%s] première tick — prix=%.2f — "
                "initialisation des ordres non implémentée (stub)",
                self.grid.symbol, price,
            )
            # TODO: call self._initialise_grid(state, ts)
            return

        # TODO: poll fills + check stop-loss + place counter-orders
        logger.debug(
            "GridStrategy [%s] tick: %.2f  cycles=%d  PnL=$%.2f",
            self.grid.symbol, price,
            self.grid.total_cycles, self.grid.total_profit_usd,
        )
