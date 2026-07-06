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

## 5. Phase-2 result (swing) — the drift, isolated to one behavior

`python3 analysis/backtest_engine.py <db> --strategy swing` — 4 support / 4 resistance levels
(`_DEF_*_OFFSETS` × first-open), `$200`/order, trend filter OFF, `max_positions == #supports`
so both sides arm the **same level set** (else the engine arms only the top-3 at init — a
level-set confound; forcing them equal isolates the one behavioral difference below).

| window (90d) | backtest trades / PnL | real engine trades / PnL | Δ |
|---|---|---|---|
| range (opens down) | 46 / −$1.50 | **4 / −$17.72** | −42 / −$16.22 |
| **bullrun (opens up)** | **3 / +$16.96** | **3 / +$16.96** | **0 / $0.00** |
| bearmarket (opens down) | 7 / −$30.77 | **4 / −$22.33** | −3 / +$8.44 |

- **The bullrun row is the isolation proof: exact match (Δ=0).** When price only rises, every
  exit is a take-profit, both sides re-arm identically, and the strategies are indistinguishable.
- **Root cause of the down-window drift — the engine never re-arms a support after a stop-loss.**
  Live `SwingStrategy` places BUYs at only two sites: `_initialise` (once) and `_on_sell_filled`
  (**TP** re-arm). `_close_sl` marks the position `closed` and does nothing else. So once a
  support stops out it is **retired for the rest of the run**. The re-implemented backtest instead
  `locked`s a level on close and **unlocks it when price recovers above the support** (`h >= sp`)
  — i.e. it re-arms after a stop-loss. On any window that opens with a dip through the initial
  SLs, the live bot fires each support once and then goes **fully dormant**, while the backtest
  keeps cycling.
- **Direction is path-dependent, not a fixed bias** (unlike grid's structural +48%): the engine
  loses *more* than the backtest in the range window but *less* in the bearmarket (its dormancy
  protects capital). So neither "over-" nor "understates" is a general statement for swing — the
  sign depends on whether post-SL re-entries would have won or lost.

**Actionable flag (deployed swing = acct-5, sim):** the no-re-arm-after-SL behavior means the
live swing bot can permanently stop trading a level after a single stop-loss — plausibly
unintended. Worth a product decision: keep (conservative "don't catch a falling knife") or add a
recovery-based re-arm to match the backtest's assumption. Documented here; not changed. **This
flag also covers swinghold** (see §6), which shares swing's entry/SL path.

## 6. SwingHold — one solid result, one harness limit (not confirmed drift)

`--strategy swinghold` (same S/R levels, `sell_fraction=0.30`):

| window | backtest trades / PnL | real engine trades / PnL |
|---|---|---|
| range (down) | 37 / −$13.82 | **4 / −$17.72** ← *identical to swing's engine* |
| bullrun (up) | 1 / +$15.63 | 1 / +$11.03 |
| bearmarket (down) | 7 / −$30.77 | **4 / −$22.33** ← *identical to swing's engine* |

- **Solid result — swinghold degenerates to swing on the SL path.** In both down windows the
  engine's numbers are *identical* to swing's (4 / −$17.72, 4 / −$22.33): when nothing reaches
  resistance, the partial-sell path never triggers and swinghold *is* swing — so it **inherits the
  same no-re-arm-after-SL dormancy** (folded into the §5 flag).
- **The bullrun partial-exit gap is NOT cleanly isolated → treat as a harness limit, not drift.**
  Unlike swing (where the bullrun gave Δ=0 and pinned the cause), swinghold's bullrun gap survives
  the isolation test: forcing `sell_fraction=0.99` (single-exit; 1.0 is rejected by the engine's
  `(0,1)` guard) does *not* drive Δ→0 — it makes it worse and splits the trade count (bt 1 vs
  engine 3). So the gap is a mix the harness can't separate on this data: (a) the low→high replay
  fills at most **one partial sell per candle**, while `run_swinghold` can sweep several
  resistances in a single candle; (b) remainder (`hold_fraction`) disposal + re-arm/trade-counting
  differ between the two. **No confirmed deployed-drift claim for swinghold's partial-exit path** —
  it needs a finer intra-candle replay to compare faithfully.

## 7. DCA — documented, not driven (artifacts would dominate)

DCA is **not** run through the harness. Its buy cadence is a wall-clock timer
(`now - last_buy_ts >= interval_s`, e.g. 4h) and it buys **at market on candle close**, whereas
the price-replay harness advances no real wall-clock and feeds low/high ticks, not close. Driving
it faithfully would need both a **candle-clock injection** (monkeypatch `time.time`) *and* a
close-price tick — two harness artifacts that would dominate any real signal, so the comparison
could not be cleanly isolated the way swing's was. Same category as accumulation (§2, 🔴): keep
`backtest_swing_dca.run_dca` as the DCA model; do not claim engine-parity for it here.

## 8. Next (proposed, not started)
- Swing RSI(4h)/EMA200 gate: feed historical indicators (currently disabled for the comparison).
- **Phase 3**: retire the re-implemented CEX backtests (or keep as a cross-check) + a CI parity
  test — the **swing bullrun fixture (Δ=0) is the natural assertion**: replay it through engine
  and harness, assert equal trades/PnL. A finer intra-candle replay would extend parity to
  swinghold's partial-exit path.
- **Accumulation**: separately, either feed historical gate streams or document it as a
  "mechanical DCA, no gates" upper bound.
