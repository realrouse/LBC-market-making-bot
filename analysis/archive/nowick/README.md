# No-Wick strategy — ARCHIVED (Phase-0 backtest rejected, 2026-07-09)

Evaluation of the FTMO ["No-Wick"](https://ftmo.com/en/blog/no-wick-strategy-how-to-read-momentum-without-a-single-indicator/)
strategy as a candidate CEX bot. **Rejected at the Phase-0 backtest gate — the live
engine (indicators primitives, `strategy_engines/nowick.py`, deploy, inventory row)
was never built.** Files kept here as reproducible evidence and a reusable multi-
timeframe candle/breakout backtest skeleton.

## Strategy tested
Long-only momentum: enter market on a wickless bullish signal candle
(`(high-close)/(high-low) < 0.10`, body/range ≥ 0.50) confirmed by RVOL ≥ 1.5, a
level breakout (close > prior 24h high), and a bullish 4h context; initial stop
below the signal-candle low, then a trailing ATR stop. Plus a **fade** counter-test
(short the same candle). Signal TF 15m aggregated from 1m data, 4h for context.

> Note: the article's "first 30–60 min of NY open" session filter was NOT wired in —
> a forex/prop-firm concept with no clean 24/7-crypto analogue. This tested no-wick
> *minus* the time filter. It does not overturn a long-only flat-at-zero-cost result.

## Verdict — no exploitable edge on BTC 15m spot
Data: `data/BTCUSDT3197d_20172026.db` (4.6M 1m candles, 2017–2026), cost 0.31% round-trip.

| Test | Result |
|---|---|
| Full history, literal config (momentum) | ×0.64 (−36%), 27.7% win, −$2591 fees |
| Parameter sweep (trail 2–12×ATR, wick, RVOL, 4h on/off) | every config loses; best ×0.91 |
| **Zero-cost idealisation (momentum)** | **×1.01 — flat, Sharpe 0.00** |
| Forward-return vs baseline (`--forward`) | −2.6 bps at k=4 (mean-reverts), +1.7–2.5 bps at k=8–32 — ≪ 30 bps cost |
| Fade (short), real cost | ×0.67–0.88 |
| **Fade, zero cost** | **×1.02 / ×0.99 — flat** |

Both directions are flat at zero cost → the no-wick candle is **noise w.r.t. future
BTC 15m returns**. FTMO is forex/index prop (low cost, session-driven liquidity);
on 24/7 crypto spot at 0.1%/side taker, a breakout-chase (or fade) has no edge that
survives fees. Being flat-at-zero-cost across a ×25 secular bull is itself damning.

## Reproduce (from repo root, `tradinebotte/`)
```
python3 analysis/archive/nowick/backtest_nowick.py --strategy analysis/archive/nowick/nowick_BTCUSDT.json            # full history
python3 analysis/archive/nowick/backtest_nowick.py --strategy analysis/archive/nowick/nowick_BTCUSDT.json --regimes  # bull/bear/range
python3 analysis/archive/nowick/backtest_nowick.py --strategy analysis/archive/nowick/nowick_BTCUSDT.json --forward   # exit-agnostic edge
```
Imports shared primitives from `analysis/backtest_scalping.py` (paths resolve to the
repo root regardless of this archived location).
