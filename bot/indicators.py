#!/usr/bin/env python3
"""
indicators.py — Technical indicator service

Subscribes to a ZeroMQ PUB feed (feed.py on port 5557) and computes
rolling technical indicators (RSI, SMA, EMA, volatility) on the best_bid
price series of each token. Publishes enriched indicator messages on a
new PUB socket (default: port 5559).

Input messages consumed (from feed.py):
  {"t": "book", "token_id": "...", "best_bid": ..., ...}

Output messages published:
  {"t": "indicators", "token_id": "...", "ts": ...,
   "rsi_14": ..., "sma_20": ..., "ema_9": ..., "vol_20": ...}
  (only published once min_ticks history is accumulated and all
   indicator periods are satisfied)

Usage:
  python3 bot/indicators.py
  python3 bot/indicators.py --verbose
  python3 bot/indicators.py --rsi 14 --sma 20 --ema 9 --vol 20
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 python3 bot/indicators.py
  TRADINEBOTTE_INDICATORS_ADDR=tcp://127.0.0.1:5560 python3 bot/indicators.py
"""

import argparse, asyncio, logging, math, os, sys, time
from collections import deque
from typing import Any

import zmq, zmq.asyncio

# ─── CONFIGURATION ───────────────────────────────────────────────────────────
_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR",       "tcp://127.0.0.1:5557")
_IND_ADDR  = os.environ.get("TRADINEBOTTE_INDICATORS_ADDR", "tcp://127.0.0.1:5559")
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT,
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("indicators")

# Set by _parse_args()
VERBOSE = False


# ─── INDICATOR MATH (pure stdlib, no numpy) ──────────────────────────────────

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
    """Ring-buffer price history + indicator computation for one token."""

    def __init__(self, maxlen: int = 200) -> None:
        self._prices: deque[float] = deque(maxlen=maxlen)

    def push(self, price: float) -> None:
        self._prices.append(price)

    def __len__(self) -> int:
        return len(self._prices)

    def indicators(self, rsi_n: int, sma_n: int, ema_n: int,
                   vol_n: int) -> dict[str, float | None]:
        p = list(self._prices)
        return {
            f"rsi_{rsi_n}": compute_rsi(p, rsi_n),
            f"sma_{sma_n}": compute_sma(p, sma_n),
            f"ema_{ema_n}": compute_ema(p, ema_n),
            f"vol_{vol_n}": compute_volatility(p, vol_n),
        }


# ─── MAIN ASYNC LOOP ─────────────────────────────────────────────────────────

async def run(feed_addr: str, ind_addr: str,
              rsi_n: int, sma_n: int, ema_n: int, vol_n: int,
              min_ticks: int) -> None:
    ctx = zmq.asyncio.Context()

    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.connect(feed_addr)
    logger.info("SUB connecté → %s", feed_addr)

    pub = ctx.socket(zmq.PUB)
    pub.bind(ind_addr)
    logger.info("PUB bind → %s", ind_addr)

    series: dict[str, PriceSeries] = {}

    try:
        while True:
            msg: dict[str, Any] = await sub.recv_json()
            if msg.get("t") != "book":
                continue

            token_id = msg.get("token_id", "")
            bid = msg.get("best_bid")
            if not token_id or bid is None:
                continue

            s = series.setdefault(token_id, PriceSeries())
            s.push(float(bid))

            if len(s) < min_ticks:
                continue

            ind = s.indicators(rsi_n, sma_n, ema_n, vol_n)
            if any(v is None for v in ind.values()):
                continue

            out: dict[str, Any] = {
                "t":        "indicators",
                "token_id": token_id,
                "ts":       int(time.time() * 1000),
                **ind,
            }
            pub.send_json(out, zmq.NOBLOCK)

            if VERBOSE:
                logger.debug("[PUB indicators] token=%.12s rsi=%.1f sma=%.4f",
                             token_id,
                             ind.get(f"rsi_{rsi_n}", 0.0) or 0.0,
                             ind.get(f"sma_{sma_n}", 0.0) or 0.0)
    finally:
        sub.close()
        pub.close()
        ctx.term()


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="tradinebotte technical indicator service")
    p.add_argument("--feed", default=_FEED_ADDR, metavar="ADDR",
                   help=f"ZMQ address to subscribe to (default: {_FEED_ADDR})")
    p.add_argument("--out", default=_IND_ADDR, metavar="ADDR",
                   help=f"ZMQ address to publish on (default: {_IND_ADDR})")
    p.add_argument("--rsi",       type=int, default=14, metavar="N",
                   help="RSI period (default: 14)")
    p.add_argument("--sma",       type=int, default=20, metavar="N",
                   help="SMA period (default: 20)")
    p.add_argument("--ema",       type=int, default=9,  metavar="N",
                   help="EMA period (default: 9)")
    p.add_argument("--vol",       type=int, default=20, metavar="N",
                   help="Volatility window (default: 20)")
    p.add_argument("--min-ticks", type=int, default=25, metavar="N",
                   help="Min price ticks before publishing (default: 25)")
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
    ))


if __name__ == "__main__":
    main()
