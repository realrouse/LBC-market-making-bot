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
    "bitstamp":   "api_bitstamp",
}

_STRATEGY_REQUIREMENTS: dict[str, list[str]] = {
    "grid": [
        "get_open_orders",
        "cancel_order",
        "get_order_status",
        "get_listen_key",
        "keepalive_listen_key",
        "make_user_stream_url",
        "parse_user_stream_msg",
    ],
    "swing": [
        "post_order",
        "get_open_orders",
        "cancel_order",
        "compute_fee",
    ],
    "swinghold": [
        "post_order",
        "get_open_orders",
        "cancel_order",
        "compute_fee",
    ],
    "dca": [
        "post_order",
        "get_open_orders",
        "compute_fee",
    ],
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


def validate(connector_module: ModuleType, strategy_type: str) -> None:
    """Raise RuntimeError if connector_module is missing methods required by strategy_type."""
    missing = [
        m for m in _STRATEGY_REQUIREMENTS.get(strategy_type, [])
        if not hasattr(connector_module, m)
    ]
    if missing:
        raise RuntimeError(
            f"Connector {connector_module.__name__!r} is incompatible with "
            f"strategy_type={strategy_type!r}: missing methods: {missing}"
        )


def available() -> list[str]:
    return sorted(_REGISTRY)
