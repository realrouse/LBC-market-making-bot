"""
SwingHold strategy — partial sells at each resistance, hold the rest.

Differences from SwingStrategy
-------------------------------
  SwingStrategy  : sells 100 % of position at first resistance above entry.
  SwingHoldStrategy: sells sell_fraction (e.g. 30 %) at each resistance level;
                    the remaining hold_fraction (70 %) advances to the next
                    resistance for the subsequent partial sell.

Algorithm
---------
Init (first tick):
  For each support level below current price, place one LIMIT BUY (up to
  max_positions).

BUY fill at support S:
  qty = order_size_usdt / fill_price
  → Find the first resistance above fill_price → place LIMIT SELL for
    sell_fraction × qty at that resistance price.
  → Record res_idx = 0 (index into sorted-resistance list above entry).

Partial SELL fill at resistance R[res_idx]:
  → sold_qty = sell_fraction × qty_held_before_sell
  → qty_held -= sold_qty
  → Accumulate partial PnL.
  → Advance: res_idx += 1
  → If qty_held >= min_qty_btc AND res_idx < available_resistances:
      place next LIMIT SELL for sell_fraction × qty_held at R[res_idx].
  → Otherwise: position closed (all BTC sold or no higher resistance).

SL (optional, sl_pct > 0):
  → Cancel active SELL order, log loss, close position.

Re-arm:
  After a position cycle completes (all partial sells done), a new LIMIT BUY
  is placed at the same support level if slots are available.

Configuration keys in strategy JSON
-------------------------------------
    "strategy_type":      "swinghold"
    "connector":          "binance" | "mexc"
    "symbol":             "BTCUSDT"

    "support_levels":     [70000, 72500, 75000]
    "resistance_levels":  [78000, 80000, 82500, 85000]

    "order_size_usdt":    200.0    notional USDT per initial BUY
    "max_positions":      3        max simultaneous open positions
    "sell_fraction":      0.30     fraction of qty_held to sell at each resistance
    "sl_pct":             0.02     stop-loss below entry (0 = disabled)
    "tp_pct_fallback":    0.04     TP when no resistance remains above position
    "min_qty_btc":        0.0001   close position when qty_held drops below this
    "poll_interval":      2.0      seconds between REST fill-detection polls
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("live")

STRATEGY_TYPE = "swinghold"


# ─── STATE DATACLASSES ────────────────────────────────────────────────────────

@dataclass
class SHPosition:
    """One active SwingHold position."""
    level_price:    float             # support that spawned this position
    buy_order_id:   Optional[str]  = None
    buy_price:      Optional[float]= None
    qty_held:       float          = 0.0   # BTC currently held
    qty_bought:     float          = 0.0   # total BTC bought (for records)
    sell_order_id:  Optional[str]  = None
    sell_price:     Optional[float]= None  # price of active partial SELL
    sell_qty:       float          = 0.0   # qty in the active partial SELL
    sl_price:       Optional[float]= None
    res_idx:        int            = 0     # next resistance index (0-based from entry)
    status:         str            = "buy_placed"  # buy_placed|long|partial_selling|closed
    pnl_net:        float          = 0.0   # accumulated PnL across all partial sells
    opened_at:      float          = 0.0
    db_id:          Optional[int]  = None


@dataclass
class SHState:
    """Runtime state for one active SwingHold instance."""
    symbol:          str
    support:         list[float]        # sorted ascending
    resistance:      list[float]        # sorted ascending
    order_size_usdt: float
    max_positions:   int
    sell_fraction:   float
    hold_fraction:   float              # = 1 - sell_fraction
    sl_pct:          float
    tp_pct_fallback: float
    min_qty_btc:     float
    positions:       list[SHPosition]  = field(default_factory=list)
    total_pnl:       float             = 0.0
    total_trades:    int               = 0
    last_price:      float             = 0.0
    initialised:     bool              = False
    last_poll_ts:    float             = 0.0
    poll_interval:   float             = 2.0


# ─── STRATEGY ─────────────────────────────────────────────────────────────────

class SwingHoldStrategy:
    """
    Swing strategy with partial sells at each resistance level.

    Instantiation validates config and builds level lists.
    Call on_book_update() on every price tick.
    """

    STRATEGY_TYPE = STRATEGY_TYPE

    def __init__(self, config: Any) -> None:
        cfg     = getattr(config, "strategy_cfg", {})
        symbol  = str(cfg.get("symbol",            "BTCUSDT"))
        sup_raw = list(cfg.get("support_levels",    []))
        res_raw = list(cfg.get("resistance_levels", []))

        if not sup_raw:
            raise ValueError("SwingHoldStrategy: 'support_levels' must not be empty")
        if not res_raw:
            raise ValueError("SwingHoldStrategy: 'resistance_levels' must not be empty")

        size      = float(cfg.get("order_size_usdt",  200.0))
        max_pos   = int(cfg.get("max_positions",         3))
        sell_frac = float(cfg.get("sell_fraction",      0.30))
        sl_pct    = float(cfg.get("sl_pct",             0.02))
        tp_fall   = float(cfg.get("tp_pct_fallback",    0.04))
        min_qty   = float(cfg.get("min_qty_btc",        0.0001))
        poll      = float(cfg.get("poll_interval",       2.0))

        if not 0 < sell_frac < 1:
            raise ValueError(f"sell_fraction must be in (0, 1), got {sell_frac}")
        if size <= 0:
            raise ValueError(f"order_size_usdt must be > 0, got {size}")
        if max_pos < 1:
            raise ValueError(f"max_positions must be >= 1, got {max_pos}")

        self.sh = SHState(
            symbol=symbol,
            support=sorted(float(p) for p in sup_raw),
            resistance=sorted(float(p) for p in res_raw),
            order_size_usdt=size,
            max_positions=max_pos,
            sell_fraction=sell_frac,
            hold_fraction=round(1.0 - sell_frac, 6),
            sl_pct=sl_pct,
            tp_pct_fallback=tp_fall,
            min_qty_btc=min_qty,
            poll_interval=poll,
        )

        from connectors import load as _load_conn
        self._api = _load_conn(config.connector)

        logger.info(
            "SwingHoldStrategy [%s]: %d support + %d resistance levels | "
            "size=$%.0f  max=%d  sell=%.0f%%  hold=%.0f%%  SL=%.1f%%  TP_fallback=%.1f%%",
            symbol,
            len(self.sh.support), len(self.sh.resistance),
            size, max_pos,
            sell_frac * 100, (1 - sell_frac) * 100,
            sl_pct * 100, tp_fall * 100,
        )
        for p in self.sh.support:
            logger.info("  support    $%.2f", p)
        for p in self.sh.resistance:
            logger.info("  resistance $%.2f", p)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _open_positions(self) -> list[SHPosition]:
        return [p for p in self.sh.positions if p.status != "closed"]

    def _armed_levels(self) -> set[float]:
        return {p.level_price for p in self._open_positions()}

    def _resistances_above(self, entry_price: float) -> list[float]:
        """Sorted resistance levels strictly above entry_price."""
        return [r for r in self.sh.resistance if r > entry_price]

    def _next_resistance(self, entry_price: float, res_idx: int) -> Optional[float]:
        """Return resistance_levels[res_idx] above entry, or None."""
        above = self._resistances_above(entry_price)
        if res_idx < len(above):
            return above[res_idx]
        return None

    # ── Schema ────────────────────────────────────────────────────────────────

    @staticmethod
    def ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS swinghold_state (
                symbol        TEXT PRIMARY KEY,
                total_pnl     REAL    DEFAULT 0,
                total_trades  INTEGER DEFAULT 0,
                initialised   INTEGER DEFAULT 0,
                updated_at    REAL
            );
            CREATE TABLE IF NOT EXISTS swinghold_positions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol        TEXT    NOT NULL,
                level_price   REAL    NOT NULL,
                buy_order_id  TEXT,
                buy_price     REAL,
                qty_held      REAL    DEFAULT 0,
                qty_bought    REAL    DEFAULT 0,
                sell_order_id TEXT,
                sell_price    REAL,
                sell_qty      REAL    DEFAULT 0,
                sl_price      REAL,
                res_idx       INTEGER DEFAULT 0,
                status        TEXT    NOT NULL DEFAULT 'buy_placed',
                pnl_net       REAL    DEFAULT 0,
                opened_at     REAL,
                closed_at     REAL
            );
        """)
        conn.commit()

    def _save_state(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            INSERT INTO swinghold_state (symbol, total_pnl, total_trades, initialised, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                total_pnl    = excluded.total_pnl,
                total_trades = excluded.total_trades,
                initialised  = excluded.initialised,
                updated_at   = excluded.updated_at
            """,
            (self.sh.symbol, self.sh.total_pnl,
             self.sh.total_trades, int(self.sh.initialised), time.time()),
        )
        for pos in self.sh.positions:
            if pos.db_id is None:
                cur = conn.execute(
                    """
                    INSERT INTO swinghold_positions
                        (symbol, level_price, buy_order_id, buy_price,
                         qty_held, qty_bought, sell_order_id, sell_price,
                         sell_qty, sl_price, res_idx, status, pnl_net, opened_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (self.sh.symbol, pos.level_price, pos.buy_order_id, pos.buy_price,
                     pos.qty_held, pos.qty_bought, pos.sell_order_id, pos.sell_price,
                     pos.sell_qty, pos.sl_price, pos.res_idx, pos.status,
                     pos.pnl_net, pos.opened_at),
                )
                pos.db_id = cur.lastrowid
            else:
                conn.execute(
                    """
                    UPDATE swinghold_positions SET
                        buy_order_id=?, buy_price=?, qty_held=?, qty_bought=?,
                        sell_order_id=?, sell_price=?, sell_qty=?,
                        sl_price=?, res_idx=?, status=?, pnl_net=?, closed_at=?
                    WHERE id=?
                    """,
                    (pos.buy_order_id, pos.buy_price, pos.qty_held, pos.qty_bought,
                     pos.sell_order_id, pos.sell_price, pos.sell_qty,
                     pos.sl_price, pos.res_idx, pos.status, pos.pnl_net,
                     time.time() if pos.status == "closed" else None,
                     pos.db_id),
                )
        conn.commit()

    def _restore(self, conn: sqlite3.Connection) -> None:
        self.ensure_schema(conn)
        row = conn.execute(
            "SELECT total_pnl, total_trades, initialised "
            "FROM swinghold_state WHERE symbol=?",
            (self.sh.symbol,),
        ).fetchone()
        if row:
            self.sh.total_pnl    = row[0]
            self.sh.total_trades = row[1]
            self.sh.initialised  = bool(row[2])

        rows = conn.execute(
            "SELECT id, level_price, buy_order_id, buy_price, qty_held, qty_bought, "
            "       sell_order_id, sell_price, sell_qty, sl_price, res_idx, status, pnl_net "
            "FROM swinghold_positions WHERE symbol=? AND status != 'closed' ORDER BY id",
            (self.sh.symbol,),
        ).fetchall()
        for r in rows:
            self.sh.positions.append(SHPosition(
                db_id=r[0], level_price=r[1], buy_order_id=r[2], buy_price=r[3],
                qty_held=r[4] or 0.0, qty_bought=r[5] or 0.0,
                sell_order_id=r[6], sell_price=r[7], sell_qty=r[8] or 0.0,
                sl_price=r[9], res_idx=r[10] or 0, status=r[11], pnl_net=r[12] or 0.0,
            ))
        logger.info(
            "SwingHoldStrategy [%s] — restored: %d open positions | "
            "total_trades=%d total_pnl=$%+.2f",
            self.sh.symbol, len(self.sh.positions),
            self.sh.total_trades, self.sh.total_pnl,
        )

    # ── Initialisation ─────────────────────────────────────────────────────────

    async def _initialise(self, state: Any, price: float) -> None:
        armed = self._armed_levels()
        slots = self.sh.max_positions - len(self._open_positions())
        placed = 0

        for sup in reversed(self.sh.support):   # nearest first
            if sup >= price or sup in armed or slots <= 0:
                continue
            qty = self.sh.order_size_usdt / sup
            oid = await self._api.post_order(
                state.session, self.sh.symbol, sup, self.sh.order_size_usdt, side="BUY",
            )
            if oid:
                pos = SHPosition(
                    level_price=sup, buy_order_id=oid,
                    buy_price=sup, qty_held=qty, qty_bought=qty,
                    status="buy_placed", opened_at=time.time(),
                )
                self.sh.positions.append(pos)
                armed.add(sup)
                slots  -= 1
                placed += 1
                logger.info("SwingHoldStrategy [%s] BUY %.2f qty=%.6f → [%s]",
                            self.sh.symbol, sup, qty, oid)
        self.sh.initialised = True
        logger.info("SwingHoldStrategy [%s] init: price=%.2f | %d BUY orders placed",
                    self.sh.symbol, price, placed)

    # ── Fill handlers ──────────────────────────────────────────────────────────

    async def _on_buy_filled(self, state: Any, pos: SHPosition, fill_price: float) -> None:
        pos.buy_price = fill_price
        pos.qty_held  = self.sh.order_size_usdt / fill_price
        pos.qty_bought = pos.qty_held
        pos.sl_price  = round(fill_price * (1 - self.sh.sl_pct), 2) if self.sh.sl_pct > 0 else None
        pos.res_idx   = 0
        pos.status    = "long"

        await self._place_next_partial_sell(state, pos)

    async def _place_next_partial_sell(self, state: Any, pos: SHPosition) -> None:
        """Place a partial SELL for sell_fraction × qty_held at next resistance."""
        target = self._next_resistance(pos.buy_price or pos.level_price, pos.res_idx)
        if target is None:
            # No resistance above: use fallback TP on remaining qty
            entry  = pos.buy_price or pos.level_price
            target = round(entry * (1.0 + self.sh.tp_pct_fallback), 2)

        sell_qty   = round(pos.qty_held * self.sh.sell_fraction, 8)
        sell_usdt  = sell_qty * target

        if pos.qty_held < self.sh.min_qty_btc or sell_qty <= 0:
            pos.status = "closed"
            logger.info("SwingHoldStrategy [%s] position at level %.2f fully exited",
                        self.sh.symbol, pos.level_price)
            await self._rearm(state, pos)
            return

        oid = await self._api.post_order(
            state.session, self.sh.symbol, target, sell_usdt, side="SELL",
        )
        if oid:
            pos.sell_order_id = oid
            pos.sell_price    = target
            pos.sell_qty      = sell_qty
            pos.status        = "partial_selling"
            logger.info(
                "SwingHoldStrategy [%s] partial SELL %.6f BTC @ %.2f  "
                "held=%.6f  res_idx=%d  [%s]",
                self.sh.symbol, sell_qty, target, pos.qty_held, pos.res_idx, oid,
            )
        else:
            logger.error("SwingHoldStrategy [%s] partial SELL post_order failed", self.sh.symbol)

    async def _on_partial_sell_filled(self, state: Any, pos: SHPosition,
                                       fill_price: float) -> None:
        """Partial SELL filled → account PnL fragment, advance to next resistance."""
        entry    = pos.buy_price or pos.level_price
        sold_qty = pos.sell_qty
        fee_b    = self._api.compute_fee(entry,      sold_qty)
        fee_s    = self._api.compute_fee(fill_price, sold_qty)
        pnl_part = (fill_price - entry) * sold_qty - fee_b - fee_s

        pos.qty_held       -= sold_qty
        pos.pnl_net        += pnl_part
        pos.sell_order_id   = None
        pos.sell_qty        = 0.0
        pos.res_idx        += 1

        logger.info(
            "SwingHoldStrategy [%s] partial SELL fill @ %.2f  "
            "pnl_part=$%+.4f  qty_remaining=%.6f  res_idx=%d",
            self.sh.symbol, fill_price, pnl_part, pos.qty_held, pos.res_idx,
        )

        if pos.qty_held < self.sh.min_qty_btc:
            # All BTC effectively sold
            self.sh.total_pnl    += pos.pnl_net
            self.sh.total_trades += 1
            pos.status = "closed"
            logger.info(
                "SwingHoldStrategy [%s] position closed | pnl=$%+.4f | "
                "total_trades=%d total_pnl=$%+.2f",
                self.sh.symbol, pos.pnl_net,
                self.sh.total_trades, self.sh.total_pnl,
            )
            await self._rearm(state, pos)
        else:
            await self._place_next_partial_sell(state, pos)

    async def _close_sl(self, state: Any, pos: SHPosition, price: float) -> None:
        if pos.sell_order_id:
            try:
                await self._api.cancel_order(state.session, self.sh.symbol, pos.sell_order_id)
            except Exception:
                pass

        entry  = pos.buy_price or pos.level_price
        fee_b  = self._api.compute_fee(entry, pos.qty_held)
        fee_s  = self._api.compute_fee(price, pos.qty_held)
        pnl_sl = (price - entry) * pos.qty_held - fee_b - fee_s + pos.pnl_net

        self.sh.total_pnl    += pnl_sl
        self.sh.total_trades += 1
        pos.status = "closed"

        logger.warning(
            "SwingHoldStrategy [%s] SL hit @ %.2f  pnl=$%+.4f  total_pnl=$%+.2f",
            self.sh.symbol, price, pnl_sl, self.sh.total_pnl,
        )

    async def _rearm(self, state: Any, closed_pos: SHPosition) -> None:
        """Re-arm the same support level after a full cycle."""
        slots = self.sh.max_positions - len(self._open_positions())
        if slots <= 0:
            return
        sup = closed_pos.level_price
        if sup in self._armed_levels():
            return
        qty = self.sh.order_size_usdt / sup
        oid = await self._api.post_order(
            state.session, self.sh.symbol, sup, self.sh.order_size_usdt, side="BUY",
        )
        if oid:
            new_pos = SHPosition(
                level_price=sup, buy_order_id=oid,
                buy_price=sup, qty_held=qty, qty_bought=qty,
                status="buy_placed", opened_at=time.time(),
            )
            self.sh.positions.append(new_pos)
            logger.info("SwingHoldStrategy [%s] re-armed support %.2f → [%s]",
                        self.sh.symbol, sup, oid)

    # ── Fill detection ─────────────────────────────────────────────────────────

    async def _poll_fills(self, state: Any, ts: Any) -> None:
        open_pos = self._open_positions()
        if not open_pos:
            return

        is_sim = any(
            (p.buy_order_id  or "").startswith("sim_") or
            (p.sell_order_id or "").startswith("sim_")
            for p in open_pos
        )

        bid = ts.best_bid
        ask = ts.best_ask

        if is_sim:
            for pos in list(open_pos):
                if pos.status == "buy_placed" and pos.buy_price is not None:
                    if ask <= pos.buy_price:
                        await self._on_buy_filled(state, pos, pos.buy_price)
                elif pos.status == "partial_selling" and pos.sell_price is not None:
                    if bid >= pos.sell_price:
                        await self._on_partial_sell_filled(state, pos, pos.sell_price)
                    elif pos.sl_price and bid <= pos.sl_price:
                        await self._close_sl(state, pos, bid)
                elif pos.status == "long" and pos.sl_price and bid <= pos.sl_price:
                    await self._close_sl(state, pos, bid)
        else:
            try:
                open_orders = await self._api.get_open_orders(state.session, self.sh.symbol)
                open_ids    = {str(o["order_id"]) for o in (open_orders or [])}
            except Exception as exc:
                logger.warning("SwingHoldStrategy get_open_orders failed: %s", exc)
                return

            for pos in list(open_pos):
                if pos.status == "buy_placed" and pos.buy_order_id:
                    if str(pos.buy_order_id) not in open_ids:
                        await self._on_buy_filled(state, pos, pos.buy_price or pos.level_price)
                elif pos.status == "partial_selling" and pos.sell_order_id:
                    if str(pos.sell_order_id) not in open_ids:
                        await self._on_partial_sell_filled(state, pos, pos.sell_price or bid)
                    elif pos.sl_price and bid <= pos.sl_price:
                        await self._close_sl(state, pos, bid)
                elif pos.status == "long" and pos.sl_price and bid <= pos.sl_price:
                    await self._close_sl(state, pos, bid)

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def on_book_update(
        self,
        state: Any,
        ts: Any,
        _t_ws: Optional[float] = None,
    ) -> None:
        """Called for every order-book update from the WebSocket feed."""
        price = ts.best_bid
        self.sh.last_price = price

        if not self.sh.initialised:
            self.ensure_schema(state.conn)
            self._restore(state.conn)
            if not self.sh.initialised:
                await self._initialise(state, price)
            self._save_state(state.conn)
            return

        now     = time.time()
        changed = False

        if now - self.sh.last_poll_ts >= self.sh.poll_interval:
            self.sh.last_poll_ts = now
            prev = self.sh.total_trades
            await self._poll_fills(state, ts)
            if self.sh.total_trades != prev:
                changed = True

        if changed:
            self._save_state(state.conn)
