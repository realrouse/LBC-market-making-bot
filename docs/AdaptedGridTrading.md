# Adapted Grid Trading — Bear and Bull Strategies

> 🇫🇷 [Version française](AdaptedGridTrading.fr.md)

This document describes three grid trading strategies developed and backtested on
three real BTC/USDT historical datasets covering distinct market regimes.

See also: [`docs/GridTrading.md`](GridTrading.md) for the base grid algorithm.

---

## Background — Why static grids fail in trending markets

A standard static grid places buy and sell orders within a fixed price range
`[grid_lower, grid_upper]`. It stops immediately when price exits that range.

Backtested on three 90-day BTC regimes (±15%, 30 levels, $50/order, $1,500 capital):

| Regime | Period | Price move | Static result | Time in grid |
|---|---|---|---|---|
| Lateral 2026 | Feb–May 2026 | $63K–$83K | **+5.0%** (+20%/yr) | 96% |
| Bear (LUNA crash) | May–Aug 2022 | $38K → $17K (−54%) | **−3.3%** | 9% |
| Bull run | Oct 2024–Jan 2025 | $66K → $108K (+64%) | **+0.1%** | 25% |

The static grid is optimal for lateral markets. In trending markets it stops within
9–25% of the period, either with a loss (bear) or near-zero profit (bull).

---

## Strategy 1 — Static grid (reference)

**Use when:** market is expected to consolidate within a known range.

### Parameters

| Parameter | Recommended | Description |
|---|---|---|
| `--range` | 15 | Grid spans ±15% from start price |
| `--levels` | 30 | 30 evenly-spaced levels |
| `--size` | 50 | $50 USDT per order |
| `--trail` | off | No trailing (default) |

**Capital required:** `levels × size = 30 × $50 = $1,500`
**Grid step:** ~$729 at BTC $80,705 (≈ 0.9% per level)

### How it works

1. At start, BUY orders placed at all levels below current price.
2. When a BUY fills → SELL placed one step above.
3. When a SELL fills → BUY placed one step below. Profit recorded.
4. If price exits `[grid_lower, grid_upper]` → all orders cancelled, remaining BTC
   liquidated at close price, bot stops.

### Backtest result (±15%, 30L)

```
Lateral 2026 : +$74  (+5.0%)  168 cycles  96% in-grid  MaxDD 2.1%
Bear 2022    : −$50  (−3.3%)   18 cycles   9% in-grid  exit_low
Bull 2024    :  +$2  (+0.1%)    5 cycles  25% in-grid  exit_high
```

### Run

```bash
python3 scripts/backtest_grid.py --all
python3 scripts/backtest_grid.py --all --sweep          # parameter search
```

---

## Strategy 2 — Bear-adapted trailing grid

**Use when:** downtrend is expected (or ongoing), with oscillations at each level.

### Core concept

Instead of stopping when price falls below `grid_lower`, the grid **re-centers
downward** to the current close price and resumes. The bot follows the price down,
capturing profit from oscillations at each price level on the way down.

When price eventually bounces back above the re-centered `grid_upper`, the grid
exits with a profit — the tight ±15% range ensures the bounce crosses the upper
boundary quickly.

```
Initial grid: BTC $37,631  →  [$31,986 – $43,275]
  Day 9: price falls to $31,981  → re-center at $31,981  →  [$27,184 – $36,778]
  Day 25: price falls to $27,437 → re-center at $27,437  →  [$23,321 – $31,553]
  Day 43: BTC bounces to $31,538 → EXIT_HIGH (profitable)
```

**Key insight:** the ±15% range is tight enough that bear-market bounces (which are
frequent even in severe crashes) will cross `grid_upper` and force a profitable exit.
A ±30% range stays in the grid longer but accumulates larger unrealized BTC losses.

### Parameters

| Parameter | Recommended | Description |
|---|---|---|
| `--trail` | bear | Re-center downward only |
| `--range` | 15 | ±15% (tight → profitable exit on bounce) |
| `--levels` | 30 | 30 levels |
| `--size` | 50 | $50/order |
| `--max-recenters` | 10 | Max re-centers before treating as stop-loss |

### Asymmetry

- Price falls below `grid_lower` → **re-center downward** (follows bear trend)
- Price rises above `grid_upper` → **STOP** (exits with accumulated profit)

This means the bear strategy is **not** hurt by bull runs — if price suddenly
rallies it exits cleanly with whatever profit was accumulated.

### Capital management at re-center

On each re-center, new BUY orders are placed from the **remaining USDT budget**,
starting from the level closest to current price downward. After several re-centers
in a severe bear market, USDT may be partially depleted (spent on BTC not yet sold),
so the re-centered grid may have fewer active buy orders than the original.

### Backtest result (±15%, 30L, `--trail bear`)

```
Lateral 2026 : +$74  (+5.0%)  168 cycles   96% in-grid  0 re-centers  identical to static
Bear 2022    : +$31  (+2.0%)  102 cycles   33% in-grid  2 re-centers  EXIT_HIGH on bounce
Bull 2024    :  +$2  (+0.1%)    5 cycles   25% in-grid  0 re-centers  identical to static
```

**Improvement vs static on bear market: −3.3% → +2.0% (+5.3 points)**

Breakdown on 2022 bear market:
```
Realized PnL  :  +$43.90  (completed cycles, net of fees)
Unrealized PnL:  −$19.83  (BTC held at $31,538 vs avg cost ~$32K)
Fees          :  −$11.76
Net PnL       :  +$30.67
```

### Warning — avoid `--trail both` in a bear market

`trail=both` re-centers in both directions. In a bear market this is catastrophic:
it re-centers down (accumulating BTC), then up (accumulating more buy orders), then
down again — compounding BTC losses on every oscillation.

```
trail=both on 2022 bear:  −$359  (−23.9%)  9 re-centers  $409 unrealized BTC loss
trail=bear on 2022 bear:   +$31  (+2.0%)   2 re-centers  $20 unrealized BTC loss
```

### Run

```bash
python3 scripts/backtest_grid.py --all --trail bear
python3 scripts/backtest_grid.py --all --trail bear --compare   # vs static
python3 scripts/backtest_grid.py --all --trail bear --sweep     # parameter search
```

### Strategy JSON

`strategies/grid_BTCUSDT_bear_trailing.json` — calibrated at BTC=$80,705 (2026-05-09):
grid `[$68,599 – $92,811]`, step $829, 30 levels, $50/order, $1,500 capital.
Recalibrate `grid_lower` / `grid_upper` to ±15% of current BTC price when deploying.

---

## Strategy 3 — Bull-adapted trailing grid

**Use when:** uptrend is expected (or ongoing), with oscillations at each level.

### Core concept

Instead of stopping when price rises above `grid_upper`, the grid **re-centers
upward** to the current close price and resumes. The bot follows the price up,
capturing profit from oscillations at each successive price level.

```
Initial grid: BTC $66,084  →  [$56,171 – $75,997]
  Day 23: price hits $75,982  → re-center at $75,982  →  [$64,585 – $87,379]
  Day 48: price hits $87,348  → re-center at $87,348  →  [$74,246 – $100,450]
  Day 67: price hits $100,761 → re-center at $100,761 →  [$85,647 – $115,875]
  Day 92: period ends at $96,600 — bot completes full period
```

In the 2024 bull run, the static grid stopped after 23 days with 5 cycles.
The trailing bull grid ran all 92 days with 134 cycles and 3 re-centers.

### Parameters

| Parameter | Recommended | Description |
|---|---|---|
| `--trail` | bull | Re-center upward only |
| `--range` | 15 | ±15% (tight → more cycles per segment) |
| `--levels` | 30 | 30 levels |
| `--size` | 50 | $50/order |
| `--max-recenters` | 10 | Max re-centers |

### Asymmetry

- Price rises above `grid_upper` → **re-center upward** (follows bull trend)
- Price falls below `grid_lower` → **STOP** (limits downside)

If a bull run reverses into a bear market, the bot stops at `grid_lower` — the same
behavior as the static grid, limiting the loss to the initial grid width.

### Backtest result (±15%, 30L, `--trail bull`)

```
Lateral 2026 : +$75  (+5.0%)  170 cycles  100% in-grid  1 re-center  ✓ completed
Bear 2022    : −$50  (−3.3%)   18 cycles    9% in-grid  0 re-centers  identical to static
Bull 2024    : +$55  (+3.7%)  134 cycles  100% in-grid  3 re-centers  ✓ completed
```

**Improvement vs static on bull run: +0.1% → +3.7% (+3.6 points, 26× more profit)**

Breakdown on 2024 bull run:
```
Realized PnL  :  +$58.86  (completed cycles, net of fees)
Unrealized PnL:  −$10.85  (small BTC position at period end)
Fees          :  −$13.72
Net PnL       :  +$54.95
```

On the lateral 2026 period, the bull strategy performs marginally better than static
(100% in-grid vs 96%, 1 re-center resolves the brief spike above $81,863).

### Run

```bash
python3 scripts/backtest_grid.py --all --trail bull
python3 scripts/backtest_grid.py --all --trail bull --compare   # vs static
python3 scripts/backtest_grid.py --all --trail bull --sweep     # parameter search
```

### Strategy JSON

`strategies/grid_BTCUSDT_bull_trailing.json` — same grid bounds as bear trailing.
Recalibrate `grid_lower` / `grid_upper` to ±15% of current BTC price.

---

## Strategy selection guide

```
Market assessment
├── Consolidation / ranging expected?
│     └── Static or bull trailing (nearly identical)
│           python3 scripts/backtest_grid.py --all
│           python3 scripts/backtest_grid.py --all --trail bull
│
├── Downtrend / bear market expected?
│     └── Bear trailing
│           python3 scripts/backtest_grid.py --all --trail bear
│
├── Uptrend / bull run expected?
│     └── Bull trailing
│           python3 scripts/backtest_grid.py --all --trail bull
│
└── Uncertain?
      └── Bear trailing (asymmetric: profits from downside oscillations,
            exits cleanly if market rallies)
```

**Never use `--trail both` in a trending market.** It is only appropriate for
confirmed ranging conditions where you want the grid to follow price in either
direction without stopping.

---

## Full comparison table

All three regimes, three strategies, ±15%, 30 levels, $50/order, $1,500 capital:

| Strategy | Regime | Cycles | Net PnL | PnL% | Ann% | MaxDD | Time% | Re-centers |
|---|---|---|---|---|---|---|---|---|
| Static | Lateral 2026 | 168 | +$74 | +5.0% | +20% | 2.1% | 96% | — |
| Static | Bear 2022 | 18 | −$50 | −3.3% | −13% | 3.5% | 9% | — |
| Static | Bull 2024 | 5 | +$2 | +0.1% | +1% | 0.1% | 25% | — |
| Bear trailing | Lateral 2026 | 168 | +$74 | +5.0% | +20% | 2.1% | 96% | 0 |
| **Bear trailing** | **Bear 2022** | **102** | **+$31** | **+2.0%** | **+8%** | 13.1% | 33% | **2** |
| Bear trailing | Bull 2024 | 5 | +$2 | +0.1% | +1% | 0.1% | 25% | 0 |
| Bull trailing | Lateral 2026 | 170 | +$75 | +5.0% | +20% | 2.1% | 100% | 1 |
| Bull trailing | Bear 2022 | 18 | −$50 | −3.3% | −13% | 3.5% | 9% | 0 |
| **Bull trailing** | **Bull 2024** | **134** | **+$55** | **+3.7%** | **+15%** | 1.7% | 100% | **3** |

---

## Parameter sweep results

### Bear trailing sweep (best configs by average Calmar across 3 regimes)

```
±Rng  Lvl    Lateral    Bear 2022   Bull 2024   AvgCal  AvgPnL
 30%   20   +2.7% 1%  −2.4%  28%  +0.2%  0%    2.26   +0.2%
 15%   20   +5.2% 2%  +2.0%  13%  +0.2%  0%    1.71   +2.5%  ← best PnL
 15%   30   +5.0% 2%  +2.0%  13%  +0.1%  0%    1.53   +2.4%
```

### Bull trailing sweep (best configs by average Calmar)

```
±Rng  Lvl    Lateral    Bear 2022   Bull 2024   AvgCal  AvgPnL
 20%   20   +3.7% 2%  −4.7%   5%  +2.1%  0%    2.19   +0.4%
 15%   20   +5.2% 2%  −3.4%   4%  +3.6%  2%    1.17   +1.8%  ← best PnL
 15%   30   +5.0% 2%  −3.3%   3%  +3.7%  2%    1.17   +1.8%  ← best PnL
```

---

## Reproduce any result

```bash
# Static — default
python3 scripts/backtest_grid.py --all --range 15 --levels 30 --size 50

# Bear-adapted trailing — recommended
python3 scripts/backtest_grid.py --all --range 15 --levels 30 --trail bear --compare

# Bull-adapted trailing — recommended
python3 scripts/backtest_grid.py --all --range 15 --levels 30 --trail bull --compare

# Full sweep — bear mode
python3 scripts/backtest_grid.py --all --trail bear --sweep --sort pnl

# Full sweep — bull mode
python3 scripts/backtest_grid.py --all --trail bull --sweep --sort pnl
```

---

## Related files

| File | Role |
|---|---|
| `scripts/backtest_grid.py` | Backtest engine (static + trailing) |
| `scripts/download_btc_history.py` | Download OHLCV data from Binance |
| `strategies/grid_BTCUSDT_tight.json` | Static ±15% config |
| `strategies/grid_BTCUSDT_moderate.json` | Static ±20% config |
| `strategies/grid_BTCUSDT_bear_trailing.json` | Bear trailing config |
| `strategies/grid_BTCUSDT_bull_trailing.json` | Bull trailing config |
| `bot/strategies/grid.py` | Live GridStrategy implementation |
| `docs/GridTrading.md` | Base grid algorithm documentation |
| `data/BTCUSDT_1m*.db` | OHLCV databases (excluded from git) |
