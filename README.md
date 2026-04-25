# tradinebotte

> 🇫🇷 [Version française](README.fr.md)

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets, targeting Bitcoin Up/Down 5-minute markets on Polygon. Uses a quantitative signal strategy (`best_bid >= 0.96`) backtested at **98.3% win rate** across 1663 trades (April 2026).

## Features

- **Quantitative signal strategy** — entry on `best_bid >= 0.96`, backtested at 98.3% win rate across 1663 trades
- **Real-time WebSocket feed** — subscribes to Polymarket order books; processes every bid/ask update with sub-second latency
- **Automatic market discovery** — polls the Gamma API every 30 s in a background task; tracks only markets expiring within ±6 minutes to avoid stale prices
- **Automated trade resolution** — closes positions automatically on WIN (bid ≥ 0.99), LOSS (bid ≤ 0.01), or market expiry
- **Daily stop-loss** — halts trading for the day after a $30 net loss; resumes the next session
- **SQLite persistence** (WAL mode) — all trades and 5-second price snapshots are stored; state survives crashes and restarts
- **Crash recovery** — restores unresolved trades from the database on startup; rebuilds capital from historical PnL
- **Backtest engine** — replays `snapshots` data against any parameter set; supports grid search across 135 combinations; `--db file1.db file2.db` or `--db data/*.db` runs independent capital simulations across multiple snapshot files; `--all` auto-scans `data/` and prepends `live.db` when usable; aggregate win-rate and PnL printed across all files; falls back to the bundled sample dataset (`data/backtest_sample_btc5m_range_2026.db`) when no live database is present
- **Optional HTML status page** — bot writes a self-refreshing page (configurable path, optional HTTP Basic Auth) — [see preview](docs/status_example.html)
- **Pluggable exchange API** — all Polymarket-specific code lives in `bot/api_polymarket.py`; swapping exchanges requires only a new adapter file and a single import change in `live_bot.py`
- **JSON strategy files** — signal and capital parameters live in `strategies/polymarket_BTC5M.json`; switch strategies by pointing `"strategy"` in `config.json` to any file
- **Simulation mode** — `--simulate` flag isolates all file I/O to `/tmp/tradinebotte-sim`, mirrors logs to stdout, and places no real orders; safe to run on any machine without affecting production data
- **Type-annotated codebase** — all 28 functions and class methods in `live_bot.py` carry full parameter and return type hints; enables static analysis and IDE autocompletion
- **108-test suite** — `tests/test_bot.py` (80 tests) covers all 11 signal guards, resolution paths, fee calculation, WebSocket parsing, HTML status page, htpasswd hashing, and state restore; `tests/test_backtest.py` (28 tests) covers the replay engine end-to-end; no network or credentials required
- **Continuous security audit** — `pip-audit` runs on every push and weekly to detect CVEs in runtime deps (`aiohttp`, `websockets`, `web3`, `py-clob-client`); Dependabot opens automated PRs when newer versions are available
- **Async logging + latency tracking** — log writes never block the event loop; each trade emits a `[LATENCY]` line with `signal_ms` (WS message → order decision) and `order_rtt_ms` (CLOB API round-trip); `scripts/latency.py` parses the log and prints min/mean/p50/p90/p99/max for each metric; a `QueueListener` daemon thread drains the log queue to disk in the background; add `--no-log` to suppress the log file entirely (SQLite DB is unaffected) for minimum disk I/O in production

## Strategy

- Monitors "Bitcoin Up or Down — 5 minutes" markets with `endDate` within ±6 minutes of now
- Entry signal: `best_bid >= 0.96` on a UP or DOWN token
- Executes LIMIT BUY at `best_ask` via Polymarket CLOB API
- Resolves WIN at bid >= 0.99, LOSS at bid <= 0.01, or at market expiry (bid >= 0.50 = WIN)
- Daily stop-loss: $30 | Stake per trade: $10 | Fee: 2%

## Database

The bot uses **SQLite** (`live.db`) with WAL journal mode for concurrent read access (the monitor script can query while the bot writes). The database file is stored at `TRADINEBOTTE_DIR/live.db` (default: `~/tradinebotte/live.db`).

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
| `webstatuspage_html` | boolean | `false` | Enable the static HTML status page. When `true`, the bot writes a page to `webstatuspage_path` every 5 minutes and after each trade resolution. Requires a web server pointed at the HTML directory — see `INSTALL` for full prerequisites. |
| `webstatuspage_path` | string | `~/public_html/tradinebot_status.html` | Filesystem path for the HTML status page. `~` is expanded to the home directory. The directory is created automatically if it does not exist. If using the default `~/public_html` path, Apache `mod_userdir` must be enabled. |
| `webstatus_user` | string | `"tradinebot"` | Username for HTTP Basic Auth protection via `.htaccess`. Only used when `webstatus_password` is set. |
| `webstatus_password` | string | `""` | Password for HTTP Basic Auth. If empty, no `.htaccess` is created and the page is publicly accessible. When set, the bot writes a `.htaccess` in the HTML directory and a `.htpasswd` (Apache `{SHA}` format) at `TRADINEBOTTE_DIR/.webstatus_htpasswd` (outside the web root). **Apache prerequisites:** `mod_auth_basic` + `mod_authn_file` enabled, `AllowOverride AuthConfig` on the HTML directory, and read permission on the `.htpasswd` file for the Apache process (`www-data`). **nginx:** does not process `.htaccess` — configure `auth_basic` manually in your server block pointing to the same `.htpasswd` file. |

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

**New user?** See **[QUICKSTART.md](QUICKSTART.md)** — 5 commands, bot running in minutes.

Full guide (requirements, wallet setup, web status page, monitoring, testing): **[INSTALL.md](INSTALL.md)**.

## Tests

```bash
bash scripts/run_tests.sh
```

The suite runs 108 tests (80 for the live bot, 28 for the backtest engine) covering: fee calculation, WebSocket message parsing, OBI computation, market registration, all 11 signal entry guards (including the daily stop-loss), trade resolution (WIN/LOSS/expiry), PnL calculation, crash-recovery state restore, htpasswd SHA1 hashing, HTML status page rendering, async book-update state, and all backtest signal/resolution/parameter paths. No network access or credentials are required — an in-memory SQLite database is used for every test.

## Backtest

Replay historical `snapshots` data against configurable strategy parameters.
If `TRADINEBOTTE_DIR/live.db` is absent or has fewer than 100 snapshots, the script falls back automatically to the bundled sample dataset (`data/backtest_sample_btc5m_range_2026.db`, 2430 snapshots from real BTC 5-minute markets collected on 2026-04-25). The selected database is printed at startup.

```bash
python3 scripts/backtest.py                        # default parameters
python3 scripts/backtest.py --threshold 0.95       # custom threshold
python3 scripts/backtest.py --detail               # print per-trade table
python3 scripts/backtest.py --compare              # compare vs actual bot trades
python3 scripts/backtest.py --sweep                # grid search (135 combinations)
python3 scripts/backtest.py --db data/s1.db data/s2.db  # explicit files
python3 scripts/backtest.py --db data/*.db         # shell glob (independent capital per file)
python3 scripts/backtest.py --all                  # scan data/ + live.db if ≥ 100 snapshots
TRADINEBOTTE_DIR=~/mybot python3 scripts/backtest.py # custom database path
```

## Notes

- The `sqlite3` CLI is optional — the bot uses Python's built-in module. Install it (`sudo apt install sqlite3`) only if you want to run manual DB queries. Without sudo, use: `~/tradinebotte/venv/bin/python3 -c "import sqlite3; c=sqlite3.connect('live.db'); print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"`

- WebSocket recv timeouts at ~30s during quiet periods are **normal** — `ping_interval=20` keepalives maintain the connection; the bot reconnects only when all tracked markets have expired
- Market refresh (Gamma API polling every 30s) runs as a **background async task** so WebSocket message processing is never blocked during HTTP calls
- The Gamma API query uses `tag_id=102892` (the `5M` tag) to pre-filter server-side to 5-minute markets only, reducing each poll from potentially thousands of markets to ~12–20 in a **single API call** (no pagination)
- If `POLY_PRIVATE_KEY` is not set, orders are simulated (no on-chain execution)
- Signals can be infrequent during low-volatility BTC periods — this is expected
- Do not modify `SIGNAL_THRESHOLD` (0.96) without re-running the full backtest

## License

See [LICENSE](LICENSE).
