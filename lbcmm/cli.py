"""CLI for LBC-market-making-bot."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import aiohttp

from lbcmm import __version__
from lbcmm.config import BotConfig, default_config_path, load_config, save_config
from lbcmm.connectors import mexc
from lbcmm.engine import Engine
from lbcmm.strategies.depth_provider import contribution_usd, plan_depth_orders


def _setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lbcmm",
        description="LBC market-making bot for MEXC LBC/USDT (fork of neofutur tradinebotte)",
    )
    parser.add_argument("--version", action="version", version=f"lbcmm {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help=f"config path (default: {default_config_path()})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="interactive config wizard")
    p_setup.add_argument("--non-interactive", action="store_true")

    p_run = sub.add_parser("run", help="run the bot (foreground)")
    p_run.add_argument("--paper", action="store_true", help="force paper mode")
    p_run.add_argument("--live", action="store_true", help="allow live (needs keys + confirm)")
    p_run.add_argument("--once", action="store_true", help="single tick then exit")

    sub.add_parser("status", help="show config + live public depth")
    sub.add_parser("depth", help="public ±2% depth + planned bot contribution")
    p_cancel = sub.add_parser("cancel", help="cancel tracked/open bot orders (live only)")

    p_gui = sub.add_parser("gui", help="start local web GUI")
    p_gui.add_argument("--host", default=None)
    p_gui.add_argument("--port", type=int, default=None)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    cfg_path = args.config

    if args.cmd == "setup":
        return cmd_setup(cfg_path, non_interactive=args.non_interactive)
    if args.cmd == "run":
        return asyncio.run(cmd_run(cfg_path, args))
    if args.cmd == "status":
        return asyncio.run(cmd_status(cfg_path))
    if args.cmd == "depth":
        return asyncio.run(cmd_depth(cfg_path))
    if args.cmd == "cancel":
        return asyncio.run(cmd_cancel(cfg_path))
    if args.cmd == "gui":
        return cmd_gui(cfg_path, host=args.host, port=args.port)
    return 1


def cmd_setup(cfg_path: Path | None, non_interactive: bool = False) -> int:
    path = cfg_path or default_config_path()
    cfg = load_config(path) if path.is_file() else BotConfig()
    if non_interactive:
        # Defaults only — still requires real first-time setup for GUI banner
        cfg.setup_complete = False
        save_config(cfg, path)
        print(f"Wrote default config to {path}")
        return 0
    print("LBC-market-making-bot setup")
    print("Attribution: forked neofutur's multibot design for an LBC-only bot (GPL-3.0)\n")
    cfg.usdt_budget = float(_prompt("USDT to assign (buy side)", cfg.usdt_budget))
    cfg.lbc_budget = float(_prompt("LBC to assign (sell side, coins)", cfg.lbc_budget))
    cfg.bid_depth_pct = float(_prompt("Buy depth %", cfg.bid_depth_pct))
    cfg.ask_depth_pct = float(_prompt("Sell depth %", cfg.ask_depth_pct))
    cfg.n_levels = int(_prompt("Levels per side", cfg.n_levels))
    cfg.paper = _prompt("Paper mode? [Y/n]", "Y").lower() != "n"
    _print_mexc_api_guide()
    key = _prompt(
        "MEXC Access Key (shown as Access Key on openapi; Enter = use env MEXC_API_KEY)",
        cfg.mexc_api_key,
    )
    if key:
        cfg.mexc_api_key = key
    secret = _prompt(
        "MEXC Secret Key (shown once when created; Enter = use env MEXC_API_SECRET)",
        "",
    )
    if secret:
        cfg.mexc_api_secret = secret
    if not cfg.paper:
        confirm = _prompt("Type LIVE to confirm real-money trading", "")
        cfg.live_confirmed = confirm.strip().upper() == "LIVE"
        if not cfg.live_confirmed:
            print("Live not confirmed — staying in paper mode.")
            cfg.paper = True
    cfg.setup_complete = True
    out = save_config(cfg, path)
    print(f"\nSaved {out}")
    print("Next:  python -m lbcmm depth")
    print("       python -m lbcmm run --paper")
    print("       python -m lbcmm gui")
    return 0


def _print_mexc_api_guide() -> None:
    """How to create a MEXC key safe for this bot (spot trade, no withdraw)."""
    print(
        """
── MEXC API key (optional for paper mode) ─────────────────────────────────
  Create / manage keys here:
    https://www.mexc.com/user/openapi

  When creating a key, under Spot enable ONLY these permissions:
    ☑  View Order Details
    ☑  Trade

  Also useful if available on your account:
    ☑  View Account Details   (so the bot can read balances)

  Do NOT enable:
    ☐  Withdraw / transfer / anything transfer-related

  MEXC shows two values — enter them on the next two prompts:
    1) Access Key  →  public identifier (safe to re-view later on the site)
    2) Secret Key  →  private secret (shown only once when you create the key)

  Tips:
    • Prefer binding the key to your server IP (allowlist) if MEXC offers it.
    • You can leave both prompts empty and set env vars instead:
        export MEXC_API_KEY=...      # Access Key
        export MEXC_API_SECRET=...   # Secret Key
  ─────────────────────────────────────────────────────────────────────────
"""
    )


def _prompt(label: str, default) -> str:
    d = "" if default is None else str(default)
    try:
        v = input(f"{label} [{d}]: ").strip()
    except EOFError:
        return d
    return v if v else d


async def cmd_run(cfg_path: Path | None, args) -> int:
    cfg = load_config(cfg_path) if cfg_path else load_config()
    if cfg_path and cfg_path.is_file():
        cfg = load_config(cfg_path)
    if args.paper:
        cfg.paper = True
        cfg.live_confirmed = False
    if args.live:
        cfg.paper = False
        if not cfg.live_confirmed:
            print("Refusing --live without live_confirmed=true in config (run setup).")
            return 2
        if not cfg.api_key() or not cfg.api_secret():
            print("Refusing --live: set MEXC_API_KEY and MEXC_API_SECRET.")
            return 2
        print("*** LIVE TRADING — real money on MEXC ***")

    from lbcmm.engine import get_engine, install_shutdown_handlers

    engine = get_engine(cfg)
    # CLI: cancel bot orders on SIGINT/SIGTERM, then exit
    install_shutdown_handlers(exit_after=False)
    if args.once:
        await engine.start()
        await asyncio.sleep(cfg.poll_interval_s + 1)
        print(json.dumps(engine.state.to_dict(), indent=2))
        await engine.stop()
        return 0

    await engine.start()
    print(
        f"Running ({'PAPER' if engine.state.paper else 'LIVE'}) "
        f"strategy={cfg.effective_strategy()} — Ctrl+C cancels bot orders & stops"
    )
    try:
        while engine.state.running:
            await asyncio.sleep(1)
            s = engine.state
            if s.ticks and s.ticks % 5 == 0:
                pd = s.public_depth
                bc = s.bot_contribution
                print(
                    f"mid={s.mid:.6f} orders={len(s.open_orders)} "
                    f"pub±2% bid=${pd.get('bid_usd', 0):.1f} ask=${pd.get('ask_usd', 0):.1f} "
                    f"bot±2% bid=${bc.get('bid_usd', 0):.1f} ask=${bc.get('ask_usd', 0):.1f}"
                )
    except KeyboardInterrupt:
        print("\nStopping — canceling bot-created orders…")
    finally:
        await engine.stop()
        print("Clean shutdown complete (bot orders canceled).")
    return 0


async def cmd_status(cfg_path: Path | None) -> int:
    cfg = load_config(cfg_path) if cfg_path else load_config()
    print(f"lbcmm {__version__}")
    print(f"config: {cfg_path or default_config_path()}")
    print(f"symbol: {cfg.symbol}")
    print(f"strategy: {cfg.strategy}")
    print(f"paper: {cfg.effective_paper()}")
    print(f"usdt_budget: {cfg.usdt_budget}  lbc_budget: {cfg.lbc_budget}")
    print(f"depth: buy {cfg.bid_depth_pct}% / sell {cfg.ask_depth_pct}%  levels={cfg.n_levels}")
    print(f"keys set: {bool(cfg.api_key() and cfg.api_secret())}")
    async with aiohttp.ClientSession() as session:
        book = await mexc.get_depth(session, cfg.symbol)
        if book:
            d = mexc.depth_within_pct(book, 2.0)
            print(
                f"public mid={d['mid']:.6f}  ±2% bid=${d['bid_usd']:.2f}  ask=${d['ask_usd']:.2f}"
            )
        else:
            print("public book: unavailable")
    return 0


async def cmd_depth(cfg_path: Path | None) -> int:
    cfg = load_config(cfg_path) if cfg_path else load_config()
    async with aiohttp.ClientSession() as session:
        book = await mexc.get_depth(session, cfg.symbol, limit=100)
        if not book:
            print("Could not fetch MEXC public depth.", file=sys.stderr)
            return 1
        pub = mexc.depth_within_pct(book, 2.0)
        mid = pub["mid"]
        desired = plan_depth_orders(
            mid,
            usdt_budget=cfg.usdt_budget,
            lbc_budget=cfg.lbc_budget,
            bid_depth_pct=cfg.bid_depth_pct,
            ask_depth_pct=cfg.ask_depth_pct,
            n_levels=cfg.n_levels,
            min_notional_usdt=cfg.min_notional_usdt,
        )
        bot = contribution_usd(desired, mid, 2.0)
        print(f"MEXC {cfg.symbol} mid={mid:.6f}")
        print(f"Public  ±2% depth:  bid ${pub['bid_usd']:.2f}   ask ${pub['ask_usd']:.2f}")
        print(f"This bot ±2% plan:  bid ${bot['bid_usd']:.2f}   ask ${bot['ask_usd']:.2f}")
        print(f"Community goal:     $100 each side")
        print(f"Planned orders ({len(desired)}):")
        for o in desired:
            print(f"  {o.side:4}  {o.qty:.4f} LBC @ {o.price:.6f}  (${o.usdt:.2f})")
    return 0


async def cmd_cancel(cfg_path: Path | None) -> int:
    cfg = load_config(cfg_path) if cfg_path else load_config()
    if cfg.effective_paper() or not cfg.api_key():
        print("No live credentials — nothing to cancel on exchange.")
        return 0
    async with aiohttp.ClientSession() as session:
        open_o = await mexc.get_open_orders(
            session, cfg.symbol, api_key=cfg.api_key(), api_secret=cfg.api_secret()
        )
        if open_o is None:
            print("Failed to list open orders.")
            return 1
        if not open_o:
            print("No open orders.")
            return 0
        n = 0
        for o in open_o:
            ok = await mexc.cancel_order(
                session,
                cfg.symbol,
                o["order_id"],
                api_key=cfg.api_key(),
                api_secret=cfg.api_secret(),
            )
            if ok:
                n += 1
                print(f"canceled {o['order_id']} {o['side']} @ {o['price']}")
        print(f"Canceled {n}/{len(open_o)}")
    return 0


def cmd_gui(cfg_path: Path | None, host=None, port=None) -> int:
    from lbcmm.gui.app import run_gui

    cfg = load_config(cfg_path) if cfg_path else load_config()
    if host:
        cfg.gui_host = host
    if port:
        cfg.gui_port = port
    return run_gui(cfg)
