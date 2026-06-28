"""
Strategy protocol — the interface every trading strategy implements.

This is the neutral core's single strategy seam (Plan D step 3): it imports
nothing from any exchange plugin, so both the Polymarket ThresholdStrategy and
the CEX engines (grid/swing/dca) can conform to it without the core depending on
either. `strategy_engines/base.py` re-exports it for backward compatibility.
"""

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class Strategy(Protocol):
    """
    A strategy receives book-update events and decides when to act.

    Implementations
    ---------------
    ThresholdStrategy  (Polymarket plugin — built into live_bot.py)
        Single-entry threshold signal on Polymarket binary prediction markets.
        Default when strategy_type is "threshold" or absent from config.

    GridStrategy       (CEX plugin — strategy_engines/grid.py)
        Multi-level grid on continuous CEX markets (Binance, MEXC).
        Activated when strategy_type is "grid" in the strategy JSON.

    SwingStrategy      (CEX plugin — strategy_engines/swing.py)
        Limit BUY at support levels, SELL at next resistance; optional RSI filter.
        Activated when strategy_type is "swing" in the strategy JSON.
    """

    STRATEGY_TYPE: str

    async def on_book_update(
        self,
        state: Any,
        ts: Any,
        _t_ws: Optional[float] = None,
    ) -> None:
        """
        Called for every order-book update that passes the token-lookup gate.

        Must handle both entry (signal detection) and exit (resolution / fill
        detection) in one call, since the book stream is the only push source.
        """
        ...  # pylint: disable=unnecessary-ellipsis
