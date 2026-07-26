"""BAMM pure planner — copied from tradinebotte-cex/strategy_engines/bamm.py (GPL-3.0).

Bullish Accumulating Market Maker: fixed-rung, downside-weighted, stashes 10% of buys.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Rung:
    price: float
    usdt: float
    coins: float
    seedable: bool = True


def _geometric_prices(top: float, floor: float, step_pct: float) -> list[float]:
    if not (0.0 < floor < top):
        raise ValueError(f"require 0 < floor < top, got floor={floor} top={top}")
    if not (0.0 < step_pct < 100.0):
        raise ValueError(f"step_pct must be in (0,100), got {step_pct}")
    factor = 1.0 - step_pct / 100.0
    prices: list[float] = []
    p = top
    while p > floor * (1.0 + 1e-9):
        prices.append(p)
        p *= factor
    if prices and prices[-1] <= floor * (1.0 + step_pct / 100.0 * 0.5):
        prices[-1] = floor
    else:
        prices.append(floor)
    return prices


def build_buy_grid(
    *,
    top: float,
    floor: float = 0.001,
    step_pct: float = 5.0,
    budget_usdt: float = 80.0,
    sizing_power: float = 1.0,
    min_notional_usdt: float = 1.1,
    extra_rungs: list[tuple[float, float]] | None = None,
) -> list[Rung]:
    rungs: list[Rung] = []
    if budget_usdt > 0:
        prices = _geometric_prices(top, floor, step_pct)
        weights = [max(0.0, (top - p) / top) ** sizing_power for p in prices]
        kept = list(range(len(prices)))
        alloc: dict[int, float] = {}
        while kept:
            wsum = sum(weights[i] for i in kept)
            if wsum <= 0:
                kept = []
                break
            alloc = {i: budget_usdt * weights[i] / wsum for i in kept}
            under = [i for i in kept if alloc[i] < min_notional_usdt]
            if not under:
                break
            worst = min(under, key=lambda i: alloc[i])
            kept.remove(worst)
        for i in kept:
            usdt = round(alloc[i], 4)
            rungs.append(Rung(price=prices[i], usdt=usdt, coins=usdt / prices[i]))
    for price, usdt in extra_rungs or []:
        if usdt <= 0 or usdt < min_notional_usdt:
            continue
        rungs.append(
            Rung(
                price=price,
                usdt=round(usdt, 4),
                coins=round(usdt, 4) / price,
                seedable=False,
            )
        )
    rungs.sort(key=lambda r: r.price, reverse=True)
    return rungs


def sell_after_buy(
    *,
    buy_price: float,
    coins_bought: float,
    step_pct: float = 5.0,
    stash_pct: float = 0.10,
) -> dict:
    return {
        "sell_price": buy_price * (1.0 + step_pct / 100.0),
        "sell_coins": coins_bought * (1.0 - stash_pct),
        "stash_coins": coins_bought * stash_pct,
        "origin_buy_price": buy_price,
    }


def rebuy_after_sell(
    *, sell_price: float, coins_sold: float, step_pct: float = 5.0
) -> dict:
    buy_price = sell_price / (1.0 + step_pct / 100.0)
    return {"buy_price": buy_price, "buy_coins": coins_sold, "buy_usdt": buy_price * coins_sold}


class BammGrid:
    def __init__(
        self,
        rungs: list[Rung],
        *,
        step_pct: float = 5.0,
        stash_pct: float = 0.10,
        free_usdt: float = 0.0,
        maker_fee: float = 0.0,
        min_notional_usdt: float = 1.1,
    ):
        if not rungs:
            raise ValueError("BammGrid needs at least one rung")
        self.step = step_pct
        self.stash_pct = stash_pct
        self.maker_fee = maker_fee
        self.min_notional = min_notional_usdt
        self.rungs = [
            {"price": r.price, "loop_coins": r.coins, "mode": "bid", "seedable": r.seedable}
            for r in rungs
        ]
        self.free_usdt = float(free_usdt)
        self.holdings = 0.0
        self.stash = 0.0
        self.realized_usdt = 0.0
        self.n_buys = 0
        self.n_sells = 0

    def _ask_price(self, i: int) -> float:
        return self.rungs[i]["price"] * (1.0 + self.step / 100.0)

    def desired_orders(self, mid: float) -> list[dict]:
        out = []
        for i, r in enumerate(self.rungs):
            if r["mode"] == "bid":
                px, coins = r["price"], r["loop_coins"]
                if (
                    px < mid
                    and coins * px >= self.min_notional
                    and coins * px <= self.free_usdt + 1e-9
                ):
                    out.append({"side": "buy", "rung": i, "price": px, "coins": coins})
            elif r["mode"] == "ask":
                px, coins = self._ask_price(i), r["loop_coins"]
                if px > mid and coins * px >= self.min_notional:
                    out.append({"side": "sell", "rung": i, "price": px, "coins": coins})
        return out

    def on_buy_settled(self, i: int, coins: float) -> dict:
        r = self.rungs[i]
        stash = coins * self.stash_pct
        self.stash += stash
        r["mode"] = "ask"
        r["loop_coins"] = coins - stash
        self.n_buys += 1
        return {"price": self._ask_price(i), "coins": r["loop_coins"]}

    def on_sell_settled(self, i: int, coins: float) -> dict:
        r = self.rungs[i]
        r["mode"] = "bid"
        r["loop_coins"] = coins
        self.n_sells += 1
        return {"price": r["price"], "coins": coins}

    def on_buy_fill(self, i: int, coins: float) -> None:
        self.free_usdt -= coins * self.rungs[i]["price"] * (1.0 + self.maker_fee)
        self.holdings += coins
        self.on_buy_settled(i, coins)

    def on_sell_fill(self, i: int, coins: float) -> None:
        ask_px = self._ask_price(i)
        self.free_usdt += coins * ask_px * (1.0 - self.maker_fee)
        self.holdings -= coins
        self.realized_usdt += coins * (ask_px - self.rungs[i]["price"])
        self.on_sell_settled(i, coins)

    def snapshot(self, mid: float) -> dict:
        return {
            "holdings": self.holdings,
            "stash": self.stash,
            "free_usdt": self.free_usdt,
            "realized_usdt": self.realized_usdt,
            "equity": self.free_usdt + self.holdings * mid,
            "n_buys": self.n_buys,
            "n_sells": self.n_sells,
        }
