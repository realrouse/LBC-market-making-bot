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
8. [Full workflow — `strategy_compare.sh`](#8-full-workflow----strategy_comparesh)
9. [Strategy JSON files](#9-strategy-json-files)
10. [Interpreting results and making decisions](#10-interpreting-results-and-making-decisions)

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
| `obi_reject_thresh` | −0.75 | strategy JSON / `--obi` | OBI floor. Entries are rejected when `OBI < obi_reject_thresh`, i.e. when sell pressure is too strong. |
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
| **Aligned backtest** | A backtest run with the parameters the bot *actually* used (detected from the `trades` table), rather than the user-specified defaults. Shown as the middle column in `--compare`. Closer to reality than the plain backtest. |
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
3. Runs a backtest `--all` on any `data/*.db` files found (non-blocking).
4. Invokes the `doc-sync` agent to audit flag documentation (requires `claude` CLI).

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
| `tests/test_bot.py` | 20 classes | Core bot logic: fee calculation, book message parsing, signal detection, trade resolution, daily PnL cache, DB schema migrations, strategy file loading, HTML escaping, OBI filter, trading-hour filter, circuit breaker |
| `tests/test_backtest.py` | 9 classes | Backtest engine: `run_backtest`, `summarize`, `_ratio`, percentile helper, `detect_actual_params`, `_actual_stats`, `_collect_dbs` |
| `tests/test_regression.py` | 2 classes | **Performance regression** against `data/paper3.db` (WR ≥ 98%, PnL ≥ $80, MaxDD < $100); **parameter consistency** between `live_bot.py` constants and `backtest.py` defaults — these two must always agree |
| `tests/test_multibot.py` | 4 classes | Multi-bot feed and account bot integration (register_market, two-bot coordination) |
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

`TestParamConsistency` checks that the parameter values in `bot/live_bot.py` (module-level constants) match the defaults in `scripts/backtest.py` (the `Params` dataclass). If they diverge, backtests no longer predict live performance.

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
python3 scripts/backtest.py

# Explicit file
python3 scripts/backtest.py --db ~/tradinebotte/live.db

# Multiple files (independent capital per file)
python3 scripts/backtest.py --db data/session_a.db data/session_b.db

# Shell glob (same as above, shell expands the pattern)
python3 scripts/backtest.py --db data/*.db

# Scan data/ automatically + live.db if usable
python3 scripts/backtest.py --all
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

---

## 7. Three-way comparison — `--compare`

### Purpose

`--compare` runs three backtests side by side for each DB and prints a comparison table:

| Column | What it is |
|--------|-----------|
| **BACKTEST (paramètres)** | Backtest with the parameters you specified (or strategy JSON defaults). |
| **BACKTEST (aligné)** | Backtest with the parameters the live bot *actually used*, inferred from the `trades` table. |
| **BOT RÉEL** | Actual results recorded in the `trades` table during the live run. |

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

## 8. Full workflow — `strategy_compare.sh`

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

## 9. Strategy JSON files

Strategy parameters are stored in `strategies/`:

| File | Status | Description |
|------|--------|-------------|
| `polymarket_BTC5M.json` | v1 — reference | Original parameters (threshold=0.96, ratio=3.61). |
| `polymarket_BTC5M_v2.json` | **v2 — active** | Sweep-optimised 2026-05-08 (threshold=0.95, ratio=4.42). |

### v2 parameters

```json
{
    "signal_threshold":   0.95,
    "min_secs_remaining": 45,
    "obi_reject_thresh":  -0.75,
    "daily_stop_loss":    30.0,
    "stake":              10.0,
    "capital_start":      100.0
}
```

The bot loads the strategy at startup. To activate a new version after updating the JSON:

```bash
bash scripts/start_bot.sh   # restarts and reloads strategy
```

---

## 10. Interpreting results and making decisions

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
   cp strategies/polymarket_BTC5M_v2.json strategies/polymarket_BTC5M_v3.json
   ```
2. Edit the parameters in the new file.
3. Update the `_description` field with the date and new ratio.
4. Update the default path in `bot/live_bot.py` (search for `polymarket_BTC5M_v2.json`).
5. Run the test suite to confirm all tests pass:
   ```bash
   bash scripts/run_tests.sh
   ```
6. Restart the bot:
   ```bash
   bash scripts/start_bot.sh
   ```

### Parameters not to change without a full backtest

`WIN_THRESHOLD` (0.99), `LOSS_THRESHOLD` (0.01), `FEE_RATE` (2%), and `STAKE` are not swept. Changing them invalidates all existing backtest comparisons. The regression tests (`TestParamConsistency`) enforce that `live_bot.py` and `backtest.py` always agree on these values.
