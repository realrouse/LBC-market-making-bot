# tradinebotte

> 🇫🇷 [Version française](README.fr.md)

Multi-strategy automated BTC/USDT trading platform for CEX spot markets. Runs accumulation, OBI scalping, grid, swing, DCA, and SwingHold strategies across Binance, MEXC, and Bitstamp, backed by a shared real-time signal pipeline. A Polymarket prediction-market connector is also included.

## Architecture

Independent subsystems communicate over ZMQ IPC sockets, built on a shared neutral core:

| Subsystem | Path | Role |
|---|---|---|
| Bot core | `tradinebotte-core/` | Neutral `botcore` package: Strategy protocol, connector registry, persistence, base schema — no exchange-specific code |
| CEX bots | `tradinebotte-cex/` | Trade execution, strategy engines |
| Indicators | `tradinebotte-indicators/` | Real-time signal pipeline |
| Status | `tradinebotte-status/` | Health monitoring, dashboard |
| Shared library | `tradinetools/` | Math, ZMQ helpers, logging |

Each bot family is a peer plugin behind the `botcore` interfaces: a `Strategy` implementation, a connector (exchange adapter), and a data plane. An optional Polymarket connector (`tradinebotte-polymarket/`) targets BTC prediction markets on Polygon — see [Polymarket module](#polymarket-module) below.

See [docs/design.md](docs/design.md) for the full process architecture and ZMQ message-flow reference.

## CEX Trading Bots

### Accumulation bot v1.5

[`tradinebotte-cex/strategy_engines/accumulation.py`] — OBI dip-buy with profit-ladder ratchet. A `live_bot.py`-hosted strategy engine (`strategy_type="accumulation"`): the indicators service feeds its `on_book_update` (scalping stream) and `on_indicator` (macro-gate streams). Monitors real-time order book imbalance via ZMQ; enters BTC/USDT on configurable dip thresholds with adaptive scale-in and rebuy trailing stop. Four optional signal gates: Fear & Greed (`fear_greed_gate`), Liquidations (`liq_gate`), Long/Short Ratio (`ls_ratio_gate`), RSI 4h (`rsi4h_gate`). Earn buffer via `earn_buffer_usd`; VWAP gate on initial buy only. Configured via `tradinebotte-cex/strategies/accumulation/btc_accumulation.json`. A MEXC-spot variant (`btc_accumulation_mexc.json`) runs the same strategy on genuine MEXC spot price/OBI (registered on demand from the shared feed as `btc_scalping_mexc`), with MEXC fees and Earn disabled. See [docs/accumulation.md](docs/accumulation.md) for the full strategy design document.

### OBI Scalping bot v2.12

[`tradinebotte-cex/orderbook_bot.py`] — High-frequency OBI scalping on Binance spot. Consumes depth20 + aggTrade WebSocket at ~100 ms; computes OBI from top-N bid/ask levels with EMA smoothing; long-only since v2.4. TP 15 bps, SL 8 bps, max hold 3 minutes. Progressive upgrades: v2.3 TFI filter, v2.5 calibrated TP/SL, v2.7 VWAP gate, v2.9 volume profile gate, v2.10 macro OBI multi-timeframe gate, v2.12 liquidations gate. Configured via `tradinebotte-cex/strategies/scalping/orderbook_btc.json`.

## Strategy Engines

Pluggable engines under `tradinebotte-cex/strategy_engines/`:

| Engine | Description |
|---|---|
| **Grid** (`grid.py`) | Static or trailing grids (static / trail=bear / trail=bull); backtested on three 90-day BTC regimes |
| **Swing** (`swing.py`) | Limit orders at support/resistance; EMA(200) 4h trend filter, ATR(14) dynamic SL, RSI(14) overbought filter |
| **SwingHold** (`swinghold.py`) | Swing entries with fractional sells at each resistance; holds remainder for long-term accumulation |
| **DCA** (`dca.py`) | Timed DCA buys at configurable intervals with TP and optional SL |

See [`docs/AdaptedGridTrading.md`](docs/AdaptedGridTrading.md) for grid strategy backtest results and selection guide, and [`docs/GridTrading.md`](docs/GridTrading.md) for operation and setup.

Long-term cycle strategy: three production configs in `tradinebotte-cex/strategies/longterm/` (V1: ×24.0, Calmar 0.54 / V2: ×24.2, Calmar 0.54 / V3: halving-relative tiers, Calmar 0.75). Backtest via `analysis/backtest_cycle_strategy.py`.

## Exchange Adapters

| Adapter | Exchange | Auth |
|---|---|---|
| `api_binance.py` | Binance spot | HMAC-SHA256 |
| `api_mexc.py` | MEXC spot | HMAC-SHA256 (Binance-compat v3); public depth WS is protobuf (`wbs-api.mexc.com`) |
| `api_mexc_futures.py` | MEXC Futures perpetual | HMAC-SHA256 (ApiKey + Request-Time headers) |
| `api_bitstamp.py` | Bitstamp spot | OAuth2 |

Shared helpers in `api_common.py`: order book parsing, HMAC signing, dry-run mode. Adding an exchange requires only a new adapter file. `validate()` in the `botcore.connectors` registry (re-exported through `tradinebotte-cex/connectors/__init__.py`) checks connector/strategy method compatibility at startup and raises `RuntimeError` with the full list of missing methods.

**Binance Simple Earn Flexible** (`earn_manager.py`): `EarnManager` parks idle USDT after sell trades (`park_idle()`) and redeems before buy trades (`ensure_liquid()`). Automatic product discovery and APR reporting. Sim mode when credentials are absent. MEXC Earn not supported (API too unstable).

## Signal Pipeline

`tradinebotte-indicators/indicators.py` runs as a standalone ZMQ pipeline stage:

- **Input**: Binance depth20 + aggTrade WebSocket streams (100 ms update rate)
- **Indicators**: RSI(14/21), SMA, EMA(50/200), ATR(14), OBI, TFI, `spread_bps`, `realized_vol_bps`, VWAP
- **Output**: ZMQ PUB enriched `{"t":"indicators"}` messages (IPC by default, or TCP port 5559)
- **Streams**: unified config in `tradinebotte-indicators/strategies/indicators_all.json`; `btc_4h` stream for swing consumers; liquidations via public `wss://fstream.binance.com/ws/{symbol}@forceOrder`; optional full-depth perp stream (`btc_full_depth_perp`)
- **Watchdog**: 120-second recv timeout (`asyncio.wait_for`) on all WS loops to prevent indefinite hangs

### Shared CEX feed (data plane)

`tradinebotte-cex/cex_feed.py` fetches each external CEX order book **once** and fans it out over ZMQ (TCP 5563) so bots never open their own exchange WebSocket; order placement stays per-bot with each account's own credentials. One independent task per exchange — currently **Binance spot**, **MEXC spot**, and **MEXC futures** for BTC. Consumers filter by `(exchange, symbol)`, so several exchanges can publish the same symbol without cross-contamination.

- **MEXC spot** uses MEXC's protobuf public WebSocket (`wbs-api.mexc.com`, channel `spot@public.limit.depth.v3.api.pb`); the binary depth frames are decoded via a vendored minimal schema (`tradinebotte-cex/mexc_proto/`). MEXC public sockets are kept alive with an app-level ping.
- The indicators service can source a scalping stream from this shared feed (`cex_scalping` source → e.g. `btc_scalping_mexc`) instead of opening its own exchange WS.

**On-demand stream registration**: bots declare the indicator streams they need in config (`indicators_streams`) and register them with the indicators REP socket (TCP 5561), re-registering periodically so a stream self-heals if the indicators service restarts — no hand-maintained static config required.

Optional shared SQLite orderbook database (`orderbook_current` + `orderbook_snapshots`), configurable per stream via `db_path`, `bucket_size_usd`, `db_write_every_n`, `history_retention_h`.

See [docs/indicators.md](docs/indicators.md) for the full reference guide.

## Monitoring

`tradinebotte-status/status_collector.py` — standalone heartbeat collector (ZMQ port 5562):

- Receives per-bot heartbeats (sent every **120 s**), writes to SQLite, prunes rows older than one year. A bot is flagged **STALE after 240 s** and **DEAD after 600 s** (`HEARTBEAT_STALE_S` / `HEARTBEAT_DEAD_S`), so a genuine outage surfaces within minutes
- `generate_status.py` polls all deployment accounts via SSH and writes a single HTML health page showing bot health, service versions, and payload details (PnL, position counts, WebSocket connectivity); all PnL figures read from the heartbeat payload (one source of truth across Polymarket and CEX bots)
- Default output: `~/public_html/tradinebottestatus.html`, overridable via `--out` or `$TRADINEBOTTE_STATUS_OUT`
- See [docs/logging.md](docs/logging.md) for the canonical log tag vocabulary used by alerting and log parsers

## Deployment

Systemd user services — no `sudo` required:

```bash
systemctl --user status tradinebotte-live.service
systemctl --user status tradinebotte-indicators.service
systemctl --user status tradinebotte-feed.service
```

Multiple isolated accounts, each with its own install directory, config, and log files. Deploy sequentially across all accounts:

```bash
bash tradinebotte-cex/scripts/deploy_all.sh
```

## Shared Library: tradinetools

Package at `tradinetools/`, installed with `pip install -e tradinetools/`:

| Module | Contents |
|---|---|
| `math.py` | `sma_last`, `ema_last`, `atr_last`, `bollinger_last`, `vwap_last`, `vol_zscore_last`, `rolling_max_last` |
| `zmq.py` | ZMQ socket factories |
| `logging.py` | `setup_root_logger()` (rotating file, 10 MB), `setup_logger()` (named service logger) |
| `schemas.py` | Versioned message dataclasses |

## Analysis and Backtesting

| Script | Strategy |
|---|---|
| `analysis/backtest.py` | Polymarket snapshot replay; `--sweep` (135 combos); fractional Kelly, Sharpe/Sortino, walk-forward |
| `analysis/backtest_grid.py` | Grid trading OHLCV replay; `--trail bear/bull`, `--sweep --sort pnl` |
| `analysis/backtest_swing_dca.py` | DCA / Swing / SwingHold; `--compare`, `--all-dbs`, `--sweep`, `--config` |
| `analysis/backtest_orderbook.py` | OBI scalping replay |
| `analysis/backtest_cycle_strategy.py` | Long-term BTC cycle strategy; V1/V2/V3 configs |
| `analysis/benchmark_api.py` | REST + WS latency benchmark across all three exchanges |
| `analysis/calibrate_obi_proxy.py` | OBI threshold calibration |

Download historical BTC OHLCV data (Binance 1-minute candles):

```bash
python3 analysis/download_btc_history.py                                       # last 90 days
python3 analysis/download_btc_history.py --start 2022-05-01 --end 2022-08-01  # bear market
python3 analysis/download_btc_history.py --start 2024-10-15 --end 2025-01-15  # bull run
```

## Polymarket Module

`tradinebotte-polymarket/` — prediction market connector for BTC Up/Down 5-minute and 15-minute markets on Polygon. Fully operational; included as one connector among several rather than the primary use case.

- **`live_bot.py`** — async entrypoint over the `botcore` neutral core; the Polymarket trading logic and data plane live in flat plugin modules beside it (`pm_strategy.py`, `pm_data.py`, `pm_types.py`, `pm_calendar.py` + `api_polymarket.py`), re-exported by `live_bot` for back-compat. Entry on `best_bid >= 0.95`; WIN at bid ≥ 0.99, LOSS at bid ≤ 0.01; daily stop-loss ($30), crash recovery (restores unresolved trades on startup), optional HTML status page with HTTP Basic Auth
- **`feed.py` + `account_bot.py`** — multi-bot WebSocket sharing via ZMQ IPC; each `account_bot.py` (run flat, no `bot/` subdir) trades with a fully isolated SQLite DB, log, and config
- **Strategy files**: `tradinebotte-polymarket/strategies/polymarket_BTC5M.json`; `polymarket_BTC5M_piste3.json` adds dynamic stake scaling (`bid_alpha`), OBI rejection, and weekly stop-loss; see [docs/KellySizing.md](docs/KellySizing.md) for the fractional Kelly sizing design
- **Database** `live.db` (SQLite WAL): `trades` table (21 columns — full signal context through resolution), `snapshots` table (5-second price snapshots for post-analysis); see [docs/snapshots.md](docs/snapshots.md) for schema and query reference
- **Useful queries**:

```bash
sqlite3 live.db "SELECT id, direction, outcome, pnl_net, capital_after FROM trades ORDER BY id DESC LIMIT 10;"
sqlite3 live.db "SELECT COUNT(*) total, SUM(CASE WHEN outcome='WIN' THEN 1 END) wins, ROUND(SUM(pnl_net),2) net_pnl FROM trades WHERE resolved=1;"
```

**Data collection** (simulate mode, 1-second snapshots):

```bash
bash tradinebotte-polymarket/scripts/start_collector.sh           # deploy + launch
bash tradinebotte-polymarket/scripts/start_collector.sh --status  # check if running
bash tradinebotte-polymarket/scripts/collect_db.sh --rotate       # download + archive + restart
bash tradinebotte-polymarket/scripts/schedule_collect.sh --install # weekly cron (Sunday 03:00 UTC)
```

See [docs/multi.md](docs/multi.md) for the full multi-bot architecture reference.

## Installation

**New user?** See **[QUICKSTART.md](QUICKSTART.md)** — 5 commands, bot running in minutes.

Full guide (requirements, wallet setup, web status page, monitoring, testing): **[INSTALL.md](INSTALL.md)**.

> **Server admin note:** `scripts/install.sh` detects missing system packages and prints the exact `sudo apt-get install` command — no manual package lookup needed. See [INSTALL.md — Server admin prerequisites](INSTALL.md#server-admin-prerequisites-debianubuntu).

## Tests

```bash
bash scripts/run_tests.sh
```

1163 tests across 6 suites. No network access or credentials required — in-memory SQLite for every test.

See [docs/HOWTO_tests_and_backtests.md](docs/HOWTO_tests_and_backtests.md) for a practical guide to running tests and backtests.

## Notes

- The `sqlite3` CLI is optional — the bot uses Python's built-in module. Install it (`sudo apt install sqlite3`) only for manual DB queries.
- Do not modify strategy parameters without re-running the corresponding backtest.
- `POLY_PRIVATE_KEY` absent → Polymarket orders are simulated (no on-chain execution).
- WebSocket recv timeouts at ~30s during quiet periods are normal — auto-reconnect handles them.
- mypy `--ignore-missing-imports` reports 0 errors across all subsystems.

## License

See [LICENSE](LICENSE).
