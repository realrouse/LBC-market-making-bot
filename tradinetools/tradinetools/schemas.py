"""
Versioned ZMQ message schemas for all tradinebotte services.

Schema version is a separate field "v" (int), independent of type "t".
This allows filtering on type alone while bumping version without breaking subscribers.

Example wire format:
    {"t": "book", "v": 1, "token_id": "...", "best_bid": 0.97, ...}
    {"t": "indicators", "v": 1, "stream_id": "btc_scalping_spot", ...}

Version history:
    v1 — initial versioned schemas (2026-05-28)
"""

from __future__ import annotations
from dataclasses import dataclass, asdict, field
from typing import Any, get_type_hints


# ─── Base ────────────────────────────────────────────────────────────────────

@dataclass
class _Base:
    v: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "_Base":
        hints = get_type_hints(cls)
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]  # pylint: disable=no-member
        _COERCE: dict[type, type] = {int: int, float: float, str: str, bool: bool}
        kwargs: dict[str, Any] = {}
        for k, v in d.items():
            if k not in known or v is None:
                continue
            target = hints.get(k)
            coerce = _COERCE.get(target)  # type: ignore[arg-type]
            if coerce is not None and not isinstance(v, target):  # type: ignore[arg-type]
                try:
                    v = coerce(v)
                except (TypeError, ValueError):
                    continue
            kwargs[k] = v
        return cls(**kwargs)


# ─── Polymarket feed messages (feed.py → account_bot.py) ─────────────────────

@dataclass
class MarketMessage(_Base):
    t: str = "market"
    market_id: str = ""
    question: str = ""
    up_token_id: str = ""
    dn_token_id: str = ""
    start_ms: int = 0
    end_ms: int = 0


@dataclass
class BookMessage(_Base):
    t: str = "book"
    token_id: str = ""
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    bid_vol: float = 0.0
    ask_vol: float = 0.0
    obi: float = 0.0


@dataclass
class PingMessage(_Base):
    t: str = "ping"
    ts: int = 0


# ─── Indicators messages (indicators.py → bots) ──────────────────────────────

@dataclass
class IndicatorsMessage(_Base):
    """
    Generic indicators message. Extra fields (rsi_14, funding_rate, dvol, ...)
    are carried in `payload` and serialized via to_dict().
    """
    t: str = "indicators"
    stream_id: str = ""
    ts: int = 0
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {"t": self.t, "v": self.v, "stream_id": self.stream_id, "ts": self.ts}
        d.update(self.payload)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IndicatorsMessage":
        reserved = {"t", "v", "stream_id", "ts"}
        payload = {k: v for k, v in d.items() if k not in reserved}
        return cls(
            v=int(d.get("v", 1)),
            stream_id=str(d.get("stream_id", "")),
            ts=int(d.get("ts", 0)),
            payload=payload,
        )
