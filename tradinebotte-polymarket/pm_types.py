"""
Polymarket plugin — leaf data types (Plan D step 4b).

Extracted from the universal entrypoint (live_bot.py) into the Polymarket plugin so the
neutral-core direction is clear and the plugin's pieces live together. These are pure
data types with no dependency on the entrypoint; live_bot re-exports them so existing
callers are unchanged. Shipped flat beside live_bot.py (same INSTALL_DIR).
"""

import time
from collections import deque
from dataclasses import dataclass

# Rolling-window length (samples) for the per-token volatility filter.
VOL_WINDOW = 12


class TokenState:
    """
    Per-token market data updated in real time from the WebSocket feed.
    Uses __slots__ to minimize memory overhead when tracking many tokens.
    """
    __slots__ = ("token_id", "market_id", "direction", "question",
                 "market_end_ms", "market_start_ms",
                 "best_bid", "best_ask", "spread",
                 "bid_vol", "ask_vol", "obi",
                 "last_update_ts", "last_snapshot_ts",
                 "bid_history", "obi_history")

    def __init__(self, token_id: str, market_id: str, direction: str, question: str,
                 market_start_ms: int, market_end_ms: int,
                 vol_window: int = VOL_WINDOW) -> None:
        self.token_id = token_id
        self.market_id = market_id
        self.direction = direction
        self.question = question
        self.market_start_ms = market_start_ms
        self.market_end_ms = market_end_ms
        # Start bid/ask at 0.5 (fair coin flip) until the first WebSocket update
        # arrives. This prevents spurious signals from uninitialized state.
        self.best_bid = 0.5; self.best_ask = 0.5; self.spread = 0.0
        # ask_vol=0.0 before the first snapshot — the signal guard checks this
        # explicitly to avoid entering trades with no visible ask-side liquidity.
        self.bid_vol = 0.0; self.ask_vol = 0.0; self.obi = 0.0
        self.last_update_ts = 0.0; self.last_snapshot_ts = 0.0
        # Rolling windows sampled every SNAPSHOT_INTERVAL seconds.
        # Used by the volatility filter in check_signal to detect choppy markets.
        self.bid_history: deque[float] = deque(maxlen=vol_window)
        self.obi_history: deque[float] = deque(maxlen=vol_window)

    @property
    def secs_remaining(self) -> float:
        """Seconds until the market's scheduled end time (0 if already past)."""
        if not self.market_end_ms: return 9999.0
        return max(0.0, (self.market_end_ms - time.time() * 1000) / 1000.0)

    @property
    def seconds_elapsed(self) -> float:
        """Seconds since the market's scheduled start time."""
        if not self.market_start_ms: return 0.0
        return max(0.0, (time.time() * 1000 - self.market_start_ms) / 1000.0)

    @property
    def market_ended(self) -> bool:
        """
        True if the market is past its end time plus a 5-second grace period.
        The grace period prevents treating a market as ended due to clock skew.
        """
        return self.market_end_ms > 0 and time.time() * 1000 > self.market_end_ms + 5000


@dataclass
class RejectionStats:
    """Counts of check_signal() early-exits per reason, logged every 60 s."""
    signalled:     int = 0
    market_ended:  int = 0
    trading_hour:  int = 0
    best_bid:      int = 0
    entry_max:     int = 0
    best_ask:      int = 0
    ask_vol:       int = 0
    secs_remaining: int = 0
    obi:           int = 0
    vol_filter:    int = 0
    capital:       int = 0
    daily_stop:    int = 0
    weekly_stop:   int = 0
    api_cooldown:  int = 0
