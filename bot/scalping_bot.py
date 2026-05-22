#!/usr/bin/env python3
"""
Binance 1m scalping bot — live / paper-trading.

Runs as a single instance per strategy. Three separate processes cover the
three scalping styles (candle_momentum, meanrev, breakout) on the scalping test account.

Startup sequence
----------------
1. Load strategy JSON config (--strategy).
2. Fetch the last 500 closed 1m candles from Binance REST API (warm-up).
3. Open WebSocket kline stream; process only closed candles (k.x == true).
4. On each closed candle: compute indicators → check exit → check entry.
5. Log all trades to SQLite (~/tradinebotte/scalping_<type>.db) and log file.

Orders are simulated (no API key needed). Position: single at a time.
Reconnects automatically on WebSocket drop (exponential backoff 1 s → 60 s).

Usage
-----
    python3 bot/scalping_bot.py --strategy strategies/scalping_candle_momentum.json
    python3 bot/scalping_bot.py --strategy strategies/scalping_meanrev.json
    python3 bot/scalping_bot.py --strategy strategies/scalping_breakout.json
"""

import argparse
import asyncio
import json
import logging
import logging.handlers
import math
import os
import signal
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import websockets

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WS_URL   = "wss://stream.binance.com:9443/ws/{stream}"
_REST_URL = "https://api.binance.com/api/v3/klines"
_TIMEOUT  = aiohttp.ClientTimeout(total=15)

BUFFER_SIZE = 500      # rolling candle buffer (covers mr_vwap_window=390 + margin)
_MIN_WARM   = 25       # minimum candles before signalling
_RECONNECT_BASE  = 1   # seconds
_RECONNECT_MAX   = 60

DEFAULTS = dict(
    strategy_type         = "candle_momentum",
    symbol                = "BTCUSDT",
    capital               = 10_000.0,
    fee_rate              = 0.001,
    slippage_pct          = 0.0005,
    stake_frac            = 0.20,
    cm_body_ratio_thresh  = 0.60,
    cm_vol_z_window       = 20,
    cm_vol_z_thresh       = 1.0,
    cm_min_range_pct      = 0.0003,
    cm_take_profit_pct    = 0.006,
    cm_stop_loss_pct      = 0.003,
    cm_max_hold_minutes   = 10,
    mr_bb_period          = 20,
    mr_bb_std_mult        = 2.0,
    mr_vwap_window        = 390,
    mr_vwap_dev_thresh    = 0.005,
    mr_atr_period         = 14,
    mr_sl_atr_mult        = 1.5,
    mr_max_hold_minutes   = 60,
    bo_range_period       = 20,
    bo_atr_period         = 14,
    bo_min_atr_pct        = 0.001,
    bo_sl_atr_mult        = 1.0,
    bo_tp_atr_mult        = 2.0,
    bo_max_hold_minutes   = 120,
)


# ---------------------------------------------------------------------------
# Indicator functions (duplicated from backtest_scalping.py — pure math)
# ---------------------------------------------------------------------------

def _sma(series, n):
    if len(series) < n:
        return None
    return sum(series[-n:]) / n


def _ema_last(series, n):
    if len(series) < n:
        return None
    k    = 2.0 / (n + 1)
    val  = sum(series[:n]) / n
    for x in series[n:]:
        val = x * k + val * (1 - k)
    return val


def _atr_last(highs, lows, closes, n):
    if len(closes) < n + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    return _ema_last(trs, n)


def _bollinger_last(closes, n, k):
    mid = _sma(closes, n)
    if mid is None:
        return None, None, None
    std = math.sqrt(sum((closes[-n + j] - mid) ** 2 for j in range(n)) / n)
    return mid + k * std, mid, mid - k * std


def _vwap_last(closes, volumes, n):
    if len(closes) < n:
        return None
    pv = sum(closes[-n + j] * volumes[-n + j] for j in range(n))
    v  = sum(volumes[-n + j] for j in range(n))
    return pv / v if v > 0 else closes[-1]


def _vol_zscore_last(volumes, n):
    if len(volumes) < n:
        return None
    w   = volumes[-n:]
    mu  = sum(w) / n
    std = math.sqrt(sum((v - mu) ** 2 for v in w) / n)
    return (volumes[-1] - mu) / std if std > 0 else 0.0


def _rolling_max_last(series, n):
    if len(series) < n:
        return None
    return max(series[-n:])


# ---------------------------------------------------------------------------
# SQLite schema
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_open_ms  INTEGER NOT NULL,
    ts_close_ms INTEGER NOT NULL,
    strategy    TEXT    NOT NULL,
    label       TEXT,
    entry_px    REAL    NOT NULL,
    exit_px     REAL    NOT NULL,
    pnl         REAL    NOT NULL,
    hold_min    REAL    NOT NULL,
    reason      TEXT    NOT NULL,
    capital     REAL    NOT NULL
);
CREATE TABLE IF NOT EXISTS candles (
    ts_ms   INTEGER PRIMARY KEY,
    open    REAL, high REAL, low REAL, close REAL, volume REAL
);
"""


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class ScalpingBot:

    def __init__(self, config_path: str, install_dir: str = None):
        raw = json.loads(Path(config_path).read_text())
        self.p = dict(DEFAULTS)
        self.p.update({k: v for k, v in raw.items() if not k.startswith("_")})

        stype      = self.p["strategy_type"]
        self._dir  = Path(install_dir or os.path.expanduser("~/tradinebotte"))
        self._dir.mkdir(parents=True, exist_ok=True)

        # Logging
        log_path = self._dir / f"scalping_{stype}.log"
        self._log = logging.getLogger(f"scalping.{stype}")
        self._log.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                datefmt="%Y-%m-%d %H:%M:%S")
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        self._log.addHandler(fh)
        self._log.addHandler(ch)

        # SQLite
        db_path = self._dir / f"scalping_{stype}.db"
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(_DDL)
        self._db.commit()

        # PID file
        pid_path = self._dir / f"scalping_{stype}.pid"
        pid_path.write_text(str(os.getpid()))

        # State
        self._buf: deque = deque(maxlen=BUFFER_SIZE)
        self._position   = None   # dict or None
        self._capital    = self.p["capital"]
        self._trades     = 0
        self._wins       = 0
        self._running    = True

        self._log.info("ScalpingBot started  strategy=%s  capital=%.2f",
                       stype, self._capital)
        self._log.info("Config: %s", config_path)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            await self._load_history(session)

        await self._ws_loop()

    def _handle_signal(self, signum, frame):
        self._log.info("Signal %d received — shutting down", signum)
        self._running = False

    # ------------------------------------------------------------------
    # History warm-up via REST
    # ------------------------------------------------------------------

    async def _load_history(self, session: aiohttp.ClientSession) -> None:
        symbol = self.p["symbol"].upper()
        try:
            async with session.get(
                _REST_URL,
                params={"symbol": symbol, "interval": "1m", "limit": BUFFER_SIZE},
            ) as resp:
                rows = await resp.json(content_type=None)
            for r in rows[:-1]:   # exclude the current open candle (last row)
                self._buf.append({
                    "ts_ms": r[0], "open": float(r[1]), "high": float(r[2]),
                    "low": float(r[3]), "close": float(r[4]), "volume": float(r[5]),
                })
            self._log.info("History loaded: %d candles (warm-up)", len(self._buf))
        except Exception as exc:
            self._log.warning("History load failed: %s — will warm up from stream", exc)

    # ------------------------------------------------------------------
    # WebSocket loop
    # ------------------------------------------------------------------

    async def _ws_loop(self) -> None:
        symbol  = self.p["symbol"].lower()
        ws_url  = _WS_URL.format(stream=f"{symbol}@kline_1m")
        backoff = _RECONNECT_BASE

        while self._running:
            try:
                self._log.info("Connecting to %s", ws_url)
                async with websockets.connect(ws_url, ping_interval=20,
                                              ping_timeout=30) as ws:
                    backoff = _RECONNECT_BASE
                    self._log.info("WebSocket connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        try:
                            msg = json.loads(raw)
                            k   = msg.get("k", {})
                            if k.get("x"):          # only closed candles
                                candle = {
                                    "ts_ms":  k["t"],
                                    "open":   float(k["o"]),
                                    "high":   float(k["h"]),
                                    "low":    float(k["l"]),
                                    "close":  float(k["c"]),
                                    "volume": float(k["v"]),
                                }
                                self._on_closed_candle(candle)
                        except Exception as exc:
                            self._log.warning("Message parse error: %s", exc)
            except Exception as exc:
                if not self._running:
                    break
                self._log.warning("WebSocket error: %s — reconnecting in %ds", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_MAX)

        self._log.info("Bot stopped  trades=%d  wins=%d  capital=%.2f",
                       self._trades, self._wins, self._capital)

    # ------------------------------------------------------------------
    # Per-candle logic
    # ------------------------------------------------------------------

    def _on_closed_candle(self, candle: dict) -> None:
        self._buf.append(candle)
        if len(self._buf) < _MIN_WARM:
            return

        hi = candle["high"]
        lo = candle["low"]
        cl = candle["close"]

        # Check exit for open position
        if self._position is not None:
            tp = self._position["tp"]
            sl = self._position["sl"]
            ts_open = self._position["ts_open_ms"]
            hold    = (candle["ts_ms"] - ts_open) // 60_000  # minutes

            max_hold = {
                "candle_momentum": self.p["cm_max_hold_minutes"],
                "meanrev":         self.p["mr_max_hold_minutes"],
                "breakout":        self.p["bo_max_hold_minutes"],
            }.get(self.p["strategy_type"], 60)

            if lo <= sl:
                self._close_position(sl, "stop_loss", candle["ts_ms"])
                return
            elif hi >= tp:
                self._close_position(tp, "take_profit", candle["ts_ms"])
                return
            elif hold >= max_hold:
                self._close_position(cl, "timeout", candle["ts_ms"])
                return

        # Check entry signal (only when flat)
        if self._position is None:
            self._check_entry(candle)

    def _check_entry(self, candle: dict) -> None:
        stype = self.p["strategy_type"]
        cl    = candle["close"]
        hi    = candle["high"]
        lo    = candle["low"]
        op    = candle["open"]
        ts    = candle["ts_ms"]

        if stype == "candle_momentum":
            rng = hi - lo
            if rng < cl * self.p["cm_min_range_pct"]:
                return
            body_r = (cl - op) / rng
            vol_z  = _vol_zscore_last(
                [c["volume"] for c in self._buf], self.p["cm_vol_z_window"])
            if (vol_z is not None
                    and body_r >= self.p["cm_body_ratio_thresh"]
                    and vol_z  >= self.p["cm_vol_z_thresh"]):
                tp = cl * (1 + self.p["cm_take_profit_pct"])
                sl = cl * (1 - self.p["cm_stop_loss_pct"])
                self._open_position(cl, tp, sl, ts, "cm_long")

        elif stype == "meanrev":
            closes  = [c["close"]  for c in self._buf]
            volumes = [c["volume"] for c in self._buf]
            highs   = [c["high"]   for c in self._buf]
            lows    = [c["low"]    for c in self._buf]
            _, _, bbl = _bollinger_last(closes, self.p["mr_bb_period"],
                                        self.p["mr_bb_std_mult"])
            vwp = _vwap_last(closes, volumes, self.p["mr_vwap_window"])
            atr = _atr_last(highs, lows, closes, self.p["mr_atr_period"])
            if bbl is None or vwp is None or atr is None:
                return
            if cl < bbl and cl < vwp * (1 - self.p["mr_vwap_dev_thresh"]):
                tp = vwp
                sl = cl - self.p["mr_sl_atr_mult"] * atr
                if sl < cl * 0.98:
                    return
                self._open_position(cl, tp, sl, ts, "mr_long")

        elif stype == "breakout":
            highs  = [c["high"]  for c in self._buf]
            lows   = [c["low"]   for c in self._buf]
            closes = [c["close"] for c in self._buf]
            rh  = _rolling_max_last(highs[:-1], self.p["bo_range_period"])
            atr = _atr_last(highs, lows, closes, self.p["bo_atr_period"])
            if rh is None or atr is None:
                return
            atr_pct = atr / closes[-2] if len(closes) >= 2 and closes[-2] > 0 else 0
            if cl > rh and atr_pct >= self.p["bo_min_atr_pct"]:
                tp = cl + self.p["bo_tp_atr_mult"] * atr
                sl = cl - self.p["bo_sl_atr_mult"] * atr
                self._open_position(cl, tp, sl, ts, "bo_long")

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    def _open_position(self, signal_px: float, tp: float, sl: float,
                       ts_ms: int, label: str) -> None:
        entry_px = signal_px * (1 + self.p["slippage_pct"])
        cost     = self._capital * self.p["stake_frac"]
        qty      = cost / entry_px
        fee_in   = cost * self.p["fee_rate"]
        self._capital -= fee_in
        self._position = {
            "entry_px":  entry_px,
            "tp":        tp,
            "sl":        sl,
            "ts_open_ms": ts_ms,
            "qty":       qty,
            "cost":      cost,
            "label":     label,
        }
        self._log.info(
            "▶ OPEN  %-12s  entry=%.2f  tp=%.2f  sl=%.2f  capital=%.2f",
            label, entry_px, tp, sl, self._capital,
        )

    def _close_position(self, exit_px: float, reason: str, ts_ms: int) -> None:
        if self._position is None:
            return
        pos      = self._position
        gross    = pos["qty"] * exit_px * (1 - self.p["slippage_pct"])
        fee_out  = gross * self.p["fee_rate"]
        pnl      = gross - fee_out - pos["cost"] - pos["cost"] * self.p["fee_rate"]
        hold_min = (ts_ms - pos["ts_open_ms"]) / 60_000
        self._capital += pnl
        self._trades  += 1
        if pnl > 0:
            self._wins += 1

        self._log.info(
            "◀ CLOSE %-12s  exit=%.2f  pnl=%+.2f  reason=%-12s  "
            "hold=%.1fmin  capital=%.2f  trades=%d  W/L=%d/%d",
            pos["label"], exit_px, pnl, reason,
            hold_min, self._capital, self._trades,
            self._wins, self._trades - self._wins,
        )

        try:
            self._db.execute(
                "INSERT INTO trades "
                "(ts_open_ms,ts_close_ms,strategy,label,entry_px,exit_px,"
                "pnl,hold_min,reason,capital) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (pos["ts_open_ms"], ts_ms, self.p["strategy_type"],
                 pos["label"], pos["entry_px"], exit_px,
                 pnl, hold_min, reason, self._capital),
            )
            self._db.commit()
        except Exception as exc:
            self._log.warning("DB write failed: %s", exc)

        self._position = None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Binance 1m scalping bot")
    ap.add_argument("--strategy", required=True, metavar="JSON",
                    help="Strategy config JSON (e.g. strategies/scalping_meanrev.json)")
    ap.add_argument("--dir", default=None,
                    help="Install dir for logs/DB/PID (default: ~/tradinebotte)")
    args = ap.parse_args()

    bot = ScalpingBot(args.strategy, install_dir=args.dir)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
