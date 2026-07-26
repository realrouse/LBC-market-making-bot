"""Single-process market-making engine for LBCUSDT on MEXC."""

from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
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
        self._shutting_down = False
        self._orders_path = self._orders_file_path(cfg)

    @staticmethod
    def _orders_file_path(cfg: BotConfig) -> Path:
        bid = (cfg.bot_id or "default").replace("/", "_")
        return cfg.resolve_data_dir() / f"open_orders_{cfg.symbol}_{bid}.json"

    def update_config(self, cfg: BotConfig) -> None:
        self.cfg = cfg
        self._orders_path = self._orders_file_path(cfg)
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

    def _persist_orders(self) -> None:
        """Write tracked order IDs so a kill/restart can still cancel them."""
        try:
            payload = {
                "symbol": self.cfg.symbol,
                "updated_at": time.time(),
                "orders": [
                    {
                        "order_id": o.order_id,
                        "side": o.side,
                        "price": o.price,
                        "qty": o.qty,
                        "usdt": o.usdt,
                        "level": o.level,
                    }
                    for o in self.state.open_orders
                    if o.order_id and not str(o.order_id).startswith("sim_")
                ],
            }
            self._orders_path.parent.mkdir(parents=True, exist_ok=True)
            self._orders_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("persist open orders failed: %s", e)

    def _load_persisted_orders(self) -> list[LiveOrder]:
        if not self._orders_path.is_file():
            return []
        try:
            raw = json.loads(self._orders_path.read_text(encoding="utf-8"))
            out: list[LiveOrder] = []
            for o in raw.get("orders") or []:
                oid = str(o.get("order_id") or "")
                if not oid or oid.startswith("sim_"):
                    continue
                out.append(
                    LiveOrder(
                        order_id=oid,
                        side=str(o.get("side") or "BUY"),
                        price=float(o.get("price") or 0),
                        qty=float(o.get("qty") or 0),
                        usdt=float(o.get("usdt") or 0),
                        level=int(o.get("level") or 0),
                    )
                )
            return out
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("load persisted orders failed: %s", e)
            return []

    def _clear_persisted_orders(self) -> None:
        try:
            if self._orders_path.is_file():
                self._orders_path.unlink()
        except OSError:
            pass

    async def start(self) -> None:
        if self.state.running:
            return
        self._stop.clear()
        self._shutting_down = False
        self.state.running = True
        self.state.started_at = time.time()
        self.state.status_msg = "starting"
        self.state.last_error = ""
        # Cancel this bot's leftover IDs from a previous crash before placing a new book
        await self.cleanup_orders(reason="startup")
        task_name = f"lbcmm-engine-{self.cfg.bot_id or 'default'}"
        self._task = asyncio.create_task(self._run_loop(), name=task_name)

    async def stop(self, *, cancel_orders: bool = True) -> None:
        """Stop the loop. By default cancel only this bot's orders.

        cancel_orders=False — halt management but leave resting orders on the
        exchange (IDs stay persisted so a later cancel/start can still find them).
        """
        if self._shutting_down:
            return
        self._shutting_down = True
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=15)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):  # pylint: disable=broad-exception-caught
                    pass
            except asyncio.CancelledError:
                pass
            self._task = None
        if cancel_orders:
            await self.cleanup_orders(reason="shutdown")
        else:
            # Keep order IDs on disk; do not cancel on exchange
            self._persist_orders()
            logger.info(
                "stop without cancel — %s bot order(s) left on book",
                len(self.state.open_orders),
            )
        self.state.running = False
        self.state.status_msg = (
            "stopped (orders left open)" if not cancel_orders else "stopped"
        )
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def cleanup_orders(self, *, reason: str = "cleanup") -> int:
        """Cancel only orders this bot created (in-memory + persisted IDs).

        Never cancels arbitrary open orders on the exchange that the user
        (or another bot) placed manually.

        Returns number of successful cancels.
        """
        logger.info("cleanup_orders (%s) …", reason)
        by_id: dict[str, LiveOrder] = {}
        for o in list(self.state.open_orders) + self._load_persisted_orders():
            oid = str(o.order_id or "")
            if not oid:
                continue
            # Keep sim IDs for paper bookkeeping; live cancels skip them
            by_id[oid] = o
        self.state.open_orders = list(by_id.values())

        n = await self._cancel_all(include_exchange_open=False)
        self._clear_persisted_orders()
        self.state.open_orders = []
        logger.info(
            "cleanup_orders (%s) done — %s bot order(s) canceled", reason, n
        )
        return n

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
            if not self._shutting_down:
                # Unexpected exit — cancel this bot's orders only
                await self.cleanup_orders(reason="loop-exit")
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
        await self._cancel_all(session, include_exchange_open=False)
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
        self.state.open_orders = new_orders
        self._persist_orders()

        # Paper: simulate fills when market crosses our resting prices (optional light sim)
        if paper and self.state.mid > 0:
            await self._paper_fill_check()
            self._persist_orders()

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

    async def _cancel_all(
        self,
        session: Optional[aiohttp.ClientSession] = None,
        *,
        include_exchange_open: bool = False,  # kept for API compat; always False path
    ) -> int:
        """Cancel only bot-tracked order IDs (never other users' / manual orders)."""
        del include_exchange_open  # unused — we never wipe the whole book
        paper = self.cfg.effective_paper()
        ok = 0
        tracked = list(self.state.open_orders)
        if not tracked:
            self.state.open_orders = []
            self._persist_orders()
            return 0

        if session is None:
            try:
                session = await self._session_get()
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("cancel session failed: %s", e)
                self.state.open_orders = []
                return 0

        for o in tracked:
            oid = str(o.order_id)
            if oid.startswith("sim_") or paper:
                ok += 1
                continue
            try:
                if await mexc.cancel_order(
                    session,
                    self.cfg.symbol,
                    oid,
                    api_key=self.cfg.api_key(),
                    api_secret=self.cfg.api_secret(),
                ):
                    ok += 1
                    logger.info(
                        "canceled bot order %s %s @ %s", o.side, oid, o.price
                    )
                else:
                    logger.warning("cancel returned false for bot order %s", oid)
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.warning("cancel bot order %s failed: %s", oid, e)

        self.state.open_orders = []
        self._persist_orders()
        return ok


# Process-wide engine for GUI
_ENGINE: Optional[Engine] = None
_SIGNAL_HOOKED = False
_EXIT_REQUESTED = False


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


def install_shutdown_handlers(
    loop: Optional[asyncio.AbstractEventLoop] = None,
    *,
    exit_after: bool = True,
) -> None:
    """On SIGINT/SIGTERM: cancel each bot's created orders, then exit the process.

    Important: we must not swallow SIGINT without exiting — that left the GUI
    hanging after Ctrl+C (cleanup ran, web server kept serving).
    """
    global _SIGNAL_HOOKED
    if _SIGNAL_HOOKED:
        return
    _SIGNAL_HOOKED = True

    def _handle(signum: int, _frame=None) -> None:
        global _EXIT_REQUESTED
        if _EXIT_REQUESTED:
            logger.warning("forced exit (signal %s again)", signum)
            os._exit(1)  # noqa: SLF001
        _EXIT_REQUESTED = True
        logger.info("signal %s — cancel bot-created orders and exit", signum)

        async def _clean_then_exit() -> None:
            try:
                try:
                    from lbcmm import bots as bots_mod

                    await bots_mod.cleanup_all()
                except Exception:  # pylint: disable=broad-exception-caught
                    eng = _ENGINE
                    if eng is not None:
                        await eng.cleanup_orders(reason=f"signal-{signum}")
                        eng._stop.set()  # noqa: SLF001
                        eng.state.running = False
            except Exception as e:  # pylint: disable=broad-exception-caught
                logger.error("signal cleanup failed: %s", e)
            finally:
                if exit_after:
                    os._exit(0)  # noqa: SLF001

        try:
            running_loop = asyncio.get_running_loop()
            running_loop.create_task(_clean_then_exit())
            if exit_after:
                running_loop.call_later(12.0, lambda: os._exit(0))  # noqa: SLF001
        except RuntimeError:
            try:
                asyncio.run(_clean_then_exit())
            except Exception:  # pylint: disable=broad-exception-caught
                if exit_after:
                    os._exit(0)  # noqa: SLF001

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            if loop is not None:
                loop.add_signal_handler(sig, lambda s=sig: _handle(s))
            else:
                signal.signal(sig, lambda s, f, sn=sig: _handle(sn))
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                signal.signal(sig, lambda s, f, sn=sig: _handle(sn))
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    def _atexit() -> None:
        if _EXIT_REQUESTED:
            return
        try:
            from lbcmm import bots as bots_mod

            asyncio.run(bots_mod.cleanup_all())
        except Exception:  # pylint: disable=broad-exception-caught
            eng = _ENGINE
            if eng is None or eng._shutting_down:  # noqa: SLF001
                return
            try:
                asyncio.run(eng.cleanup_orders(reason="atexit"))
            except Exception:  # pylint: disable=broad-exception-caught
                pass

    atexit.register(_atexit)
