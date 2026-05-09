#!/usr/bin/env python3
"""
indicators.py — Technical indicator service

SUB to feed.py (ZeroMQ) and/or Binance WebSocket kline streams → compute
RSI / SMA / EMA / volatility → PUB enriched messages on a second socket.

Two data sources are supported, configured via a JSON file:

  source="feed"        — subscribes to feed.py PUB (best_bid tick-by-tick,
                         one PriceSeries per Polymarket token)
  source="binance_ws"  — opens a Binance kline WebSocket for one asset/
                         timeframe; seeds from REST on startup; pushes the
                         close price of each completed candle

Output messages:
  source="feed":
    {"t":"indicators", "token_id":"...", "stream_id":"...", "ts":...,
     "rsi_14":..., ...}
  source="binance_ws":
    {"t":"indicators", "asset":"BTCUSDT", "timeframe":"4h", "stream_id":"...",
     "ts":..., "rsi_14":..., "vol_20":...}

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
from typing import Any

import aiohttp, websockets, zmq, zmq.asyncio

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR",       "tcp://127.0.0.1:5557")
_IND_ADDR  = os.environ.get("TRADINEBOTTE_INDICATORS_ADDR", "tcp://127.0.0.1:5559")
_BINANCE_REST_URL = "https://api.binance.com/api/v3/klines"
_BINANCE_WS_BASE  = "wss://stream.binance.com:9443/ws"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

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
    """Wilder RSI(n). Returns 0–100 or None when insufficient data."""
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

_VALID_INDICATOR_TYPES = frozenset({"rsi", "sma", "ema", "volatility"})
_VALID_SOURCES         = frozenset({"feed", "binance_ws"})


@dataclass
class IndicatorSpec:
    """One indicator to compute (type + period)."""
    type:   str   # "rsi" | "sma" | "ema" | "volatility"
    period: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "IndicatorSpec":
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
    id:           str
    asset:        str
    source:       str            # "feed" | "binance_ws"
    timeframe:    str            # "tick" | "1m" | "5m" | "1h" | "4h" | "1d" …
    indicators:   list[IndicatorSpec]
    seed_periods: int = 50       # REST candles to fetch at startup (binance_ws only)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StreamSpec":
        source = str(d.get("source", "feed")).lower()
        if source not in _VALID_SOURCES:
            raise ValueError(
                f"Stream {d.get('id')!r}: unknown source {d.get('source')!r}. "
                f"Valid: {sorted(_VALID_SOURCES)}"
            )
        indicators = [IndicatorSpec.from_dict(i) for i in d.get("indicators", [])]
        if not indicators:
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
        )


def load_config(path: str) -> "tuple[str, str, int, list[StreamSpec]]":
    """Load indicators.json. Returns (feed_addr, out_addr, min_ticks, streams)."""
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    feed_addr = cfg.get("zmq_feed_addr", _FEED_ADDR)
    out_addr  = cfg.get("zmq_out_addr",  _IND_ADDR)
    min_ticks = int(cfg.get("min_ticks", 25))
    raw_streams = [s for s in cfg.get("streams", []) if "id" in s]
    streams = [StreamSpec.from_dict(s) for s in raw_streams]
    if not streams:
        raise ValueError(f"Config {path!r}: at least one stream required")
    return feed_addr, out_addr, min_ticks, streams


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
    pub.send_json(out, zmq.NOBLOCK)
    if VERBOSE:
        keys = [k for k in out if k not in ("t", "token_id", "asset",
                                             "timeframe", "stream_id", "ts")]
        logger.debug("[PUB %s] %s  %s",
                     out.get("stream_id", "?"),
                     out.get("asset") or out.get("token_id", "?"),
                     "  ".join(f"{k}={out[k]:.4f}" for k in keys))


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
    logger.info("[feed] SUB connecté → %s", feed_addr)

    # For simplicity, apply ALL feed-source stream indicator specs to every token.
    # (Polymarket tokens are dynamic — we can't filter by asset name.)
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


# ─── MAIN RUN ────────────────────────────────────────────────────────────────

async def run(feed_addr: str, ind_addr: str,
              rsi_n: int, sma_n: int, ema_n: int, vol_n: int,
              min_ticks: int,
              config_path: str | None = None) -> None:
    """
    Orchestrate indicator streams.

    If config_path is given: load the JSON and spawn one asyncio task per stream.
    Otherwise: legacy mode — one ZMQ feed stream with CLI-specified periods.
    """
    ctx = zmq.asyncio.Context()
    pub = ctx.socket(zmq.PUB)

    if config_path:
        cfg_feed, cfg_out, cfg_min, streams = load_config(config_path)
        # env var overrides take precedence over config file addresses
        actual_feed = feed_addr if feed_addr != _FEED_ADDR else cfg_feed
        actual_out  = ind_addr  if ind_addr  != _IND_ADDR  else cfg_out
        actual_min  = cfg_min
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
        actual_min  = min_ticks

    pub.bind(actual_out)
    logger.info("PUB bind → %s", actual_out)

    feed_streams    = [s for s in streams if s.source == "feed"]
    binance_streams = [s for s in streams if s.source == "binance_ws"]

    tasks: list[asyncio.Task] = []  # type: ignore[type-arg]
    try:
        if feed_streams:
            tasks.append(asyncio.create_task(
                _zmq_feed_task(actual_feed, pub, feed_streams, actual_min)
            ))
        for bstream in binance_streams:
            tasks.append(asyncio.create_task(
                _binance_kline_task(bstream, pub, actual_min)
            ))
        if not tasks:
            logger.error("No streams to run — check your config")
            return
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
                   help=f"ZMQ address to publish on (default: {_IND_ADDR})")
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
        rsi_n=args.rsi,
        sma_n=args.sma,
        ema_n=args.ema,
        vol_n=args.vol,
        min_ticks=args.min_ticks,
        config_path=args.config,
    ))


if __name__ == "__main__":
    main()
