"""Local web GUI — aiohttp (already a project dependency)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web

from lbcmm.config import BotConfig
from lbcmm.connectors import mexc
from lbcmm.strategies.bamm import build_buy_grid
from lbcmm.strategies.depth_provider import contribution_usd, plan_depth_orders
from lbcmm.strategies.grid import plan_grid_orders

logger = logging.getLogger("lbcmm.gui")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _use_color() -> bool:
    """Color only on a real TTY; respect NO_COLOR. Use os.environ (not sys.environ)
    — some restricted runtimes expose a stripped sys without environ."""
    try:
        tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    except Exception:  # pylint: disable=broad-exception-caught
        tty = False
    if not tty:
        return False
    return not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    if not _use_color():
        return text
    return f"\033[{code}m{text}\033[0m"


def _print_gui_banner(host: str, port: int) -> None:
    url = f"http://{host}:{port}/"
    green, cyan, yellow, dim, white = "1;32", "1;36", "1;33", "2", "1;37"
    bar = _c(green, "═" * 64)
    print()
    print(bar)
    print(_c("1", "  LBC-market-making-bot  ·  Control Panel"))
    print(bar)
    print()
    print(_c(yellow, "  Open this link in your browser:"))
    print()
    print(f"      {_c(cyan, url)}")
    print()
    print(_c(dim, "      Copy-paste into Chrome / Firefox / Safari if needed."))
    print()
    print(_c(yellow, "  In the browser:"))
    print(_c(white, "      • Complete first-time setup if asked (paper or live)"))
    print(_c(white, "      • Set USDT / LBC, then press Start"))
    print()
    print(_c(white, "  Keep this terminal open.  Press Ctrl+C here to quit."))
    print()
    print(_c(dim, "  Forked from neofutur’s multibot design · GPL-3.0"))
    print(bar)
    print()


def run_gui(cfg: BotConfig) -> int:
    from lbcmm import bots as bots_mod
    from lbcmm.engine import install_shutdown_handlers

    active_id = bots_mod.init_registry(cfg)
    app = web.Application()
    app["base_cfg"] = cfg
    app["active_id"] = active_id

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/bots", handle_list_bots)
    app.router.add_post("/api/bots", handle_create_bot)
    app.router.add_post("/api/bots/rename", handle_rename_bot)
    app.router.add_post("/api/bots/delete", handle_delete_bot)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/market", handle_market)
    app.router.add_post("/api/config", handle_config)
    app.router.add_post("/api/start", handle_start)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)

    async def _on_startup(_app: web.Application) -> None:
        install_shutdown_handlers(asyncio.get_running_loop(), exit_after=True)

    async def _on_cleanup(_app: web.Application) -> None:
        logger.info("GUI cleanup — canceling bot-created orders for all bots")
        try:
            await bots_mod.cleanup_all()
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("GUI cleanup failed: %s", e)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    host = cfg.gui_host or "127.0.0.1"
    port = int(cfg.gui_port or 8787)
    _print_gui_banner(host, port)
    web.run_app(app, host=host, port=port, print=None, handle_signals=False)
    return 0


def _bot_id_from_request(request: web.Request, body: Optional[dict] = None) -> str:
    from lbcmm import bots as bots_mod

    bid = ""
    if body and body.get("bot_id"):
        bid = str(body["bot_id"])
    if not bid:
        bid = str(request.query.get("bot_id") or "")
    if not bid:
        bots = bots_mod.list_bots()
        bid = bots[0]["id"] if bots else ""
    return bid


async def handle_index(_request: web.Request) -> web.Response:
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html")


def _cfg_payload(cfg: BotConfig, bot_id: str = "") -> dict:
    return {
        "bot_id": bot_id or cfg.bot_id,
        "usdt_budget": cfg.usdt_budget,
        "lbc_budget": cfg.lbc_budget,
        "bid_depth_pct": cfg.bid_depth_pct,
        "ask_depth_pct": cfg.ask_depth_pct,
        "n_levels": cfg.n_levels,
        "strategy": cfg.effective_strategy(),
        "strategies_unlocked": bool(cfg.strategies_unlocked),
        "paper": cfg.effective_paper(),
        "advanced": cfg.advanced,
        "symbol": cfg.symbol,
        "has_keys": bool(cfg.api_key() and cfg.api_secret()),
        "live_confirmed": cfg.live_confirmed,
        "setup_complete": bool(cfg.setup_complete),
        "min_notional_usdt": cfg.min_notional_usdt,
        "reprice_pct": cfg.reprice_pct,
        "poll_interval_s": cfg.poll_interval_s,
        "access_key_set": bool(cfg.api_key()),
        "secret_key_set": bool(cfg.api_secret()),
    }


async def handle_list_bots(_request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    return web.json_response({"ok": True, "bots": bots_mod.list_bots()})


async def handle_create_bot(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-exception-caught
        body = {}
    name = (body or {}).get("name")
    clone = (body or {}).get("clone_from")
    bid = bots_mod.create_bot(name=name, clone_from=clone)
    return web.json_response({"ok": True, "bot_id": bid, "bots": bots_mod.list_bots()})


async def handle_rename_bot(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-exception-caught
        return web.json_response({"error": "invalid json"}, status=400)
    bid = str(body.get("bot_id") or "")
    name = str(body.get("name") or "")
    try:
        bots_mod.rename_bot(bid, name)
    except KeyError:
        return web.json_response({"error": "unknown bot"}, status=404)
    return web.json_response({"ok": True, "bots": bots_mod.list_bots()})


async def handle_delete_bot(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-exception-caught
        return web.json_response({"error": "invalid json"}, status=400)
    bid = str(body.get("bot_id") or "")
    cancel = body.get("cancel_orders", True)
    try:
        await bots_mod.delete_bot(bid, cancel_orders=bool(cancel))
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)
    except KeyError:
        return web.json_response({"error": "unknown bot"}, status=404)
    return web.json_response({"ok": True, "bots": bots_mod.list_bots()})


async def handle_state(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    bid = _bot_id_from_request(request)
    try:
        engine = bots_mod.get_engine(bid)
        cfg = engine.cfg
    except KeyError:
        return web.json_response({"error": "unknown bot"}, status=404)
    payload = engine.state.to_dict()
    payload["config"] = _cfg_payload(cfg, bid)
    payload["bot_id"] = bid
    payload["bots"] = bots_mod.list_bots()
    return web.json_response(payload)


async def handle_market(request: web.Request) -> web.Response:
    """Public book + planned contribution for the selected bot."""
    from lbcmm import bots as bots_mod

    bid = _bot_id_from_request(request)
    try:
        cfg = bots_mod.get_cfg(bid)
    except KeyError:
        return web.json_response({"ok": False, "error": "unknown bot"}, status=404)
    try:
        async with aiohttp.ClientSession() as session:
            book = await mexc.get_depth(session, cfg.symbol, limit=1000)
    except Exception as e:  # pylint: disable=broad-exception-caught
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    if not book:
        return web.json_response({"ok": False, "error": "book unavailable"}, status=502)

    pub = mexc.depth_within_pct(book, 2.0)
    ladder = mexc.depth_ladder(book)
    mid = pub.get("mid") or 0.0
    desired = _plan_for_cfg(cfg, mid)
    bot = contribution_usd(desired, mid, 2.0)
    return web.json_response({
        "ok": True,
        "bot_id": bid,
        "symbol": cfg.symbol,
        "mid": mid,
        "best_bid": book.get("best_bid"),
        "best_ask": book.get("best_ask") if book.get("best_ask") != float("inf") else None,
        "public_depth": pub,
        "public_depth_ladder": ladder,
        "bot_contribution": bot,
        "desired": [
            {
                "side": o.side,
                "price": o.price,
                "qty": o.qty,
                "usdt": o.usdt,
                "level": o.level,
            }
            for o in desired
        ],
        "goal_usd": 100.0,
        "config": _cfg_payload(cfg, bid),
        "bots": bots_mod.list_bots(),
    })


def _plan_for_cfg(cfg: BotConfig, mid: float):
    if mid <= 0:
        return []
    strat = cfg.effective_strategy()
    if strat == "grid":
        lower = cfg.grid_lower if cfg.grid_lower > 0 else mid * 0.95
        upper = cfg.grid_upper if cfg.grid_upper > 0 else mid * 1.05
        return plan_grid_orders(
            mid,
            lower=lower,
            upper=upper,
            levels=cfg.grid_levels,
            order_size_usdt=cfg.grid_order_size_usdt,
            min_notional_usdt=cfg.min_notional_usdt,
        )
    if strat == "bamm":
        top = cfg.bamm_top if cfg.bamm_top > 0 else mid * 1.05
        rungs = build_buy_grid(
            top=top,
            floor=cfg.bamm_floor,
            step_pct=cfg.bamm_step_pct,
            budget_usdt=cfg.usdt_budget,
            min_notional_usdt=cfg.min_notional_usdt,
        )
        from lbcmm.strategies.depth_provider import DesiredOrder

        return [
            DesiredOrder(
                side="BUY",
                price=r.price,
                qty=r.coins,
                usdt=r.usdt,
                level=i,
            )
            for i, r in enumerate(rungs)
            if r.price < mid
        ]
    return plan_depth_orders(
        mid,
        usdt_budget=cfg.usdt_budget,
        lbc_budget=cfg.lbc_budget,
        bid_depth_pct=cfg.bid_depth_pct,
        ask_depth_pct=cfg.ask_depth_pct,
        n_levels=cfg.n_levels,
        min_notional_usdt=cfg.min_notional_usdt,
    )


def _apply_body(cfg: BotConfig, body: dict) -> None:
    if "usdt_budget" in body:
        cfg.usdt_budget = max(0.0, float(body["usdt_budget"]))
    if "lbc_budget" in body:
        cfg.lbc_budget = max(0.0, float(body["lbc_budget"]))
    if "bid_depth_pct" in body:
        # Expert may set very wide bands (sells far above mid). Soft cap 10000% = 100× mid.
        cfg.bid_depth_pct = max(0.1, min(10000.0, float(body["bid_depth_pct"])))
    if "ask_depth_pct" in body:
        cfg.ask_depth_pct = max(0.1, min(10000.0, float(body["ask_depth_pct"])))
    if "n_levels" in body:
        # Expert steps limited in-engine by $1/order budget, not a hard 30
        cfg.n_levels = max(1, min(100_000, int(body["n_levels"])))
    # Strategy locked to depth_provider until strategies_unlocked (untested otherwise)
    if cfg.strategies_unlocked and "strategy" in body and body["strategy"] in (
        "depth_provider", "bamm", "grid",
    ):
        cfg.strategy = body["strategy"]
    else:
        cfg.strategy = "depth_provider"
    if "advanced" in body:
        cfg.advanced = bool(body["advanced"])
    if "min_notional_usdt" in body:
        # Floor at $1 — exchange min notional; never allow sub-dollar resting clips
        cfg.min_notional_usdt = max(1.0, float(body["min_notional_usdt"]))
    if "reprice_pct" in body:
        cfg.reprice_pct = max(0.05, float(body["reprice_pct"]))
    if "poll_interval_s" in body:
        cfg.poll_interval_s = max(1.0, float(body["poll_interval_s"]))
    if "paper" in body:
        cfg.paper = bool(body["paper"])
        if cfg.paper:
            cfg.live_confirmed = False
    if body.get("live_confirm") is True:
        cfg.paper = False
        cfg.live_confirmed = True
    # Optional key updates (empty string ignored so we don't wipe secrets)
    if body.get("mexc_api_key"):
        cfg.mexc_api_key = str(body["mexc_api_key"]).strip()
    if body.get("mexc_api_secret"):
        cfg.mexc_api_secret = str(body["mexc_api_secret"]).strip()
    if "setup_complete" in body:
        cfg.setup_complete = bool(body["setup_complete"])


async def handle_config(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-exception-caught
        return web.json_response({"error": "invalid json"}, status=400)

    bid = _bot_id_from_request(request, body)
    try:
        cfg = bots_mod.get_cfg(bid)
    except KeyError:
        return web.json_response({"error": "unknown bot"}, status=404)

    _apply_body(cfg, body)
    bots_mod.set_cfg(bid, cfg)
    return web.json_response({
        "ok": True,
        "bot_id": bid,
        "paper": cfg.effective_paper(),
        "config": _cfg_payload(cfg, bid),
        "bots": bots_mod.list_bots(),
    })


async def handle_start(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-exception-caught
        body = {}

    bid = _bot_id_from_request(request, body)
    try:
        engine = bots_mod.get_engine(bid)
        cfg = engine.cfg
    except KeyError:
        return web.json_response({"ok": False, "error": "unknown bot"}, status=404)

    if not cfg.setup_complete:
        return web.json_response(
            {"ok": False, "error": "Complete first-time setup before starting."},
            status=400,
        )
    if body:
        _apply_body(cfg, body)
        bots_mod.set_cfg(bid, cfg)
        cfg = engine.cfg

    min_n = max(1.0, float(cfg.min_notional_usdt or 1.0))
    buy_ok = cfg.usdt_budget >= min_n
    sell_ok = cfg.lbc_budget > 0
    if not buy_ok and not sell_ok:
        return web.json_response(
            {
                "ok": False,
                "error": (
                    f"Nothing to place: need at least ${min_n:.0f} USDT for buys "
                    "and/or enough LBC for sells (≥ $1 notional per order)."
                ),
            },
            status=400,
        )

    if not engine.state.running:
        await engine.start()
    return web.json_response({
        "ok": True,
        "running": True,
        "bot_id": bid,
        "bots": bots_mod.list_bots(),
    })


async def handle_stop(request: web.Request) -> web.Response:
    from lbcmm import bots as bots_mod

    try:
        body = await request.json()
    except Exception:  # pylint: disable=broad-exception-caught
        body = {}
    bid = _bot_id_from_request(request, body)
    # Default: cancel this bot's orders. cancel_orders=false leaves them on the book.
    cancel = True if body.get("cancel_orders") is None else bool(body.get("cancel_orders"))
    try:
        engine = bots_mod.get_engine(bid)
    except KeyError:
        return web.json_response({"ok": False, "error": "unknown bot"}, status=404)
    await engine.stop(cancel_orders=cancel)
    return web.json_response({
        "ok": True,
        "running": False,
        "bot_id": bid,
        "canceled_orders": cancel,
        "bots": bots_mod.list_bots(),
    })
