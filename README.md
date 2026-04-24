# tradinebotte

> 🇫🇷 [Version française](README.fr.md)

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets, targeting Bitcoin Up/Down 5-minute markets on Polygon. Uses a quantitative signal strategy (`best_bid >= 0.96`) backtested at **98.3% win rate** across 1663 trades (April 2026).

## Strategy

- Monitors "Bitcoin Up or Down — 5 minutes" markets with `endDate` within ±6 minutes of now
- Entry signal: `best_bid >= 0.96` on a UP or DOWN token
- Executes LIMIT BUY at `best_ask` via Polymarket CLOB API
- Resolves WIN at bid >= 0.99, LOSS at bid <= 0.01, or at market expiry (bid >= 0.50 = WIN)
- Daily stop-loss: $30 | Stake per trade: $10 | Fee: 2%

## Database

The bot uses **SQLite** (`live.db`) with WAL journal mode for concurrent read access (the monitor script can query while the bot writes). The database file is stored at `POLYMARKET_DIR/live.db` (default: `/opt/polymarket-live/live.db`).

### Table: `trades`

One row per trade entry. All signal conditions at the moment of entry are captured alongside the resolution outcome, enabling full post-session analysis.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incremented trade ID |
| `market_id` | TEXT | Polymarket condition ID |
| `token_id` | TEXT | Token subscribed (UP or DOWN) |
| `direction` | TEXT | `"UP"` or `"DOWN"` |
| `question` | TEXT | Market title (truncated to 80 chars) |
| `signal_ts_ms` | INTEGER | Unix timestamp (ms) when signal fired |
| `signal_seconds_elapsed` | REAL | Seconds since market open at entry |
| `signal_secs_remaining` | REAL | Seconds until market close at entry |
| `signal_best_bid` | REAL | Best bid at signal time |
| `signal_best_ask` | REAL | Best ask (= entry price) |
| `signal_spread` | REAL | Spread at entry |
| `signal_ask_vol` | REAL | Ask-side liquidity at entry (USD) |
| `signal_obi` | REAL | Order book imbalance at entry (−1 to +1) |
| `entry_ts_ms` | INTEGER | Unix timestamp (ms) of order submission |
| `entry_price` | REAL | Limit price submitted |
| `clob_order_id` | TEXT | Order ID returned by the CLOB API |
| `stake` | REAL | USD committed |
| `tokens_bought` | REAL | Tokens quantity = stake / entry_price |
| `fee` | REAL | Protocol fee (2% of min(p, 1−p) × tokens) |
| `cost_total` | REAL | stake + fee |
| `resolved` | INTEGER | 0 = open, 1 = resolved |
| `resolution_ts_ms` | INTEGER | Unix timestamp (ms) of resolution |
| `resolution_bid` | REAL | Best bid at resolution time |
| `outcome` | TEXT | `"WIN"` or `"LOSS"` |
| `pnl_gross` | REAL | Gross P&L before fees |
| `pnl_net` | REAL | Net P&L after protocol fee and gas |
| `pnl_roi_pct` | REAL | ROI as percentage of stake |
| `capital_before` | REAL | Capital before this trade |
| `capital_after` | REAL | Capital after resolution |
| `created_at` | INTEGER | Row creation timestamp (ms) |

### Table: `snapshots`

Price snapshots saved every 5 seconds per tracked token, used for post-session charting and strategy analysis.

| Column | Type | Description |
|---|---|---|
| `id` | INTEGER PK | Auto-incremented |
| `ts_ms` | INTEGER | Snapshot timestamp (ms) |
| `market_id` | TEXT | Polymarket condition ID |
| `token_id` | TEXT | Token ID |
| `direction` | TEXT | `"UP"` or `"DOWN"` |
| `secs_remaining` | REAL | Seconds until market close |
| `best_bid` | REAL | Best bid at snapshot time |
| `best_ask` | REAL | Best ask at snapshot time |
| `spread` | REAL | Spread |
| `ask_vol` | REAL | Top-5 ask depth (USD) |
| `obi` | REAL | Order book imbalance |
| `has_open_trade` | INTEGER | 1 if a trade was open at this moment |

### Configuration options

The following optional keys can be added to `config.json`:

| Key | Type | Default | Description |
|---|---|---|---|
| `db_mmap_mb` | integer | `0` | Memory-map the database file for faster reads. `0` = disabled. Set to e.g. `256` to map up to 256 MB via the kernel page cache. The OS already keeps the file in RAM for this workload, so this is optional. |

### Useful queries

```bash
# Recent trades
sqlite3 live.db "SELECT id, direction, outcome, pnl_net, capital_after
                 FROM trades ORDER BY id DESC LIMIT 10;"

# Session summary
sqlite3 live.db "SELECT COUNT(*) total,
                        SUM(CASE WHEN outcome='WIN' THEN 1 END) wins,
                        SUM(CASE WHEN outcome='LOSS' THEN 1 END) losses,
                        ROUND(SUM(pnl_net), 2) net_pnl
                 FROM trades WHERE resolved=1;"

# Open positions
sqlite3 live.db "SELECT id, market_id, direction, entry_price, signal_ts_ms
                 FROM trades WHERE resolved=0;"

# Price history for a token
sqlite3 live.db "SELECT ts_ms, best_bid, best_ask, obi
                 FROM snapshots WHERE token_id='<id>'
                 ORDER BY ts_ms DESC LIMIT 100;"
```

## Installation

See **[INSTALL](INSTALL)** for the full installation guide, including requirements, dependencies, wallet setup, configuration, running, monitoring, and how to test in a virtual environment.

## Tests

```bash
bash scripts/run_tests.sh
```

The suite runs 99 tests (71 for the live bot, 28 for the backtest engine) covering: fee calculation, WebSocket message parsing, OBI computation, market registration, all 8 signal entry guards (including the daily stop-loss), trade resolution (WIN/LOSS/expiry), PnL calculation, crash-recovery state restore, and all backtest signal/resolution/parameter paths. No network access or credentials are required — an in-memory SQLite database is used for every test.

## Backtest

Replay historical `snapshots` data against configurable strategy parameters:

```bash
python3 scripts/backtest.py                        # default parameters
python3 scripts/backtest.py --threshold 0.95       # custom threshold
python3 scripts/backtest.py --detail               # print per-trade table
python3 scripts/backtest.py --compare              # compare vs actual bot trades
python3 scripts/backtest.py --sweep                # grid search (135 combinations)
POLYMARKET_DIR=~/mybot python3 scripts/backtest.py # custom database path
```

## Notes

- WebSocket recv timeouts at ~30s during quiet periods are **normal** — `ping_interval=20` keepalives maintain the connection; the bot reconnects only when all tracked markets have expired
- Market refresh (Gamma API polling every 90s) runs as a **background async task** so WebSocket message processing is never blocked during HTTP calls
- The Gamma API query uses `tag_id=102892` (the `5M` tag) to pre-filter server-side to 5-minute markets only, reducing each poll from potentially thousands of markets to ~12–20 in a **single API call** (no pagination)
- If `POLY_PRIVATE_KEY` is not set, orders are simulated (no on-chain execution)
- Signals can be infrequent during low-volatility BTC periods — this is expected
- Do not modify `SIGNAL_THRESHOLD` (0.96) without re-running the full backtest

## License

See [LICENSE](LICENSE).
