"""Single-process market-making engine for LBCUSDT on MEXC."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import aiohttp

from lbcmm.config import BotConfig
from lbcmm.connectors import mexc
from lbcmm.strategies.bamm import BammGrid, build_buy_grid
from lbcmm.strategies.depth_provider import (
    DepthProvider,
    DepthProviderConfig,
    DesiredOrder,
    contribution_usd,
)
from lbcmm.strategies.grid import plan_grid_orders

logger = logging.getLogger("lbcmm.engine")


@dataclass
class LiveOrder:
    order_id: str
    side: str
    price: float
    qty: float
    usdt: float
    level: int = 0


@dataclass
class EngineState:
    running: bool = False
    paper: bool = True
    mid: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    public_depth: dict = field(default_factory=dict)
    bot_contribution: dict = field(default_factory=dict)
    open_orders: list[LiveOrder] = field(default_factory=list)
    last_error: str = ""
    started_at: float = 0.0
    ticks: int = 0
    strategy: str = "depth_provider"
    realized_pnl: float = 0.0
    free_usdt: float = 0.0
    free_lbc: float = 0.0
    status_msg: str = "idle"
    desired: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "paper": self.paper,
            "mid": self.mid,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "public_depth": self.public_depth,
            "bot_contribution": self.bot_contribution,
            "open_orders": [
                {
                    "order_id": o.order_id,
                    "side": o.side,
                    "price": o.price,
                    "qty": o.qty,
                    "usdt": o.usdt,
                    "level": o.level,
                }
                for o in self.open_orders
            ],
            "last_error": self.last_error,
            "uptime_s": (time.time() - self.started_at) if self.started_at else 0,
            "ticks": self.ticks,
            "strategy": self.strategy,
            "realized_pnl": self.realized_pnl,
            "free_usdt": self.free_usdt,
            "free_lbc": self.free_lbc,
            "status_msg": self.status_msg,
            "desired": self.desired,
        }


class Engine:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.state = EngineState(
            paper=cfg.effective_paper(),
            strategy=cfg.effective_strategy(),
            free_usdt=cfg.usdt_budget,
            free_lbc=cfg.lbc_budget,
        )
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._provider = DepthProvider(
            DepthProviderConfig(
                usdt_budget=cfg.usdt_budget,
                lbc_budget=cfg.lbc_budget,
                bid_depth_pct=cfg.bid_depth_pct,
                ask_depth_pct=cfg.ask_depth_pct,
                n_levels=cfg.n_levels,
                min_notional_usdt=cfg.min_notional_usdt,
            )
        )
        self._bamm: Optional[BammGrid] = None
        self._session: Optional[aiohttp.ClientSession] = None

    def update_config(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self.state.paper = cfg.effective_paper()
        self.state.strategy = cfg.effective_strategy()
        self._provider = DepthProvider(
            DepthProviderConfig(
                usdt_budget=cfg.usdt_budget,
                lbc_budget=cfg.lbc_budget,
                bid_depth_pct=cfg.bid_depth_pct,
                ask_depth_pct=cfg.ask_depth_pct,
                n_levels=cfg.n_levels,
                min_notional_usdt=cfg.min_notional_usdt,
            )
        )

    async def start(self) -> None:
        if self.state.running:
            return
        self._stop.clear()
        self.state.running = True
        self.state.started_at = time.time()
        self.state.status_msg = "starting"
        self.state.last_error = ""
        self._task = asyncio.create_task(self._run_loop(), name="lbcmm-engine")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        await self._cancel_all()
        self.state.running = False
        self.state.status_msg = "stopped"
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _run_loop(self) -> None:
        paper = self.cfg.effective_paper()
        self.state.paper = paper
        self.state.strategy = self.cfg.effective_strategy()
        self.state.status_msg = "paper" if paper else "LIVE"
        logger.info(
            "Engine start strategy=%s paper=%s usdt=%.2f lbc=%.4f depth=±%.1f/%.1f",
            self.state.strategy,
            paper,
            self.cfg.usdt_budget,
            self.cfg.lbc_budget,
            self.cfg.bid_depth_pct,
            self.cfg.ask_depth_pct,
        )
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception as e:  # pylint: disable=broad-exception-caught
                    self.state.last_error = str(e)
                    logger.exception("tick error: %s", e)
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.cfg.poll_interval_s
                    )
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._cancel_all()
            self.state.running = False

    async def _tick(self) -> None:
        session = await self._session_get()
        book = await mexc.get_depth(session, self.cfg.symbol, limit=100)
        if not book:
            self.state.last_error = "no public book"
            self.state.status_msg = "waiting for book"
            return
        mid = book.get("mid") or 0.0
        if mid <= 0:
            return
        self.state.mid = mid
        self.state.best_bid = book["best_bid"]
        self.state.best_ask = (
            book["best_ask"] if book["best_ask"] != float("inf") else 0.0
        )
        self.state.public_depth = mexc.depth_within_pct(book, 2.0)
        self.state.ticks += 1

        strat = self.cfg.effective_strategy()
        if strat == "bamm":
            await self._tick_bamm(session, mid)
        elif strat == "grid":
            await self._tick_grid(session, mid)
        else:
            await self._tick_depth(session, mid)

    async def _tick_depth(self, session: aiohttp.ClientSession, mid: float) -> None:
        if not self._provider.needs_reprice(mid, self.cfg.reprice_pct) and self.state.open_orders:
            # still refresh contribution display
            self.state.bot_contribution = contribution_usd(
                self._provider.last_orders, mid, 2.0
            )
            return

        desired = self._provider.plan(mid)
        self.state.bot_contribution = contribution_usd(desired, mid, 2.0)
        self.state.desired = [
            {
                "side": o.side,
                "price": o.price,
                "qty": o.qty,
                "usdt": o.usdt,
                "level": o.level,
            }
            for o in desired
        ]
        await self._reconcile(session, desired)
        self.state.status_msg = (
            f"{'paper' if self.state.paper else 'LIVE'} depth @ {mid:.6f}"
        )

    async def _tick_grid(self, session: aiohttp.ClientSession, mid: float) -> None:
        lower = self.cfg.grid_lower if self.cfg.grid_lower > 0 else mid * 0.95
        upper = self.cfg.grid_upper if self.cfg.grid_upper > 0 else mid * 1.05
        desired = plan_grid_orders(
            mid,
            lower=lower,
            upper=upper,
            levels=self.cfg.grid_levels,
            order_size_usdt=self.cfg.grid_order_size_usdt,
            min_notional_usdt=self.cfg.min_notional_usdt,
        )
        self.state.desired = [
            {"side": o.side, "price": o.price, "qty": o.qty, "usdt": o.usdt, "level": o.level}
            for o in desired
        ]
        self.state.bot_contribution = contribution_usd(desired, mid, 2.0)
        if self._provider.needs_reprice(mid, self.cfg.reprice_pct) or not self.state.open_orders:
            self._provider.last_mid = mid
            await self._reconcile(session, desired)
        self.state.status_msg = f"{'paper' if self.state.paper else 'LIVE'} grid @ {mid:.6f}"

    async def _tick_bamm(self, session: aiohttp.ClientSession, mid: float) -> None:
        if self._bamm is None:
            top = self.cfg.bamm_top if self.cfg.bamm_top > 0 else mid * 1.05
            rungs = build_buy_grid(
                top=top,
                floor=self.cfg.bamm_floor,
                step_pct=self.cfg.bamm_step_pct,
                budget_usdt=self.cfg.usdt_budget,
                min_notional_usdt=self.cfg.min_notional_usdt,
            )
            self._bamm = BammGrid(
                rungs,
                step_pct=self.cfg.bamm_step_pct,
                stash_pct=self.cfg.bamm_stash_pct,
                free_usdt=self.cfg.usdt_budget,
                min_notional_usdt=self.cfg.min_notional_usdt,
            )
        desired_raw = self._bamm.desired_orders(mid)
        desired = [
            DesiredOrder(
                side=d["side"].upper(),
                price=d["price"],
                qty=d["coins"],
                usdt=d["price"] * d["coins"],
                level=d.get("rung", 0),
            )
            for d in desired_raw
        ]
        self.state.desired = [
            {"side": o.side, "price": o.price, "qty": o.qty, "usdt": o.usdt, "level": o.level}
            for o in desired
        ]
        self.state.bot_contribution = contribution_usd(desired, mid, 2.0)
        snap = self._bamm.snapshot(mid)
        self.state.realized_pnl = snap["realized_usdt"]
        self.state.free_usdt = snap["free_usdt"]
        self.state.free_lbc = snap["holdings"]
        await self._reconcile(session, desired)
        self.state.status_msg = f"{'paper' if self.state.paper else 'LIVE'} bamm @ {mid:.6f}"

    async def _reconcile(
        self, session: aiohttp.ClientSession, desired: list[DesiredOrder]
    ) -> None:
        """Cancel existing bot orders and place desired set (simple full replace)."""
        await self._cancel_all(session)
        paper = self.cfg.effective_paper()
        new_orders: list[LiveOrder] = []
        for o in desired:
            oid = await mexc.post_order(
                session,
                self.cfg.symbol,
                o.price,
                size_usdc=o.usdt if o.side == "BUY" else None,
                quantity=o.qty if o.side == "SELL" else None,
                side=o.side,
                order_type="LIMIT_MAKER",
                api_key=self.cfg.api_key(),
                api_secret=self.cfg.api_secret(),
                paper=paper,
            )
            if not oid:
                self.state.last_error = f"failed to place {o.side} @ {o.price}"
                continue
            new_orders.append(
                LiveOrder(
                    order_id=oid,
                    side=o.side,
                    price=o.price,
                    qty=o.qty,
                    usdt=o.usdt,
                    level=o.level,
                )
            )
            # Paper fill simulation: if price crosses mid aggressively, leave resting
            # (we keep sim orders as resting for display).
        self.state.open_orders = new_orders

        # Paper: simulate fills when market crosses our resting prices (optional light sim)
        if paper and self.state.mid > 0:
            await self._paper_fill_check()

    async def _paper_fill_check(self) -> None:
        """Credit paper inventory when mid crosses a resting order (simple)."""
        mid = self.state.mid
        remaining = []
        for o in self.state.open_orders:
            filled = False
            if o.side == "BUY" and mid <= o.price:
                self.state.free_usdt = max(0.0, self.state.free_usdt - o.usdt)
                self.state.free_lbc += o.qty
                filled = True
                logger.info("paper FILL BUY %.6f qty=%.4f", o.price, o.qty)
            elif o.side == "SELL" and mid >= o.price:
                self.state.free_lbc = max(0.0, self.state.free_lbc - o.qty)
                self.state.free_usdt += o.usdt
                filled = True
                logger.info("paper FILL SELL %.6f qty=%.4f", o.price, o.qty)
            if not filled:
                remaining.append(o)
        self.state.open_orders = remaining

    async def _cancel_all(self, session: Optional[aiohttp.ClientSession] = None) -> None:
        if not self.state.open_orders:
            return
        if session is None:
            try:
                session = await self._session_get()
            except Exception:  # pylint: disable=broad-exception-caught
                self.state.open_orders = []
                return
        for o in list(self.state.open_orders):
            await mexc.cancel_order(
                session,
                self.cfg.symbol,
                o.order_id,
                api_key=self.cfg.api_key(),
                api_secret=self.cfg.api_secret(),
            )
        self.state.open_orders = []


# Process-wide engine for GUI
_ENGINE: Optional[Engine] = None


def get_engine(cfg: Optional[BotConfig] = None) -> Engine:
    global _ENGINE
    if _ENGINE is None:
        if cfg is None:
            from lbcmm.config import load_config

            cfg = load_config()
        _ENGINE = Engine(cfg)
    elif cfg is not None:
        _ENGINE.update_config(cfg)
    return _ENGINE
