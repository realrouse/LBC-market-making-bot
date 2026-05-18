#!/usr/bin/env python3
"""
indicators.py — Technical indicator service

SUB to feed.py (ZeroMQ) and/or Binance WebSocket kline streams → compute
RSI / SMA / EMA / volatility → PUB enriched messages on a second socket.

A REP socket accepts dynamic stream-registration requests from any bot at
runtime so new indicator streams can be added without restarting the service.

Five data sources are supported, configured via a JSON file:

  source="feed"            — subscribes to feed.py PUB (best_bid tick-by-tick,
                             one PriceSeries per Polymarket token)
  source="binance_ws"      — opens a Binance kline WebSocket for one asset/
                             timeframe; seeds from REST on startup; pushes the
                             close price of each completed candle
  source="binance_funding" — polls Binance perp funding rate (REST, every 15 min
                             by default); no indicator math required
  source="deribit_iv"      — polls Deribit DVOL implied volatility index (REST,
                             every 5 min by default); no indicator math required
  source="fear_greed"      — polls Alternative.me Fear & Greed Index (REST,
                             every 1 h by default); no indicator math required

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
  python3 bot/indicators.py --config strategies/indicators.json
  python3 bot/indicators.py          # legacy: CLI flags, ZMQ feed source
  python3 bot/indicators.py --rsi 14 --sma 20 --ema 9 --vol 20
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 python3 bot/indicators.py \\
      --config strategies/indicators.json
"""

import argparse, asyncio, json, logging, math, os, sys, time
from collections import deque
from dataclasses import dataclass
from typing import Any, NamedTuple

import aiohttp, websockets, zmq, zmq.asyncio

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
# TRADINEBOTTE_PORT_BASE shifts the entire default port layout uniformly.
# Default layout (base=5557): feed=5557, indicators PUB=5559, indicators REP=5561.
# Example — run a second independent stack on base 6557:
#   TRADINEBOTTE_PORT_BASE=6557 python3 bot/indicators.py --config ...
# Per-service env vars still override PORT_BASE when set explicitly.
_PORT_BASE  = int(os.environ.get("TRADINEBOTTE_PORT_BASE", "5557"))
_PORT_SHIFT = _PORT_BASE - 5557  # 0 when using defaults

def _warn_if_external_bind(addr: str, name: str) -> None:
    """Log a security warning if a ZMQ bind address is not loopback."""
    if addr.startswith("tcp://") and "127.0.0.1" not in addr and "localhost" not in addr:
        logging.getLogger("indicators").warning(
            "SECURITY: %s (%s) is bound to a non-loopback address — "
            "ensure ZMQ CURVE auth is active before exposing to the network.", name, addr
        )

_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR",
                             f"tcp://127.0.0.1:{_PORT_BASE}")
_IND_ADDR  = os.environ.get("TRADINEBOTTE_INDICATORS_ADDR",
                             f"tcp://127.0.0.1:{_PORT_BASE + 2}")
_REG_ADDR  = os.environ.get("TRADINEBOTTE_INDICATORS_REG_ADDR",
                             f"tcp://127.0.0.1:{_PORT_BASE + 4}")
_BINANCE_REST_URL    = "https://api.binance.com/api/v3/klines"
_BINANCE_WS_BASE     = "wss://stream.binance.com:9443/ws"
_BINANCE_FUTURES_URL      = "https://fapi.binance.com/fapi/v1/premiumIndex"
_BINANCE_OI_HIST_URL      = "https://fapi.binance.com/futures/data/openInterestHist"
_BINANCE_LS_RATIO_URL     = "https://fapi.binance.com/futures/data/topLongShortAccountRatio"
_BINANCE_FORCE_ORDERS_URL = "https://fapi.binance.com/fapi/v1/forceOrders"
_DERIBIT_DVOL_URL         = "https://www.deribit.com/api/v2/public/get_index_price"
_FEAR_GREED_URL           = "https://api.alternative.me/fng/"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"


def _shift_addr(addr: str) -> str:
    """Apply _PORT_SHIFT to a tcp://host:port address. No-op when shift is 0."""
    if _PORT_SHIFT == 0 or not addr.startswith("tcp://"):
        return addr
    host, port_str = addr.rsplit(":", 1)
    return f"{host}:{int(port_str) + _PORT_SHIFT}"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("indicators")

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


# ─── CONFIG TYPES ────────────────────────────────────────────────────────────

_VALID_INDICATOR_TYPES      = frozenset({"rsi", "sma", "ema", "volatility"})
_VALID_SOURCES              = frozenset({
    "feed", "binance_ws", "binance_funding", "deribit_iv", "fear_greed",
    "binance_oi", "binance_ls_ratio", "binance_liquidations",
})
_SOURCES_WITHOUT_INDICATORS = frozenset({
    "binance_funding", "deribit_iv", "fear_greed",
    "binance_oi", "binance_ls_ratio", "binance_liquidations",
})
_DEFAULT_POLL_INTERVALS: dict[str, int] = {
    "binance_funding":     900,    # 15 min
    "deribit_iv":          300,    # 5 min
    "fear_greed":         3600,    # 1 hour
    "binance_oi":          300,    # 5 min
    "binance_ls_ratio":    300,    # 5 min
    "binance_liquidations": 300,   # 5 min
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
        return cls(
            id=str(d.get("id", "")),
            asset=str(d.get("asset", "")),
            source=source,
            timeframe=str(d.get("timeframe", "tick")),
            indicators=indicators,
            seed_periods=int(d.get("seed_periods", 50)),
            poll_interval_s=int(d.get("poll_interval_s", 0)),
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


def _publish(pub: zmq.asyncio.Socket, out: dict[str, Any]) -> None:
    """Send one enriched indicator message on the PUB socket (non-blocking)."""
    pub.send_json(out, zmq.NOBLOCK)
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
    """Stream live klines from Binance WebSocket for spec.asset/spec.timeframe."""
    series = PriceSeries()
    await _seed_series(spec.asset, spec.timeframe, spec.seed_periods, series)

    ws_url  = f"{_BINANCE_WS_BASE}/{spec.asset.lower()}@kline_{spec.timeframe}"
    backoff = 5

    while True:
        try:
            async with websockets.connect(
                ws_url, ping_interval=20, ping_timeout=10
            ) as ws:
                logger.info("[%s] connected → %s", spec.id, ws_url)
                backoff = 5
                async for raw in ws:
                    msg  = json.loads(raw)
                    kline = msg.get("k", {})
                    if not kline.get("x"):    # only on closed candles
                        continue
                    series.push(float(kline["c"]))
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
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(feed_addr)
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
    """Poll Deribit DVOL implied volatility index at the configured interval."""
    interval   = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["deribit_iv"]
    index_name = spec.asset.lower() if spec.asset else "dvol_btc"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    _DERIBIT_DVOL_URL,
                    params={"index_name": index_name},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    data = await resp.json(content_type=None)
                _publish(pub, {
                    "t":         "indicators",
                    "stream_id": spec.id,
                    "dvol":      float(data["result"]["index_price"]),
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
    """Aggregate Binance forced liquidation orders over the last poll interval."""
    interval = spec.poll_interval_s or _DEFAULT_POLL_INTERVALS["binance_liquidations"]
    asset    = spec.asset or "BTCUSDT"
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                start_ms = int((time.time() - interval) * 1000)
                async with session.get(
                    _BINANCE_FORCE_ORDERS_URL,
                    params={"symbol": asset.upper(), "startTime": start_ms, "limit": 1000},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    orders = await resp.json(content_type=None)
                liq_long = liq_short = 0.0
                for o in orders:
                    notional = float(o.get("executedQty", 0)) * float(o.get("averagePrice", 0))
                    if o.get("side") == "SELL":   # long position liquidated
                        liq_long += notional
                    else:                          # short position liquidated
                        liq_short += notional
                _publish(pub, {
                    "t":             "indicators",
                    "stream_id":     spec.id,
                    "liq_long_usd":  liq_long,
                    "liq_short_usd": liq_short,
                    "liq_net_usd":   liq_short - liq_long,
                    "liq_count":     len(orders),
                    "ts":            int(time.time() * 1000),
                })
            except Exception as exc:  # pylint: disable=broad-except
                logger.warning("[%s] liquidations fetch failed (%s)", spec.id, exc)
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
    rep = ctx.socket(zmq.REP)
    rep.bind(reg_addr)
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
    pub = ctx.socket(zmq.PUB)

    # active: stream_id → (StreamSpec, Task) — shared with _registration_task
    active: dict[str, tuple[StreamSpec, asyncio.Task[None]]] = {}

    if config_path:
        cfg = load_config(config_path)
        # env var overrides take precedence over config file addresses
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

    _warn_if_external_bind(actual_out, "INDICATORS_ADDR")
    _warn_if_external_bind(actual_reg, "INDICATORS_REG_ADDR")
    pub.bind(actual_out)
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
