# HOWTO — Tests and Backtests

> 🇫🇷 [Version française](HOWTO_tests_and_backtests.fr.md)

This guide explains how to run the automated test suite and the backtest engine,
defines every concept, field, and column that appears in their output, and describes
how to interpret the results to make informed strategy decisions.

---

## Table of contents

1. [Glossary](#1-glossary)
2. [Running the tests](#2-running-the-tests)
3. [Running a backtest](#3-running-a-backtest)
4. [All backtest flags](#4-all-backtest-flags)
5. [Standard output explained](#5-standard-output-explained)
6. [Grid search — `--sweep` and `--sweep-all`](#6-grid-search----sweep-and---sweep-all)
7. [Three-way comparison — `--compare`](#7-three-way-comparison----compare)
8. [The STOP/GHOST modelling gap](#8-the-stopghost-modelling-gap)
9. [Full workflow — `strategy_compare.sh`](#9-full-workflow----strategy_comparesh)
10. [Strategy JSON files](#10-strategy-json-files)
11. [Interpreting results and making decisions](#11-interpreting-results-and-making-decisions)
12. [Other backtest scripts](#12-other-backtest-scripts)

---

## 1. Glossary

### Market and price data

| Term | Definition |
|------|-----------|
| **Snapshot** | A price record written to the `snapshots` SQLite table every ~5 seconds by the live bot. Each row captures one market at one instant: best bid, best ask, volumes, OBI, seconds remaining. |
| **best_bid** | Highest price a buyer is currently willing to pay for the YES token (0 to 1). A value of 0.96 means buyers think the market resolves YES with 96% probability. |
| **best_ask** | Lowest price a seller is willing to accept. Always ≥ best_bid. A value ≥ 1.0 means the market is already resolved. |
| **bid_vol** | Total USD volume on the top 5 bid levels of the order book. |
| **ask_vol** | Total USD volume on the top 5 ask levels of the order book. |
| **OBI** | **Order Book Imbalance** = `(bid_vol − ask_vol) / (bid_vol + ask_vol)`. Ranges from −1 (all sellers) to +1 (all buyers). A negative OBI signals sell pressure. Formula computed over the top 5 levels on each side. |
| **secs_remaining** | Seconds until the market closes. Markets with zero or negative seconds are expired. |
| **spread** | `best_ask − best_bid`. A tight spread (< 0.01) indicates a liquid market. |

### Trade outcomes

| Outcome | When it occurs |
|---------|---------------|
| **WIN** | `best_bid` reaches the WIN threshold (≥ 0.99). The trade is closed at a profit. Net PnL = `tokens × 1.0 − stake − fee`. |
| **LOSS** | `best_bid` drops to the LOSS threshold (≤ 0.01). The trade is closed at a near-total loss. Net PnL ≈ `−stake`. |
| **OPEN** | The market expired before either threshold was reached and the data ended. The trade remains unresolved. Only the backtest engine produces OPEN; the live bot handles these as GHOST or WIN/LOSS at expiry. |
| **STOP** | Live bot only. The daily stop-loss was hit during the session; the bot halted all new trades for the rest of that day. STOP trades are counted in the actual stats but are not modelled by the backtest engine. |
| **GHOST** | Live bot only. A trade was entered but the market expired with no recorded WIN or LOSS exit (e.g. WebSocket disconnection, API timeout). Counted in actual stats but not modelled in backtest. |

### Strategy parameters

| Parameter | Default | Where it lives | Meaning |
|-----------|---------|---------------|---------|
| `signal_threshold` | 0.95 | strategy JSON / `--threshold` | Minimum `best_bid` required to open a trade. The primary entry signal. |
| `entry_max` | 0.998 | strategy JSON | Maximum `best_bid` accepted. Guards against already-resolved markets that slip through the time filter. |
| `min_secs_remaining` | 45 | strategy JSON / `--min-secs` | Minimum seconds left in the market at entry. Too short → no time to recover; too long → less accurate signal. |
| `min_ask_vol` | 10 | strategy JSON / `--min-ask` | Minimum ask-side liquidity in USD. Entries are skipped when the market is too illiquid. 0 = disabled. |
| `obi_reject_thresh` | −0.25 (CLI) / −0.65 (piste3 live) | strategy JSON / `--obi` | OBI floor. Entries are rejected when `OBI < obi_reject_thresh`, i.e. when sell pressure is too strong. CLI flag `--obi` defaults to −0.25; the active live strategy (piste3) uses −0.65. |
| `stake` | 10 | strategy JSON / `--stake` | USD amount wagered per trade. |
| `daily_stop_loss` | 30 | strategy JSON | Maximum cumulative loss per calendar day (UTC). When reached, the bot stops entering new trades for the rest of the day. |
| `capital_start` | 100 | strategy JSON | Starting capital for a backtest run. Used as the denominator for PnL%. |
| `win_threshold` | 0.99 | strategy JSON | `best_bid` at which an open trade is auto-resolved as WIN. Do not change without re-running the full backtest. |
| `loss_threshold` | 0.01 | strategy JSON | `best_bid` at which an open trade is auto-resolved as LOSS. Do not change without re-running the full backtest. |
| `fee_rate` | 2% | constant in `live_bot.py` | Polymarket taker fee applied to every trade. |

### Performance metrics

| Metric | Definition |
|--------|-----------|
| **Trades** | Total simulated (or actual) trades including OPEN, WIN, LOSS. In backtest, open trades at end of data are listed separately. |
| **WR% (win rate)** | `wins / (wins + losses) × 100`. OPEN, STOP, GHOST are excluded from the denominator. Normal range: 97–99%. |
| **Total PnL** | Net profit/loss in USD over all resolved trades. PnL per WIN ≈ `stake × (1/entry_price − 1) × (1 − fee_rate)`. PnL per LOSS = `−stake`. |
| **PnL%** | `total_pnl / capital_start × 100`. Comparable across configurations regardless of stake or capital size. |
| **MaxDD (max drawdown)** | Worst cumulative loss in a single calendar day across all sessions. A proxy for risk exposure. Expressed in USD. |
| **PnL/DD ratio** | `total_pnl / max_drawdown`. A Calmar-style risk-adjusted metric. Higher is better. A ratio ≥ 3.5 is good; ≥ 4.0 is excellent. A ratio of `∞` means zero drawdown (treat as suspicious — too few trades or very favourable data). |

### Backtest terminology

| Term | Definition |
|------|-----------|
| **Backtest** | Replay of the `snapshots` table through the strategy logic. The engine reads rows chronologically, applies entry/exit conditions, and accumulates simulated PnL. No real orders are placed. |
| **Aligned backtest** | A **simulation** (replay of `snapshots` data, no real orders) that uses the parameters the bot *actually had at runtime*, inferred from the `trades` table. It is still purely simulated — but its parameters (threshold, stake, min_secs, capital_start, DSL) are corrected to match the bot's real configuration, making it the most accurate simulation of what *should* have happened. Shown as the middle column in `--compare`. |
| **Bot réel** | The **actual trades executed** by the live bot on Polymarket, as recorded in the `trades` table during a real session. These are not simulated — they represent what *really* happened: real money, real order fills at market prices, real execution latency. Includes outcomes not modelled in backtest (STOP, GHOST), and is affected by WebSocket interruptions and API delays. Shown as the right column in `--compare`. |
| **Capital reset** | When multiple DB files are processed, each file starts with a fresh `capital_start`. This isolates sessions so a bad day in one file does not affect another. |
| **Sweep / grid search** | Running the backtest for every combination of parameter values in a predefined grid and ranking the results. Used to find optimal strategy parameters. |
| **Deduplication (--top)** | The sweep table can contain near-identical rows where only `min_ask` or `dsl` differs but the result is identical. `--top N` collapses the table to the N best *unique* configurations (deduped on `threshold / min_secs / obi`). |

---

## 2. Running the tests

### Quick start

```bash
bash scripts/run_tests.sh
```

This script:
1. Locates the project virtual environment (`.venv/` or `~/tradinebotte/venv/`).
2. Runs the full unittest suite (`tests/test_*.py`) with verbose output.
3. Runs the data quality check (`analysis/check_data_quality.py --no-gaps --warn-only`) on any `data/*.db` files found (non-blocking).
4. Runs a backtest `--all` on any `data/*.db` files found (non-blocking).
5. Invokes the `doc-sync` agent to audit flag documentation (requires `claude` CLI).

### Reading the output

```
test_compute_fee_win (TestComputeFee) ... ok
test_parse_book_message_bid (TestParseBookMessage) ... ok
...
Ran 368 tests in 12.8s
OK (skipped=1)
```

| Status | Meaning |
|--------|---------|
| `ok` | Test passed. |
| `FAIL` | A test assertion failed — the code does not match the expected behaviour. |
| `ERROR` | An unexpected exception was raised inside the test. |
| `skipped` | Test was intentionally skipped (usually: required DB file absent). |
| `OK` at the end | All tests passed. |
| `FAILED (failures=N)` | N tests failed — investigate before committing. |

### Test files and what they cover

| File | Classes | What is tested |
|------|---------|---------------|
| `tradinebotte-polymarket/tests/test_bot.py` | 20 classes | Core bot logic: fee calculation, book message parsing, signal detection, trade resolution, daily PnL cache, DB schema migrations, strategy file loading, HTML escaping, OBI filter, trading-hour filter, circuit breaker |
| `tests/test_backtest.py` | 9 classes | Backtest engine: `run_backtest`, `summarize`, `_ratio`, percentile helper, `detect_actual_params`, `_actual_stats`, `_collect_dbs` |
| `tradinebotte-polymarket/tests/test_regression.py` | 2 classes | **Performance regression** against `data/paper3.db` (WR ≥ 98%, PnL ≥ $80, MaxDD < $100); **parameter consistency** between `tradinebotte-polymarket/live_bot.py` constants and `analysis/backtest.py` defaults — these two must always agree |
| `tradinebotte-polymarket/tests/test_multibot.py` | 4 classes | Multi-bot feed and account bot integration (register_market, two-bot coordination) |
| `tests/test_api_cex.py` | 10 classes | CEX adapter contract: fee calculation, metadata parsing, order book parsing for Binance and MEXC |

### The regression tests

`TestBacktestPerformance` is the most important safety net. It runs the backtest on `data/paper3.db` with the current default parameters and asserts:

- Snapshot count is plausible (≥ 2 700)
- Win rate ≥ 98%
- Total PnL ≥ $80
- Losses < 50
- MaxDD < $100
- Capital accounting identity holds

If any parameter change silently degrades performance, this test catches it. It is automatically **skipped** when `data/paper3.db` is absent (e.g. fresh CI checkout without data).

`TestParamConsistency` checks that the parameter values in `tradinebotte-polymarket/live_bot.py` (module-level constants) match the defaults in `analysis/backtest.py` (the `Params` dataclass). If they diverge, backtests no longer predict live performance.

---

## 3. Running a backtest

### Database resolution (when no `--db` or `--all` flag is given)

The backtest engine tries the following in order and uses the first match:

1. `$TRADINEBOTTE_DIR/live.db` — the live bot database, if it has ≥ 100 snapshots.
2. `data/paper3.db` — the paper-trading session (764k snapshots).
3. `data/backtest_sample_btc5m_range_2026.db` — the bundled sample dataset.

### Basic usage

```bash
# Default: auto-select best available DB, default strategy parameters
python3 analysis/backtest.py

# Explicit file
python3 analysis/backtest.py --db ~/tradinebotte/live.db

# Multiple files (independent capital per file)
python3 analysis/backtest.py --db data/session_a.db data/session_b.db

# Shell glob (same as above, shell expands the pattern)
python3 analysis/backtest.py --db data/*.db

# Scan data/ automatically + live.db if usable
python3 analysis/backtest.py --all
```

---

## 4. All backtest flags

### Database selection

| Flag | Description |
|------|-------------|
| `--db PATH [PATH…]` | One or more explicit database file paths. Accepts shell globs (expanded by the shell, not the script). |
| `--all` | Scan `data/` for all `.db` files and prepend `live.db` if it has ≥ 100 snapshots. |

### Strategy parameter overrides

These flags override the corresponding value from the strategy JSON for a single run. They do not permanently change the strategy file.

| Flag | Default | Description |
|------|---------|-------------|
| `--threshold FLOAT` | 0.95 | Entry signal: minimum `best_bid` to open a trade. |
| `--min-secs FLOAT` | 30.0 | Minimum seconds remaining at entry. |
| `--min-ask FLOAT` | 10.0 | Minimum ask-side liquidity in USD (0 = disabled). |
| `--obi FLOAT` | −0.25 | OBI reject threshold. Entries with OBI below this are skipped. |
| `--stake FLOAT` | 10.0 | USD stake per trade. |

### Output modes

| Flag | Description |
|------|-------------|
| `--detail` | Print a line per trade showing entry/exit timestamps, prices, seconds remaining, outcome, and PnL. Useful for diagnosing specific losses. |
| `--compare` | Three-way comparison table: backtest with user params | backtest aligned to actual bot params | actual bot results from the `trades` table. |

### Grid search (sweep)

| Flag | Description |
|------|-------------|
| `--sweep` | Standard grid search: 135 combinations of `threshold × min_secs × min_ask` on one DB. |
| `--sweep-all` | Extended grid search: 405 combinations (`threshold × min_secs × min_ask × obi × dsl`) across all available DBs. Results are aggregated (sum PnL, worst MaxDD across sessions). |
| `--sort METRIC` | Sort sweep results by `ratio` (PnL/MaxDD, default), `pnl` (total PnL), or `wr` (win rate). |
| `--top N` | Show only the top-N unique configurations in the sweep table, deduplicated on `(threshold, min_secs, obi)`. Removes redundant `min_ask` and `dsl` variants that produce identical results. Default: 0 (show all). |

---

## 5. Standard output explained

### Per-file block

```
BACKTEST — paper3.db
signal=0.95  min_secs=30  min_ask=10  obi=-0.25
Snapshots: 764,399
==============================================================
Trades   : 2856
Wins     : 2808
Losses   : 30
Open     : 18  (unresolved at end of data)
Stake    : $10.00  (capital start: $100.00)
Win rate : 98.9%
Total PnL: $+89.13  (+89.1%)
Max DD   : $80.69
Capital  : $189.13
```

| Field | Meaning |
|-------|---------|
| `signal=…` | Parameters used for this run (threshold, min_secs, min_ask, obi). |
| `Snapshots` | Number of rows read from the `snapshots` table. |
| `Trades` | Total trades opened (resolved + open). |
| `Open` | Trades still open at end of data — the market's final snapshot was processed but neither WIN nor LOSS threshold was reached. These are excluded from win rate. |
| `Stake` / `capital start` | Per-trade wager and starting capital for PnL% calculation. |
| `Win rate` | `wins / (wins + losses) × 100`. |
| `Total PnL` | Net USD profit/loss. The `+89.1%` is `PnL / capital_start × 100`. |
| `Max DD` | Worst single-day cumulative loss (UTC calendar day). |
| `Capital` | `capital_start + total_pnl` — ending capital if you had started with `capital_start`. |

### Aggregate block (multiple files)

When several files are processed, an AGGREGATE block summarises all sessions:

```
AGGREGATE — 5 file(s)  912,777 snapshots
(capital reset per file — independent sessions)
Trades   : 3202
Win rate : 99.0%
Total PnL: $+108.09
PnL%     : +21.6%  (sur capital total $500.00)
Worst DD : $80.69  (worst single session)
```

Note that `PnL%` here divides by the *total* starting capital across all files (`5 × $100 = $500`).

### `--detail` trade table

```
  ts_entry             market_id        dir   bid_in  secs  outcome  bid_out   fee   pnl_net
  2026-01-15 10:23:05  0x1a2b…          YES   0.9612    52  WIN      0.9902  $0.20  $+0.22
```

| Column | Meaning |
|--------|---------|
| `ts_entry` | UTC timestamp of trade entry. |
| `market_id` | Polymarket market token identifier (truncated). |
| `dir` | Direction — always `YES` in BTC Up/Down markets. |
| `bid_in` | `best_bid` at entry (triggered the signal). |
| `secs` | Seconds remaining at entry. |
| `outcome` | WIN / LOSS / OPEN. |
| `bid_out` | `best_bid` at exit (WIN ≈ 0.99, LOSS ≈ 0.01, OPEN = last known value). |
| `fee` | Taker fee paid. |
| `pnl_net` | Net profit/loss for this trade. |

---

## 6. Grid search — `--sweep` and `--sweep-all`

### What it does

The grid search runs the backtest for every combination of parameter values, then ranks results by a chosen metric. It is the primary tool for finding optimal strategy settings.

### Parameter grids

**`--sweep`** (135 combos, single DB):

| Parameter | Values tested |
|-----------|--------------|
| `threshold` | 0.94, 0.95, 0.96, 0.97, 0.98 |
| `min_secs` | 30, 45, 60 |
| `min_ask` | 5, 10, 20 |

**`--sweep-all`** (405 combos, all DBs, capital reset per file):

| Parameter | Values tested |
|-----------|--------------|
| `threshold` | 0.94, 0.95, 0.96, 0.97, 0.98 |
| `min_secs` | 30, 45, 60 |
| `min_ask` | 5, 10, 20 |
| `obi` | −0.75, −0.50, −0.25 |
| `dsl` | 30, 100, 500 |

### Sweep table columns

```
threshold | min_secs | min_ask |    obi |    dsl | trades |  wins |    WR% |       PnL |    PnL% |   MaxDD |  PnL/DD
```

| Column | Meaning |
|--------|---------|
| `threshold` | Entry signal threshold. |
| `min_secs` | Minimum seconds remaining at entry. |
| `min_ask` | Minimum ask-side volume. |
| `obi` | OBI reject threshold. |
| `dsl` | Daily stop-loss in USD. |
| `trades` | Total trades across all DBs for this config. |
| `wins` | Total winning trades. |
| `WR%` | Win rate (wins / resolved × 100). |
| `PnL` | Total net PnL in USD (sum across all files). |
| `PnL%` | `PnL / total_capital_start × 100`. Total capital = `capital_start × number_of_files`. |
| `MaxDD` | Worst single-day loss across all files (worst case exposure). |
| `PnL/DD` | Calmar-style risk-adjusted ratio. `∞` = zero drawdown (suspicious). |

### Recommendations section

After the table, the script prints the top-5 configs by three criteria:
- **By PnL/MaxDD ratio** — recommended for risk-adjusted selection.
- **By total PnL** — configs with the highest absolute profit.
- **By win rate** — highest % of winning trades (note: very high WR often means very few trades with high threshold — verify trade count).

The final line gives the exact CLI command to reproduce the best overall config.

### `min_ask` column note

`min_ask` has negligible effect on most markets — the top-N deduplication (`--top`) removes the `min_ask` variants so you only see configs that differ on the meaningful axes (`threshold`, `min_secs`, `obi`).

### WARNING — `--sweep-all` contamination by non-Polymarket databases

`--sweep-all` includes **every database that has a `snapshots` table**, not just Polymarket databases. CEX strategy databases (`grid_cex_*.db`, `swing_cex_*.db`) also contain a `snapshots` table and are silently included in the sweep.

This contaminates OBI optimisation: CEX snapshots have different OBI distributions, and the aggregated sweep will recommend a much more lenient OBI threshold (e.g. `obi=−0.75`) that looks good in aggregate but degrades Polymarket performance when tested in isolation.

**Safe usage of `--sweep-all`**: always validate the top recommendation by re-running it on a single Polymarket database (`--db data/polymarket_5M_c2_*.db`) before adopting it. Confirmed case from 2026-06-08 session: sweep-all recommended `obi=−0.75` (Sharpe 6.4) but `obi=−0.25` (current) scored Sharpe 10.1 on c2 alone.

---

## 7. Three-way comparison — `--compare`

### Purpose

`--compare` runs three backtests side by side for each DB and prints a comparison table:

| Column | What it is |
|--------|-----------|
| **BACKTEST (paramètres)** | A **simulation**: replay of `snapshots` with the parameters you specified (or strategy JSON defaults). No real trades. |
| **BACKTEST (aligné)** | A **simulation**: replay of the same `snapshots`, but with the parameters the bot *actually had at runtime* (inferred from the `trades` table). Still no real trades, but parameters are corrected — this is the fairest simulation-side prediction of what should have happened. |
| **BOT RÉEL** | **Real trades**: what the live bot actually executed on Polymarket, as stored in the `trades` table. Real money, real order fills, real latency. Not a simulation. May include STOP and GHOST outcomes that the simulations cannot model. |

### How actual parameters are detected

The engine reads the `trades` table and infers:

| Parameter | Detection method |
|-----------|----------------|
| `stake` | Modal (most common) stake value. |
| `threshold` | 5th-percentile of `signal_best_bid` rounded down to 0.01. |
| `min_secs` | 5th-percentile of `signal_secs_remaining` rounded down to 5s. |
| `capital_start` | `capital_before` of the chronologically first resolved trade. |
| `daily_stop_loss` | Worst daily loss + 20% headroom. **Discarded** when result > 5× stake (single trade ≥ daily limit → heuristic is unreliable). Falls back to user's own DSL. |

### Comparison table rows

```
                               BACKTEST         BACKTEST              BOT
                           (paramètres)         (aligné)             RÉEL
  Threshold                        0.95             0.96          ≈0.9600
  Min secs                          30s              45s             ≈47s
  Stake                             $10              $10      $10 (modal)
  Capital start                    $100             $100             $100
  Daily stop-loss                   $30              $50             ≈$50
  Trades                            299              199              301
  Wins                              297              197              293
  Losses                              2                2                8
  Win rate                        99.3%            99.0%            97.3%
  Total PnL                     $+14.73           $+4.97          $-15.18
  PnL%                           +14.7%            +5.0%           -15.2%
  Max DD                         $13.90           $16.24                —
```

### Reading the divergence warnings

When the bot ran with different parameters than the backtest defaults, the script prints:

```
⚠  Paramètres divergents :
   stake: backtest=$10, actual=$150 (×15)
   threshold: backtest=0.95, actual≈0.93
```

These divergences explain why the plain BACKTEST column is not directly comparable to BOT RÉEL. The **aligned** column corrects for this.

### Common divergences explained

| Divergence | What it means |
|------------|---------------|
| **Stake ×N** | The bot ran with a higher stake (e.g. after manually changing the config). PnL$ values scale proportionally; PnL% is comparable if capital_start is also aligned. |
| **DSL shown as `—`** | The daily stop-loss could not be reliably detected (stake > DSL: a single losing trade already exceeds the daily limit, making the worst-day heuristic unreliable). The aligned backtest falls back to your specified DSL. PnL comparison for this DB is less reliable. |
| **Capital start >> $100** | The bot started this session with accumulated capital from previous trades. PnL% columns use different bases. |
| **STOP/GHOST gap** | The bot had STOP or GHOST outcomes not modelled in the backtest. A positive gap (backtest PnL% > bot réel PnL%) is expected and partially explained by these outcomes. |

### When to be concerned

- **Bot réel PnL% negative while aligned backtest is positive**: investigate fee rate accuracy, threshold divergence, or an atypical data period.
- **Gap > 2× between aligned backtest PnL% and bot réel PnL%**: check the STOP/GHOST count. If these are low, investigate the execution data.
- **8+ losses where backtest shows 2**: the data period may have included structural break in the market not captured in snapshots.

---

## 8. The STOP/GHOST modelling gap

This section explains the most important systematic difference between backtest results
and live bot results, and how to account for it when interpreting `--compare` output.

### What STOP means

A **STOP** outcome is recorded when the **daily stop-loss fires while a trade is open**.
The live bot force-closes the position immediately at the current market bid — which is
typically somewhere between 0.01 and 0.99, not at a clean WIN or LOSS threshold.

```
paper3.db — 24 STOP trades:
  Average exit bid : 0.356  (i.e. the market was at ~35% probability at exit)
  Average PnL      : −$79   (on average stake $127)
  Average loss     : −62% of stake
  Total PnL impact : −$1,895
```

Mechanically: if stake = $150 and entry ask ≈ $0.953, the bot bought ≈ 157 tokens.
Exiting at bid = 0.356 returns 157 × 0.356 ≈ $56. Net loss = $56 − $150 = **−$94**
(close to the −$79 average, which reflects variation in exit bid).

The daily stop-loss does not guarantee a fixed loss — it exits at whatever bid the
market is at when the trigger fires. A stop at bid=0.1 loses 90% of stake; a stop at
bid=0.5 loses 50%.

### What GHOST means

A **GHOST** outcome is recorded when a trade was opened but **no resolution was ever
detected** by the bot. The market expired (or was resolved on-chain) without the bot
capturing the WIN or LOSS event — typically due to a WebSocket disconnection, an API
timeout, or a market that resolved between reconnections.

```
paper3.db — 19 GHOST trades:
  pnl_net         : $0.00 for all 19 trades
  resolution_bid  : 0.000 (no exit price recorded)
  stake           : $150 each
```

`pnl_net = 0.0` does **not** mean the stake was recovered. It means the bot wrote
zero because it could not determine the outcome. The actual economic impact depends
on what happened on-chain: if the market resolved YES the position was profitable;
if it resolved NO the full stake was lost. Without oracle reconciliation, the outcome
is unknown and conservatively treated as a zero PnL placeholder.

### Why the backtest cannot model either outcome

| Outcome | What backtest does instead | Why it differs |
|---------|--------------------------|----------------|
| **STOP** | Blocks new entries once `daily_pnl < −DSL`. Existing open trades continue until they reach bid ≥ 0.99 (WIN) or bid ≤ 0.01 (LOSS). | The backtest never force-exits at an intermediate price. A trade that would have been STOPped in live at bid=0.35 is eventually simulated as a WIN or LOSS — usually WIN, since most markets resolve YES. This makes the backtest **systematically optimistic** for sessions where stops fire. |
| **GHOST** | No concept of a missing resolution. If snapshots end before resolution, the trade is recorded as `OPEN`. | The `snapshots` table contains every message the bot received. A market that resolved during a disconnection left no snapshot — so the backtest never entered that trade, or entered it and left it as OPEN. The stakes deployed in GHOST trades are invisible to the backtest. |

### Quantifying the gap (paper3.db example)

```
Aligned backtest PnL%  : +168.2%  (stake=$150, capital=$1000)
Bot réel PnL%          :  +31.4%
Gap                    : −136.8 pp

Known contributors:
  STOP  : 24 trades × avg −$79  = −$1,895  (backtest counted these as WIN/LOSS, mostly WIN)
  GHOST : 19 trades × unknown   =     $0   (stake deployed, outcome unknown)
  Extra losses: 78 real vs 28 aligned backtest = 50 additional LOSSes × −$137 avg = −$6,850
```

The extra losses (50 more than the aligned backtest predicts) account for the majority
of the gap. Their origin is a fundamental temporal resolution gap explained in the
section below. The STOP losses add a further −$1,895 that the backtest treats as wins.

### How to read the gap in `--compare` output

When the comparison table shows:

```
  Stops (daily SL)   : —     —     24
  Ghosts (no exit)   : —     —     19
  PnL%               : +267%  +168%  +31%
```

The two backtest columns are optimistic by construction for this session. The correct
interpretation is:

1. The **+267% / +168%** figures represent the ceiling — what the strategy *would* achieve
   if all trades resolved cleanly (no forced stops, no missing exits).
2. The **+31.4%** bot réel figure is the floor — actual performance including all friction.
3. The remaining gap after subtracting STOP/GHOST impact is explained by extra real losses,
   execution slippage, and snapshot coverage gaps.

### The 5-second snapshot blind spot — origin of the extra losses

The backtest engine reads one row per market every ~5 seconds (the snapshot interval).
The live bot processes **every WebSocket tick** — which can arrive several times per second.

When a market's best_bid briefly dips to ≤ 0.01 (the LOSS threshold) between two snapshots
and then recovers, the following happens:

```
Timeline of a LOSS that is invisible to the backtest:

  t= 0s  snapshot → bid=0.96  ← backtest enters trade
  t= 5s  snapshot → bid=0.82
  t=10s  snapshot → bid=0.06  ← backtest sees this (not yet 0.01)
  t=12.3s WebSocket tick → bid=0.01  ← live bot: LOSS recorded
  t=12.5s WebSocket tick → bid=0.04  (market recovers)
  t=15s  snapshot → bid=0.04  ← backtest sees recovery, continues
  ...
  t=240s snapshot → bid=0.98  ← backtest resolves as WIN
```

Evidence from paper3.db: every LOSS trade contains many snapshots (14–88 during the
trade), yet the minimum bid ever captured in those snapshots is 0.01–0.06 — the dip
to ≤0.01 that triggered the live LOSS is nowhere in the snapshot data. Average LOSS
trade duration is 24 minutes (1,477 seconds), giving plenty of snapshots to catch the
dip — but the dip is too brief (hundreds of milliseconds) to be recorded.

**This gap is structural and cannot be fixed** without storing every WebSocket tick
(~100× more data). It does not depend on strategy parameters — every configuration
experiences the same blind spot equally. The sweep rankings remain valid for comparing
configurations against each other.

### What this means for strategy decisions

- Do **not** use the aligned backtest PnL% directly as a prediction of live performance
  on sessions with many STOP or GHOST trades.
- A ratio of `(bot réel PnL%) / (aligned backtest PnL%)` far below 1.0 is expected and
  **normal** when STOP+GHOST counts are significant relative to total trades.
- The sweep / grid search is still valid for ranking configurations against each other —
  the STOP/GHOST gap affects all configurations equally (it depends on session conditions,
  not strategy parameters).
- To reduce the gap: lower the daily stop-loss relative to stake so fewer STOP events
  occur, or accept that live performance will structurally trail backtest by this margin.

---

## 9. Full workflow — `strategy_compare.sh`


The shell script `scripts/strategy_compare.sh` automates the full optimisation workflow:

```bash
bash scripts/strategy_compare.sh              # default: top 10, sort by ratio
bash scripts/strategy_compare.sh --top 20     # show top 20 unique configs
bash scripts/strategy_compare.sh --sort pnl   # sort by total PnL
bash scripts/strategy_compare.sh --db data/liveweek.db  # one DB only
bash scripts/strategy_compare.sh --no-save    # print only, no report file
```

It runs in two sections:

1. **Section 1 — Grid search** (`--sweep-all`): 405 combinations across all DBs.
2. **Section 2 — Per-DB comparison** (`--compare`): three-way table for each DB.

Output is saved to `reports/strategy_compare_YYYYMMDD_HHMMSS.txt` and printed to stdout simultaneously.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--top N` | 10 | Number of unique configs in the sweep table. |
| `--sort METRIC` | `ratio` | Sort order: `ratio`, `pnl`, or `wr`. |
| `--db PATH` | all DBs | Restrict to one DB file. |
| `--out FILE` | auto-timestamped | Override the report file path. |
| `--no-save` | off | Print only, do not write a file. |

---

## 10. Strategy JSON files

Strategy parameters are stored in `strategies/`:

| File | Status | Description |
|------|--------|-------------|
| `polymarket_BTC5M.json` | v1 — reference | Original parameters (threshold=0.96, ratio=3.61). |
| `polymarket_BTC5M_piste3.json` | **piste3 — active** | OBI-calibrated (obi=-0.65, threshold=0.95, min_secs=30). |

### piste3 parameters

```json
{
    "signal_threshold":   0.95,
    "min_secs_remaining": 30,
    "obi_reject_thresh":  -0.65,
    "daily_stop_loss":    30.0,
    "weekly_stop_loss":   60.0,
    "stake":              10.0,
    "capital_start":      100.0
}
```

The bot loads the strategy at startup. To activate a new version after updating the JSON:

```bash
bash tradinebotte-polymarket/scripts/start_bot.sh   # restarts and reloads strategy
```

---

## 11. Interpreting results and making decisions

### Is my backtest result good?

| Metric | Target | Warning |
|--------|--------|---------|
| WR% | 97–99% | < 97% with high threshold = something is wrong |
| PnL/DD ratio | ≥ 3.5 | < 2.0 = risk not justified |
| MaxDD | < $80 | ≥ $100 = dangerous daily exposure |
| Trades | ≥ 500 total | < 500 = insufficient data for reliable statistics |

### Should I update the strategy?

| Verdict | Condition | Action |
|---------|-----------|--------|
| **KEEP** | Best found ratio ≤ current + 0.3 | No change needed. Current config is near-optimal. |
| **MONITOR** | Best found ratio > current + 0.3 but total trades < 500, or bot réel underperforms aligned backtest by > 3× PnL% | Collect more live data before deciding. |
| **UPDATE** | Best found ratio > current + 0.3 AND trade count within ±30% of current config | Create a new versioned strategy file and update the bot. |

### Creating a new strategy version

When a better configuration is found:

1. Copy the current strategy file:
   ```bash
   cp tradinebotte-polymarket/strategies/polymarket_BTC5M_piste3.json tradinebotte-polymarket/strategies/polymarket_BTC5M_piste4.json
   ```
2. Edit the parameters in the new file.
3. Update the `_description` field with the date and new ratio.
4. Update the default path in `tradinebotte-polymarket/live_bot.py` (search for `polymarket_BTC5M_piste3.json`).
5. Run the test suite to confirm all tests pass:
   ```bash
   bash scripts/run_tests.sh
   ```
6. Restart the bot:
   ```bash
   bash tradinebotte-polymarket/scripts/start_bot.sh
   ```

### Parameters not to change without a full backtest

`WIN_THRESHOLD` (0.99), `LOSS_THRESHOLD` (0.01), `FEE_RATE` (2%), and `STAKE` are not swept. Changing them invalidates all existing backtest comparisons. The regression tests (`TestParamConsistency`) enforce that `live_bot.py` and `backtest.py` always agree on these values.

---

## 12. Other backtest scripts

The sections above document `analysis/backtest.py` (Polymarket). The following scripts cover CEX and accumulation strategies and share the same general philosophy: historical replay → stats → optional sweep.

### `scripts/backtest_accumulation.py` — BTC long-term accumulation

Fetches live Binance 1h klines (BTCUSDT) and replays the accumulation strategy from `tradinebotte-cex/strategies/accumulation/btc_accumulation.json`.

```bash
# Default: full history from 2024-09-01 with dip proxy
python3 scripts/backtest_accumulation.py

# Specific date range
python3 scripts/backtest_accumulation.py --start 2026-01-01 --end 2026-06-30

# OBI proxy (taker-buy EMA) instead of price-dip proxy
python3 scripts/backtest_accumulation.py --proxy obi

# Show full trade log
python3 scripts/backtest_accumulation.py --trades

# Use a different strategy config
python3 scripts/backtest_accumulation.py --strategy path/to/btc_accumulation.json
```

**Key flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--start` | 2024-09-01 | Start date (YYYY-MM-DD) |
| `--end` | today | End date |
| `--proxy` | dip | Scale-in signal: `dip` (price drop from N-candle high) or `obi` (taker-buy EMA) |
| `--dip-pct` | 4.0 | Dip proxy: % drop from recent high to trigger |
| `--dip-lookback` | 72 | Dip proxy: rolling-high lookback in candles |
| `--capital` | from JSON | Override capital_usdt |

**Note:** `--dip-pct` and `--dip-lookback` are backtest-only simulation knobs — they approximate the live OBI signal and do not map to any live strategy parameter. The live bot uses real-time OBI from the indicators service.

**Validation guidance:** Always use a train/test split when tuning parameters. Use 2024-09 → 2025-12 as train and 2026-01 → today as the held-out test. Re-running on the full period with newly chosen parameters is in-sample and overstates improvement.

### `analysis/backtest_orderbook.py` — OB scalping

Replays the orderbook_bot strategy against `ob_snapshots` data from a `live_ob_*.db` file.

```bash
python3 analysis/backtest_orderbook.py                              # auto-detect DB
python3 analysis/backtest_orderbook.py --db data/live_ob_2026-05-26.db
python3 analysis/backtest_orderbook.py --mode spot --direction both
python3 analysis/backtest_orderbook.py --sweep                      # 576-combo grid search
python3 analysis/backtest_orderbook.py --sweep --csv results/ob_sweep.csv
```

**Note:** Databases created before fix M-1 (2026-05-23) lack the `tfi` column and cannot be used.

### `analysis/backtest_grid.py` — CEX grid strategy

Replays a static or trailing grid strategy on OHLCV (1-minute klines) databases.

```bash
python3 analysis/backtest_grid.py --all                     # all BTCUSDT_1m*.db in data/
python3 analysis/backtest_grid.py --all --trail bear        # trailing grid following downtrend
python3 analysis/backtest_grid.py --all --sweep             # 15-combo parameter sweep
python3 analysis/backtest_grid.py --all --compare           # static vs trailing side-by-side
```

**Key flags:** `--range` (grid ±% around center), `--levels` (order count), `--trail {off,bear,bull,both}`, `--sweep` (15 combos: range × levels), `--sort {calmar,pnl}`.

### `analysis/backtest_swing_dca.py` — CEX swing / DCA

Replays DCA, Swing, or SwingHold strategies on OHLCV databases.

```bash
python3 analysis/backtest_swing_dca.py --all-dbs --compare   # 3 strategies × 3 regimes
python3 analysis/backtest_swing_dca.py --strategy dca --sweep
python3 analysis/backtest_swing_dca.py data/BTCUSDT_1m90d_range_20260208-20260509.db
```

**Key flags:** `--all-dbs` (bull 2024 + bear 2022 + range 2026), `--compare` (all 3 strategies side-by-side), `--strategy {dca,swing,swinghold}`, `--sweep` (tp_pct × sl_pct grid).

### `analysis/backtest_volfilter.py` — Polymarket volatility filter

Tests a bid-volatility / range / OBI-volatility filter against a Polymarket snapshots database. Runs a comparison of 3 scenarios automatically.

```bash
python3 analysis/backtest_volfilter.py                         # auto-detect DB
python3 analysis/backtest_volfilter.py --db data/polymarket_5M_c2_*.db --sweep
```

**Note:** This script does not support `--help`; calling it directly runs the comparison.

### `analysis/backtest_stake_secs.py` — Stake curve optimization

Optimises the per-trade stake as a function of seconds-remaining and bid confidence. Tests three curve shapes (A = continuous, B = step function, C = Kelly bucket).

```bash
python3 analysis/backtest_stake_secs.py --db data/polymarket_5M_c2_*.db --curve all --top 15
```

**Note:** Curve C (Kelly) produces large in-sample gains. Validate with walk-forward before considering for live use.

### `analysis/backtest_scalping.py` — CEX scalping

Replays a short-term CEX scalping strategy on 1-minute OHLCV klines.

```bash
python3 analysis/backtest_scalping.py                          # default DB
python3 analysis/backtest_scalping.py --compare                # 3 configs side-by-side
```

### `analysis/backtest_cycle_strategy.py` — BTC market cycle strategy

Backtests a Mayer-Multiple / drawback regime strategy on the long-term daily OHLCV dataset.

```bash
python3 analysis/backtest_cycle_strategy.py                    # default params
python3 analysis/backtest_cycle_strategy.py --compare          # parameter comparison
python3 analysis/backtest_cycle_strategy.py --top-mm 2.2 --bot-mm 0.8
```
