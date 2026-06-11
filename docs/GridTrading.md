# Grid Trading — Operation and Setup

See [docs/GridTrading.fr.md](GridTrading.fr.md) for the French version.

## What is grid trading?

Grid trading is a strategy that divides a price range into evenly spaced levels
(a "grid") and places LIMIT BUY orders below the current price and LIMIT SELL
orders above it. Each time the price oscillates between two levels, a buy/sell
cycle completes and generates a profit equal to the grid spacing multiplied by
the traded quantity.

The strategy is **market-neutral**: it does not bet on a direction. It profits
from volatility in both directions, as long as the price stays within the
`[grid_lower, grid_upper]` range.

---

## Differences from the threshold strategy (Polymarket)

| | Threshold | Grid |
|---|---|---|
| Market | Polymarket binary (UP/DOWN) | CEX continuous spot (BTC/USDT) |
| Price scale | 0–1 (probability) | Absolute USDT (~90,000–110,000) |
| Positions | One at a time, binary resolution | Multiple levels simultaneously |
| Duration | ~45–300 seconds | Indefinite (runs continuously) |
| Entry signal | `best_bid >= 0.96` | Price crosses a grid level |
| Profit | Bet value × (1 − fee) | Spacing × quantity − fees |
| Data source | Polymarket WebSocket | Binance / MEXC WebSocket |

---

## Architecture

### Files involved

```
tradinebotte-cex/
  strategy_engines/
    __init__.py      ← factory load("grid", config) → GridStrategy
    base.py          ← Strategy protocol (common interface)
    grid.py          ← GridStrategy, GridLevel, GridState
  connectors/
    __init__.py      ← factory load("binance"|"mexc") → module api_*
  api_binance.py     ← Binance connector (REST + WebSocket)
  api_mexc.py        ← MEXC connector (REST + WebSocket)
strategies/
  grid/
    grid_BTCUSDT.json              ← example flat grid config
    grid_BTCUSDT_bear_trailing.json ← bear/trailing variant (account-3)
```

### Execution flow

```
main()
 ├─ make_config()                 reads the strategy JSON
 ├─ _load_connector("binance")    selects api_binance module
 ├─ BotState(conn, config)
 ├─ load_strategy("grid", config) → GridStrategy(config)
 │    └─ computes price levels + validates bounds
 └─ ws_loop()
      └─ handle_book_update()
           └─ state.strategy.on_book_update(state, ts)
                └─ GridStrategy.on_book_update() [see algorithm]
```

---

## Algorithm

### Initialization

```
grid_step = (grid_upper - grid_lower) / (grid_levels - 1)
levels    = [grid_lower + i × grid_step  for i in 0 to grid_levels-1]
```

Example: `grid_lower=90000`, `grid_upper=110000`, `grid_levels=21`
→ `grid_step = 1000` USDT
→ levels = [90,000, 91,000, 92,000, …, 110,000]

### Initial placement (on first tick)

- Current price = 100,000 USDT
- BUY orders placed at: 90,000, 91,000, …, 99,000
- SELL orders placed at: 101,000, 102,000, …, 110,000

### Complete cycle (unit profit)

```
1. BUY fill at 99,000 USDT
   → place SELL at 100,000 USDT (= 99,000 + grid_step)

2. SELL fill at 100,000 USDT
   → gross profit = grid_step × qty_btc = 1,000 × (50 / 99,000) ≈ 0.505 USDT
   → place BUY at 99,000 USDT (cycle restarts)
```

### Grid stop-loss

If `best_bid < grid_lower` or `best_bid > grid_upper`: cancel all open orders
and stop the grid. The bot logs the event and exits.

### Per-level state (`GridLevel`)

| Field | Type | Description |
|---|---|---|
| `price` | float | This level's price in USDT |
| `buy_order_id` | str \| None | Active BUY order ID on this level |
| `sell_order_id` | str \| None | Active SELL order ID on this level |
| `status` | str | `idle` \| `buy_placed` \| `sell_placed` \| `cycle_complete` |
| `filled_at_ts` | float \| None | Unix timestamp of the last fill |

---

## Configuration

### Strategy JSON file

```json
{
    "strategy_type": "grid",
    "connector":     "binance",

    "grid_symbol":          "BTCUSDT",
    "grid_lower":           90000.0,
    "grid_upper":           110000.0,
    "grid_levels":          20,
    "grid_order_size_usdt": 50.0,

    "capital_start":  1000.0,
    "stake":            50.0,
    "daily_stop_loss": 100.0,

    "hour_filter": { "enabled": false }
}
```

### Grid parameters

| Key | Type | Required | Description |
|---|---|---|---|
| `strategy_type` | string | yes | Must be `"grid"` |
| `connector` | string | yes | `"binance"` or `"mexc"` |
| `grid_symbol` | string | yes | Pair, e.g. `"BTCUSDT"` |
| `grid_lower` | float | yes | Grid lower bound (USDT, > 0) |
| `grid_upper` | float | yes | Grid upper bound (USDT, > `grid_lower`) |
| `grid_levels` | int | yes | Number of levels (≥ 2) |
| `grid_order_size_usdt` | float | yes | USDT amount per order (> 0) |

### Common parameters used by the grid

| Key | Description |
|---|---|
| `capital_start` | Initial capital in USDT |
| `daily_stop_loss` | Maximum daily loss before stopping |
| `hour_filter` | Hour filter (same format as threshold) |

### Parameters ignored by the grid

The keys `signal_threshold`, `entry_max`, `min_secs_remaining`, `obi_reject_thresh`,
`win_threshold`, `loss_threshold`, `min_ask_vol` are specific to the Polymarket
threshold strategy and are meaningless for the grid.

---

## Environment variables

### Binance

```bash
export BINANCE_API_KEY="your_api_key"
export BINANCE_API_SECRET="your_api_secret"
```

### MEXC

```bash
export MEXC_API_KEY="your_api_key"
export MEXC_API_SECRET="your_api_secret"
```

In the absence of these variables, orders are **simulated** (no real orders are
sent — identical behavior to `--simulate`).

---

## Setup and launch

### 1. Configure the exchange

On Binance or MEXC, create an API key with the following permissions:
- **Read** (mandatory)
- **Spot trading** (mandatory for real orders)
- **Withdrawal**: disable without exception

### 2. Create the strategy file

```bash
cp strategies/grid/grid_BTCUSDT.json strategies/grid/grid_BTCUSDT_live.json
# adjust grid_lower, grid_upper, grid_levels, grid_order_size_usdt
```

Calibration rules:
- `grid_lower` / `grid_upper`: cover 10–20% on each side of the current price
  (e.g. BTC at 100k → [88k, 112k])
- `grid_levels`: 20–50 levels → `grid_step` of 500–2,000 USDT
- `grid_order_size_usdt`: budget at least 10 × `grid_step` of available capital
  to avoid running out of funds if all BUY orders fill

### 3. Reference the strategy in config.json

```json
{
    "strategy": "/path/to/strategies/grid/grid_BTCUSDT_live.json"
}
```

### 4. Test in simulation

```bash
BINANCE_API_KEY="" BINANCE_API_SECRET="" bash scripts/start_bot.sh --simulate
```

Verify in the logs that `GridStrategy` loads correctly and that levels are computed.

### 5. Launch in production

```bash
export BINANCE_API_KEY="..."
export BINANCE_API_SECRET="..."
bash scripts/start_bot.sh
```

---

## Profitability calculation

### Profit per cycle

```
qty_btc      = grid_order_size_usdt / buy_price
gross_profit = grid_step × qty_btc
buy_fee      = buy_price × qty_btc × fee_rate
sell_fee     = (buy_price + grid_step) × qty_btc × fee_rate
net_profit   = gross_profit − buy_fee − sell_fee
```

Example (Binance, fee_rate = 0.1%):
```
grid_order_size = 50 USDT
buy_price       = 99,000 USDT
qty_btc         = 50 / 99,000 = 0.000505 BTC
grid_step       = 1,000 USDT

gross_profit = 1,000 × 0.000505 = 0.505 USDT
fees         ≈ 0.10 USDT  (0.1% × 2 × ~50 USDT)
net_profit   ≈ 0.40 USDT per cycle
```

### Cycle frequency

Depends on volatility. On BTC/USDT with a `grid_step` of 1,000 USDT and a
daily volatility of ±3,000 USDT, expect 6–15 cycles per active level per day.

### Main risk: grid breakout

If the price leaves `[grid_lower, grid_upper]`, all BUY orders are filled
(price falls) or all SELL orders (price rises), and the grid must be
reconfigured. The maximum theoretical loss equals the purchase cost of all BUY
levels if the price collapses.

```
max_theoretical_loss = grid_levels × grid_order_size_usdt
```

With 20 levels at 50 USDT = 1,000 USDT maximum capital at risk.

---

## Implementation status

| Component | Status |
|---|---|
| `GridStrategy.__init__()` — level computation | ✅ Implemented |
| `GridStrategy.level_at_price()` — price lookup | ✅ Implemented |
| `GridState`, `GridLevel` — data structures | ✅ Implemented |
| Parameter validation (bounds, levels, size) | ✅ Implemented |
| `connectors.load("binance")` / `load("mexc")` | ✅ Implemented |
| `strategies.load("grid", config)` — factory | ✅ Implemented |
| Routing in `handle_book_update()` | ✅ Implemented |
| `_initialise_grid()` — initial order placement | ✅ Implemented |
| `_poll_fills()` — fill detection via REST | ✅ Implemented |
| `_on_buy_filled()` / `_on_sell_filled()` — counter-orders | ✅ Implemented |
| `_check_stop_loss()` — cancel if outside grid | ✅ Implemented |
| `_save_state()` — SQLite persistence (`grid_state` + `grid_levels`) | ✅ Implemented |
| `restore_from_db()` — restart recovery + exchange reconciliation | ✅ Implemented |
| WebSocket user data stream (real-time fills) | ✅ Implemented |

---

## Adding a CEX connector

To connect an exchange other than Binance or MEXC:

1. Create `tradinebotte-cex/api_myexchange.py` by copying `api_binance.py`
2. Implement the 8 interface functions (see `tradinebotte-cex/connectors/__init__.py`)
3. Add the entry in `tradinebotte-cex/connectors/__init__.py`:
   ```python
   _REGISTRY["myexchange"] = "api_myexchange"
   ```
4. Use `"connector": "myexchange"` in the strategy JSON

---

## Related files

| File | Role |
|---|---|
| `tradinebotte-cex/strategy_engines/grid.py` | `GridStrategy` implementation |
| `tradinebotte-cex/strategy_engines/__init__.py` | Strategy factory |
| `tradinebotte-cex/strategy_engines/base.py` | `Strategy` protocol (interface) |
| `tradinebotte-cex/connectors/__init__.py` | Connector factory |
| `tradinebotte-cex/api_binance.py` | Binance REST + WebSocket connector |
| `tradinebotte-cex/api_mexc.py` | MEXC REST + WebSocket connector |
| `strategies/grid/grid_BTCUSDT.json` | Example BTC/USDT Binance config |
| `docs/AdaptedGridTrading.md` | Bear/bull/lateral grid variants |
