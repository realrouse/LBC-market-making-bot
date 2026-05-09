"""
Connector factory — maps exchange names to their api_* modules.

Each connector module exposes the same interface:
    parse_book_update(msg)          → dict | None
    get_markets(session, ...)       → list[dict]
    post_order(session, ...)        → str | None
    compute_fee(price, qty)         → float
    make_subscribe_msg(symbols)     → str
    get_market_id/question/...      → str
    WS_URL                          → str
    WS_BATCH_SIZE                   → int

Usage from live_bot.py:
    from connectors import load as load_connector
    # replaces the module-level `api` global:
    _load_connector(config.connector)
"""

import importlib
from types import ModuleType

_REGISTRY: dict[str, str] = {
    "polymarket": "api_polymarket",
    "binance":    "api_binance",
    "mexc":       "api_mexc",
}


def load(name: str) -> ModuleType:
    """Return the api_* module for the given connector name."""
    module_name = _REGISTRY.get(name)
    if module_name is None:
        raise ValueError(
            f"Unknown connector: {name!r}. "
            f"Valid connectors: {sorted(_REGISTRY)}"
        )
    return importlib.import_module(module_name)


def available() -> list[str]:
    return sorted(_REGISTRY)
