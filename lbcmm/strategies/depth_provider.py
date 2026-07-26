"""Depth Provider — symmetric band liquidity for CoinGecko-style ±2% depth.

Pure planner: numbers in, desired resting orders out. No I/O.

MEXC (and this bot) require roughly **$1 minimum notional per order**. Budgets
below that place **no** orders on that side. When budget is tight, the number of
steps is automatically reduced so each resting order stays ≥ min notional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Exchange-safe default; UI presents this as "$1 minimum per order".
DEFAULT_MIN_NOTIONAL_USDT = 1.0


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
    min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT
    mid_gap_bps: float = 5.0


def max_levels_for_budget(budget_usdt: float, min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT) -> int:
    """How many ≥min-notional orders fit in a USDT budget (0 if none)."""
    if budget_usdt < min_notional_usdt or min_notional_usdt <= 0:
        return 0
    return int(budget_usdt // min_notional_usdt)


def effective_levels(
    n_levels: int,
    budget_usdt: float,
    min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT,
) -> int:
    """Cap requested steps so each order can be ≥ min_notional."""
    n = max(0, int(n_levels))
    if n <= 0:
        return 0
    cap = max_levels_for_budget(budget_usdt, min_notional_usdt)
    if cap <= 0:
        return 0
    return max(1, min(n, cap))


def plan_depth_orders(
    mid: float,
    *,
    usdt_budget: float,
    lbc_budget: float,
    bid_depth_pct: float = 2.0,
    ask_depth_pct: float = 2.0,
    n_levels: int = 4,
    min_notional_usdt: float = DEFAULT_MIN_NOTIONAL_USDT,
    mid_gap_bps: float = 5.0,
) -> list[DesiredOrder]:
    """Place bids in [mid*(1-bid%), mid) and asks in (mid, mid*(1+ask%)].

    - Buy side: needs usdt_budget ≥ min_notional; steps auto-capped.
    - Sell side: needs lbc_budget * price ≥ min_notional per level; steps auto-capped.
    - A side with 0 / dust budget produces **no** orders on that side.
    """
    if mid <= 0:
        return []
    min_n = max(0.01, float(min_notional_usdt))
    n_req = max(1, int(n_levels))
    gap = mid * (mid_gap_bps / 10_000.0)
    orders: list[DesiredOrder] = []

    # ── Buys ──────────────────────────────────────────────────────────────
    buy_n = effective_levels(n_req, usdt_budget, min_n)
    if buy_n > 0 and usdt_budget >= min_n and bid_depth_pct > 0:
        floor = mid * (1.0 - bid_depth_pct / 100.0)
        top_bid = mid - gap
        if top_bid > floor:
            if buy_n == 1:
                prices = [top_bid]
            else:
                step = (top_bid - floor) / (buy_n - 1)
                prices = [top_bid - i * step for i in range(buy_n)]
            # Equal split of the full buy budget across placeable levels
            per = usdt_budget / buy_n
            for i, px in enumerate(prices):
                if px <= 0 or per + 1e-12 < min_n:
                    continue
                qty = per / px
                if qty * px + 1e-12 >= min_n:
                    orders.append(
                        DesiredOrder(side="BUY", price=px, qty=qty, usdt=per, level=i)
                    )

    # ── Sells ─────────────────────────────────────────────────────────────
    # Approximate sell-side USDT capacity at mid for step capping
    sell_budget_usdt = lbc_budget * mid if lbc_budget > 0 and mid > 0 else 0.0
    sell_n = effective_levels(n_req, sell_budget_usdt, min_n)
    if sell_n > 0 and lbc_budget > 0 and ask_depth_pct > 0:
        ceiling = mid * (1.0 + ask_depth_pct / 100.0)
        bot_ask = mid + gap
        if bot_ask < ceiling:
            if sell_n == 1:
                prices = [bot_ask]
            else:
                step = (ceiling - bot_ask) / (sell_n - 1)
                prices = [bot_ask + i * step for i in range(sell_n)]
            per_coins = lbc_budget / sell_n
            for i, px in enumerate(prices):
                usdt = per_coins * px
                if usdt + 1e-12 < min_n:
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
