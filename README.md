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
- **Backtest engine** — replays `snapshots` data against any parameter set; supports grid search across 135 combinations; `--db file1.db file2.db` or `--db data/*.db` runs independent capital simulations across multiple snapshot files; `--all` auto-scans `data/` and prepends `live.db` when usable; aggregate win-rate and PnL printed across all files; falls back to the bundled sample dataset (`data/backtest_sample_btc5m_range_2026.db`) when no live database is present; `analysis/backtest.py` now supports fractional Kelly position sizing, Sharpe and Sortino ratios, walk-forward optimization, weekday volatility filter (P1), and step-function stake sizing (P2)
- **Optional HTML status page** — bot writes a self-refreshing page (configurable path, optional HTTP Basic Auth) — [see preview](docs/status_example.html)
- **Multi-bot WebSocket sharing** — `bot/feed.py` holds a single WebSocket connection and broadcasts every book update over ZeroMQ PUB; one or more `bot/account_bot.py` processes subscribe and trade with fully isolated SQLite databases, logs, and configs; each bot evaluates signals with **its own** strategy parameters (different thresholds, stakes, or hour filters); works across Linux users (`/home/user1`, `/home/user2`); **feed auto-starts** — the first account_bot to launch starts feed.py automatically via a race-safe file lock, no manual feed management needed — see [docs/multi.md](docs/multi.md) for the full decision guide and architecture reference
- **Pluggable exchange API** — all Polymarket-specific code lives in `bot/api_polymarket.py`; swapping exchanges requires only a new adapter file and a single import change in `live_bot.py`; Binance spot (`bot/api_binance.py`), MEXC spot (`bot/api_mexc.py`), and Bitstamp spot (`bot/api_bitstamp.py`) connectors are included, implementing the identical interface with HMAC-SHA256 signing, OBI computation, and dry-run mode
- **Binance Simple Earn Flexible manager** (`bot/earn_manager.py`) — `EarnManager` parks idle USDT after sell trades (`park_idle()`) and redeems before buy trades (`ensure_liquid()`); automatic product discovery and APR reporting; sim mode when credentials are absent; MEXC Earn not supported (API too unstable)
- **Technical indicator service** (`bot/indicators.py`) — ZeroMQ pipeline stage: subscribes to feed.py PUB (port 5557), computes RSI, SMA, EMA, and rolling volatility on the `best_bid` price series of each token, republishes enriched `{"t":"indicators"}` messages on a second PUB socket (port 5559); pure stdlib math (no numpy); consumers subscribe to port 5559 to get live indicator values alongside book data; new `binance_scalping` source combines Binance depth20 + aggTrade WebSocket streams to compute OBI, EMA, deceleration, `spread_bps`, `realized_vol_bps`, and TFI in real time; unified 9-stream config in `strategies/indicators/indicators_all.json` (PUB 5559, REP 5561); `btc_4h` stream now includes EMA(50), EMA(200), ATR(14) for swing strategy consumers
- **Grid trading** — BTC/USDT spot grid strategy for Binance/MEXC: places buy orders across N evenly-spaced levels, collects profit on each BUY→SELL cycle; three modes: `static` (stop when price exits range), `trail=bear` (re-center downward — profitable exit on bounce; −3.3% → +2.0% on 2022 LUNA crash), `trail=bull` (re-center upward — captures full bull run; +0.1% → +3.7% on 2024 bull run); `trail_mode` can be set directly in the strategy JSON to persist across restarts; backtested on three 90-day BTC regimes (lateral 2026, bear 2022, bull 2024); see [`docs/AdaptedGridTrading.md`](docs/AdaptedGridTrading.md)
- **Binance OBI scalping bot** (`bot/orderbook_bot.py`) v2.1 — connects to Binance spot and perpetual depth20 WebSocket streams (100 ms update rate), computes OBI from top-N bid/ask levels with EMA smoothing; SHORT-only strategy (bid-heavy order book signals spoofing pressure and an imminent price fall); TP 15 bps, SL 8 bps, max hold 3 minutes; limit-order simulation mode with `sim_`-prefixed order IDs; records snapshots and trades to `live_ob.db`; configured via `strategies/scalping/orderbook_btc.json` (`entry_thresh`, `confirm_n`, `tp`, `sl`, `n_levels`)
- **Swing trading strategy** (`bot/strategy_engines/swing.py`) — `SwingStrategy` engine places limit BUY orders at support levels and SELL orders at resistance levels; EMA(200) 4h directional filter skips buys when price is below the 200-period EMA; ATR(14) dynamic stop-loss; RSI(14, 4h) overbought filter; subscribes to the shared indicators service (ZMQ SUB); SQLite persistence with position restore on restart; configured via `strategies/swing/swing_BTCUSDT.json` (`supports`, `resistances`, `position_size`, `max_positions`, `atr_sl_multiplier`); deploy with `scripts/update_swing.sh`
- **Live Binance scalping bot** (`bot/scalping_bot.py`) — three independent strategies (`candle_momentum`, `meanrev`, `breakout`) on 1-minute OHLCV data; all 27 `DEFAULTS` parameters documented in the module docstring; backtestable with `analysis/backtest_scalping.py`
- **Shared scalping math** (`bot/scalping_math.py`) — ATR, Bollinger Bands, VWAP, volume z-score, and rolling max helpers shared between `scalping_bot.py` and `indicators.py`
- **API latency benchmark** — `analysis/benchmark_api.py` measures REST round-trip time and WebSocket time-to-first-message across all three exchanges; reports min/mean/p50/p90/p99/max/σ; `--rounds N` and `--no-ws` options
- **Bilingual interface** — `scripts/setup.py`, `install.sh`, `start_bot.sh`, and `monitor.sh` prompt `[E] English / [F] Français` at startup; the choice is persisted as `"lang"` in `config.json` and inherited automatically by subsequent scripts
- **JSON strategy files** — signal and capital parameters live in `strategies/polymarket/polymarket_BTC5M.json`; switch strategies by pointing `"strategy"` in `config.json` to any file; `strategies/polymarket/polymarket_BTC5M_piste3.json` adds dynamic stake scaling (`bid_alpha`), OBI rejection (`obi_reject_thresh`), and weekly stop-loss (`weekly_stop_loss`); backtest vs original: PnL +85%, MaxDD −28%, Sharpe 3.28 vs 1.97
- **Long-term BTC cycle strategy** — three production-ready configs: `strategies/longterm/longtermcyclestrategygridV1.json` (5%/25% rebound/tranche, ×24.0, Calmar 0.54), `V2.json` (4%/20%, ×24.2, Calmar 0.54), `V3.json` (halving-relative prudence tiers T1/T2, Calmar 0.75); backtest using `analysis/backtest_cycle_strategy.py` with flags `--top-mm`, `--rebound`, `--drawback`, `--tranche`, `--prudence`, `--compare`; cycle analysis via `analysis/analyze_btc_cycles.py` and `analysis/analyze_cycle_volatility.py`
- **Configurable market timeframe** — `market_tag_id` and `market_window_mins` in the strategy JSON switch between 5-minute and 15-minute BTC Up/Down markets; `strategies/polymarket/polymarket_BTC15M_piste3.json` ships ready to use; startup log confirms the active tag and window
- **Connector/strategy compatibility check** — `validate()` in `bot/connectors/__init__.py` raises a `RuntimeError` at startup if the connector lacks methods required by the chosen strategy, listing every missing method; prevents silent runtime failures when swapping connectors
- **Simulation mode** — `--simulate` flag isolates all file I/O to `~/tradinebotte-sim` by default, mirrors logs to stdout, and places no real orders; set `TRADINEBOTTE_DIR` before launching to use a custom path — enabling multiple bots to run in parallel without directory conflicts
- **Type-annotated codebase** — all 28 functions and class methods in `live_bot.py` carry full parameter and return type hints; enables static analysis and IDE autocompletion
- **Hour/day filter** — optional `hour_filter` block in the strategy JSON restricts entries to configurable UTC hour ranges per weekday/weekend, with built-in handling for the US weekly open (Monday before 13:30 UTC) and close (Friday from 20:00 UTC); disabled by default; applied identically in the live bot and backtest engine; see [INSTALL.md](INSTALL.md#hour--day-filter) for full documentation
- **Test suite** — `tests/test_bot.py` covers all 11 signal guards, resolution paths, fee calculation, WebSocket parsing, HTML status page, htpasswd hashing, state restore, hour filter logic, stake scaling, weekly stop-loss, and market discovery config; `tests/test_backtest.py` covers the replay engine, fractional Kelly sizing, Sharpe/Sortino, walk-forward optimization, and weekday volatility filter; `tests/test_multibot.py` covers `feed.py` and `account_bot.py` including ZMQ round-trip integration; `tests/test_grid_trail.py` covers connector validation, stop-loss branching, grid re-centering, and DB restore; `tests/test_earn_manager.py` covers `EarnManager` subscribe/redeem/sim mode; `tests/test_cycle_strategy.py` covers the long-term cycle strategy backtest; `tests/test_scalping_bot.py` covers the three scalping strategies; Bitstamp adapter coverage in `tests/test_api_cex.py`; no network or credentials required
- **systemd services** — `scripts/install_service.sh` (Option A) and `scripts/install_feed_service.sh` / `scripts/install_account_service.sh` (Option B) generate ready-to-install unit files; bots restart automatically on failure or reboot (`Restart=on-failure`); the feed service uses `RestartSec=10` while account bots use `RestartSec=30`; `Requires=tradinebotte-feed.service` enforces the start/restart ordering
- **mypy type checking** — `mypy bot/ --ignore-missing-imports` reports 0 errors; CI workflow runs on every push and pull request
- **Integration test script** — `scripts/test_multibot_deploy.sh` automates a full clean-install and end-to-end test on a configurable set of Linux test accounts: cleanup, rsync deploy, venv creation, simultaneous launch of all N bots in `--verbose` mode (stress-tests the race-safe feed auto-start), 30s heartbeat monitoring, log analysis (WebSocket, book update count, error lines), teardown; server, port, users, and passwords are read from `~/.tradinebotte-test.conf` (template: `scripts/test_multibot.conf.example`); `--skip-deploy` reuses an existing install; `--duration N` adjusts the test window; exits 0 on full pass
- **Continuous security audit** — `pip-audit` runs on every push and weekly to detect CVEs in runtime deps (`aiohttp`, `websockets`, `web3`, `py-clob-client`); Dependabot opens automated PRs when newer versions are available
- **Async logging + latency tracking** — log writes never block the event loop; each trade emits a `[LATENCY]` line with `signal_ms` (WS message → order decision) and `order_rtt_ms` (CLOB API round-trip); `analysis/latency.py` parses the log and prints min/mean/p50/p90/p99/max for each metric; a `QueueListener` daemon thread drains the log queue to disk in the background; add `--no-log` to suppress the log file entirely (SQLite DB is unaffected) for minimum disk I/O in production; add `--no-snapshots` to skip writing 5-second price snapshots to the DB (trades are still recorded) — reduces write pressure during long sessions; add `--snapshot-interval SECS` to override the snapshot write interval in seconds (default: 5; use 1 for data-collection mode); add `--reset-db` to back up `live.db` to a timestamped file and delete it before launch so the bot starts from zero capital and trade history (prompts for confirmation; safe no-op if DB absent)
- **Data collection** (first deployment account — simulate mode, 1-second snapshots):
  - Deploy and start the collector:
    `bash scripts/start_collector.sh`           # deploy + launch
    `bash scripts/start_collector.sh --status`  # check if running
    `bash scripts/start_collector.sh --stop`    # stop
  - Download weekly database:
    `bash scripts/collect_db.sh --status`       # remote row counts
    `bash scripts/collect_db.sh --rotate`       # download + archive + restart
  - Automate weekly collection (cron):
    `bash scripts/schedule_collect.sh --install`   # every Sunday 03:00 UTC
    `bash scripts/schedule_collect.sh --status`    # show cron entry
    `bash scripts/schedule_collect.sh --run-now`   # run immediately

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

> **Server admin note:** `scripts/install.sh` detects missing system packages and prints the exact `sudo apt-get install` command to run as root — no manual package lookup needed. See [INSTALL.md — Server admin prerequisites](INSTALL.md#server-admin-prerequisites-debianubuntu).

## Tests

```bash
bash scripts/run_tests.sh
```

The suite covers: fee calculation, WebSocket message parsing, OBI computation, market registration, all 11 signal entry guards (including the daily stop-loss), trade resolution (WIN/LOSS/expiry), PnL calculation, crash-recovery state restore, htpasswd SHA1 hashing, HTML status page rendering, async book-update state, all backtest signal/resolution/parameter paths, fractional Kelly sizing, Sharpe/Sortino ratios, walk-forward optimization, weekday volatility filter, ZMQ feed/account_bot integration with one and two simultaneous bots, `EarnManager` subscribe/redeem/sim-mode flows, long-term cycle strategy backtest, the three scalping strategies (`candle_momentum`, `meanrev`, `breakout`), and Bitstamp adapter. No network access or credentials are required — an in-memory SQLite database is used for every test.

## Backtest

Replay historical `snapshots` data against configurable strategy parameters.
If `TRADINEBOTTE_DIR/live.db` is absent or has fewer than 100 snapshots, the script falls back automatically to the bundled sample dataset (`data/backtest_sample_btc5m_range_2026.db`, 2430 snapshots from real BTC 5-minute markets collected on 2026-04-25). The selected database is printed at startup.

Three real-session datasets are included in `data/`:

| File | Snapshots | Period | Trades | Win rate |
|---|---|---|---|---|
| `backtest_sample_btc5m_range_2026.db` | 2,430 | 2026-04-25 | 0 | — |
| `calmsaturday.db` | 10,126 | 2026-04-26 ~11h | 9 | 100% |
| `basicsunday.db` | 24,870 | 2026-04-25→26 ~26h | 43 | 97.7% |

Run all three at once with `--all` (aggregate: 52 trades, 51 wins, **98.1% win rate**).

```bash
python3 analysis/backtest.py                        # default parameters
python3 analysis/backtest.py --threshold 0.95       # custom threshold
python3 analysis/backtest.py --detail               # print per-trade table
python3 analysis/backtest.py --compare              # compare vs actual bot trades
python3 analysis/backtest.py --sweep                # grid search (135 combinations)
python3 analysis/backtest.py --sweep-all            # extended grid (405 combos, all DBs)
python3 analysis/backtest.py --sweep-all --sort pnl # sort sweep by pnl|ratio|wr
python3 analysis/backtest.py --sweep-all --top 10   # show top-10 unique configs (deduped)
python3 analysis/backtest.py --db data/s1.db data/s2.db  # explicit files
python3 analysis/backtest.py --db data/*.db         # shell glob (independent capital per file)
python3 analysis/backtest.py --all                  # scan data/ + live.db if ≥ 100 snapshots
TRADINEBOTTE_DIR=~/mybot python3 analysis/backtest.py # custom database path
```

## Grid Trading Backtest

Replay historical BTC/USDT OHLCV data against a configurable grid strategy. Fill model: price-touch on candle `[low, high]`. Requires 1-minute SQLite databases in `data/` — download with `analysis/download_btc_history.py`.

```bash
python3 analysis/backtest_grid.py --all                           # static grid, all DBs in data/
python3 analysis/backtest_grid.py --all --trail bear              # bear-adapted trailing
python3 analysis/backtest_grid.py --all --trail bull --compare    # bull trailing vs static
python3 analysis/backtest_grid.py --all --sweep --sort pnl        # parameter sweep
```

Download historical OHLCV data from Binance:

```bash
python3 analysis/download_btc_history.py                                          # last 90 days
python3 analysis/download_btc_history.py --start 2022-05-01 --end 2022-08-01     # bear market
python3 analysis/download_btc_history.py --start 2024-10-15 --end 2025-01-15     # bull run
```

See [`docs/AdaptedGridTrading.md`](docs/AdaptedGridTrading.md) for full strategy documentation, backtest results, and strategy selection guide.

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
