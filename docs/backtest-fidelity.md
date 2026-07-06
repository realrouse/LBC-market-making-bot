# Backtest fidelity — divergence backtest ↔ strategy_engines

Investigation of whether the backtests faithfully model the *deployed* strategies, and a
plan to unify them by driving the real engines.

## 1. Coverage — the backtests span Polymarket AND CEX

- **Polymarket**: `analysis/backtest.py` (threshold/OBI, `snapshots` table), `backtest_stake_secs.py`.
- **CEX**: `backtest_grid.py` (grid), `backtest_swing_dca.py` (swing/DCA), `backtest_scalping.py`,
  `backtest_orderbook.py`, `backtest_cycle_strategy.py`, `backtest_volfilter.py`,
  `scripts/backtest_accumulation.py`.

They **re-implement** the strategy logic standalone — they import neither `strategy_engines`
nor the `api_*` adapters. Only `tradinetools.pnl.round_trip_pnl` is shared. So the money math
is faithful by construction; the risk is the *decision* logic drifting from live.

## 2. Per-strategy fidelity verdict

- 🟢 **Grid** — core formulas match (`step=(upper−lower)/(levels−1)`, fee 0.1%, `capital=levels×size`).
  Deployed config = `grid_trail_mode=static` → halts on boundary exit; backtest default `--trail off`
  stops too. **BUT** a large structural divergence at init (see §4). Trail-mode-only gaps
  (not deployed): recenter range-width (live fixed-absolute vs backtest re-derived), and the
  backtest caps recenters at 10 while live is uncapped. acct-6 runs **MEXC Futures** but
  `backtest_grid.py` models **Binance spot** (no funding/perp) → doesn't predict acct-6.
- 🟢 **Swing** — TP = lowest resistance above entry (else pct fallback) matches; `round_trip_pnl`
  shared; 0.1% fee. Optional **RSI(4h) gate** exists live → backtest needs historical RSI or runs
  with the filter stale.
- 🔴 **Accumulation** (deployed ×3) — **major divergence**. The backtest models the primary
  scale-in signal (OBI/dip) but **omits the entire macro-gate stack** (Fear&Greed, liquidations,
  L/S ratio, RSI-4h, VWAP gate) because it replays **klines only** — it can't see those streams.
  Live entries are heavily gated → backtest overstates activity and misses boosts. **Do not use
  `backtest_accumulation` to calibrate the live bot** without historical gate streams.

Cross-cutting: fills modelled as "candle high/low touch" (all backtests) vs real order fills
(live) — a standard approximation, not a bug.

## 3. Unification — drive the real engines (feasible & clean for CEX engines)

The engines expose one seam — `async on_book_update(state, ts)` — and **already self-simulate
fills in sim mode**: no API key → `post_order` returns `sim_` ids (no HTTP), and `_poll_fills`
(called *from* `on_book_update`) detects fills from `ts.best_ask`/`ts.best_bid`. So a faithful
backtest is: **feed historical prices to the real engine's `on_book_update` and read its own
cycle/PnL accounting.** No re-implementation → no drift.

Harness = `analysis/backtest_engine.py`: replay klines → `ts(best_bid, best_ask)` per candle,
the **real** `api_binance` module in sim mode as the connector, in-memory sqlite `state`,
`poll_interval=0`. One harness drives grid / swing / dca / swinghold.

**Scope**: CEX engines only. NOT `backtest.py` (Polymarket, not a `strategy_engine`) nor
accumulation (standalone bot + macro gates needing historical F&G/liq/LS/RSI). Swing's RSI(4h)
gate needs historical RSI or stale mode.

**Caveats**: intra-candle order (replay low→high manufactures a within-candle buy→sell cycle —
inherited from the old backtests); `poll_interval=0` (the live 2s gate is wall-clock, which a
fast replay would skip).

## 4. Phase-1 PoC result (grid) — the drift, quantified and explained

`python3 analysis/backtest_engine.py data/BTCUSDT_1m90d_range_20260208-20260509.db`
(90d / 129,600 candles, same grid `[60508, 81863]`, 30 levels, $50):

| | cycles | realized PnL | $/cycle |
|---|---|---|---|
| `backtest_grid.py` (re-implemented) | 168 | +$74.34 | $0.44 |
| **real grid engine** | **249** | **+$106.61** | $0.43 |
| **Δ** | **+81 (+48%)** | **+$32.26 (+43%)** | ≈ 0 |

- **$/cycle identical (~$0.43)** → fee/PnL math is aligned.
- **+48% cycles** → root cause: `backtest_grid._init_buys` arms **only BUYs below center**
  (half-grid), while the live `_initialise_grid` places **BUYs below AND SELLs above** the
  current price (full two-sided grid). The engine trades both sides from t0; the backtest can't
  act on upward moves until a buy fills first. So the re-implemented backtest **understates the
  deployed grid's activity/PnL by ~33%** — it models a structurally different strategy.

**Conclusion**: the unification is validated — driving the real engine both eliminates the drift
and, as a diagnostic, exposed a concrete half-grid-vs-full-grid discrepancy in the legacy
backtest.

## 5. Next (proposed, not started)
- **Phase 2**: swing (+ RSI handling), then dca/swinghold "for free" on the same harness.
- **Phase 3**: retire the re-implemented CEX backtests (or keep as a cross-check) + a CI parity
  test (replay a fixture through engine and harness, assert equal trades).
- **Accumulation**: separately, either feed historical gate streams or document it as a
  "mechanical DCA, no gates" upper bound.
