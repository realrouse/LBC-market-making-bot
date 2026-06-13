"""
Dollar-Cost Averaging (DCA) strategy — buys at fixed time intervals regardless of price.

Algorithm
---------
Every dca_interval_h hours (and when at least one position slot is free):
  1. Place a LIMIT BUY at current ask price for dca_amount_usdt.
  2. Record position with TP = entry_price * (1 + tp_pct).

On each tick:
  - Simulated positions: TP detected when best_bid >= tp_price.
  - Real orders: REST poll every poll_interval seconds; missing order = filled.
  - On TP fill: place LIMIT SELL at tp_price, record PnL, free slot.
  - Optional SL (sl_pct > 0): close at market when best_bid <= sl_price.

Configuration keys in strategy JSON
-------------------------------------
    "strategy_type":   "dca"
    "connector":       "binance" | "mexc"
    "symbol":          "BTCUSDT"

    "dca_interval_h":  4        hours between scheduled DCA buys
    "dca_amount_usdt": 100.0    USDT per buy order
    "max_positions":   5        maximum simultaneous open positions
    "tp_pct":          0.03     take-profit distance above entry (3 %)
    "sl_pct":          0.0      stop-loss below entry (0 = disabled, pure DCA)
    "poll_interval":   2.0      seconds between REST fill-detection polls (live mode)
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

STRATEGY_TYPE = "dca"


# ─── STATE DATACLASSES ────────────────────────────────────────────────────────

@dataclass
class DCAPosition:
    """One open DCA position."""
    db_id:          int
    buy_order_id:   str
    entry_ts:       float
    entry_price:    float
    qty:            float
    tp_price:       float
    sl_price:       Optional[float]   # None when sl_pct == 0
    sell_order_id:  Optional[str]     = None
    status:         str               = "buy_placed"  # buy_placed | long | tp_placed | closed
    exit_price:     Optional[float]   = None
    exit_reason:    Optional[str]     = None
    pnl_net:        Optional[float]   = None


@dataclass
class DCAState:
    """Runtime state for the DCA strategy."""
    symbol:         str
    interval_s:     float
    amount_usdt:    float
    max_positions:  int
    tp_pct:         float
    sl_pct:         float
    positions:      list[DCAPosition] = field(default_factory=list)
    last_buy_ts:    float             = 0.0
    total_trades:   int               = 0
    total_pnl:      float             = 0.0
    last_poll_ts:   float             = 0.0
    poll_interval:  float             = 2.0
    initialised:    bool              = False


# ─── STRATEGY ─────────────────────────────────────────────────────────────────

class DCAStrategy:
    """
    Dollar-Cost Averaging strategy.

    Buys a fixed USDT amount every N hours up to max_positions open slots.
    Takes profit at tp_pct above entry; optionally stops at sl_pct below.
    """

    STRATEGY_TYPE = STRATEGY_TYPE

    def __init__(self, config: Any) -> None:
        symbol   = str(getattr(config, "symbol",          "BTCUSDT"))
        interval = float(getattr(config, "dca_interval_h", 4.0))
        amount   = float(getattr(config, "dca_amount_usdt", 100.0))
        max_pos  = int(getattr(config,   "max_positions",  5))
        tp_pct   = float(getattr(config, "tp_pct",         0.03))
        sl_pct   = float(getattr(config, "sl_pct",         0.0))
        poll     = float(getattr(config, "poll_interval",  2.0))

        if interval <= 0:
            raise ValueError(f"dca_interval_h must be > 0, got {interval}")
        if amount <= 0:
            raise ValueError(f"dca_amount_usdt must be > 0, got {amount}")
        if max_pos < 1:
            raise ValueError(f"max_positions must be >= 1, got {max_pos}")
        if tp_pct < 0:
            raise ValueError(f"tp_pct must be >= 0, got {tp_pct}")

        self.dca = DCAState(
            symbol=symbol,
            interval_s=interval * 3600,
            amount_usdt=amount,
            max_positions=max_pos,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            poll_interval=poll,
        )

        from connectors import load as _load_conn
        self._api = _load_conn(config.connector)

        logger.info(
            "DCAStrategy [%s]: interval=%gh  amount=$%.0f  max=%d  TP=%.1f%%  SL=%s",
            symbol, interval, amount, max_pos,
            tp_pct * 100,
            f"{sl_pct*100:.1f}%" if sl_pct > 0 else "disabled",
        )

    # ── Schema ────────────────────────────────────────────────────────────────

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS dca_positions (
                id            INTEGER PRIMARY KEY,
                symbol        TEXT    NOT NULL,
                buy_order_id  TEXT,
                entry_ts      REAL,
                entry_price   REAL,
                qty           REAL,
                tp_price      REAL,
                sl_price      REAL,
                sell_order_id TEXT,
                status        TEXT    DEFAULT 'buy_placed',
                exit_price    REAL,
                exit_reason   TEXT,
                pnl_net       REAL
            )""")
        conn.commit()

    def _save_position(self, conn: sqlite3.Connection, pos: DCAPosition) -> None:
        conn.execute("""
            INSERT OR REPLACE INTO dca_positions
                (id, symbol, buy_order_id, entry_ts, entry_price, qty,
                 tp_price, sl_price, sell_order_id, status,
                 exit_price, exit_reason, pnl_net)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos.db_id, self.dca.symbol, pos.buy_order_id, pos.entry_ts,
             pos.entry_price, pos.qty, pos.tp_price, pos.sl_price,
             pos.sell_order_id, pos.status,
             pos.exit_price, pos.exit_reason, pos.pnl_net))
        conn.commit()

    def _restore(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            "SELECT id, buy_order_id, entry_ts, entry_price, qty, "
            "       tp_price, sl_price, sell_order_id, status "
            "FROM dca_positions WHERE symbol=? AND status NOT IN ('closed')",
            (self.dca.symbol,)
        ).fetchall()
        for (db_id, buy_oid, entry_ts, entry_price, qty,
             tp_price, sl_price, sell_oid, status) in rows:
            self.dca.positions.append(DCAPosition(
                db_id=db_id, buy_order_id=buy_oid, entry_ts=entry_ts,
                entry_price=entry_price, qty=qty,
                tp_price=tp_price, sl_price=sl_price,
                sell_order_id=sell_oid, status=status,
            ))
        totals = conn.execute(
            "SELECT count(*), coalesce(sum(pnl_net),0) "
            "FROM dca_positions WHERE symbol=? AND status='closed'",
            (self.dca.symbol,)
        ).fetchone()
        if totals:
            self.dca.total_trades = totals[0]
            self.dca.total_pnl    = totals[1]
        if self.dca.positions:
            self.dca.last_buy_ts = max(p.entry_ts for p in self.dca.positions
                                       if p.entry_ts)
        logger.info(
            "DCAStrategy [%s] — restored: %d open positions | "
            "total_trades=%d total_pnl=$%+.2f",
            self.dca.symbol, len(self.dca.positions),
            self.dca.total_trades, self.dca.total_pnl,
        )

    # ── Open positions helpers ─────────────────────────────────────────────────

    def _open_positions(self) -> list[DCAPosition]:
        return [p for p in self.dca.positions if p.status != "closed"]

    def _open_count(self) -> int:
        return len(self._open_positions())

    # ── Place buy ─────────────────────────────────────────────────────────────

    async def _place_buy(self, state: Any, price: float) -> None:
        d = self.dca
        qty      = d.amount_usdt / price
        tp_price = round(price * (1 + d.tp_pct), 2)
        sl_price = round(price * (1 - d.sl_pct), 2) if d.sl_pct > 0 else None

        oid = await self._api.post_order(
            state.session, d.symbol, price, d.amount_usdt, side="BUY"
        )
        if oid is None:
            logger.warning("DCAStrategy [%s] BUY post_order failed", d.symbol)
            return

        cur = state.conn.execute(
            "INSERT INTO dca_positions "
            "(symbol, buy_order_id, entry_ts, entry_price, qty, "
            " tp_price, sl_price, status) "
            "VALUES (?,?,?,?,?,?,?,'buy_placed')",
            (d.symbol, oid, time.time(), price, qty, tp_price, sl_price))
        state.conn.commit()
        db_id = cur.lastrowid

        pos = DCAPosition(
            db_id=db_id, buy_order_id=oid, entry_ts=time.time(),
            entry_price=price, qty=qty,
            tp_price=tp_price, sl_price=sl_price,
        )
        d.positions.append(pos)
        d.last_buy_ts = time.time()

        logger.info(
            "DCAStrategy [%s] BUY $%.0f @ %.2f  qty=%.6f  TP=%.2f  SL=%s  [%s]",
            d.symbol, d.amount_usdt, price, qty, tp_price,
            f"{sl_price:.2f}" if sl_price else "off", oid,
        )

    # ── Fill detection ─────────────────────────────────────────────────────────

    async def _poll_fills(self, state: Any, ts: Any) -> None:
        """Detect fills for all open positions."""
        d = self.dca

        is_sim = all(
            (p.buy_order_id or "").startswith("sim_") and
            (p.sell_order_id is None or (p.sell_order_id or "").startswith("sim_"))
            for p in self._open_positions()
        ) if self._open_positions() else True

        if is_sim:
            await self._check_sim_fills(state, ts)
        else:
            await self._check_rest_fills(state, ts)

    async def _check_sim_fills(self, state: Any, ts: Any) -> None:
        bid = ts.best_bid
        ask = ts.best_ask
        for pos in list(self._open_positions()):
            if pos.status == "buy_placed":
                # Simulated BUY fills when ask <= entry_price
                if ask <= pos.entry_price:
                    pos.status = "long"
                    self._save_position(state.conn, pos)
                    logger.info(
                        "DCAStrategy [%s] BUY fill %.2f → TP %.2f  [%s]",
                        self.dca.symbol, pos.entry_price, pos.tp_price, pos.buy_order_id,
                    )
            elif pos.status == "long":
                if bid >= pos.tp_price:
                    await self._on_tp_hit(state, pos, pos.tp_price)
                elif pos.sl_price and bid <= pos.sl_price:
                    await self._on_sl_hit(state, pos, bid)
            elif pos.status == "tp_placed":
                if bid >= pos.tp_price:
                    await self._on_tp_filled(state, pos, pos.tp_price)

    async def _check_rest_fills(self, state: Any, ts: Any) -> None:
        try:
            open_oids = {
                o["order_id"]
                for o in await self._api.get_open_orders(state.session, self.dca.symbol)
            }
        except Exception as exc:
            logger.warning("DCAStrategy [%s] get_open_orders failed: %s", self.dca.symbol, exc)
            return

        bid = ts.best_bid
        for pos in list(self._open_positions()):
            if pos.status == "buy_placed" and pos.buy_order_id not in open_oids:
                pos.status = "long"
                self._save_position(state.conn, pos)
                logger.info(
                    "DCAStrategy [%s] BUY fill %.2f → TP %.2f  [%s]",
                    self.dca.symbol, pos.entry_price, pos.tp_price, pos.buy_order_id,
                )
            elif pos.status == "long":
                if pos.sl_price and bid <= pos.sl_price:
                    await self._on_sl_hit(state, pos, bid)
                elif pos.sell_order_id is None:
                    await self._on_tp_hit(state, pos, pos.tp_price)
            elif pos.status == "tp_placed" and pos.sell_order_id not in open_oids:
                await self._on_tp_filled(state, pos, pos.tp_price)

    async def _on_tp_hit(self, state: Any, pos: DCAPosition, tp_price: float) -> None:
        d   = self.dca
        oid = await self._api.post_order(
            state.session, d.symbol, tp_price, d.amount_usdt, side="SELL"
        )
        if oid is None:
            logger.warning("DCAStrategy [%s] TP SELL post_order failed", d.symbol)
            return
        pos.sell_order_id = oid
        pos.status        = "tp_placed"
        self._save_position(state.conn, pos)
        logger.info(
            "DCAStrategy [%s] TP SELL placed @ %.2f  [%s]",
            d.symbol, tp_price, oid,
        )

    async def _on_tp_filled(self, state: Any, pos: DCAPosition,
                            exit_price: float) -> None:
        fee_buy  = self._api.compute_fee(pos.entry_price, pos.qty)
        fee_sell = self._api.compute_fee(exit_price, pos.qty)
        pnl      = (exit_price - pos.entry_price) * pos.qty - fee_buy - fee_sell

        pos.status      = "closed"
        pos.exit_price  = exit_price
        pos.exit_reason = "tp"
        pos.pnl_net     = round(pnl, 4)
        self._save_position(state.conn, pos)
        self.dca.positions.remove(pos)
        self.dca.total_trades += 1
        self.dca.total_pnl    += pnl

        logger.info(
            "DCAStrategy [%s] TP FILL @ %.2f  PnL=$%+.4f  total_trades=%d  total_pnl=$%+.2f",
            self.dca.symbol, exit_price, pnl,
            self.dca.total_trades, self.dca.total_pnl,
        )

    async def _on_sl_hit(self, state: Any, pos: DCAPosition, price: float) -> None:
        fee_buy  = self._api.compute_fee(pos.entry_price, pos.qty)
        fee_sell = self._api.compute_fee(price, pos.qty)
        pnl      = (price - pos.entry_price) * pos.qty - fee_buy - fee_sell

        pos.status      = "closed"
        pos.exit_price  = price
        pos.exit_reason = "sl"
        pos.pnl_net     = round(pnl, 4)
        self._save_position(state.conn, pos)
        self.dca.positions.remove(pos)
        self.dca.total_trades += 1
        self.dca.total_pnl    += pnl

        logger.warning(
            "DCAStrategy [%s] SL HIT @ %.2f  PnL=$%+.4f",
            self.dca.symbol, price, pnl,
        )

    # ── Main tick ─────────────────────────────────────────────────────────────

    async def on_book_update(
        self,
        state: Any,
        ts: Any,
        _t_ws: Optional[float] = None,
    ) -> None:
        """Called for every order-book update from the WebSocket feed."""
        d   = self.dca
        ask = ts.best_ask

        if not d.initialised:
            self.ensure_schema(state.conn)
            self._restore(state.conn)
            d.initialised = True

        now = time.time()

        # Fill detection (throttled for live REST calls)
        if now - d.last_poll_ts >= d.poll_interval:
            d.last_poll_ts = now
            await self._poll_fills(state, ts)

        # Scheduled DCA buy
        time_ok = (now - d.last_buy_ts) >= d.interval_s
        slot_ok  = self._open_count() < d.max_positions
        if time_ok and slot_ok and ask > 0:
            await self._place_buy(state, ask)
