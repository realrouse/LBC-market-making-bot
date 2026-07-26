"""Multi-bot registry — several independent engines in one GUI process."""

from __future__ import annotations

import json
import logging
import secrets
import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from lbcmm.config import BotConfig, default_config_path, load_config, save_config
from lbcmm.engine import Engine

logger = logging.getLogger("lbcmm.bots")

_REGISTRY: dict[str, Engine] = {}
_META: dict[str, dict[str, Any]] = {}  # id -> {name, created_at}
_BASE: Optional[BotConfig] = None


def _data_dir(cfg: Optional[BotConfig] = None) -> Path:
    c = cfg or _BASE or BotConfig()
    return c.resolve_data_dir()


def _registry_path() -> Path:
    return _data_dir() / "bots_registry.json"


def _new_id() -> str:
    return secrets.token_hex(4)


def init_registry(base_cfg: BotConfig) -> str:
    """Load or create bots; return active bot id."""
    global _BASE
    _BASE = base_cfg
    path = _registry_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            bots = raw.get("bots") or []
            active = raw.get("active_id") or ""
            for b in bots:
                bid = str(b.get("id") or _new_id())
                name = str(b.get("name") or f"Bot {bid}")
                cfg = _cfg_from_dict(base_cfg, b)
                cfg.bot_id = bid
                eng = Engine(cfg)
                _REGISTRY[bid] = eng
                _META[bid] = {
                    "name": name,
                    "created_at": b.get("created_at") or time.time(),
                }
            if active in _REGISTRY:
                return active
            if _REGISTRY:
                return next(iter(_REGISTRY))
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.warning("bots registry load failed: %s", e)

    # Default single bot from base config
    bid = _new_id()
    base_cfg.bot_id = bid
    _REGISTRY[bid] = Engine(base_cfg)
    _META[bid] = {"name": "Bot 1", "created_at": time.time()}
    _persist()
    return bid


def _cfg_from_dict(base: BotConfig, d: dict) -> BotConfig:
    cfg = deepcopy(base)
    for k in (
        "usdt_budget",
        "lbc_budget",
        "bid_depth_pct",
        "ask_depth_pct",
        "n_levels",
        "min_notional_usdt",
        "reprice_pct",
        "poll_interval_s",
        "strategy",
        "paper",
        "live_confirmed",
        "setup_complete",
        "symbol",
        "advanced",
    ):
        if k in d and d[k] is not None:
            setattr(cfg, k, d[k])
    # Shared credentials always from base/env
    cfg.mexc_api_key = base.mexc_api_key
    cfg.mexc_api_secret = base.mexc_api_secret
    return cfg


def _persist() -> None:
    bots = []
    for bid, eng in _REGISTRY.items():
        meta = _META.get(bid) or {}
        cfg = eng.cfg
        bots.append(
            {
                "id": bid,
                "name": meta.get("name") or f"Bot {bid}",
                "created_at": meta.get("created_at") or time.time(),
                "usdt_budget": cfg.usdt_budget,
                "lbc_budget": cfg.lbc_budget,
                "bid_depth_pct": cfg.bid_depth_pct,
                "ask_depth_pct": cfg.ask_depth_pct,
                "n_levels": cfg.n_levels,
                "min_notional_usdt": cfg.min_notional_usdt,
                "reprice_pct": cfg.reprice_pct,
                "poll_interval_s": cfg.poll_interval_s,
                "strategy": cfg.strategy,
                "paper": cfg.paper,
                "live_confirmed": cfg.live_confirmed,
                "setup_complete": cfg.setup_complete,
                "symbol": cfg.symbol,
                "advanced": cfg.advanced,
            }
        )
    active = next(iter(_REGISTRY), "")
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"active_id": active, "bots": bots}, indent=2),
        encoding="utf-8",
    )


def list_bots() -> list[dict[str, Any]]:
    out = []
    for bid, eng in _REGISTRY.items():
        meta = _META.get(bid) or {}
        st = eng.state
        out.append(
            {
                "id": bid,
                "name": meta.get("name") or f"Bot {bid}",
                "running": st.running,
                "status_msg": st.status_msg,
                "paper": eng.cfg.effective_paper(),
                "usdt_budget": eng.cfg.usdt_budget,
                "lbc_budget": eng.cfg.lbc_budget,
                "bid_depth_pct": eng.cfg.bid_depth_pct,
                "n_levels": eng.cfg.n_levels,
                "open_orders": len(st.open_orders),
            }
        )
    return out


def get_engine(bot_id: str) -> Engine:
    if bot_id not in _REGISTRY:
        raise KeyError(f"unknown bot_id {bot_id}")
    return _REGISTRY[bot_id]


def get_cfg(bot_id: str) -> BotConfig:
    return get_engine(bot_id).cfg


def create_bot(name: Optional[str] = None, clone_from: Optional[str] = None) -> str:
    if not _BASE:
        raise RuntimeError("registry not initialized")
    bid = _new_id()
    if clone_from and clone_from in _REGISTRY:
        cfg = deepcopy(_REGISTRY[clone_from].cfg)
    else:
        cfg = deepcopy(_BASE)
        # sensible empty defaults for a fresh tab
        cfg.usdt_budget = 10.0
        cfg.lbc_budget = 0.0
        cfg.bid_depth_pct = 2.0
        cfg.ask_depth_pct = 2.0
        cfg.n_levels = 4
    cfg.bot_id = bid
    n = len(_REGISTRY) + 1
    label = (name or "").strip() or f"Bot {n}"
    _REGISTRY[bid] = Engine(cfg)
    _META[bid] = {"name": label, "created_at": time.time()}
    _persist()
    logger.info("created bot %s (%s)", bid, label)
    return bid


async def delete_bot(bot_id: str, *, cancel_orders: bool = True) -> None:
    if bot_id not in _REGISTRY:
        return
    if len(_REGISTRY) <= 1:
        raise ValueError("Cannot delete the last bot")
    eng = _REGISTRY[bot_id]
    if eng.state.running:
        await eng.stop(cancel_orders=cancel_orders)
    elif cancel_orders:
        await eng.cleanup_orders(reason="delete-bot")
    del _REGISTRY[bot_id]
    _META.pop(bot_id, None)
    _persist()


def rename_bot(bot_id: str, name: str) -> None:
    if bot_id not in _META:
        raise KeyError(bot_id)
    _META[bot_id]["name"] = (name or "").strip() or _META[bot_id]["name"]
    _persist()


def set_cfg(bot_id: str, cfg: BotConfig) -> None:
    eng = get_engine(bot_id)
    cfg.bot_id = bot_id
    # keep shared credentials from base
    if _BASE:
        if not cfg.mexc_api_key:
            cfg.mexc_api_key = _BASE.mexc_api_key
        if not cfg.mexc_api_secret:
            cfg.mexc_api_secret = _BASE.mexc_api_secret
    eng.update_config(cfg)
    _persist()
    # also mirror first bot / shared flags to main config.toml for CLI compatibility
    try:
        if _BASE:
            _BASE.setup_complete = cfg.setup_complete
            _BASE.paper = cfg.paper
            _BASE.live_confirmed = cfg.live_confirmed
            if cfg.mexc_api_key:
                _BASE.mexc_api_key = cfg.mexc_api_key
            if cfg.mexc_api_secret:
                _BASE.mexc_api_secret = cfg.mexc_api_secret
            save_config(_BASE)
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.warning("mirror base config: %s", e)


async def cleanup_all() -> None:
    for bid, eng in list(_REGISTRY.items()):
        try:
            if eng.state.running:
                await eng.stop(cancel_orders=True)
            else:
                await eng.cleanup_orders(reason="app-exit")
        except Exception as e:  # pylint: disable=broad-exception-caught
            logger.error("cleanup bot %s: %s", bid, e)
