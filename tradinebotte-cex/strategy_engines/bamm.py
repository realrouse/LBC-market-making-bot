"""bamm.py — PURE planning for BAMM: the Bullish Accumulating Market Maker (LBC real-money bot).

strategy_type = "bamm". A two-sided resting maker that is net-accumulating, downside-weighted,
floored, and never stop-losses — NOT a symmetric grid.

No I/O, no exchange calls, no state mutation: numbers in, DESIRED orders out. That is what lets it
be unit-tested exhaustively and behave identically in shadow / backtest / live (same discipline as
accumulation.plan_sell_ladder).

Operator intent (neofutur):
  • Fixed price rungs from `top` down to a HARD `floor` (0.001). Rung prices never move.
  • The WHOLE USDT budget is deployed by the floor: Σ(buy-rung USDT) == budget, weighted so a
    DEEPER rung buys MORE ("5% dip → ~5% of funds, 10% dip → ~10%…", tunable via sizing_power) —
    support the price, accumulate cheaper lower.
  • Keep 10% of EVERY buy as a permanent accumulation stash (operator-confirmed); the other 90%
    loops (sell one rung up, rebuy one rung down) — self-funding and net-accumulating.
  • No stop-loss: below the floor the bot is simply out of USDT (the accepted max-loss boundary).

Why fixed rungs (not the old avg-entry ladder): the +10% band stranded for 6 days owing a rebuy it
could never fill, so it missed the 2026-07-24 spike. Fixed rungs with paired buy<->sell orders make
"a band stuck owing a rebuy" structurally impossible.
"""

from __future__ import annotations

from dataclasses import dataclass

STRATEGY_TYPE = "bamm"


@dataclass(frozen=True)
class Rung:
    price: float          # rung price (a fixed grid line)
    usdt:  float          # USDT to spend buying this rung
    coins: float          # coins that USDT buys at `price` (usdt / price)


def _geometric_prices(top: float, floor: float, step_pct: float) -> list[float]:
    """Grid lines from `top` down to `floor` (inclusive), spaced `step_pct` apart geometrically.
    The last line is snapped exactly to `floor` so the deploy-at-floor guarantee is exact."""
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


def build_buy_grid(*, top: float, floor: float = 0.001, step_pct: float = 5.0,
                   budget_usdt: float = 80.0, sizing_power: float = 1.0,
                   min_notional_usdt: float = 1.1) -> list[Rung]:
    """The buy ladder: fixed rungs from `top` down to `floor`, together spending the WHOLE
    `budget_usdt`, weighted so deeper rungs buy more.

    Weight per rung = (dip fraction from top) ** sizing_power, dip = (top - price)/top.
    sizing_power = 1 ≈ "5% dip → ~5% of funds"; >1 tilts harder toward the floor. Any rung whose
    share falls under `min_notional_usdt` (MEXC rejects < ~1 USDT) is dropped and its budget
    re-spread over the survivors, so every returned rung is placeable and Σ usdt still equals the
    budget (deploy-at-floor holds)."""
    if budget_usdt <= 0:
        return []
    prices = _geometric_prices(top, floor, step_pct)
    weights = [max(0.0, (top - p) / top) ** sizing_power for p in prices]
    kept = list(range(len(prices)))
    alloc: dict[int, float] = {}
    while kept:
        wsum = sum(weights[i] for i in kept)
        if wsum <= 0:
            return []
        alloc = {i: budget_usdt * weights[i] / wsum for i in kept}
        under = [i for i in kept if alloc[i] < min_notional_usdt]
        if not under:
            break
        worst = min(under, key=lambda i: alloc[i])
        kept.remove(worst)
    rungs = []
    for i in kept:
        usdt = round(alloc[i], 4)                 # coins follow the ACTUAL rounded spend
        rungs.append(Rung(price=prices[i], usdt=usdt, coins=usdt / prices[i]))
    return rungs


def deploy_summary(rungs: list[Rung]) -> dict:
    """Roll-up of a buy grid: total USDT, total coins if every rung fills, and blended avg cost."""
    tot = sum(r.usdt for r in rungs)
    coins = sum(r.coins for r in rungs)
    return {"rungs": len(rungs), "usdt": tot, "coins": coins,
            "avg_cost": (tot / coins) if coins else 0.0,
            "bottom": rungs[-1].price if rungs else None,
            "top": rungs[0].price if rungs else None}


# ── The round-trip cycle: keep 10% of EVERY buy, loop the 90% (operator-confirmed) ─────────────
# On a buy fill: bank stash_pct of the coins forever, rest a SELL of the rest one rung up.
# On a sell fill: rest a REBUY of exactly what was sold one rung down (back at the origin rung).
# Because every buy (including a rebuy) banks another 10%, the rung's traded lot shrinks 10% per
# cycle while the permanently-held stash grows toward the rung's full lot — a self-funding loop
# (sell high / rebuy low at +step) that also accumulates. No coins are ever sold below the stash.

def sell_after_buy(*, buy_price: float, coins_bought: float, step_pct: float = 5.0,
                   stash_pct: float = 0.10) -> dict:
    """After a buy fills: the resting SELL to place one rung UP, plus the stash to bank.
    Sells (1 - stash_pct) of the coins; stash_pct stays as permanent accumulation."""
    return {
        "sell_price":  buy_price * (1.0 + step_pct / 100.0),
        "sell_coins":  coins_bought * (1.0 - stash_pct),
        "stash_coins": coins_bought * stash_pct,
        "origin_buy_price": buy_price,
    }


def rebuy_after_sell(*, sell_price: float, coins_sold: float, step_pct: float = 5.0) -> dict:
    """After a sell fills: the resting BUY to place one rung DOWN — back at the origin rung —
    re-arming the cycle for exactly the coins just sold. Its own fill banks another stash slice."""
    buy_price = sell_price / (1.0 + step_pct / 100.0)
    return {"buy_price": buy_price, "buy_coins": coins_sold, "buy_usdt": buy_price * coins_sold}


# ── The grid state machine (pure) — the shadow/backtest brain ───────────────────────────────────
# Holds per-rung state and the running book/wallet; fills drive the transitions. No I/O: the live
# engine will feed it real fills and read desired_orders() back out; the backtester feeds it a
# price path. One source of truth for shadow, backtest and live.

class BammGrid:
    """Fixed-rung BAMM ladder as a deterministic state machine.

    Each rung is either a resting BID (empty, buying `loop_coins` at its price) or a resting ASK
    (loaded, selling `loop_coins` one rung up). A buy fill banks `stash_pct` of the coins forever
    and rests the rest as an ask; a sell fill rests a rebuy of exactly what sold. `loop_coins`
    therefore shrinks `stash_pct` per buy while the permanent stash grows — self-funding + net
    accumulating. No stop-loss; below the floor there is simply nothing left to buy with."""

    def __init__(self, rungs: list[Rung], *, step_pct: float = 5.0, stash_pct: float = 0.10,
                 free_usdt: float = 0.0, maker_fee: float = 0.0, min_notional_usdt: float = 1.1):
        if not rungs:
            raise ValueError("BammGrid needs at least one rung")
        self.step = step_pct
        self.stash_pct = stash_pct
        self.maker_fee = maker_fee
        self.min_notional = min_notional_usdt
        # rung i: dict(price, loop_coins, mode 'bid'|'ask'|'idle')
        self.rungs = [{"price": r.price, "loop_coins": r.coins, "mode": "bid"} for r in rungs]
        self.free_usdt = float(free_usdt)
        self.holdings = 0.0        # all coins we hold (stash + coins parked in loaded rungs)
        self.stash = 0.0           # permanent accumulation, never offered for sale
        self.realized_usdt = 0.0   # grid profit booked on sell fills (sell px − origin px)
        self.n_buys = 0
        self.n_sells = 0

    def _ask_price(self, i: int) -> float:
        return self.rungs[i]["price"] * (1.0 + self.step / 100.0)

    def seed_holdings(self, coins: float) -> None:
        """Assign pre-existing holdings to the LOWEST rungs as already-loaded ASKs, so they get
        sold on the way up (cutover from the current position). Coins beyond the grid's capacity
        become pure stash (never sold)."""
        left = coins
        for i in range(len(self.rungs) - 1, -1, -1):     # deepest first
            if left <= 0:
                break
            r = self.rungs[i]
            take = min(left, r["loop_coins"])
            if take * self._ask_price(i) >= self.min_notional:
                r["mode"] = "ask"
                r["loop_coins"] = take
                left -= take
        self.holdings += coins
        self.stash += max(0.0, left)                      # overflow → permanent stash

    def desired_orders(self, mid: float) -> list[dict]:
        """The resting book we WANT right now: a bid at every empty rung below `mid`, an ask at
        every loaded rung. Dust (< min_notional) is skipped — those coins just sit as stash."""
        out = []
        for i, r in enumerate(self.rungs):
            if r["mode"] == "bid":
                px, coins = r["price"], r["loop_coins"]
                if px < mid and coins * px >= self.min_notional and coins * px <= self.free_usdt + 1e-9:
                    out.append({"side": "buy", "rung": i, "price": px, "coins": coins})
            elif r["mode"] == "ask":
                px, coins = self._ask_price(i), r["loop_coins"]
                if px > mid and coins * px >= self.min_notional:
                    out.append({"side": "sell", "rung": i, "price": px, "coins": coins})
        return out

    # State-only transitions (no wallet) — LIVE calls these; the real wallet is credited from real
    # fills by the engine. Each returns the paired order the engine must now rest.
    def on_buy_settled(self, i: int, coins: float) -> dict:
        """Rung i's buy fully filled for `coins`: bank the stash, flip to an ask of the rest, and
        return the SELL to rest one rung up."""
        r = self.rungs[i]
        stash = coins * self.stash_pct
        self.stash += stash
        r["mode"] = "ask"
        r["loop_coins"] = coins - stash          # only the 90% is offered for sale
        self.n_buys += 1
        return {"price": self._ask_price(i), "coins": r["loop_coins"]}

    def on_sell_settled(self, i: int, coins: float) -> dict:
        """Rung i's ask fully filled for `coins`: flip back to a rebuy bid and return the BUY to
        rest one rung down (back at the origin rung price)."""
        r = self.rungs[i]
        r["mode"] = "bid"
        r["loop_coins"] = coins                  # rebuy exactly what we sold, back at r["price"]
        self.n_sells += 1
        return {"price": r["price"], "coins": coins}

    # Sim-wallet variants — SHADOW/BACKTEST only (drive the internal wallet + realized).
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
        return {"holdings": self.holdings, "stash": self.stash, "free_usdt": self.free_usdt,
                "realized_usdt": self.realized_usdt, "equity": self.free_usdt + self.holdings * mid,
                "n_buys": self.n_buys, "n_sells": self.n_sells}
