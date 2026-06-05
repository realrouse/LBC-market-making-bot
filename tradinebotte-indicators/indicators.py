#!/usr/bin/env python3
"""
indicators.py — Technical indicator service

SUB to feed.py (ZeroMQ) and/or Binance WebSocket kline streams → compute
RSI / SMA / EMA / volatility → PUB enriched messages on a second socket.

A REP socket accepts dynamic stream-registration requests from any bot at
runtime so new indicator streams can be added without restarting the service.

Eight data sources are supported, configured via a JSON file:

  source="feed"                  — subscribes to feed.py PUB (best_bid tick-by-tick,
                                   one PriceSeries per Polymarket token)
  source="binance_ws"            — opens a Binance kline WebSocket for one asset/
                                   timeframe; seeds from REST on startup; pushes the
                                   close price of each completed candle
  source="binance_funding"       — polls Binance perp funding rate (REST, every 15 min
                                   by default); no indicator math required
  source="deribit_iv"            — polls Deribit DVOL implied volatility index (REST,
                                   every 5 min by default); no indicator math required
  source="fear_greed"            — polls Alternative.me Fear & Greed Index (REST,
                                   every 1 h by default); no indicator math required
  source="binance_vwap_context"  — polls Binance klines hourly; computes VWAP of the
                                   last N 4h candles and publishes dip_score =
                                   (vwap - price) / vwap as a price-context filter
  source="binance_volume_profile"— polls Binance 5m klines hourly; builds a taker
                                   buy/sell volume profile by price bucket; publishes
                                   price_zone ("buy_hvn"|"sell_hvn"|"neutral") and
                                   zone_score for the current price bucket
  source="binance_macro_obi"     — polls Binance 1m klines every minute; computes an
                                   EMA-smoothed macro OBI from taker_buy_ratio as a
                                   trend-direction filter; publishes macro_obi and
                                   macro_obi_direction ("bullish"|"neutral"|"bearish")
  source="binance_full_depth"   — maintains a full spot order book (up to 5000 levels)
                                   via REST snapshot + incremental WS diffs; publishes
                                   OBI at multiple depths (obi_10/100/500), cumulative
                                   bid/ask volume within N% of mid, largest bid/ask walls,
                                   best bid/ask, spread_bps; reconnects + resyncs on drop

Output messages (PUB socket):
  source="feed":
    {"t":"indicators", "token_id":"...", "stream_id":"...", "ts":...,
     "rsi_14":..., ...}
  source="binance_ws":
    {"t":"indicators", "asset":"BTCUSDT", "timeframe":"4h", "stream_id":"...",
     "ts":..., "rsi_14":..., "vol_20":...}
  source="binance_funding":
    {"t":"indicators", "stream_id":"btc_funding",
     "funding_rate":0.0001, "next_funding_ms":1746000000000, "ts":...}
  source="deribit_iv":
    {"t":"indicators", "stream_id":"btc_dvol", "dvol":62.5, "ts":...}
  source="fear_greed":
    {"t":"indicators", "stream_id":"fear_greed",
     "fear_greed":72, "fear_greed_label":"Greed", "ts":...}

Dynamic registration (REP socket):
  Request:  {"cmd":"subscribe", "asset":"BTCUSDT", "timeframe":"4h",
             "source":"binance_ws", "indicators":[{"type":"rsi","period":14}]}
  Response: {"status":"ok", "stream_id":"btc_4h"}
         or {"status":"error", "message":"..."}

Usage:
  python3 tradinebotte-indicators/indicators.py --config tradinebotte-indicators/strategies/indicators.json
  python3 tradinebotte-indicators/indicators.py          # legacy: CLI flags, ZMQ feed source
  python3 tradinebotte-indicators/indicators.py --rsi 14 --sma 20 --ema 9 --vol 20
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 python3 tradinebotte-indicators/indicators.py \\
      --config tradinebotte-indicators/strategies/indicators.json
"""

import argparse, asyncio, json, logging, math, os, sqlite3, sys, time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, NamedTuple

import aiohttp, websockets, zmq, zmq.asyncio

from tradinetools.zmq import make_pub, make_sub, make_rep
from tradinetools.math import (atr_last, bollinger_last, vwap_last,
                                vol_zscore_last, rolling_max_last)
from tradinetools.logging import setup_logger

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
# TRADINEBOTTE_PORT_BASE shifts the entire default port layout uniformly.
# Default layout (base=5557): feed=5557, indicators PUB=5559, indicators REP=5561.
# Example — run a second independent stack on base 6557:
#   TRADINEBOTTE_PORT_BASE=6557 python3 tradinebotte-indicators/indicators.py --config ...
# Per-service env vars still override PORT_BASE when set explicitly.
_PORT_BASE  = int(os.environ.get("TRADINEBOTTE_PORT_BASE", "5557"))
_PORT_SHIFT = _PORT_BASE - 5557  # 0 when using defaults

_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR",
                             f"tcp://127.0.0.1:{_PORT_BASE}")
_IND_ADDR  = os.environ.get("TRADINEBOTTE_INDICATORS_ADDR",
                             f"tcp://127.0.0.1:{_PORT_BASE + 2}")
_REG_ADDR  = os.environ.get("TRADINEBOTTE_INDICATORS_REG_ADDR",
                             f"tcp://127.0.0.1:{_PORT_BASE + 4}")
_BINANCE_REST_URL             = "https://api.binance.com/api/v3/klines"
_BINANCE_WS_BASE              = "wss://stream.binance.com:9443/ws"
_BINANCE_SPOT_COMBINED_WS     = "wss://stream.binance.com:9443/stream"
_BINANCE_PERP_COMBINED_WS     = "wss://fstream.binance.com/stream"
_BINANCE_FUTURES_URL          = "https://fapi.binance.com/fapi/v1/premiumIndex"
_BINANCE_OI_HIST_URL      = "https://fapi.binance.com/futures/data/openInterestHist"
_BINANCE_LS_RATIO_URL     = "https://fapi.binance.com/futures/data/topLongShortAccountRatio"
_BINANCE_TICKER_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
_BINANCE_SPOT_DEPTH_URL    = "https://api.binance.com/api/v3/depth"
_BINANCE_FUTURES_DEPTH_URL = "https://fapi.binance.com/fapi/v1/depth"
_DERIBIT_DVOL_URL         = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
_FEAR_GREED_URL           = "https://api.alternative.me/fng/"
_WS_RECV_TIMEOUT_S        = 120   # force reconnect if no WS message in this many seconds
_INSTALL_DIR = os.environ.get("TRADINEBOTTE_DIR", os.getcwd())


def _shift_addr(addr: str) -> str:
    """Apply _PORT_SHIFT to a tcp://host:port address. No-op when shift is 0."""
    if _PORT_SHIFT == 0 or not addr.startswith("tcp://"):
        return addr
    host, port_str = addr.rsplit(":", 1)
    return f"{host}:{int(port_str) + _PORT_SHIFT}"

logger = setup_logger("indicators", os.path.join(_INSTALL_DIR, "indicators.log"))

VERBOSE = False


# ─── INDICATOR MATH (pure stdlib) ────────────────────────────────────────────

def compute_sma(prices: list[float], n: int) -> float | None:
    """Simple moving average of the last n prices."""
    if len(prices) < n:
        return None
    return sum(prices[-n:]) / n


def compute_ema(prices: list[float], n: int) -> float | None:
    """Exponential moving average(n), seeded with SMA on first n prices."""
    if len(prices) < n:
        return None
    k = 2.0 / (n + 1)
    ema = sum(prices[:n]) / n
    for p in prices[n:]:
        ema = p * k + ema * (1.0 - k)
    return ema


def compute_rsi(prices: list[float], n: int = 14) -> float | None:
    """Cutler RSI(n): simple-mean gains/losses over the last n bars. Returns 0–100 or None."""
    if len(prices) < n + 1:
        return None
    deltas = [prices[i + 1] - prices[i] for i in range(len(prices) - 1)]
    recent = deltas[-n:]
    avg_gain = sum(d for d in recent if d > 0) / n
    avg_loss = sum(-d for d in recent if d < 0) / n
    if avg_loss == 0.0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def compute_volatility(prices: list[float], n: int = 20) -> float | None:
    """Rolling volatility: population std-dev of log-returns over last n prices."""
    if len(prices) < n + 1:
        return None
    window = prices[-(n + 1):]
    log_returns = []
    for i in range(1, len(window)):
        prev, curr = window[i - 1], window[i]
        if prev > 0.0 and curr > 0.0:
            log_returns.append(math.log(curr / prev))
    if not log_returns:
        return None
    mean = sum(log_returns) / len(log_returns)
    variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
    return math.sqrt(variance)


# ─── PRICE SERIES ────────────────────────────────────────────────────────────

class PriceSeries:
    """Ring-buffer price history + indicator computation for one token/asset."""

    def __init__(self, maxlen: int = 200) -> None:
        self._prices: deque[float] = deque(maxlen=maxlen)

    def push(self, price: float) -> None:
        """Append one price sample to the ring buffer."""
        self._prices.append(price)

    def __len__(self) -> int:
        return len(self._prices)

    def indicators(self, rsi_n: int, sma_n: int, ema_n: int,
                   vol_n: int) -> dict[str, float | None]:
        """Legacy fixed-quad interface (used by existing tests)."""
        p = list(self._prices)
        return {
            f"rsi_{rsi_n}": compute_rsi(p, rsi_n),
            f"sma_{sma_n}": compute_sma(p, sma_n),
            f"ema_{ema_n}": compute_ema(p, ema_n),
            f"vol_{vol_n}": compute_volatility(p, vol_n),
        }

    def compute_indicators(self,
                           specs: "list[IndicatorSpec]") -> dict[str, float | None]:
        """Config-driven interface: compute each indicator in specs.

        Key format: ``<abbrev>_<period>`` — e.g. ``rsi_14``, ``sma_20``,
        ``ema_9``, ``vol_20`` — consistent with the legacy ``indicators()``
        method (volatility is abbreviated to "vol").
        """
        _abbrev = {"rsi": "rsi", "sma": "sma", "ema": "ema", "volatility": "vol"}
        p = list(self._prices)
        result: dict[str, float | None] = {}
        for spec in specs:
            key = f"{_abbrev[spec.type]}_{spec.period}"
            if spec.type == "rsi":
                result[key] = compute_rsi(p, spec.period)
            elif spec.type == "sma":
                result[key] = compute_sma(p, spec.period)
            elif spec.type == "ema":
                result[key] = compute_ema(p, spec.period)
            elif spec.type == "volatility":
                result[key] = compute_volatility(p, spec.period)
        return result


# ─── OHLCV SERIES ────────────────────────────────────────────────────────────

class OHLCVSeries:
    """Ring-buffer OHLCV history for ATR / Bollinger / VWAP / vol-z / rolling-max.

    Also supports all close-only indicators (RSI, SMA, EMA, volatility), so
    binance_ws streams can request any combination without switching series types.

    Bollinger Bands use a fixed k=2.0 (standard 2σ). Period is configurable.
    """

    _ABBREV: dict[str, str] = {
        "rsi":             "rsi",
        "sma":             "sma",
        "ema":             "ema",
        "volatility":      "vol",
        "atr":             "atr",
        "bollinger_upper": "bb_upper",
        "bollinger_mid":   "bb_mid",
        "bollinger_lower": "bb_lower",
        "vwap":            "vwap",
        "vol_zscore":      "vol_z",
        "rolling_max":     "rmax",
    }

    def __init__(self, maxlen: int = 200) -> None:
        self._highs:   deque[float] = deque(maxlen=maxlen)
        self._lows:    deque[float] = deque(maxlen=maxlen)
        self._closes:  deque[float] = deque(maxlen=maxlen)
        self._volumes: deque[float] = deque(maxlen=maxlen)

    def push(self, h: float, l: float, c: float, v: float) -> None:
        """Append one OHLCV bar (open is unused — only H/L/C/V are needed)."""
        self._highs.append(h)
        self._lows.append(l)
        self._closes.append(c)
        self._volumes.append(v)

    def __len__(self) -> int:
        return len(self._closes)

    def compute_indicators(self,
                           specs: "list[IndicatorSpec]") -> "dict[str, float | None]":
        """Compute every indicator in specs. Returns {key: value} dict."""
        closes  = list(self._closes)
        highs   = list(self._highs)
        lows    = list(self._lows)
        volumes = list(self._volumes)
        result: dict[str, float | None] = {}
        _bb: dict[int, tuple] = {}       # cache per period to avoid triple computation
        for spec in specs:
            key = f"{self._ABBREV[spec.type]}_{spec.period}"
            if spec.type == "rsi":
                result[key] = compute_rsi(closes, spec.period)
            elif spec.type == "sma":
                result[key] = compute_sma(closes, spec.period)
            elif spec.type == "ema":
                result[key] = compute_ema(closes, spec.period)
            elif spec.type == "volatility":
                result[key] = compute_volatility(closes, spec.period)
            elif spec.type == "atr":
                result[key] = atr_last(highs, lows, closes, spec.period)
            elif spec.type in ("bollinger_upper", "bollinger_mid", "bollinger_lower"):
                if spec.period not in _bb:
                    _bb[spec.period] = bollinger_last(closes, spec.period, 2.0)
                upper, mid, lower = _bb[spec.period]
                if spec.type == "bollinger_upper":
                    result[key] = upper
                elif spec.type == "bollinger_mid":
                    result[key] = mid
                else:
                    result[key] = lower
            elif spec.type == "vwap":
                # Uses close-price VWAP (close × volume weighted average).
                # Note: _binance_vwap_context_task uses typical-price VWAP
                # ((H+L+C)/3 × volume). Values differ slightly; consumers
                # should pick one source and stick to it.
                result[key] = vwap_last(closes, volumes, spec.period)
            elif spec.type == "vol_zscore":
                result[key] = vol_zscore_last(volumes, spec.period)
            elif spec.type == "rolling_max":
                result[key] = rolling_max_last(highs, spec.period)
        return result


# ─── CONFIG TYPES ────────────────────────────────────────────────────────────

_OHLCV_INDICATOR_TYPES      = frozenset({
    "atr", "bollinger_upper", "bollinger_mid", "bollinger_lower",
    "vwap", "vol_zscore", "rolling_max",
})
_VALID_INDICATOR_TYPES      = frozenset({"rsi", "sma", "ema", "volatility"}) | _OHLCV_INDICATOR_TYPES
_VALID_SOURCES              = frozenset({
    "feed", "binance_ws", "binance_funding", "deribit_iv", "fear_greed",
    "binance_oi", "binance_ls_ratio", "binance_liquidations",
    "binance_scalping",
    "binance_vwap_context", "binance_volume_profile", "binance_macro_obi",
    "binance_full_depth",
})
_SOURCES_WITHOUT_INDICATORS = frozenset({
    "binance_funding", "deribit_iv", "fear_greed",
    "binance_oi", "binance_ls_ratio", "binance_liquidations",
    "binance_scalping",
    "binance_vwap_context", "binance_volume_profile", "binance_macro_obi",
    "binance_full_depth",
})
_DEFAULT_POLL_INTERVALS: dict[str, int] = {
    "binance_funding":        900,    # 15 min
    "deribit_iv":             300,    # 5 min
    "fear_greed":            3600,    # 1 hour
    "binance_oi":             300,    # 5 min
    "binance_ls_ratio":       300,    # 5 min
    "binance_liquidations":   300,    # 5 min
    "binance_vwap_context":  3600,    # 1 hour (VWAP changes slowly)
    "binance_volume_profile": 3600,   # 1 hour (volume profile rebuilt hourly)
    "binance_macro_obi":        60,   # 1 min  (macro OBI tracks recent flow)
}


@dataclass
class IndicatorSpec:
    """One indicator to compute (type + period)."""
    type:   str   # "rsi" | "sma" | "ema" | "volatility"
    period: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IndicatorSpec":
        """Construct from a config dict; raises ValueError on invalid type or period."""
        ind_type = str(d.get("type", "")).lower()
        if ind_type not in _VALID_INDICATOR_TYPES:
            raise ValueError(
                f"Unknown indicator type {d.get('type')!r}. "
                f"Valid: {sorted(_VALID_INDICATOR_TYPES)}"
            )
        period = int(d.get("period", 0))
        if period < 2:
            raise ValueError(f"Indicator period must be >= 2, got {period}")
        return cls(type=ind_type, period=period)


@dataclass
class StreamSpec:
    """One data stream: asset + source + timeframe + list of indicators."""
    id:              str
    asset:           str
    source:          str            # "feed" | "binance_ws" | "binance_funding" | ...
    timeframe:       str            # "tick" | "1m" | "5m" | "1h" | "4h" | "1d" | "n/a"
    indicators:      list[IndicatorSpec]
    seed_periods:    int = 50       # REST candles to fetch at startup (binance_ws only)
    poll_interval_s: int = 0        # poll interval override; 0 = use source default
    params:          dict[str, Any] = field(default_factory=dict)  # source-specific params

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StreamSpec":
        """Construct from a config dict; raises ValueError on unknown source or missing indicators."""
        source = str(d.get("source", "feed")).lower()
        if source not in _VALID_SOURCES:
            raise ValueError(
                f"Stream {d.get('id')!r}: unknown source {d.get('source')!r}. "
                f"Valid: {sorted(_VALID_SOURCES)}"
            )
        indicators = [IndicatorSpec.from_dict(i) for i in d.get("indicators", [])]
        if not indicators and source not in _SOURCES_WITHOUT_INDICATORS:
            raise ValueError(
                f"Stream {d.get('id')!r}: at least one indicator required"
            )
        if source == "feed":
            ohlcv_reqs = [i.type for i in indicators if i.type in _OHLCV_INDICATOR_TYPES]
            if ohlcv_reqs:
                raise ValueError(
                    f"Stream {d.get('id')!r}: {ohlcv_reqs} require binance_ws source "
                    f"(feed only provides best_bid — no H/L/V data)"
                )
        return cls(
            id=str(d.get("id", "")),
            asset=str(d.get("asset", "")),
            source=source,
            timeframe=str(d.get("timeframe", "tick")),
            indicators=indicators,
            seed_periods=int(d.get("seed_periods", 50)),
            poll_interval_s=int(d.get("poll_interval_s", 0)),
            params=dict(d.get("params", {})),
        )


class IndicatorsConfig(NamedTuple):
    """Parsed result of load_config()."""
    feed_addr: str
    out_addr:  str
    reg_addr:  str
    min_ticks: int
    streams:   list[StreamSpec]


def load_config(path: str) -> IndicatorsConfig:
    """Load indicators.json. Returns IndicatorsConfig(feed, out, reg, min_ticks, streams).

    When TRADINEBOTTE_PORT_BASE is set, addresses declared in the JSON file are
    shifted by the same offset as the built-in defaults, so a single env var
    moves the entire port layout. Explicit per-service env vars (TRADINEBOTTE_
    FEED_ADDR, TRADINEBOTTE_INDICATORS_ADDR, TRADINEBOTTE_INDICATORS_REG_ADDR)
    override everything without shifting.
    """
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    raw_feed = cfg.get("zmq_feed_addr")
    raw_out  = cfg.get("zmq_out_addr")
    raw_reg  = cfg.get("zmq_reg_addr")
    feed_addr = _shift_addr(raw_feed) if raw_feed else _FEED_ADDR
    out_addr  = _shift_addr(raw_out)  if raw_out  else _IND_ADDR
    reg_addr  = _shift_addr(raw_reg)  if raw_reg  else _REG_ADDR
    min_ticks = int(cfg.get("min_ticks", 25))
    raw_streams = [s for s in cfg.get("streams", []) if "id" in s]
    streams = [StreamSpec.from_dict(s) for s in raw_streams]
    if not streams:
        raise ValueError(f"Config {path!r}: at least one stream required")
    return IndicatorsConfig(feed_addr, out_addr, reg_addr, min_ticks, streams)


# ─── DYNAMIC REGISTRATION HELPERS ────────────────────────────────────────────

def derive_stream_id(asset: str, timeframe: str) -> str:
    """Build a short stream identifier from asset + timeframe.

    "BTCUSDT" + "4h"  → "btc_4h"
    "ETHUSDT" + "1d"  → "eth_1d"
    "BTCEUR"  + "1h"  → "btceur_1h"
    """
    base = asset.lower()
    for suffix in ("usdt", "usdc", "busd"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return f"{base}_{timeframe}"


def parse_subscribe_request(req: dict[str, Any]) -> tuple[str, StreamSpec]:
    """Validate and parse a subscribe request dict into (stream_id, StreamSpec).

    Raises ValueError with a descriptive message on any validation error.
    Poll-based sources (binance_funding, deribit_iv, fear_greed) do not require
    asset or timeframe — stream_id is derived from the source name when absent.
    """
    source = str(req.get("source", "binance_ws")).lower()

    if source in _SOURCES_WITHOUT_INDICATORS:
        asset     = str(req.get("asset", "")).strip().upper()
        timeframe = str(req.get("timeframe", "n/a")).strip() or "n/a"
        stream_id = str(req.get("stream_id") or source)
    else:
        asset = str(req.get("asset", "")).strip().upper()
        if not asset:
            raise ValueError("'asset' is required (e.g. 'BTCUSDT')")
        timeframe = str(req.get("timeframe", "")).strip()
        if not timeframe:
            raise ValueError("'timeframe' is required (e.g. '4h', '1d')")
        stream_id = str(req.get("stream_id") or derive_stream_id(asset, timeframe))

    spec_dict: dict[str, Any] = {
        "id":         stream_id,
        "asset":      asset,
        "source":     source,
        "timeframe":  timeframe,
        "indicators": req.get("indicators", []),
    }
    if "seed_periods" in req:
        spec_dict["seed_periods"] = int(req["seed_periods"])
    if "poll_interval_s" in req:
        spec_dict["poll_interval_s"] = int(req["poll_interval_s"])
    if "params" in req:
        spec_dict["params"] = dict(req["params"])

    spec = StreamSpec.from_dict(spec_dict)
    return stream_id, spec


# ─── DATA SOURCES ────────────────────────────────────────────────────────────

async def _seed_series(symbol: str, timeframe: str,
                       n: int, series: PriceSeries) -> None:
    """Seed PriceSeries with the last n closed candle closes from Binance REST."""
    params = {"symbol": symbol.upper(), "interval": timeframe, "limit": n + 1}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _BINANCE_REST_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
        for candle in data[:-1]:          # skip last (still open)
            series.push(float(candle[4])) # index 4 = close price
        logger.info("[seed] %s/%s: %d closed candles loaded",
                    symbol, timeframe, len(data) - 1)
    except Exception as exc:              # pylint: disable=broad-except
        logger.warning("[seed] %s/%s: REST seed failed (%s) — continuing without history",
                       symbol, timeframe, exc)


async def _seed_ohlcv_series(symbol: str, timeframe: str,
                              n: int, series: OHLCVSeries) -> None:
    """Seed OHLCVSeries with the last n closed candles (H/L/C/V) from Binance REST."""
    params = {"symbol": symbol.upper(), "interval": timeframe, "limit": n + 1}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                _BINANCE_REST_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json(content_type=None)
        for candle in data[:-1]:          # skip last (still open)
            series.push(float(candle[2]), float(candle[3]),
                        float(candle[4]), float(candle[5]))
        logger.info("[seed] %s/%s: %d closed OHLCV candles loaded",
                    symbol, timeframe, len(data) - 1)
    except Exception as exc:              # pylint: disable=broad-except
        logger.warning("[seed] %s/%s: REST seed failed (%s) — continuing without history",
                       symbol, timeframe, exc)


def _publish(pub: zmq.asyncio.Socket, out: dict[str, Any]) -> None:
    """Send one enriched indicator message on the PUB socket (non-blocking)."""
    pub.send_json({"v": 1, **out}, zmq.NOBLOCK)
    if VERBOSE:
        keys = [k for k in out if k not in ("t", "token_id", "asset",
                                             "timeframe", "stream_id", "ts")]
        logger.debug("[PUB %s] %s  %s",
                     out.get("stream_id", "?"),
                     out.get("asset") or out.get("token_id", "?"),
                     "  ".join(f"{k}={out[k]:.4f}" if isinstance(out[k], float)
                                else f"{k}={out[k]}" for k in keys))


async def _binance_kline_task(spec: StreamSpec, pub: zmq.asyncio.Socket,
                              min_ticks: int) -> None:
    """Stream live klines from Binance WebSocket for spec.asset/spec.timeframe.

    Uses OHLCVSeries so both close-only (RSI/SMA/EMA/vol) and OHLCV-based
    (ATR/Bollinger/VWAP/vol_zscore/rolling_max) indicators can be requested
    on the same stream without switching series types.
    """
    series = OHLCVSeries()
    await _seed_ohlcv_series(spec.asset, spec.timeframe, spec.seed_periods, series)

    ws_url  = f"{_BINANCE_WS_BASE}/{spec.asset.lower()}@kline_{spec.timeframe}"
    backoff = 5

    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=10
            ) as ws:
                logger.info("[%s] connected → %s", spec.id, ws_url)
                backoff = 5
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        logger.warning("[%s] WS stale — no data in %ds, reconnecting",
                                       spec.id, _WS_RECV_TIMEOUT_S)
                        break
                    msg   = json.loads(raw)
                    kline = msg.get("k", {})
                    if not kline.get("x"):    # only on closed candles
                        continue
                    series.push(float(kline["h"]), float(kline["l"]),
                                float(kline["c"]), float(kline["v"]))
                    if len(series) < min_ticks:
                        continue
                    ind = series.compute_indicators(spec.indicators)
                    if any(v is None for v in ind.values()):
                        continue
                    _publish(pub, {
                        "t":          "indicators",
                        "asset":      spec.asset,
                        "timeframe":  spec.timeframe,
                        "stream_id":  spec.id,
                        "ts":         int(time.time() * 1000),
                        **ind,
                    })
        except Exception as exc:           # pylint: disable=broad-except
            logger.warning("[%s] WS error (%s) — reconnect in %ds",
                           spec.id, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _zmq_feed_task(feed_addr: str, pub: zmq.asyncio.Socket,
                         feed_streams: list[StreamSpec],
                         min_ticks: int) -> None:
    """Subscribe to the ZeroMQ feed and compute indicators on best_bid per token."""
    ctx = zmq.asyncio.Context.instance()
    sub = make_sub(ctx, feed_addr)
    logger.info("[feed] SUB connected → %s", feed_addr)

    all_specs: list[IndicatorSpec] = []
    for s in feed_streams:
        all_specs.extend(s.indicators)
    stream_id = feed_streams[0].id if len(feed_streams) == 1 else "feed"

    series: dict[str, PriceSeries] = {}
    try:
        while True:
            msg: dict[str, Any] = await sub.recv_json()
            if msg.get("t") != "book":
                continue
            token_id = msg.get("token_id", "")
            bid      = msg.get("best_bid")
            if not token_id or bid is None:
                continue
            s = series.setdefault(token_id, PriceSeries())
            s.push(float(bid))
            if len(s) < min_ticks:
                continue
            ind = s.compute_indicators(all_specs)
            if any(v is None for v in ind.values()):
                continue
            _publish(pub, {
                "t":         "indicators",
                "token_id":  token_id,
                "stream_id": stream_id,
                "ts":        int(time.time() * 1000),
                **ind,
            })
    finally:
        sub.close()


async def _binance_funding_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Binance perpetual funding rate at the configured interval."""
    interval = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_funding"]
    asset    = spec.asset or "BTCUSDT"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _BINANCE_FUTURES_URL,
                    params={"symbol": asset.upper()},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                _publish(pub, {
                    "t":               "indicators",
                    "stream_id":       spec.id,
                    "funding_rate":    float(data["lastFundingRate"]),
                    "next_funding_ms": int(data["nextFundingTime"]),
                    "ts":              int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] funding rate fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _deribit_iv_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Deribit DVOL implied volatility index at the configured interval.

    Uses get_volatility_index_data (1h resolution, last closed bar).
    asset field accepts "BTC", "ETH", "dvol_btc", "btc_dvol" — currency is extracted.
    """
    interval = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["deribit_iv"]
    currency = spec.asset.upper().replace("DVOL_", "").replace("_DVOL", "") or "BTC"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_ms = int(time.time() * 1000)
                async with session.get(
                    _DERIBIT_DVOL_URL,
                    params={
                        "currency":        currency,
                        "resolution":      3600,
                        "start_timestamp": now_ms - 7_200_000,
                        "end_timestamp":   now_ms,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                # data["result"]["data"] → [[ts, open, high, low, close], ...]
                dvol = float(data["result"]["data"][-1][4])
                _publish(pub, {
                    "t":         "indicators",
                    "stream_id": spec.id,
                    "dvol":      dvol,
                    "ts":        int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] Deribit IV fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _fear_greed_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Alternative.me Fear & Greed Index at the configured interval."""
    interval = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["fear_greed"]
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _FEAR_GREED_URL,
                    params={"limit": 1},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                entry = data["data"][0]
                _publish(pub, {
                    "t":                "indicators",
                    "stream_id":        spec.id,
                    "fear_greed":       int(entry["value"]),
                    "fear_greed_label": str(entry["value_classification"]),
                    "ts":               int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] Fear & Greed fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _binance_oi_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Binance futures open interest + 5-min change at the configured interval."""
    interval = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_oi"]
    asset    = spec.asset or "BTCUSDT"
    prev_oi_btc: float | None = None
    prev_oi_usd: float | None = None
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _BINANCE_OI_HIST_URL,
                    params={"symbol": asset.upper(), "period": "5m", "limit": 2},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                latest   = data[-1]
                oi_btc   = float(latest["sumOpenInterest"])
                oi_usd   = float(latest["sumOpenInterestValue"])
                chg_btc  = oi_btc - prev_oi_btc if prev_oi_btc is not None else 0.0
                chg_usd  = oi_usd - prev_oi_usd if prev_oi_usd is not None else 0.0
                prev_oi_btc, prev_oi_usd = oi_btc, oi_usd
                _publish(pub, {
                    "t":             "indicators",
                    "stream_id":     spec.id,
                    "oi_btc":        oi_btc,
                    "oi_usd":        oi_usd,
                    "oi_change_btc": chg_btc,
                    "oi_change_usd": chg_usd,
                    "ts":            int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] open interest fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _binance_ls_ratio_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Binance top-trader long/short account ratio at the configured interval."""
    interval = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_ls_ratio"]
    asset    = spec.asset or "BTCUSDT"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _BINANCE_LS_RATIO_URL,
                    params={"symbol": asset.upper(), "period": "5m", "limit": 1},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                entry = data[0]
                _publish(pub, {
                    "t":                "indicators",
                    "stream_id":        spec.id,
                    "long_short_ratio": float(entry["longShortRatio"]),
                    "long_pct":         float(entry["longAccount"]),
                    "short_pct":        float(entry["shortAccount"]),
                    "ts":               int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] long/short ratio fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _binance_liquidations_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Stream Binance forced liquidation orders via the public @forceOrder WebSocket.

    No API credentials required — uses the public fstream endpoint.
    Maintains a rolling window (poll_interval_s) and publishes on every event.
    On quiet periods (no liquidations for _WS_RECV_TIMEOUT_S), publishes the current window
    and continues (keeps the connection alive — quiet markets are normal).
    """
    window_s = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_liquidations"]
    symbol   = (spec.asset or "BTCUSDT").lower()
    ws_url   = f"wss://fstream.binance.com/ws/{symbol}@forceOrder"
    backoff  = 5
    events: deque[tuple[float, float, float]] = deque()  # (ts_ms, liq_long, liq_short)

    def _publish_window(now_ms: float) -> None:
        cutoff = now_ms - window_s * 1000
        while events and events[0][0] < cutoff:
            events.popleft()
        liq_long  = sum(e[1] for e in events)
        liq_short = sum(e[2] for e in events)
        _publish(pub, {
            "t":             "indicators",
            "stream_id":     spec.id,
            "liq_long_usd":  liq_long,
            "liq_short_usd": liq_short,
            "liq_net_usd":   liq_short - liq_long,
            "liq_count":     len(events),
            "ts":            int(now_ms),
        })

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("[%s] liquidations WS connected → %s", spec.id, ws_url)
                backoff = 5
                _publish_window(time.time() * 1000)   # initial empty publish on connect
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        # quiet market — no liquidations; publish current window and keep alive
                        _publish_window(time.time() * 1000)
                        continue
                    msg   = json.loads(raw)
                    order = msg.get("o", {})
                    side      = order.get("S", "")
                    qty       = float(order.get("z", order.get("l", 0)))
                    avg_price = float(order.get("ap", order.get("p", 0)))
                    notional  = qty * avg_price
                    ts_ms     = float(order.get("T", time.time() * 1000))
                    liq_long_ev  = notional if side == "SELL" else 0.0
                    liq_short_ev = notional if side == "BUY"  else 0.0
                    events.append((ts_ms, liq_long_ev, liq_short_ev))
                    _publish_window(ts_ms)
        except Exception as exc:          # pylint: disable=broad-except
            logger.warning("[%s] liquidations WS error (%s) — reconnect in %ds",
                           spec.id, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


_BINANCE_FUTURES_AGG_TRADES_URL = "https://fapi.binance.com/fapi/v1/aggTrades"


async def _perp_agg_trade_rest_loop(
    symbol: str,
    trade_window: deque,
    tfi_window_s: float,
    stream_id: str,
) -> None:
    """Poll Binance futures aggTrades via REST every second.

    fstream WebSocket streams (both /ws/ and combined) silently drop aggTrade
    messages in the presence of depth20@100ms. REST polling is reliable.
    Rebuilds trade_window atomically on each poll — safe in single-threaded asyncio.
    """
    poll_s = 1.0
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now_ms = int(time.time() * 1000)
                start_ms = now_ms - int(tfi_window_s * 1000)
                async with session.get(
                    _BINANCE_FUTURES_AGG_TRADES_URL,
                    params={"symbol": symbol.upper(), "limit": 1000,
                            "startTime": start_ms, "endTime": now_ms},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    trades = await resp.json()
                if isinstance(trades, list):
                    trade_window.clear()
                    for t in trades:
                        ts_ms    = float(t.get("T", now_ms))
                        qty      = float(t.get("q", 0))
                        is_maker = t.get("m", False)
                        trade_window.append((
                            ts_ms,
                            0.0 if is_maker else qty,
                            qty if is_maker else 0.0,
                        ))
            except Exception as exc:
                logger.warning("[%s] aggTrade REST error: %s", stream_id, exc)
            await asyncio.sleep(poll_s)


async def _binance_scalping_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Stream Binance combined depth20 + aggTrade for scalping microstructure indicators.

    Publishes every publish_every_n depth updates:
      obi          — raw order book imbalance (bid_vol - ask_vol) / total
      obi_ema      — EMA-smoothed OBI (spoofing filter)
      obi_decel    — first difference of obi_ema (OBI deceleration signal)
      spread_bps   — best ask - best bid, in basis points
      realized_vol_bps — rolling population std of log-returns, in bps
      tfi          — trade flow imbalance: (buy_vol - sell_vol) / total_vol over tfi_window_s

    Source params (configured in JSON "params" dict):
      obi_levels      int   (10)    — book levels summed for OBI
      obi_ema_alpha   float (0.05)  — EMA smoothing factor
      tfi_window_s    float (60.0)  — TFI rolling window in seconds
      vol_window_n    int   (200)   — mid-price samples for realized vol
      publish_every_n int   (10)    — throttle: publish every N depth updates
      market          str   ("spot") — "spot" or "perp"
    """
    p               = spec.params
    obi_levels      = int(p.get("obi_levels", 10))
    obi_ema_alpha   = float(p.get("obi_ema_alpha", 0.05))
    tfi_window_s    = float(p.get("tfi_window_s", 60.0))
    vol_window_n    = int(p.get("vol_window_n", 200))
    publish_every_n = int(p.get("publish_every_n", 10))
    market          = str(p.get("market", "spot")).lower()

    symbol      = spec.asset.lower()
    ws_base     = _BINANCE_PERP_COMBINED_WS if market == "perp" else _BINANCE_SPOT_COMBINED_WS

    # Mutable state
    obi_ema:      float | None = None
    prev_obi_ema: float | None = None
    mid_prices:   deque[float] = deque(maxlen=vol_window_n)
    # (ts_ms, buy_vol, sell_vol) — trimmed by time window
    trade_window: deque[tuple[float, float, float]] = deque()
    depth_count   = 0
    backoff       = 5

    if market == "perp":
        # fstream combined stream silently drops aggTrade alongside depth20@100ms;
        # use depth-only WS for OBI + REST polling for TFI.
        ws_url = f"{ws_base}?streams={symbol}@depth20@100ms"
        asyncio.create_task(_perp_agg_trade_rest_loop(
            symbol, trade_window, tfi_window_s, spec.id,
        ))
    else:
        ws_url = f"{ws_base}?streams={symbol}@depth20@100ms/{symbol}@aggTrade"

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("[%s] scalping WS connected → %s", spec.id, ws_url)
                backoff = 5
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        logger.warning("[%s] scalping WS stale — no data in %ds, reconnecting",
                                       spec.id, _WS_RECV_TIMEOUT_S)
                        break
                    msg         = json.loads(raw)
                    stream_name = msg.get("stream", "")
                    data        = msg.get("data", {})

                    if "aggTrade" in stream_name:
                        # m=True: buyer is maker → taker sold (sell trade)
                        qty      = float(data.get("q", 0))
                        ts_ms    = float(data.get("T", time.time() * 1000))
                        buy_vol  = 0.0 if data.get("m") else qty
                        sell_vol = qty if data.get("m") else 0.0
                        trade_window.append((ts_ms, buy_vol, sell_vol))
                        cutoff = ts_ms - tfi_window_s * 1000
                        while trade_window and trade_window[0][0] < cutoff:
                            trade_window.popleft()
                        continue

                    if "depth20" not in stream_name:
                        continue

                    # spot uses "bids"/"asks"; futures depth20 uses "b"/"a"
                    bids = data.get("bids") or data.get("b", [])
                    asks = data.get("asks") or data.get("a", [])
                    if not bids or not asks:
                        continue

                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    if best_bid <= 0 or best_ask <= 0:
                        continue
                    mid = (best_bid + best_ask) / 2.0

                    # OBI
                    bid_vol = sum(float(b[1]) for b in bids[:obi_levels])
                    ask_vol = sum(float(a[1]) for a in asks[:obi_levels])
                    total   = bid_vol + ask_vol
                    obi_raw = (bid_vol - ask_vol) / total if total > 0 else 0.0

                    # OBI EMA + deceleration
                    if obi_ema is None:
                        obi_ema = obi_raw
                    prev_obi_ema = obi_ema
                    obi_ema  = obi_ema_alpha * obi_raw + (1.0 - obi_ema_alpha) * obi_ema
                    obi_decel = obi_ema - prev_obi_ema

                    # Spread
                    spread_bps = (best_ask - best_bid) / mid * 10000.0

                    # Realized volatility (population std of log-returns, in bps)
                    mid_prices.append(mid)
                    realized_vol_bps: float | None = None
                    if len(mid_prices) >= 2:
                        mids     = list(mid_prices)
                        log_rets = [
                            math.log(mids[i] / mids[i - 1]) * 10000.0
                            for i in range(1, len(mids))
                            if mids[i - 1] > 0 and mids[i] > 0
                        ]
                        if len(log_rets) >= 2:
                            mean     = sum(log_rets) / len(log_rets)
                            variance = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
                            realized_vol_bps = math.sqrt(variance)

                    # TFI
                    total_buy  = sum(t[1] for t in trade_window)
                    total_sell = sum(t[2] for t in trade_window)
                    total_flow = total_buy + total_sell
                    tfi        = (total_buy - total_sell) / total_flow if total_flow > 0 else 0.0

                    # Throttle
                    depth_count += 1
                    if depth_count % publish_every_n != 0:
                        continue

                    out: dict[str, Any] = {
                        "t":           "indicators",
                        "stream_id":   spec.id,
                        "asset":       spec.asset,
                        "best_bid":    best_bid,
                        "best_ask":    best_ask,
                        "mid":         mid,
                        "obi":         obi_raw,
                        "obi_ema":     obi_ema,
                        "obi_decel":   obi_decel,
                        "spread_bps":  spread_bps,
                        "tfi":         tfi,
                        "ts":          int(time.time() * 1000),
                    }
                    if realized_vol_bps is not None:
                        out["realized_vol_bps"] = realized_vol_bps
                    _publish(pub, out)

        except Exception as exc:          # pylint: disable=broad-except
            logger.warning("[%s] scalping WS error (%s) — reconnect in %ds",
                           spec.id, exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _apply_depth_event(bids: dict[float, float], asks: dict[float, float],
                        event: dict[str, Any]) -> None:
    """Apply one Binance depthUpdate diff event to the local bid/ask level dicts.

    Levels with qty=0 are deleted; qty>0 update (or insert) the level.
    Works for both spot and perp format ('b'/'a' keys are identical).
    """
    for price_str, qty_str in event.get("b", []):
        p, q = float(price_str), float(qty_str)
        if q == 0.0:
            bids.pop(p, None)
        else:
            bids[p] = q
    for price_str, qty_str in event.get("a", []):
        p, q = float(price_str), float(qty_str)
        if q == 0.0:
            asks.pop(p, None)
        else:
            asks[p] = q


async def _fetch_depth_snapshot(session: aiohttp.ClientSession,
                                 asset: str,
                                 url: str = _BINANCE_SPOT_DEPTH_URL,
                                 limit: int = 5000) -> dict[str, Any]:
    """Fetch an order book snapshot from Binance REST (spot or futures)."""
    async with session.get(
        url,
        params={"symbol": asset.upper(), "limit": limit},
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        return await resp.json(content_type=None)


def _init_depth_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the shared orderbook SQLite DB.

    Uses DELETE journal mode so cross-user read-only access works without needing
    write permission on the directory (WAL requires directory write for -shm/-wal).
    Readers should open with:  sqlite3.connect("file:path?mode=ro", uri=True)
    """
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_current (
            stream_id  TEXT,
            side       TEXT,
            price      REAL,
            qty        REAL,
            ts         INTEGER,
            PRIMARY KEY (stream_id, side, price)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orderbook_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            stream_id  TEXT,
            ts         INTEGER,
            bids       TEXT,
            asks       TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snap_stream_ts "
        "ON orderbook_snapshots (stream_id, ts)"
    )
    conn.commit()
    os.chmod(db_path, 0o644)
    return conn


def _write_depth_to_db(conn: sqlite3.Connection, stream_id: str,
                        sorted_bids: list[tuple[float, float]],
                        sorted_asks: list[tuple[float, float]],
                        bucket: float, ts_ms: int, retention_s: int) -> None:
    """Bucket and persist the current order book to SQLite (blocking, run in executor)."""
    def _bucket(levels: list[tuple[float, float]]) -> list[tuple[float, float]]:
        d: dict[float, float] = {}
        for price, qty in levels:
            k = round(price / bucket) * bucket
            d[k] = d.get(k, 0.0) + qty
        return sorted(d.items())

    b = _bucket(sorted_bids)
    a = _bucket(sorted_asks)

    cur = conn.cursor()
    cur.execute("DELETE FROM orderbook_current WHERE stream_id=? AND side='bid'", (stream_id,))
    cur.execute("DELETE FROM orderbook_current WHERE stream_id=? AND side='ask'", (stream_id,))
    cur.executemany(
        "INSERT INTO orderbook_current (stream_id, side, price, qty, ts) VALUES (?,?,?,?,?)",
        [(stream_id, "bid", p, q, ts_ms) for p, q in b] +
        [(stream_id, "ask", p, q, ts_ms) for p, q in a],
    )
    cur.execute(
        "INSERT INTO orderbook_snapshots (stream_id, ts, bids, asks) VALUES (?,?,?,?)",
        (stream_id, ts_ms, json.dumps(b), json.dumps(a)),
    )
    cutoff = ts_ms - retention_s * 1000
    cur.execute(
        "DELETE FROM orderbook_snapshots WHERE stream_id=? AND ts<?", (stream_id, cutoff)
    )
    conn.commit()


async def _binance_full_depth_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Maintain a full order book (spot or futures) via REST snapshot + WS diffs.

    Implements the Binance documented reconstruction algorithm:
      1. Connect to the diff WebSocket (btcusdt@depth@100ms).
      2. Buffer incoming events while fetching a REST snapshot.
      3. Apply snapshot; replay buffered events discarding those with u <= lastUpdateId.
      4. For each subsequent event validate sequence continuity; break on gap → resync.
         Spot:    U == lastUpdateId + 1
         Futures: ev["pu"] == lastUpdateId  (pu = previous event's u)
      5. On any error/gap: reconnect and re-snapshot from scratch.

    Publishes every publish_every_n depth updates:
      best_bid, best_ask, mid, spread_bps
      obi_N             — OBI at N levels for each N in obi_levels_list
      cum_bid_vol_Xpct  — cumulative bid qty within X% below mid
      cum_ask_vol_Xpct  — cumulative ask qty within X% above mid
      wall_bid_price, wall_bid_qty — largest single bid level within wall_range_pct of mid
      wall_ask_price, wall_ask_qty — largest single ask level within wall_range_pct of mid
      book_levels_bid, book_levels_ask — total maintained price levels

    Source params:
      market             str       ("spot")          — "spot" or "perp"
      bid_depth_pct      float     (0.0)             — trim bids below mid×(1-pct/100); 0=off
      ask_depth_pct      float     (0.0)             — trim asks above mid×(1+pct/100); 0=off
      obi_levels_list    list[int] ([10, 100, 500])  — book depths for OBI
      cum_vol_range_pct  float     (1.0)             — cumulative vol within X% of mid
      wall_range_pct     float     (2.0)             — wall search range (% from mid)
      publish_every_n    int       (10)              — throttle: publish every N depth events
      db_path            str       ("")              — shared SQLite path; empty = disabled
      bucket_size_usd    float     (50.0)            — price bucket width for DB storage
      db_write_every_n   int       (60)              — write DB every N publishes (~60s at 1pub/s)
      history_retention_h float   (24.0)            — hours of snapshot history to keep
    """
    p                   = spec.params
    obi_levels_list     = [int(x) for x in p.get("obi_levels_list", [10, 100, 500])]
    cum_pct             = float(p.get("cum_vol_range_pct", 1.0))
    wall_pct            = float(p.get("wall_range_pct", 2.0))
    publish_every_n     = int(p.get("publish_every_n", 10))
    market              = str(p.get("market", "spot")).lower()
    bid_depth_pct       = float(p.get("bid_depth_pct", 0.0))
    ask_depth_pct       = float(p.get("ask_depth_pct", 0.0))
    db_path             = str(p.get("db_path", ""))
    bucket_size_usd     = float(p.get("bucket_size_usd", 50.0))
    db_write_every_n    = int(p.get("db_write_every_n", 60))
    history_retention_h = float(p.get("history_retention_h", 24.0))
    retention_s         = int(history_retention_h * 3600)

    # Restrict db_path to the install directory to prevent a subscriber from
    # creating or chmoding arbitrary files via the REQ/REP registration socket.
    if db_path:
        import pathlib
        _install = os.environ.get("TRADINEBOTTE_DIR", os.path.expanduser("~/tradinebotte"))
        _resolved = str(pathlib.Path(db_path).resolve())
        _base     = str(pathlib.Path(_install).resolve())
        if not _resolved.startswith(_base + os.sep) and _resolved != _base:
            logger.error(
                "[%s] db_path %r is outside TRADINEBOTTE_DIR %r — rejected",
                spec.id, db_path, _install,
            )
            db_path = ""

    db_conn: sqlite3.Connection | None = None
    if db_path:
        try:
            db_conn = _init_depth_db(db_path)
            logger.info("[%s] orderbook DB opened → %s (bucket=$%.0f, retention=%.0fh)",
                        spec.id, db_path, bucket_size_usd, history_retention_h)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("[%s] orderbook DB init failed (%s) — DB disabled", spec.id, exc)
            db_conn = None

    symbol = spec.asset.lower()
    if market == "perp":
        snap_url   = _BINANCE_FUTURES_DEPTH_URL
        snap_limit = 1000
        ws_url     = f"wss://fstream.binance.com/ws/{symbol}@depth@100ms"
    else:
        snap_url   = _BINANCE_SPOT_DEPTH_URL
        snap_limit = 5000
        ws_url     = f"{_BINANCE_WS_BASE}/{symbol}@depth@100ms"
    backoff = 5

    def _trim_book(book_bids: dict[float, float], book_asks: dict[float, float],
                   mid_price: float) -> None:
        if bid_depth_pct > 0:
            floor = mid_price * (1.0 - bid_depth_pct / 100.0)
            for k in [kk for kk in book_bids if kk < floor]:
                del book_bids[k]
        if ask_depth_pct > 0:
            ceil = mid_price * (1.0 + ask_depth_pct / 100.0)
            for k in [kk for kk in book_asks if kk > ceil]:
                del book_asks[k]

    db_count = 0

    while True:
        bids:           dict[float, float] = {}
        asks:           dict[float, float] = {}
        last_update_id: int                = 0
        depth_count                        = 0

        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                logger.info("[%s] full depth WS connected → %s", spec.id, ws_url)
                backoff = 5

                # Buffer events while REST snapshot is in flight
                event_buffer: list[dict[str, Any]] = []
                async with aiohttp.ClientSession() as session:
                    snap_task = asyncio.create_task(
                        _fetch_depth_snapshot(session, spec.asset, snap_url, snap_limit)
                    )
                    while not snap_task.done():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=0.1)
                            event_buffer.append(json.loads(raw))
                        except asyncio.TimeoutError:
                            continue
                    snapshot = await snap_task

                last_update_id = int(snapshot["lastUpdateId"])
                for ps, qs in snapshot["bids"]:
                    q = float(qs)
                    if q > 0:
                        bids[float(ps)] = q
                for ps, qs in snapshot["asks"]:
                    q = float(qs)
                    if q > 0:
                        asks[float(ps)] = q

                # Trim to configured price range right after snapshot
                if bids and asks and (bid_depth_pct > 0 or ask_depth_pct > 0):
                    _trim_book(bids, asks, (max(bids) + min(asks)) / 2.0)

                logger.info("[%s] snapshot: %d bids / %d asks  lastUpdateId=%d",
                            spec.id, len(bids), len(asks), last_update_id)

                # Replay buffered events onto the snapshot
                synced = False
                for ev in event_buffer:
                    u = int(ev.get("u", 0))
                    U = int(ev.get("U", 0))
                    if u <= last_update_id:
                        continue            # already included in snapshot
                    if not synced:
                        if U > last_update_id + 1:
                            logger.warning("[%s] buffered gap U=%d > lastId+1=%d — resync",
                                           spec.id, U, last_update_id + 1)
                            break
                        synced = True
                    _apply_depth_event(bids, asks, ev)
                    last_update_id = u
                else:
                    synced = True

                if not synced:
                    logger.warning("[%s] failed to sync from buffer — resync", spec.id)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                    continue

                # Live diff stream
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=_WS_RECV_TIMEOUT_S)
                    except asyncio.TimeoutError:
                        logger.warning("[%s] full depth WS stale — no data in %ds, reconnecting",
                                       spec.id, _WS_RECV_TIMEOUT_S)
                        break
                    ev = json.loads(raw)
                    u  = int(ev.get("u", 0))
                    U  = int(ev.get("U", 0))

                    if u <= last_update_id:
                        continue            # stale event

                    if market == "perp":
                        pu = int(ev.get("pu", -1))
                        if pu != last_update_id:
                            logger.warning("[%s] futures pu=%d expected %d — resync",
                                           spec.id, pu, last_update_id)
                            break
                    else:
                        if U != last_update_id + 1:
                            logger.warning("[%s] sequence gap U=%d expected %d — resync",
                                           spec.id, U, last_update_id + 1)
                            break

                    _apply_depth_event(bids, asks, ev)
                    last_update_id = u

                    depth_count += 1
                    if depth_count % publish_every_n != 0:
                        continue
                    if not bids or not asks:
                        continue

                    best_bid = max(bids)
                    best_ask = min(asks)
                    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
                        continue
                    mid        = (best_bid + best_ask) / 2.0
                    spread_bps = (best_ask - best_bid) / mid * 10000.0

                    # Trim book to configured price range (keeps memory bounded)
                    if bid_depth_pct > 0 or ask_depth_pct > 0:
                        _trim_book(bids, asks, mid)

                    sorted_bids = sorted(bids.items(), reverse=True)   # high → low
                    sorted_asks = sorted(asks.items())                  # low → high

                    out: dict[str, Any] = {
                        "t":               "indicators",
                        "stream_id":       spec.id,
                        "asset":           spec.asset,
                        "best_bid":        best_bid,
                        "best_ask":        best_ask,
                        "mid":             mid,
                        "spread_bps":      spread_bps,
                        "book_levels_bid": len(bids),
                        "book_levels_ask": len(asks),
                        "ts":              int(time.time() * 1000),
                    }

                    # OBI at each requested depth
                    for n in obi_levels_list:
                        bv    = sum(q for _, q in sorted_bids[:n])
                        av    = sum(q for _, q in sorted_asks[:n])
                        total = bv + av
                        out[f"obi_{n}"] = (bv - av) / total if total > 0 else 0.0

                    # Cumulative bid/ask volume within cum_pct% of mid
                    bid_floor = mid * (1.0 - cum_pct / 100.0)
                    ask_ceil  = mid * (1.0 + cum_pct / 100.0)
                    out[f"cum_bid_vol_{cum_pct}pct"] = sum(
                        q for pr, q in sorted_bids if pr >= bid_floor)
                    out[f"cum_ask_vol_{cum_pct}pct"] = sum(
                        q for pr, q in sorted_asks if pr <= ask_ceil)

                    # Largest single bid/ask wall within wall_pct% of mid
                    wall_bid_floor = mid * (1.0 - wall_pct / 100.0)
                    wall_ask_ceil  = mid * (1.0 + wall_pct / 100.0)
                    bids_in_range  = [(pr, q) for pr, q in sorted_bids if pr >= wall_bid_floor]
                    asks_in_range  = [(pr, q) for pr, q in sorted_asks if pr <= wall_ask_ceil]
                    if bids_in_range:
                        wp, wq = max(bids_in_range, key=lambda x: x[1])
                        out["wall_bid_price"] = wp
                        out["wall_bid_qty"]   = wq
                    if asks_in_range:
                        wp, wq = max(asks_in_range, key=lambda x: x[1])
                        out["wall_ask_price"] = wp
                        out["wall_ask_qty"]   = wq

                    _publish(pub, out)

                    # Persist bucketed book to shared SQLite (non-blocking)
                    if db_conn is not None:
                        db_count += 1
                        if db_count % db_write_every_n == 0:
                            loop = asyncio.get_running_loop()
                            # Copy lists to avoid data race if next iteration
                            # replaces sorted_bids/sorted_asks before the
                            # executor thread finishes reading them.
                            _bids_snap = list(sorted_bids)
                            _asks_snap = list(sorted_asks)
                            fut = loop.run_in_executor(
                                None, _write_depth_to_db,
                                db_conn, spec.id, _bids_snap, _asks_snap,
                                bucket_size_usd, int(time.time() * 1000), retention_s,
                            )
                            fut.add_done_callback(
                                lambda f: logger.warning(
                                    "[%s] depth DB write error: %s", spec.id, f.exception()
                                ) if f.exception() else None
                            )

        except Exception as exc:            # pylint: disable=broad-except
            logger.warning("[%s] full depth error (%s) — reconnect in %ds",
                           spec.id, exc, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


async def _binance_vwap_context_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Binance klines to compute VWAP context and price-distance (dip_score).

    Fetches the last vwap_period closed candles at the configured timeframe (default 4h),
    computes VWAP using typical price (H+L+C)/3 × base volume, then compares to the
    current spot price fetched from the REST ticker.

    Publishes every poll_interval_s (default 1h):
      vwap        — VWAP of the last vwap_period candles
      price       — current spot mid price
      dip_score   — (vwap - price) / vwap; positive = below VWAP (dip), negative = above
      dip_zone    — "below_vwap" | "above_vwap"

    Source params:
      vwap_period  int   (24)   — number of closed candles for VWAP (24×4h = 4 days)
      timeframe    str   ("4h") — kline interval
    """
    interval    = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_vwap_context"]
    asset       = spec.asset or "BTCUSDT"
    p           = spec.params
    vwap_period = int(p.get("vwap_period", 24))
    timeframe   = str(p.get("timeframe", "4h"))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _BINANCE_REST_URL,
                    params={"symbol": asset.upper(), "interval": timeframe,
                            "limit": vwap_period + 1},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    candles = await resp.json(content_type=None)
                candles = candles[:-1]   # drop the still-open candle
                if len(candles) < vwap_period:
                    logger.warning("[%s] only %d candles (need %d) — skipping",
                                   spec.id, len(candles), vwap_period)
                    await asyncio.sleep(interval)
                    continue

                highs   = [float(c[2]) for c in candles]
                lows    = [float(c[3]) for c in candles]
                closes  = [float(c[4]) for c in candles]
                volumes = [float(c[5]) for c in candles]
                typical = [(h + l + c) / 3.0 for h, l, c in zip(highs, lows, closes)]
                total_vol = sum(volumes)
                vwap = (sum(tp * v for tp, v in zip(typical, volumes)) / total_vol
                        if total_vol > 0 else closes[-1])

                async with session.get(
                    _BINANCE_TICKER_PRICE_URL,
                    params={"symbol": asset.upper()},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    ticker = await resp.json(content_type=None)
                price = float(ticker["price"])

                dip_score = (vwap - price) / vwap if vwap > 0 else 0.0
                dip_zone  = "below_vwap" if price < vwap else "above_vwap"

                _publish(pub, {
                    "t":          "indicators",
                    "stream_id":  spec.id,
                    "asset":      asset,
                    "vwap":       vwap,
                    "price":      price,
                    "dip_score":  dip_score,
                    "dip_zone":   dip_zone,
                    "ts":         int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] VWAP context fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _binance_volume_profile_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Binance klines to build a taker buy/sell volume profile by price bucket.

    Fetches kline_limit closed 5m candles (default 288 = 24h), aggregates taker
    buy/sell volume into bucket_size_usd-wide price buckets (midpoint-based),
    identifies the top hvn_top_n High-Volume-Nodes (HVN), and determines whether
    the current price sits in a buy-dominant HVN, sell-dominant HVN, or neutral zone.

    Publishes every poll_interval_s (default 1h):
      price           — current spot price
      price_bucket    — bucket containing current price
      bucket_buy_vol  — taker buy volume in current bucket
      bucket_sell_vol — taker sell volume in current bucket
      bucket_net_vol  — buy_vol - sell_vol (positive = buy pressure)
      price_zone      — "buy_hvn" | "sell_hvn" | "neutral"
      zone_score      — net_vol / total_vol in bucket  (range: -1 to +1)
      hvn_buckets     — sorted list of the top hvn_top_n bucket prices

    Source params:
      bucket_size_usd  float (500)  — bucket width in USD
      hvn_top_n        int   (5)    — number of HVN buckets to flag
      kline_limit      int   (288)  — candles to fetch (288 × 5m = 24h)
      timeframe        str   ("5m") — kline interval
    """
    interval    = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_volume_profile"]
    asset       = spec.asset or "BTCUSDT"
    p           = spec.params
    bucket_size = float(p.get("bucket_size_usd", 500.0))
    hvn_top_n   = int(p.get("hvn_top_n", 5))
    kline_limit = int(p.get("kline_limit", 288))
    timeframe   = str(p.get("timeframe", "5m"))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _BINANCE_REST_URL,
                    params={"symbol": asset.upper(), "interval": timeframe,
                            "limit": kline_limit + 1},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    candles = await resp.json(content_type=None)
                candles = candles[:-1]   # drop open candle

                buy_vol:  dict[float, float] = {}
                sell_vol: dict[float, float] = {}
                for c in candles:
                    high         = float(c[2])
                    low          = float(c[3])
                    total_v      = float(c[5])
                    taker_buy_v  = float(c[9])
                    taker_sell_v = total_v - taker_buy_v
                    mid          = (high + low) / 2.0
                    bucket       = round(mid / bucket_size) * bucket_size
                    buy_vol[bucket]  = buy_vol.get(bucket, 0.0)  + taker_buy_v
                    sell_vol[bucket] = sell_vol.get(bucket, 0.0) + taker_sell_v

                all_buckets = sorted(set(buy_vol) | set(sell_vol))
                total_vols  = {b: buy_vol.get(b, 0.0) + sell_vol.get(b, 0.0)
                               for b in all_buckets}
                hvn_buckets = set(
                    sorted(all_buckets, key=lambda b: total_vols[b], reverse=True)
                    [:hvn_top_n]
                )

                async with session.get(
                    _BINANCE_TICKER_PRICE_URL,
                    params={"symbol": asset.upper()},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    ticker = await resp.json(content_type=None)
                price  = float(ticker["price"])
                bucket = round(price / bucket_size) * bucket_size

                bv = buy_vol.get(bucket, 0.0)
                sv = sell_vol.get(bucket, 0.0)
                nv = bv - sv

                if bucket in hvn_buckets:
                    price_zone = "buy_hvn" if nv >= 0 else "sell_hvn"
                else:
                    price_zone = "neutral"

                bucket_total = bv + sv
                zone_score   = nv / bucket_total if bucket_total > 0 else 0.0

                _publish(pub, {
                    "t":               "indicators",
                    "stream_id":       spec.id,
                    "asset":           asset,
                    "price":           price,
                    "price_bucket":    bucket,
                    "bucket_buy_vol":  bv,
                    "bucket_sell_vol": sv,
                    "bucket_net_vol":  nv,
                    "price_zone":      price_zone,
                    "zone_score":      zone_score,
                    "hvn_buckets":     sorted(hvn_buckets),
                    "ts":              int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] volume profile fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


async def _binance_macro_obi_task(spec: StreamSpec, pub: zmq.asyncio.Socket) -> None:
    """Poll Binance klines to compute a macro Order Book Imbalance from taker flow.

    Fetches kline_limit closed 1m candles, computes taker_buy_ratio per candle,
    maps it to [-1, +1] via (ratio - 0.5) × 2, then EMA-smooths the series.
    Acts as a trend-direction filter: if macro OBI is bearish, suppress long entries.

    Publishes every poll_interval_s (default 60s):
      macro_obi           — EMA-smoothed macro OBI  (-1 = full sell pressure, +1 = buy)
      macro_obi_raw       — raw OBI of the most recent candle
      macro_obi_direction — "bullish" | "neutral" | "bearish"

    Source params:
      kline_limit       int   (60)   — candles to fetch (60 × 1m = 1h look-back)
      ema_alpha         float (0.20) — EMA smoothing factor applied over candle sequence
      neutral_threshold float (0.10) — |macro_obi| below this → "neutral"
      timeframe         str   ("1m") — kline interval
    """
    interval    = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_macro_obi"]
    asset       = spec.asset or "BTCUSDT"
    p           = spec.params
    kline_limit = int(p.get("kline_limit", 60))
    ema_alpha   = float(p.get("ema_alpha", 0.20))
    neutral_thr = float(p.get("neutral_threshold", 0.10))
    timeframe   = str(p.get("timeframe", "1m"))

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _BINANCE_REST_URL,
                    params={"symbol": asset.upper(), "interval": timeframe,
                            "limit": kline_limit + 1},
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    candles = await resp.json(content_type=None)
                candles = candles[:-1]   # drop open candle

                obis: list[float] = []
                for c in candles:
                    total_v     = float(c[5])
                    taker_buy_v = float(c[9])
                    if total_v > 0:
                        obis.append((taker_buy_v / total_v - 0.5) * 2.0)

                if not obis:
                    await asyncio.sleep(interval)
                    continue

                ema = obis[0]
                for v in obis[1:]:
                    ema = ema_alpha * v + (1.0 - ema_alpha) * ema

                direction = (
                    "bullish" if ema >  neutral_thr
                    else "bearish" if ema < -neutral_thr
                    else "neutral"
                )

                _publish(pub, {
                    "t":                   "indicators",
                    "stream_id":           spec.id,
                    "asset":               asset,
                    "macro_obi":           ema,
                    "macro_obi_raw":       obis[-1],
                    "macro_obi_direction": direction,
                    "ts":                  int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] macro OBI fetch failed (%s)", spec.id, exc)
            await asyncio.sleep(interval)


# ─── DYNAMIC REGISTRATION ────────────────────────────────────────────────────

async def _start_stream(
    spec: StreamSpec,
    pub: zmq.asyncio.Socket,
    min_ticks: int,
    active: dict[str, tuple[StreamSpec, "asyncio.Task[None]"]],
) -> None:
    """Start a task for spec if one is not already running.

    Dispatches to the right coroutine based on spec.source.
    feed-source streams are not dispatchable here (they share one SUB task).
    """
    if spec.id in active:
        _spec, _task = active[spec.id]
        if _task.done():
            exc = _task.exception() if not _task.cancelled() else None
            logger.warning(
                "[reg] stream %r task has exited (exc=%s) — restarting",
                spec.id, exc,
            )
            del active[spec.id]
        else:
            logger.info("[reg] stream %r already active — skipping", spec.id)
            return
    if spec.source == "binance_ws":
        coro = _binance_kline_task(spec, pub, min_ticks)
    elif spec.source == "binance_funding":
        coro = _binance_funding_task(spec, pub)
    elif spec.source == "deribit_iv":
        coro = _deribit_iv_task(spec, pub)
    elif spec.source == "fear_greed":
        coro = _fear_greed_task(spec, pub)
    elif spec.source == "binance_oi":
        coro = _binance_oi_task(spec, pub)
    elif spec.source == "binance_ls_ratio":
        coro = _binance_ls_ratio_task(spec, pub)
    elif spec.source == "binance_liquidations":
        coro = _binance_liquidations_task(spec, pub)
    elif spec.source == "binance_scalping":
        coro = _binance_scalping_task(spec, pub)
    elif spec.source == "binance_full_depth":
        coro = _binance_full_depth_task(spec, pub)
    elif spec.source == "binance_vwap_context":
        coro = _binance_vwap_context_task(spec, pub)
    elif spec.source == "binance_volume_profile":
        coro = _binance_volume_profile_task(spec, pub)
    elif spec.source == "binance_macro_obi":
        coro = _binance_macro_obi_task(spec, pub)
    else:
        logger.error("[reg] cannot start stream %r: source %r is not dispatchable",
                     spec.id, spec.source)
        return
    task: asyncio.Task[None] = asyncio.create_task(
        coro, name=f"{spec.source}_{spec.id}"
    )
    active[spec.id] = (spec, task)
    logger.info("[reg] started stream %r (%s)", spec.id, spec.source)


# Keep the old name as an alias so existing test mocks still resolve.
_start_binance_stream = _start_stream


async def _handle_subscribe(
    req: dict[str, Any],
    pub: zmq.asyncio.Socket,
    min_ticks: int,
    active: dict[str, tuple[StreamSpec, "asyncio.Task[None]"]],
) -> dict[str, Any]:
    """Process one subscribe request and return the response dict."""
    try:
        stream_id, spec = parse_subscribe_request(req)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}

    if spec.source != "feed":
        await _start_stream(spec, pub, min_ticks, active)
    else:
        # feed-source streams must be declared statically in the JSON config
        if spec.id not in active:
            return {
                "status":  "error",
                "message": (
                    "Dynamic registration is only supported for non-feed sources. "
                    "Declare feed-source streams in the JSON config file."
                ),
            }

    return {"status": "ok", "stream_id": stream_id}


async def _registration_task(
    reg_addr: str,
    pub: zmq.asyncio.Socket,
    min_ticks: int,
    active: dict[str, tuple[StreamSpec, "asyncio.Task[None]"]],
) -> None:
    """REP loop: accept subscribe requests from bots and start new streams on demand."""
    ctx = zmq.asyncio.Context.instance()
    rep = make_rep(ctx, reg_addr, name="INDICATORS_REG_ADDR")
    logger.info("[reg] REP bind → %s", reg_addr)
    try:
        while True:
            req: dict[str, Any] = await rep.recv_json()
            cmd = req.get("cmd", "")
            if cmd == "subscribe":
                resp = await _handle_subscribe(req, pub, min_ticks, active)
            else:
                resp = {
                    "status":  "error",
                    "message": f"Unknown command {cmd!r}. Use 'subscribe'.",
                }
            await rep.send_json(resp)
    except asyncio.CancelledError:
        raise
    finally:
        rep.close()


# ─── MAIN RUN ────────────────────────────────────────────────────────────────

async def run(feed_addr: str, ind_addr: str, reg_addr: str,
              rsi_n: int, sma_n: int, ema_n: int, vol_n: int,
              min_ticks: int,
              config_path: str | None = None) -> None:
    """
    Orchestrate indicator streams.

    If config_path is given: load the JSON and spawn one asyncio task per
    non-feed stream; one shared task for all feed-source streams; and one
    REP task that accepts dynamic subscribe requests from bots.

    Otherwise: legacy mode — one ZMQ feed stream with CLI-specified periods.
    """
    ctx = zmq.asyncio.Context()

    # active: stream_id → (StreamSpec, Task) — shared with _registration_task
    active: dict[str, tuple[StreamSpec, asyncio.Task[None]]] = {}

    if config_path:
        cfg = load_config(config_path)
        # CLI flags (resolved from env vars at startup) override config file
        # addresses only when they differ from the module-level defaults —
        # i.e. when the caller passed an explicit --feed/--out/--reg flag.
        # When no flag was given, the argparse default equals the env-var
        # resolved module constant, so cfg.* wins (config file takes effect).
        actual_feed = feed_addr if feed_addr != _FEED_ADDR else cfg.feed_addr
        actual_out  = ind_addr  if ind_addr  != _IND_ADDR  else cfg.out_addr
        actual_reg  = reg_addr  if reg_addr  != _REG_ADDR  else cfg.reg_addr
        actual_min  = cfg.min_ticks
        streams     = cfg.streams
    else:
        # Legacy mode: build a synthetic single-stream from CLI flags
        streams = [StreamSpec(
            id="legacy",
            asset="*",
            source="feed",
            timeframe="tick",
            indicators=[
                IndicatorSpec(type="rsi",        period=rsi_n),
                IndicatorSpec(type="sma",        period=sma_n),
                IndicatorSpec(type="ema",        period=ema_n),
                IndicatorSpec(type="volatility", period=vol_n),
            ],
        )]
        actual_feed = feed_addr
        actual_out  = ind_addr
        actual_reg  = reg_addr
        actual_min  = min_ticks

    pub = make_pub(ctx, actual_out, name="INDICATORS_ADDR")
    logger.info("PUB bind → %s", actual_out)

    tasks: list[asyncio.Task[None]] = []

    # Start all static non-feed streams (binance_ws, binance_funding, deribit_iv, fear_greed)
    for spec in streams:
        if spec.source != "feed":
            await _start_stream(spec, pub, actual_min, active)
    tasks.extend(task for _, task in active.values())

    # Start static feed streams (one shared SUB task)
    feed_streams = [s for s in streams if s.source == "feed"]
    if feed_streams:
        tasks.append(asyncio.create_task(
            _zmq_feed_task(actual_feed, pub, feed_streams, actual_min),
            name="zmq_feed",
        ))

    if not tasks and not feed_streams:
        logger.error("No streams to run — check your config")
        pub.close()
        ctx.term()
        return

    # Dynamic registration listener (always started)
    tasks.append(asyncio.create_task(
        _registration_task(actual_reg, pub, actual_min, active),
        name="registration",
    ))

    try:
        await asyncio.gather(*tasks)
    finally:
        pub.close()
        ctx.term()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="tradinebotte technical indicator service")
    p.add_argument("--config", metavar="FILE",
                   help="JSON config file (e.g. strategies/indicators.json); "
                        "when set, --rsi/--sma/--ema/--vol/--feed/--out are ignored")
    p.add_argument("--feed", default=_FEED_ADDR, metavar="ADDR",
                   help=f"ZMQ address to subscribe to (default: {_FEED_ADDR})")
    p.add_argument("--out", default=_IND_ADDR, metavar="ADDR",
                   help=f"ZMQ PUB address (default: {_IND_ADDR})")
    p.add_argument("--reg-addr", default=_REG_ADDR, metavar="ADDR",
                   help=f"ZMQ REP address for dynamic stream registration "
                        f"(default: {_REG_ADDR})")
    p.add_argument("--rsi",       type=int, default=14, metavar="N",
                   help="RSI period — legacy mode only (default: 14)")
    p.add_argument("--sma",       type=int, default=20, metavar="N",
                   help="SMA period — legacy mode only (default: 20)")
    p.add_argument("--ema",       type=int, default=9,  metavar="N",
                   help="EMA period — legacy mode only (default: 9)")
    p.add_argument("--vol",       type=int, default=20, metavar="N",
                   help="Volatility window — legacy mode only (default: 20)")
    p.add_argument("--min-ticks", type=int, default=25, metavar="N",
                   help="Min price ticks before publishing — legacy mode only (default: 25)")
    p.add_argument("--verbose", action="store_true",
                   help="Enable DEBUG logging")
    return p.parse_args()


def main() -> None:
    global VERBOSE  # pylint: disable=global-statement
    args = _parse_args()
    if args.verbose:
        VERBOSE = True
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    asyncio.run(run(
        feed_addr=args.feed,
        ind_addr=args.out,
        reg_addr=args.reg_addr,
        rsi_n=args.rsi,
        sma_n=args.sma,
        ema_n=args.ema,
        vol_n=args.vol,
        min_ticks=args.min_ticks,
        config_path=args.config,
    ))


if __name__ == "__main__":
    main()
