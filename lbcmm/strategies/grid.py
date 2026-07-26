"""Simple symmetric grid planner (advanced mode).

Places equal-spaced levels in [lower, upper]; buys below mid, sells above.
"""

from __future__ import annotations

from lbcmm.strategies.depth_provider import DesiredOrder


def plan_grid_orders(
    mid: float,
    *,
    lower: float,
    upper: float,
    levels: int = 10,
    order_size_usdt: float = 5.0,
    min_notional_usdt: float = 1.1,
) -> list[DesiredOrder]:
    if mid <= 0 or upper <= lower or levels < 2:
        return []
    step = (upper - lower) / (levels - 1)
    orders: list[DesiredOrder] = []
    for i in range(levels):
        px = lower + i * step
        if px < mid and order_size_usdt >= min_notional_usdt:
            qty = order_size_usdt / px
            orders.append(
                DesiredOrder(side="BUY", price=px, qty=qty, usdt=order_size_usdt, level=i)
            )
        elif px > mid and order_size_usdt >= min_notional_usdt:
            qty = order_size_usdt / px
            orders.append(
                DesiredOrder(side="SELL", price=px, qty=qty, usdt=order_size_usdt, level=i)
            )
    return orders
