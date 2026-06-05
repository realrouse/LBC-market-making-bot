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
    python3 bot/scalping_bot.py --strategy strategies/scalping/scalping_candle_momentum.json
    python3 bot/scalping_bot.py --strategy strategies/scalping/scalping_meanrev.json
    python3 bot/scalping_bot.py --strategy strategies/scalping/scalping_breakout.json

Parameters (strategy JSON keys)
--------------------------------
All parameters live in the strategy JSON file and override the DEFAULTS dict.
Keys starting with ``_`` are documentation-only and are ignored by the bot.

Global — apply to all three strategies
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
strategy_type : str
    Which strategy logic to run. One of ``"candle_momentum"``, ``"meanrev"``,
    or ``"breakout"``. Must match the file being loaded.
symbol : str
    Binance trading pair (e.g. ``"BTCUSDT"``). Drives both the REST warm-up
    endpoint and the WebSocket ``<symbol>@kline_1m`` stream.
capital : float
    Starting capital in USDT. Updated in-memory after every closed trade;
    also persisted to the ``capital`` column in the SQLite trades table.
fee_rate : float
    Binance taker fee per side as a decimal (``0.001`` = 0.1 %).
    Applied on both open and close legs; round-trip cost = 2 × fee_rate.
slippage_pct : float
    Estimated market-order slippage per side as a decimal (``0.0005`` = 0.05 %).
    Entry price is nudged up by this fraction; exit is nudged down.
    Round-trip cost = 2 × slippage_pct + 2 × fee_rate ≈ 0.31 % at defaults.
stake_frac : float
    Fraction of current capital allocated per trade (``0.20`` = 20 %).
    One open position at a time; position size = capital × stake_frac.

candle_momentum (prefix ``cm_``)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Signal: bullish body ratio ≥ threshold  AND  volume z-score ≥ threshold
        AND  candle range ≥ min_range_pct × close.
Exit:   fixed TP / SL percentages from entry, or timeout.

cm_body_ratio_thresh : float
    Minimum ``(close − open) / (high − low)`` for a candle to qualify.
    ``0.60`` means the bullish body must span ≥ 60 % of the full range —
    filters out indecision candles and dojis.
cm_vol_z_window : int
    Number of past candles used to compute the volume z-score baseline
    (mean and standard deviation). Default 20.
cm_vol_z_thresh : float
    Minimum volume z-score required to confirm unusual activity.
    ``1.0`` = volume must be at least 1 standard deviation above the mean.
cm_min_range_pct : float
    Minimum candle range (``high − low``) as a fraction of close price.
    ``0.0003`` = 0.03 % — discards flat / near-doji candles where the
    body ratio calculation is unreliable.
cm_take_profit_pct : float
    TP distance from entry expressed as a fraction of entry price.
    ``0.006`` = 0.6 %; net edge after round-trip costs ≈ 0.29 %.
cm_stop_loss_pct : float
    SL distance from entry expressed as a fraction of entry price.
    ``0.003`` = 0.3 %; gives a gross 2:1 reward-to-risk ratio.
cm_max_hold_minutes : int
    Hard time limit in minutes. Position is closed at the current close
    price if neither TP nor SL is reached within this window. Default 10.
cm_vwap_gate : bool
    When True, skip long entries when the candle close is above the rolling
    VWAP. Entering above VWAP means chasing momentum into resistance; the
    gate preserves entries for genuine dip recoveries only. Default False.
cm_vwap_window : int
    Rolling VWAP lookback in candles used by the gate. 390 ≈ one 6.5-hour
    US session. Default 390.

meanrev (prefix ``mr_``)
~~~~~~~~~~~~~~~~~~~~~~~~~
Signal: close < Bollinger lower band  AND  close < VWAP × (1 − vwap_dev_thresh).
Exit:   TP = VWAP re-touch;  SL = entry − sl_atr_mult × ATR;  or timeout.

mr_bb_period : int
    Bollinger Band lookback in candles. Used for both the SMA mid-band and
    the standard deviation. Default 20.
mr_bb_std_mult : float
    Standard-deviation multiplier for the Bollinger bands.
    ``2.0`` produces the classic ±2σ envelope.
mr_vwap_window : int
    Rolling VWAP lookback in candles (``390`` ≈ one 6.5-hour US session).
    Price and volume from the last ``mr_vwap_window`` closed 1m candles
    are used; the VWAP re-touch level is used as take-profit.
mr_vwap_dev_thresh : float
    Minimum distance below VWAP required to trigger entry.
    ``0.005`` = price must be at least 0.5 % below VWAP — avoids chasing
    marginal dips that don't have meaningful mean-reversion potential.
mr_atr_period : int
    ATR lookback for stop-loss sizing. Default 14 candles.
mr_sl_atr_mult : float
    Stop-loss = entry − (mr_sl_atr_mult × ATR). An additional hard cap
    prevents SL from exceeding 2 % below entry (avoids runaway SL on
    low-ATR candles where ATR temporarily spikes).
mr_max_hold_minutes : int
    Hard time limit in minutes. Closed at current price if VWAP
    re-touch has not occurred. Default 60.

breakout (prefix ``bo_``)
~~~~~~~~~~~~~~~~~~~~~~~~~~
Signal: close breaks above the rolling max of prior highs (excluding current candle)
        AND  ATR / close ≥ bo_min_atr_pct (confirms the market is not flat).
Exit:   TP = entry + tp_mult × ATR;  SL = entry − sl_mult × ATR;  or timeout.

bo_range_period : int
    Number of candles for the rolling-high calculation.
    ``20`` means the bot watches whether the current close exceeds the
    highest high of the prior 20 candles (classic range-breakout trigger).
bo_atr_period : int
    ATR lookback used to size both TP and SL. Default 14 candles.
bo_min_atr_pct : float
    Minimum ``ATR / close`` ratio required to validate the breakout.
    ``0.001`` = 0.1 % — filters breakouts in flat / illiquid conditions
    where ATR-sized targets would be too small to overcome round-trip costs.
bo_sl_atr_mult : float
    Stop-loss distance = bo_sl_atr_mult × ATR below entry. Default 1.0.
bo_tp_atr_mult : float
    Take-profit distance = bo_tp_atr_mult × ATR above entry. Default 2.0,
    which gives a gross 2:1 reward-to-risk ratio relative to the SL.
bo_max_hold_minutes : int
    Hard time limit in minutes. Position closed at current price if TP/SL
    not reached. Default 120 (breakouts need more room to develop).
"""

import argparse
import asyncio
import json
import os
import signal
import sqlite3
from collections import deque
from pathlib import Path

import aiohttp
import websockets

from tradinetools.logging import setup_logger
from tradinetools.math import (
    sma_last        as _sma,
    ema_last        as _ema_last,
    atr_last        as _atr_last,
    bollinger_last  as _bollinger_last,
    vwap_last       as _vwap_last,
    vol_zscore_last as _vol_zscore_last,
    rolling_max_last as _rolling_max_last,
)

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

DEFAULTS = {
    # ── Global ────────────────────────────────────────────────────────────────
    "strategy_type":         "candle_momentum",  # "candle_momentum" | "meanrev" | "breakout"
    "symbol":                "BTCUSDT",          # Binance trading pair
    "capital":               10_000.0,           # starting capital (USDT)
    "fee_rate":              0.001,              # taker fee per side (0.1%)
    "slippage_pct":          0.0005,             # market-order slippage per side (0.05%)
    "stake_frac":            0.20,               # fraction of capital per trade (20%)

    # ── candle_momentum ───────────────────────────────────────────────────────
    # Entry: body_ratio >= thresh AND vol_zscore >= thresh AND range >= min_range_pct
    "cm_body_ratio_thresh":  0.60,   # min (close−open)/(high−low); 0.60 = body ≥ 60% of range
    "cm_vol_z_window":       20,     # candle lookback for volume z-score baseline
    "cm_vol_z_thresh":       1.0,    # min volume z-score (σ above mean) to confirm activity
    "cm_min_range_pct":      0.0003, # min (high−low)/close to filter doji/flat candles (0.03%)
    "cm_take_profit_pct":    0.006,  # TP = entry × (1 + pct); 0.6% → ~0.29% net after costs
    "cm_stop_loss_pct":      0.003,  # SL = entry × (1 − pct); 0.3% → 2:1 gross R:R
    "cm_max_hold_minutes":   10,     # forced close at current price after N minutes
    "cm_vwap_gate":          False,  # skip longs when close > rolling VWAP (above resistance)
    "cm_vwap_window":        390,    # VWAP rolling window (candles); 390 min ≈ one US session

    # ── meanrev ───────────────────────────────────────────────────────────────
    # Entry: close < BB_lower(period, std_mult) AND close < VWAP × (1 − vwap_dev_thresh)
    # Exit:  TP = VWAP re-touch;  SL = entry − sl_atr_mult × ATR
    "mr_bb_period":          20,     # Bollinger Band lookback (candles)
    "mr_bb_std_mult":        2.0,    # BB standard-deviation multiplier (classic ±2σ)
    "mr_vwap_window":        390,    # VWAP rolling window (candles); 390 min ≈ one US session
    "mr_vwap_dev_thresh":    0.005,  # min % below VWAP to trigger entry (0.5%)
    "mr_atr_period":         14,     # ATR lookback for SL sizing (candles)
    "mr_sl_atr_mult":        1.5,    # SL = entry − mult × ATR; hard cap at mr_sl_hard_cap_pct below entry
    "mr_sl_hard_cap_pct":    0.02,   # hard cap: SL no more than this fraction below entry (2%)
    "mr_max_hold_minutes":   60,     # forced close if VWAP re-touch not reached after N minutes

    # ── breakout ──────────────────────────────────────────────────────────────
    # Entry: close > rolling_max(highs[:-1], period) AND ATR/close >= min_atr_pct
    # Exit:  TP = entry + tp_mult × ATR;  SL = entry − sl_mult × ATR  (2:1 gross R:R)
    "bo_range_period":       20,     # rolling-max lookback for resistance level (candles)
    "bo_atr_period":         14,     # ATR lookback for TP/SL sizing (candles)
    "bo_min_atr_pct":        0.001,  # min ATR/close to confirm volatility (0.1%); filters flat markets
    "bo_sl_atr_mult":        1.0,    # SL = entry − mult × ATR
    "bo_tp_atr_mult":        2.0,    # TP = entry + mult × ATR; 2.0 → 2:1 gross R:R vs SL
    "bo_max_hold_minutes":   120,    # forced close after N minutes (breakouts need more time)
}


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
"""


# ---------------------------------------------------------------------------
# Bot class
# ---------------------------------------------------------------------------

class ScalpingBot:

    def __init__(self, config_path: str, install_dir: str = None):
        raw = json.loads(Path(config_path).read_text(encoding="utf-8"))
        self.p = dict(DEFAULTS)
        self.p.update({k: v for k, v in raw.items() if not k.startswith("_")})

        stype      = self.p["strategy_type"]
        self._dir  = Path(install_dir or os.path.expanduser("~/tradinebotte"))
        self._dir.mkdir(parents=True, exist_ok=True)

        log_path = self._dir / f"scalping_{stype}.log"
        self._log = setup_logger(f"scalping.{stype}", str(log_path))

        db_path = self._dir / f"scalping_{stype}.db"
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.executescript(_DDL)
        self._db.commit()

        pid_path = self._dir / f"scalping_{stype}.pid"
        pid_path.write_text(str(os.getpid()))

        self._buf: deque = deque(maxlen=BUFFER_SIZE)
        self._position   = None   # dict or None
        self._capital    = self.p["capital"]
        self._trades     = 0
        self._wins       = 0
        self._running    = True

        self._log.info("ScalpingBot started  strategy=%s  capital=%.2f",
                       stype, self._capital)
        self._log.info("Config: %s", config_path)
        if stype == "candle_momentum" and self.p.get("cm_vwap_gate", False):
            self._log.info("VWAP gate: enabled  window=%d candles  (skip longs above VWAP)",
                           self.p.get("cm_vwap_window", 390))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT,  self._handle_signal)

        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                await self._load_history(session)

            await self._ws_loop()
        finally:
            self._db.close()

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

            if hi >= tp and lo <= sl:
                # Both TP and SL touched in the same candle: TP is more likely
                # to have been hit first on a long when the candle has upside.
                self._close_position(tp, "take_profit", candle["ts_ms"])
                return
            if hi >= tp:
                self._close_position(tp, "take_profit", candle["ts_ms"])
                return
            if lo <= sl:
                self._close_position(sl, "stop_loss", candle["ts_ms"])
                return
            if hold >= max_hold:
                self._close_position(cl, "timeout", candle["ts_ms"])
                return

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

            if self.p.get("cm_vwap_gate", False):
                closes  = [c["close"]  for c in self._buf]
                volumes = [c["volume"] for c in self._buf]
                vwp = _vwap_last(closes, volumes, self.p.get("cm_vwap_window", 390))
                if vwp is not None and cl > vwp:
                    return

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
                if sl < cl * (1 - self.p.get("mr_sl_hard_cap_pct", 0.02)):
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
            atr_pct = atr / closes[-1] if closes and closes[-1] > 0 else 0
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
        pnl      = gross - fee_out - pos["cost"]
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
                    help="Strategy config JSON (e.g. strategies/scalping/scalping_meanrev.json)")
    ap.add_argument("--dir", default=None,
                    help="Install dir for logs/DB/PID (default: ~/tradinebotte)")
    args = ap.parse_args()

    bot = ScalpingBot(args.strategy, install_dir=args.dir)
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
