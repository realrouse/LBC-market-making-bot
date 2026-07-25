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

from .accumulation import AccumulationStrategy
from .dca       import DCAStrategy
from .grid      import GridStrategy
from .swing     import SwingStrategy
from .swinghold import SwingHoldStrategy

_REGISTRY: dict[str, type] = {
    "accumulation": AccumulationStrategy,
    # BAMM (Bullish Accumulating Market Maker) is a MODE of the accumulation engine — the class
    # switches on cfg["strategy_type"]=="bamm" internally. The dispatcher routes by strategy_type,
    # so it must map here too, else live_bot's load("bamm") raises "Unknown strategy" at boot
    # (crash-looped the real LBC bot on the 2026-07-24 cutover — the dispatcher was the gap).
    "bamm":      AccumulationStrategy,
    "dca":       DCAStrategy,
    "grid":      GridStrategy,
    "swing":     SwingStrategy,
    "swinghold": SwingHoldStrategy,
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
