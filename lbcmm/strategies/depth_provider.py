"""Depth Provider — symmetric band liquidity for CoinGecko-style ±2% depth.

Pure planner: numbers in, desired resting orders out. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DesiredOrder:
    side: str          # "BUY" | "SELL"
    price: float
    qty: float         # base (LBC)
    usdt: float        # quote notional
    level: int


@dataclass
class DepthProviderConfig:
    usdt_budget: float = 10.0
    lbc_budget: float = 0.0
    bid_depth_pct: float = 2.0
    ask_depth_pct: float = 2.0
    n_levels: int = 4
    min_notional_usdt: float = 1.1
    # leave a small gap from mid so we stay maker (bps of mid)
    mid_gap_bps: float = 5.0


def plan_depth_orders(
    mid: float,
    *,
    usdt_budget: float,
    lbc_budget: float,
    bid_depth_pct: float = 2.0,
    ask_depth_pct: float = 2.0,
    n_levels: int = 4,
    min_notional_usdt: float = 1.1,
    mid_gap_bps: float = 5.0,
) -> list[DesiredOrder]:
    """Place n_levels of bids in [mid*(1-bid%), mid) and asks in (mid, mid*(1+ask%)].

    USDT budget is split across buy levels; LBC budget across sell levels (by coin qty).
    Levels near mid get equal share of budget (simple equal split for v1).
    """
    if mid <= 0:
        return []
    n = max(1, int(n_levels))
    gap = mid * (mid_gap_bps / 10_000.0)
    orders: list[DesiredOrder] = []

    # ── Buys ──────────────────────────────────────────────────────────────
    if usdt_budget >= min_notional_usdt and bid_depth_pct > 0:
        floor = mid * (1.0 - bid_depth_pct / 100.0)
        top_bid = mid - gap
        if top_bid > floor:
            # prices from near-mid down to floor
            if n == 1:
                prices = [top_bid]
            else:
                step = (top_bid - floor) / (n - 1)
                prices = [top_bid - i * step for i in range(n)]
            per = usdt_budget / n
            for i, px in enumerate(prices):
                if px <= 0 or per < min_notional_usdt:
                    continue
                qty = per / px
                if qty * px >= min_notional_usdt:
                    orders.append(
                        DesiredOrder(side="BUY", price=px, qty=qty, usdt=per, level=i)
                    )

    # ── Sells ─────────────────────────────────────────────────────────────
    if lbc_budget > 0 and ask_depth_pct > 0:
        ceiling = mid * (1.0 + ask_depth_pct / 100.0)
        bot_ask = mid + gap
        if bot_ask < ceiling:
            if n == 1:
                prices = [bot_ask]
            else:
                step = (ceiling - bot_ask) / (n - 1)
                prices = [bot_ask + i * step for i in range(n)]
            per_coins = lbc_budget / n
            for i, px in enumerate(prices):
                usdt = per_coins * px
                if usdt < min_notional_usdt:
                    continue
                orders.append(
                    DesiredOrder(
                        side="SELL", price=px, qty=per_coins, usdt=usdt, level=i
                    )
                )

    return orders


def contribution_usd(orders: list[DesiredOrder], mid: float, pct: float = 2.0) -> dict:
    """How much of our desired book sits inside mid±pct (CoinGecko-style)."""
    if mid <= 0:
        return {"bid_usd": 0.0, "ask_usd": 0.0}
    lo = mid * (1.0 - pct / 100.0)
    hi = mid * (1.0 + pct / 100.0)
    bid = sum(o.usdt for o in orders if o.side == "BUY" and o.price >= lo)
    ask = sum(o.usdt for o in orders if o.side == "SELL" and o.price <= hi)
    return {"bid_usd": bid, "ask_usd": ask, "pct": pct}


class DepthProvider:
    """Stateful wrapper around plan_depth_orders + reprice threshold."""

    def __init__(self, cfg: Optional[DepthProviderConfig] = None, **kwargs):
        if cfg is None:
            cfg = DepthProviderConfig(**kwargs)
        self.cfg = cfg
        self.last_mid: float = 0.0
        self.last_orders: list[DesiredOrder] = []

    def plan(self, mid: float) -> list[DesiredOrder]:
        c = self.cfg
        orders = plan_depth_orders(
            mid,
            usdt_budget=c.usdt_budget,
            lbc_budget=c.lbc_budget,
            bid_depth_pct=c.bid_depth_pct,
            ask_depth_pct=c.ask_depth_pct,
            n_levels=c.n_levels,
            min_notional_usdt=c.min_notional_usdt,
            mid_gap_bps=c.mid_gap_bps,
        )
        self.last_mid = mid
        self.last_orders = orders
        return orders

    def needs_reprice(self, mid: float, threshold_pct: float = 0.35) -> bool:
        if self.last_mid <= 0:
            return True
        drift = abs(mid - self.last_mid) / self.last_mid * 100.0
        return drift >= threshold_pct
