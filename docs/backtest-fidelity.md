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

## 7. DCA — driven; the one difference is a capital budget (Δ=0 under matched capital)

DCA *is* driven through the harness (`--strategy dca`). It's timer-gated, not price-triggered:
its buy cadence is a wall-clock timer (`now - last_buy_ts >= interval_s`, 4h) and it buys **at
market on candle close**. Two harness accommodations, both mechanical and verified harmless (see
the Δ=0 proof below), make the comparison faithful rather than artifact-dominated:
  * **candle-clock injection** — `_FakeClock` swaps the DCA module's `time` so `time.time()`
    returns the current candle's timestamp (a fast replay advances no real wall-clock, so the
    timer would otherwise fire once and stall).
  * **close-first tick scheme** — the candle is replayed close→low→high, so the timed market buy
    executes at the close (matching `run_dca`) and TP/SL still detect on low/high.

DCA's default `sl_pct=0` → **no stop-loss, no re-arm**, so none of swing's SL drift applies. At
the deployed config (`4h / $100 / TP 3% / max 5`):

| window | backtest trades / PnL | real engine trades / PnL | Δ |
|---|---|---|---|
| range | 27 / +$75.52 | 26 / +$72.72 | −1 / −$2.80 |
| bullrun | 76 / +$212.57 | 74 / +$206.98 | −2 / −$5.59 |
| bearmarket | 7 / +$19.58 | **7 / +$19.58** | **0 / $0.00** |

- **Dominant difference — the backtest enforces a capital budget; the live engine does not.**
  `run_dca` gates each buy on `usdt >= cost` and debits/credits a `$3000` cash balance; the live
  `DCAStrategy` (sim) **tracks no balance** and posts a buy every interval up to `max_positions`,
  regardless of cash. This is *masked* at `max_positions=5` (5 × $100 = $500 exposure ≪ $3000, so
  the slot limit binds before capital does). **Isolation proof:** loosen slots to
  `max_positions=1000` and the backtest starves (~30 buys on $3000) while the engine runs unbounded
  → Δ ≈ +200–440 trades; give the backtest **unlimited capital** at that slot count and it matches
  the engine **exactly** (505 / +$1412.48, **Δ=0**). So the capital cap is the *only* thing
  separating them when neither slots nor capital bind.
- **Second, minor difference — skip-vs-defer on slot saturation** (the ≤2-trade residuals above).
  When the interval timer fires while all slots are full, `run_dca` still advances its schedule
  (`while next_buy_ms <= ts_ms: next_buy_ms += interval_ms`, *before* the slot check) → it **drops**
  that installment; the engine only updates `last_buy_ts` inside `_place_buy` → it **defers** and
  buys the moment a slot frees. This nets to ≈0 when slots never saturate (max=1000) *or* always
  saturate (bearmarket: TPs rarely hit → both pinned at 5 → both stop, hence Δ=0 there), and shows
  up only under *intermittent* saturation (range/bullrun at max=5) as the engine trailing by 1–2
  trades / < $6. Same *class* of divergence as grid's half-grid and swing's SL-re-arm, but tiny
  and PnL-immaterial. (Not confused with the fixture's end-of-run straggler, which the parity
  test's settle candle handles separately.)
- **Actionable flag (no DCA bot live today; sim only):** in *live* mode an over-budget buy is
  simply order-rejected on insufficient balance, so `run_dca`'s capital cap is the realistic model.
  The gap is that the **sim** engine doesn't model bankroll exhaustion — so sim/backtest DCA runs
  with loose `max_positions` overstate activity vs a capital-bound reality. Keep `max_positions`
  sized to the budget, or add a sim balance guard. Documented; not changed.

## 8. Phase-3 parity guard (DONE) + what's left

**CI parity test — `tests/test_backtest_engine_parity.py` (implemented).** Pins the Δ=0 property
for **swing** (tiny no-SL fixture: arm BUY at support 97 → fill on a shallow dip → TP at resistance
103) and **DCA** (short fixture on realistic timestamps, ample capital + slots → the capital cap
never binds): the real engine driven through the harness and the re-implemented backtest must book
the *same* trades and PnL (to 6 dp). Any future change that breaks the engine↔backtest agreement on
the shared path — in either the engine or the backtest — fails CI. No `data/*.db` dependency (those
aren't in git); fixtures are built in code. Runs under the root `tests/` discovery.

Left (proposed, not started):
- **Retire or cross-check** the re-implemented CEX backtests now that the engine-driven path is
  validated for grid/swing/dca (keep them as an independent second opinion, or delete once parity
  is trusted). Not done — a judgement call for the maintainer.
- **Swing RSI(4h)/EMA200 gate**: feed historical indicators (currently disabled for the comparison).
- **Finer intra-candle replay** to extend parity to swinghold's partial-exit path (§6).

## 9. Accumulation — NOT driveable by this harness (architectural, not just missing data)

Accumulation is the one CEX strategy the harness structurally cannot drive, for two independent
reasons — neither fixable by feeding more price data:

1. **It is not a `strategy_engine`.** `accumulation_bot.py` is a standalone bot with its own
   `main()`/`_run()`/`while True` event loop and its own DB — it does **not** implement the
   `async on_book_update(state, ts)` seam every `strategy_engines/*` exposes and the harness drives.
   There is no method to feed ticks into. (Grid/swing/swinghold/dca all share that seam; accumulation
   predates/sidesteps it.)
2. **Its entries are gated on macro streams it pulls live, not on price.** The bot registers
   indicator streams (Fear&Greed, liquidations, long/short ratio, RSI-4h, VWAP) with the shared
   indicators service over a ZMQ REP socket (`_register_loop`) and gates every scale-in on them.
   These gates **are** the strategy (§2, 🔴). A klines replay carries none of them, and unlike
   swing's trend filter they can't simply be toggled off for an apples-to-apples run without
   deleting the strategy itself.

Driving it would require *either* refactoring `accumulation_bot` into an `on_book_update` engine
*and* reconstructing 5+ historical macro streams over a fake indicators service — a project, not a
harness extension. **Verdict: out of scope by construction.** Keep `scripts/backtest_accumulation.py`
as the accumulation model, with the standing caveat that it omits the macro-gate stack and so
overstates activity (§2).
