"""
Strategy factory — maps strategy names to their implementation classes.

Usage from live_bot.py:
    from strategy_engines import load as load_strategy
    state.strategy = load_strategy(config.strategy_type, config)
    # Returns None for "threshold" (live_bot uses its built-in logic).
    # Returns a GridStrategy instance for "grid".
"""

from __future__ import annotations
from typing import Any, Optional

from .grid import GridStrategy

_REGISTRY: dict[str, type] = {
    "grid": GridStrategy,
}


def load(name: str, config: Any) -> Optional[Any]:
    """
    Return an initialised strategy instance, or None for the default threshold.

    "threshold" is the built-in Polymarket strategy implemented directly in
    live_bot.py via check_signal() / check_resolution().  Returning None
    signals live_bot to use those module-level functions instead of
    dispatching through a strategy object.
    """
    if name == "threshold":
        return None
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(
            f"Unknown strategy: {name!r}. "
            f"Valid names: threshold, {', '.join(sorted(_REGISTRY))}"
        )
    return cls(config)


def available() -> list[str]:
    """Return all registered strategy names, including the built-in threshold."""
    return ["threshold"] + sorted(_REGISTRY)
