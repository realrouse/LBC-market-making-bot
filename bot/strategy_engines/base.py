"""
Strategy protocol — defines the interface that all trading strategies must implement.
"""

from typing import Any, Optional, Protocol, runtime_checkable


@runtime_checkable
class Strategy(Protocol):
    """
    A strategy receives book-update events and decides when to act.

    Implementations
    ---------------
    ThresholdStrategy  (built into live_bot.py)
        Single-entry threshold signal on Polymarket binary prediction markets.
        Default when strategy_type is "threshold" or absent from config.

    GridStrategy       (bot/strategies/grid.py)
        Multi-level grid on continuous CEX markets (Binance, MEXC).
        Activated when strategy_type is "grid" in the strategy JSON.
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
