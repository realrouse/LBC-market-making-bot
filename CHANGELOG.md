# Changelog

> 🇫🇷 [Version française](CHANGELOG.fr.md)

All notable changes to this project are documented here.

---

## [0.56] — 2026-06-02

### Changed
- **`live_bot.py` — `▶ TRADE` log line now includes signal context**: added `obi=%.3f` and `ask_vol=%.0f` so the entry log line is self-contained; no DB join needed to retrieve the OBI and ask-volume values that triggered the signal
- **`live_bot.py` — `✓ WIN` / `✗ LOSS` log line now includes hold duration**: added `duration=%ds` computed from `resolution_ts_ms − signal_ts_ms`; lets log-grep workflows flag unusually long holds without querying the database

---

## [0.55] — 2026-06-02

### Fixed
- **`.gitignore` — remove `data/*.db` exceptions**: the four sample databases (`backtest_sample_btc5m_range_2026.db`, `calmsaturday.db`, `basicsunday.db`, `liveweek.db`) were explicitly un-ignored and committed as binary blobs; removed the `!data/*.db` exceptions so all `.db` files are ignored; files removed from the git index with `git rm --cached` (local copies preserved)
- **GitHub Actions — pin all actions to commit SHA**: `actions/checkout`, `actions/setup-python`, and `anthropics/claude-code-action` were referenced by tag (`@v6`, `@v1`, etc.); a compromised tag could silently redirect CI to malicious code; all five workflow files now pin to the exact commit SHA with the tag kept as a comment for readability

---

## [0.54] — 2026-06-02

### Added
- **`tradinebotte-cex/strategies/accumulation/btc_accumulation_deepdip.json` — deep-dip accumulation strategy v1.0**: differentiated strategy for the second accumulation account; backtested 2024-01-01 → 2026-06-02 against live Binance 1h klines; +41% vs the standard v1.5 +38% (+3pp), peak +96% vs +75% (+21pp at bull-run peak); key differences from v1.5: no initial stake (waits for real dips), stricter OBI threshold 0.70 vs 0.50, larger tranches $250 vs $100, higher profit bands (15/30/50/75/100% vs 5/10/20/30/50%), lower sell fraction 8% vs 15% (holds more BTC), Fear&Greed and L/S-ratio gates disabled (avoids blocking on strong OBI dips in greed regime), macro OBI block tightened to -0.50
- **`tradinebotte-cex/scripts/deploy_accumulation_claude4.sh`**: updated `BOT_STRATEGY` to point to `btc_accumulation_deepdip.json`

---

## [0.53] — 2026-06-02

### Fixed
- **`scripts/update_standalone.sh` — eliminate DB lock on deploy**: replaced the nohup + PID file restart mechanism with `systemctl --user restart tradinebotte-live.service` for accounts that have migrated to user services; no sudo required since `systemctl --user` operates entirely within the account's own systemd instance; falls back to the kill-and-let-systemd-restart path for accounts still on system services

### Added
- **`scripts/migrate_to_user_services.sh` — migrate live bot services to user units**: phase 1 (SSH as the bot user, zero sudo) writes `~/.config/systemd/user/tradinebotte-live.service`, runs `systemctl --user enable` + `start`; phase 2 prints the two admin commands needed once per account: `loginctl enable-linger <user>` (makes the user's systemd instance persist across reboots — requires root because it writes to `/var/lib/systemd/linger/`) and `systemctl stop/disable tradinebotte-live-<user>.service` to remove the old system unit
- **`scripts/systemd/tradinebotte-live.service` — user unit template**: identical to the former system unit minus `User=` (implicit for user services) and with `WantedBy=default.target` + `After=network.target` (user-accessible targets)

---

## [0.52] — 2026-06-02

### Added
- **`docs/logging.md` — canonical log-tag vocabulary**: documents every `[TAG]`-prefixed structured log line across all four bot modules (`live_bot.py`, `feed.py`, `account_bot.py`, `indicators.py`); includes the two visual-marker lines (`▶ TRADE`, `✓ WIN`/`✗ LOSS`) with their grep patterns; defines the dynamic `[<stream_id>]` convention used by `indicators.py`; includes guidance for adding new structured log lines

### Changed
- **`live_bot.py` — standardize unbracketed structured log prefixes**: four log lines that were missing `[TAG]` brackets are now consistent with the rest of the codebase: `VOL FILTER` → `[VOL_FILTER]`, `Kelly:` → `[KELLY]`, `CIRCUIT-BREAKER:` → `[CIRCUIT_BREAKER]`, `post_order returned None — aborting entry to prevent ghost trade` → `[GHOST_GUARD] post_order returned None — aborting entry`
- **`feed.py` — remove spurious tags**: `[VERBOSE]` (not a parseable event — renamed to plain prose) and `[WS ERROR]` (redundant — merged into the `[WS]` family as `[WS] traceback:`)

---

## [0.51] — 2026-06-02

### Fixed
- **`tradinebotte-polymarket/live_bot.py` — logging leak to syslog**: `logging.basicConfig()` is a no-op when third-party libraries (`aiohttp`, `websockets`) configure the root logger before `_setup_logging()` runs; without `force=True` the root logger retains a `StreamHandler` pointing to stderr, all `"live"` logger records propagate there, and systemd captures them into the system journal/syslog; added `force=True` to the `basicConfig()` call in `_setup_logging()` so existing root handlers are always replaced with the intended `FileHandler`-only pipeline
- **`tradinebotte-polymarket/feed.py` — logging to file instead of stdout**: replaced `logging.basicConfig(handlers=[StreamHandler(stdout)])` with `tradinetools.setup_logger("feed", feed.log)`; logs now go to a rotating local file; stdout output is preserved only when running in an interactive TTY
- **`tradinebotte-polymarket/account_bot.py` — logging to file instead of stdout**: same migration from `basicConfig(stream=sys.stdout)` to `tradinetools.setup_logger("account", account.log)`
- **`tradinebotte-indicators/indicators.py` — logging to file instead of stdout**: same migration to `tradinetools.setup_logger("indicators", indicators.log)`
- **systemd service files — defense-in-depth**: added `StandardOutput=null` and `StandardError=null` to all six tradinebotte service files so the system journal never captures bot output even if a future logging regression occurs

---

## [0.50] — 2026-05-31

### Added
- **`tradinebotte-indicators/indicators.py` — full-depth futures stream (`btc_full_depth_perp`)**: new `_BINANCE_FUTURES_DEPTH_URL` constant (`https://fapi.binance.com/fapi/v1/depth`); `_fetch_depth_snapshot()` now accepts `url` and `limit` parameters (spot uses limit=5000, futures limit=1000); `_binance_full_depth_task` gains two new parameters — `market` (`"spot"` or `"perp"`) selects the correct WebSocket and REST endpoints, and `bid_depth_pct` / `ask_depth_pct` trim the book to a dynamic price window around mid-price (0=disabled), applied at snapshot load and on every publish cycle to keep memory bounded; futures sync validation uses `pu`-chaining (`ev["pu"] == last_update_id`) instead of `U == lastId+1` (the Binance futures diff protocol differs from spot); new stream `btc_full_depth_perp` deployed alongside the existing `btc_full_depth` spot stream
- **`tradinebotte-indicators/indicators.py` — shared SQLite orderbook database**: new helper functions `_init_depth_db()` and `_write_depth_to_db()`; SQLite journal mode = DELETE (not WAL) so cross-user readers need only file read permissions — no directory write access required for `-shm`/`-wal` sidecars; two tables: `orderbook_current` (latest bucketed book — 1 row per stream/side/price bucket, replaced on each write) and `orderbook_snapshots` (ring-buffer of timestamped JSON snapshots with configurable retention); new stream parameters: `db_path` (default `""` = disabled), `bucket_size_usd` (default 50), `db_write_every_n` (default 60, approximately once per minute), `history_retention_h` (default 24); DB file created with `0o644` permissions — readable by all users; `run_in_executor` used so SQLite writes never stall the async event loop; the shared orderbook database path and bucket size are configurable per deployment

---

## [0.49] — 2026-05-31

### Added
- **`tradinebotte-cex/accumulation_bot.py` v1.4**: adaptive cooldown that shortens the scale-in wait when OBI pressure is strong; rebuy trailing stop with expiry (removes stale rebuy levels automatically); configurable Earn buffer (`earn_buffer_usd`) to keep a minimum liquid USDT in spot; VWAP gate applied to the initial buy only (`vwap_gate_initial`), leaving scale-in entries unfiltered; stronger OBI threshold required for cooldown reduction
- **`tradinebotte-cex/accumulation_bot.py` v1.5 — four new signal gates**:
  - Fear & Greed gate (`fear_greed_gate`): blocks buys when index > 80 (extreme greed), boosts stake when index < 25 (extreme fear)
  - Liquidations gate (`liq_gate`): blocks entry on a large short-liquidation spike (crowded long signal), boosts stake on a long-liquidation spike (forced selling)
  - Long/Short Ratio gate (`ls_ratio_gate`): blocks new buys when the ratio exceeds 3.0 (over-leveraged longs)
  - RSI 4h gate (`rsi4h_gate`): blocks when RSI > 70 (overbought), relaxes the VWAP requirement when price is below VWAP and RSI < 35
- **`tradinebotte-cex/orderbook_bot.py` v2.12 — liquidations gate**: `liq_gate` parameter (disabled by default, `"liq_gate": false`) and `liq_long_block_usd` threshold; mirrors the accumulation bot gate logic
- **`tradinebotte-cex/strategies/scalping/orderbook_btc.json` v2.12** — updated defaults
- **`tradinebotte-cex/strategies/longterm/btc_accumulation.json` v1.5** — updated defaults with all new gate parameters

### Changed
- **All hardcoded strategy parameters now overridable via JSON** (`accumulation_bot.py`, `orderbook_bot.py`, and Polymarket bots): every Python constant that previously required a code edit can now be set in the strategy JSON file; constants remain as defaults when the key is absent from JSON

### Fixed
- **`tradinebotte-indicators/indicators.py` — WebSocket timeout watchdog**: all three Binance WebSocket loops (`_binance_kline_task`, `_binance_scalping_task`, `_binance_full_depth_task`) now wrap `ws.recv()` in `asyncio.wait_for(..., timeout=120)` — prevents indefinite hang when Binance maintains TCP keepalive but stops sending data (observed: 38-hour stale-price incident); new constant `_WS_RECV_TIMEOUT_S = 120`
- **`tradinebotte-indicators/indicators.py` — liquidations stream**: `_binance_liquidations_task` rewritten from a signed REST polling endpoint (required API credentials, was always effectively disabled) to the public WebSocket `wss://fstream.binance.com/ws/{symbol}@forceOrder`; no credentials required; liquidations data is now live and available to all gate consumers
- **`tradinebotte-indicators/indicators.py` — cleanup**: removed unused imports (`hashlib`, `hmac`, `urllib.parse`) and the unused constant `_BINANCE_FORCE_ORDERS_URL`

---

## [0.48] — 2026-05-30

### Added
- **Monorepo Phase 1 — `tradinetools` v0.1 shared library**: `tradinetools/` Python package with `zmq.py` (socket factories `make_pub/sub/rep/req`, `warn_if_external_bind`, port constants), `schemas.py` (versioned ZMQ message dataclasses `BookMessage`, `IndicatorsMessage`, `RegisterRequest`, `RegisterReply`, etc. with `to_dict()`/`from_dict()` round-trip), `math.py` (scalar indicator helpers `sma_last`, `ema_last`, `atr_last`, `bollinger_last`, `vwap_last`, `vol_zscore_last`, `rolling_max_last`), `logging.py` (shared `setup_logger`); installed as editable package via `pyproject.toml`
- **Monorepo Phase 2 — `tradinebotte-indicators/`**: indicators service fully isolated into its own sub-service directory with dedicated `scripts/`, `strategies/`, and `tests/`; service adopts tradinetools ZMQ factories and schema v1 for all published messages
- **Monorepo Phase 3 — `tradinebotte-polymarket/`**: Polymarket service isolated with `feed.py`, `live_bot.py`, `account_bot.py`, `api_polymarket.py`, `bot_utils.py` and all related scripts/strategies/tests
- **Monorepo Phase 4 — `tradinebotte-cex/`**: CEX service isolated with all strategy engines, bots, deploy scripts, JSON configs, and tests
- **`tradinebotte-cex/strategy_engines/dca.py` — `DCAStrategy` plugin**: timed DCA buys at configurable intervals; limit orders with TP and optional SL; SQLite persistence; integrates with the shared connector injection pattern
- **`tradinebotte-cex/strategy_engines/swinghold.py` — `SwingHoldStrategy` plugin**: swing strategy variant that partially exits at each resistance level (`sell_fraction` per level) instead of a single TP; `hold_fraction = 1 − sell_fraction` kept for long-term accumulation; full SL on remaining position
- **`tradinebotte-cex/strategy_engines/swing.py` — market SELL for stop-loss exits**: SL now executes via REST market order (`post_market_order`) instead of a cancelled limit, ensuring fills in fast-moving markets
- **`tradinebotte-cex/accumulation_bot.py` — BTC accumulation bot v1.2**: OBI dip-buy with profit-ladder ratchet; adaptive scale-in with rebuy logic; state persistence across restarts; v1.1 fixed bugs + added adaptive rebuy; v1.2 promoted wide profit-band defaults from calibration
- **`tradinebotte-cex/orderbook_bot.py` — OBI scalping v2.3–v2.10**: v2.3 adds TFI (Trade Flow Imbalance) filter; v2.4 long-only direction; v2.5 wider TP/SL calibrated 2026-05-26; v2.7 TFI flat gate + removes `obi_exit_thresh`; v2.8 VWAP context gate; v2.9 volume profile gate; v2.10 macro OBI gate (multi-timeframe filter)
- **`tradinebotte-indicators/indicators.py` — 4 new data sources**: Bitcoin liquidations (Coinalyze), open interest (OI), long/short ratio, funding rate; all published on the unified indicators ZMQ stream; HMAC auth added for liquidations endpoint to silence spam
- **`analysis/backtest_swing_dca.py` — CEX strategy backtester**: simulates DCA, Swing, and SwingHold against 1-minute OHLCV SQLite databases; fill model: BUY fills when `candle_low ≤ limit_price`, SELL when `candle_high ≥ limit_price`, SL at `candle_low ≤ sl_price`; recovery lock prevents cascade re-entries during sharp moves through the support cluster; realized-PnL-only capital model; `--compare`, `--all-dbs`, `--sweep`, `--config` modes
- **`analysis/backtest_orderbook.py`** — OBI scalping backtester against live order book snapshots
- **`analysis/calibrate_obi_proxy.py`** — calibration script for OBI proxy parameters
- **`scripts/backtest_accumulation.py`** — accumulation strategy backtester
- **`tradinebotte-cex/scripts/deploy_accumulation_claude4.sh`** — accumulation bot deploy script
- **`tradinebotte-polymarket/scripts/update_claude3.sh`** — deploy wrapper targeting the third test account
- **Test suite expansion**: `tradinebotte-cex/tests/test_strategy_engines.py` (64 tests covering `SwingStrategy`, `DCAStrategy`, `SwingHoldStrategy` — init validation, pure calculation methods, async simulation fills via `IsolatedAsyncioTestCase`); `tradinetools/tests/test_zmq.py`, `test_schemas.py`, `test_math.py` (87 tests total); all 5 sub-service test directories now discovered by CI

### Changed
- **`.github/workflows/tests.yml`** — CI now runs `unittest discover` on all five test directories (`tests/`, `tradinetools/tests/`, `tradinebotte-cex/tests/`, `tradinebotte-polymarket/tests/`, `tradinebotte-indicators/tests/`) and installs `tradinetools` as editable package before the test run
- **`tradinebotte-indicators/indicators.py`** — ZMQ publish layer replaced with tradinetools `make_pub`/`make_sub`/`make_rep`/`make_req` factories; all messages serialized as schema v1 dicts; `warn_if_external_bind` called on every bind address
- **All deploy scripts** (`update_claude1.sh`, `deploy_scalping_claude4.sh`, `deploy_accumulation_claude4.sh`) updated to rsync `tradinetools/` to the remote install directory and install it in the remote `.venv`

### Fixed
- **`tradinebotte-polymarket/scripts/update_claude1.sh` — `--restart-feed`**: syncs `feed.py` and `tradinetools/` to the remote; installs tradinetools in `.venv`; patches the systemd unit file if `ExecStart` still points to the legacy `bot/feed.py` path
- **`tradinebotte-polymarket/scripts/install_feed_service.sh`** — `BOT_DIR` corrected to point to the project root instead of the `bot/` subdirectory
- **`tradinebotte-indicators/indicators.py`** — perpetual futures TFI switched from aggTrade WebSocket (unavailable) to REST polling; Binance futures depth keys fixed (`b`/`a` vs `bids`/`asks`)
- **Pylint 10.00/10** across all new modules and scripts

### Tests
- Total: **1,148 passing tests** across five sub-service suites (340 + 87 + 170 + 415 + 136)

---

## [0.47] — 2026-05-24

### Added
- **`bot/strategy_engines/swing.py` — `SwingStrategy` swing trading engine**: places limit BUY orders at configurable support levels and SELL orders at resistance levels; EMA(200) 4h directional filter skips BUY entries when price is below the 200-period EMA; ATR(14) dynamic stop-loss with a configurable multiplier; RSI(14, 4h) overbought filter suppresses buys in overextended conditions; subscribes to the shared indicators service via ZMQ SUB; SQLite persistence with `restore_from_db()` so open positions survive restarts
- **`strategies/swing/swing_BTCUSDT.json` — swing strategy config for BTC/USDT**: supports `[70000, 72500, 75000, 76000]`, resistances `[78000, 80000, 82500, 85000]`, $200/position, max 3 simultaneous positions, ATR SL multiplier 1.5
- **`bot/connectors/__init__.py`** — swing strategy connector requirements registered
- **`bot/strategy_engines/__init__.py`** — `SwingStrategy` registered under the `"swing"` strategy type
- **`bot/live_bot.py` — `strategy_cfg` dict in `BotConfig`**: strategy engines can now read arbitrary JSON keys from the strategy file via `config.strategy_cfg`, removing the need to hard-code per-strategy config fields in `BotConfig`
- **`strategies/indicators/indicators_all.json` — unified 9-stream indicator process**: PUB on port 5559, REP on port 5561; `btc_4h` stream extended with EMA(50), EMA(200), and ATR(14); `seed_periods` increased to 250 for reliable warm-up of long-window indicators
- **`scripts/update_swing.sh` — swing account deploy script**: rsync + `config.json` write + restart + verify in a single SSH session, mirroring the pattern of `update_standalone.sh`

### Changed
- **`bot/indicators.py` — `binance_scalping` source**: combined Binance WebSocket stream (depth20 + aggTrade) that computes OBI, EMA, deceleration, `spread_bps`, `realized_vol_bps`, and TFI in real time; consumed by `orderbook_bot.py` v2.1 and by the swing strategy via the shared indicators service
- **`scripts/test_multibot.conf.example`** — updated to cover an additional test account dedicated to swing strategy validation

### Fixed
- **`bot/orderbook_bot.py` v2.1 — OBI signal direction inverted**: the strategy is now SHORT-only; a bid-heavy order book is treated as spoofing pressure indicating a price fall; the LONG branch has been removed entirely
- **`bot/orderbook_bot.py` v2.1 — `obi_exit` mechanism disabled**: premature exits at the worst price point have been eliminated; TP widened to 15 bps, SL to 8 bps, `max_hold` extended to 3 minutes
- **`bot/orderbook_bot.py` v2.1 — limit-order simulation mode added**: simulated orders are identified by `sim_`-prefixed order IDs, enabling paper-trade validation without modifying the live order flow
- **`bot/indicators.py` — Deribit DVOL endpoint corrected**: `get_volatility_index_data` was calling the wrong endpoint; fixed to use the correct Deribit API method

---

## [0.46] — 2026-05-23

### Fixed
- **`bot/orderbook_bot.py` — Binance futures depth WebSocket key names**: Binance spot sends `"bids"`/`"asks"` but perpetual futures sends `"b"`/`"a"`; perp OBI was always 0.000 (silent bug); fixed with `msg.get("bids") or msg.get("b")` / `msg.get("asks") or msg.get("a")`
- **`bot/live_bot.py` `make_config()` — stale default strategy path**: fallback pointed to `strategies/polymarket_BTC5M_v2.json` (deleted file); updated to `strategies/polymarket/polymarket_BTC5M_piste3.json` (current active strategy); a fresh install with no `"strategy"` key in `config.json` would silently use module-level defaults instead of calibrated piste3 parameters
- **All stale file paths purged** after project tree reorganization: 42 files updated (bot docstrings, argparse help, JSON `_run` metadata fields, test docstrings, docs/)

### Changed
- **Project tree reorganization — 5 steps**:
  1. `notes/` — root-level `.txt` planning files moved to `notes/`
  2. `bot/strategy_engines/` — `bot/strategies/` renamed to `bot/strategy_engines/` to eliminate Python module naming collision with the `strategies/` JSON config directory
  3. `scripts/systemd/` — systemd service templates (`tradinebotte*.service`) moved from `scripts/` root to `scripts/systemd/`
  4. `analysis/` — 16 Python analysis scripts (`backtest*.py`, `analyze_*.py`, `calibrate_obi.py`, `benchmark_api.py`, `download_*.py`, `latency.py`, `profile_*.py`) moved from `scripts/` to `analysis/`
  5. `strategies/` subdirectories — 24 JSON strategy configs organized into typed subdirectories: `strategies/polymarket/`, `strategies/grid/`, `strategies/scalping/`, `strategies/longterm/`, `strategies/indicators/`
- **rsync strategy filter** in all three deploy scripts (`deploy_scalping_claude4.sh`, `update_standalone.sh`, `test_multibot_deploy.sh`): replaced `--include='*.json' --exclude='*'` (did not recurse into subdirectories) with `--filter='+ **/' --filter='+ *.json' --filter='- *'`
- **Shell script headers** corrected: stale product names fixed, French text removed from code comments (English-only policy), outdated claims updated
- **Code comments** audited across all bot modules: WHAT comments removed; WHY comments added where a hidden constraint, subtle invariant, or non-obvious workaround was present

---

## [0.45] — 2026-05-23

### Fixed
- **`scripts/deploy_scalping_claude4.sh`** — added `--exclude='live_ob.db'` to rsync to explicitly protect the OBI data collection database; replaced undefined `${STRATEGIES[*]}` variable (crashed pre-flight under `set -u`) with `$BOT_STRATEGY`

---

## [0.44] — 2026-05-23

### Added
- **`bot/orderbook_bot.py`** — new Binance OBI scalping bot: connects to Binance spot + perpetual depth20 WebSocket streams (100 ms), computes OBI from top-N bid/ask levels with EMA smoothing, enters paper trades (long on spot, long or short on perp) when OBI exceeds a configurable threshold for N consecutive snapshots; exits on OBI reversal, TP/SL, or max-hold timeout; records snapshots and trades to `live_ob.db`
- **`strategies/orderbook_btc.json`** — initial config for the OBI scalping bot: `entry_thresh=0.30`, `confirm_n=3`, `tp=0.5%`, `sl=0.3%`, 10 OBI levels, spot + perp mode
- **`bot/scalping_math.py`** — extracted math helpers (ATR, Bollinger Bands, VWAP, volume z-score, rolling max) shared between `bot/scalping_bot.py` and `bot/indicators.py`
- **`bot/scalping_bot.py`** — live Binance scalping bot with three strategies (`candle_momentum`, `meanrev`, `breakout`); full parameter docstring covering all 27 `DEFAULTS` keys
- **`scripts/backtest_scalping.py`** — backtest engine for the three scalping strategies (`candle_momentum`, `meanrev`, `breakout`)
- **`bot/bot_utils.py`** — added `setup_bot_logger()` and `warn_if_external_bind()`
- **`bot/indicators.py`** — added `OHLCVSeries` ring-buffer (ATR, Bollinger Bands, VWAP, volume z-score, rolling max); imports shared helpers from `scalping_math`
- **Per-account update wrappers** — two thin scripts targeting the first two test deployment accounts; each accepts the same flags as `update_standalone.sh` and delegates to it
- **`.pylintrc`** — `zmq` and `py_clob_client` added to `ignored-modules`

### Fixed
- **`scripts/test_multibot_deploy.sh`** — replaced `rsync --delete` on the full repo with a flat `bot/` rsync + separate `strategies/` rsync + `requirements.txt`; the old `--delete` flag wiped `live_bot.py` on standalone accounts that use the flat install layout
- **`scripts/install.sh`** — `run.sh` now delegates to `start_bot.sh` via `exec` instead of launching `live_bot.py` directly; direct launch bypassed the PID file, allowing silent duplicate instances that corrupted `live.db`
- **`scripts/update_standalone.sh`** — added `requirements.txt` rsync and `pip install -r requirements.txt` before restart; dependencies were never updated on code-only pushes
- **`scripts/start_bot.sh`** — prefers `.venv` over `venv`; prefers `bot/live_bot.py` over a flat `live_bot.py`
- **OBI scalping deploy** — replaced three failing OHLCV-based scalping bots (`candle_momentum`, `meanrev`, `breakout`, all <20% WR across all market regimes) with a single `orderbook_bot.py` instance; the old deploy script is superseded

### Tests
- **`tests/test_indicators.py`** — z-score spike threshold corrected from 5.0 to 4.3 (mathematical limit √(n−1) = √19 ≈ 4.36 with n=20)
- **`tests/test_scalping_bot.py`** — patch target changed from `logging.getLogger` to `setup_bot_logger`

---

## [Unreleased] - 2026-05-22

### Added
- **`bot/earn_manager.py` — Binance Simple Earn Flexible manager**: `EarnManager` class parks idle USDT after sell trades (`park_idle()`) and redeems before buys (`ensure_liquid()`); sim mode when `BINANCE_API_KEY`/`BINANCE_API_SECRET` are absent; MEXC Earn not supported (API too unstable)
- **`bot/api_bitstamp.py` — Bitstamp spot exchange adapter**: same interface as `api_binance.py` (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`); FEE_RATE = 0.1% taker; WebSocket `wss://ws.bitstamp.net`; credentials via `BITSTAMP_API_KEY`, `BITSTAMP_API_SECRET`, `BITSTAMP_CUSTOMER_ID`; sim mode when credentials absent
- **`strategies/longtermcyclestrategygridV1.json` / `V2.json` / `V3.json` — long-term BTC cycle strategy configs**: V1 (5%/25% rebound/tranche, ×24.0, CAGR 43.8%, MaxDD 81.4%, Calmar 0.54); V2 (4%/20%, ×24.2, CAGR 43.9%, MaxDD 81.4%, Calmar 0.54); V3 adds halving-relative prudence tiers (T1 at 400d post-halving, T2 at 480d), Calmar 0.75
- **`scripts/analyze_btc_cycles.py` — BTC halving cycle analysis**: per-cycle returns, durations, and Mayer Multiple statistics
- **`scripts/analyze_cycle_volatility.py` — extended cycle volatility analysis**: rolling 730d gain, 200DMA slope, C3 frontrunning analysis, indicator reliability table
- **`scripts/backtest_cycle_strategy.py` — long-term cycle strategy backtest**: flags `--top-mm`, `--rebound`, `--drawback`, `--tranche`, `--prudence`, `--compare`
- **`scripts/download_btc_daily_extended.py` — extended BTC daily OHLCV downloader**: fetches multi-year history for cycle analysis
- **`scripts/backtest.py` — fractional Kelly sizing, Sharpe/Sortino, walk-forward optimization, weekday volatility filter, step-function stake sizing**

### Changed
- **`strategies/grid_BTCUSDT_bear_trailing.json`** — updated grid bounds to $60K–$100K with ±30%/40 levels

### Fixed
- **`bot/api_binance.py`** — public API fallback active when no credentials are set
- **`scripts/download_btc_history.py`** — data gaps are skipped instead of aborting the download

## [0.5.0] - 2026-05-18

### Added
- **`bot/live_bot.py` + `strategies/polymarket_BTC5M_piste3.json` — dynamic stake scaling + OBI calibration (Piste 3 strategy)**: stake scales with bid price using `stake = base × (1 + bid_α × (bid − 0.96))`, capped at `stake_max` and `cap × capital`; OBI rejection filter skips trades where OBI < `obi_reject_thresh` (removes low-win-rate buckets); weekly stop-loss halts the bot when `weekly_pnl < −weekly_stop_loss`; new `BotConfig` fields: `bid_alpha`, `secs_alpha`, `stake_max`, `capital_cap`, `obi_reject_thresh`, `weekly_stop_loss`; `strategies/polymarket_BTC5M_piste3.json` ships with `bid_alpha=2.0`, `stake_max=$15`, `cap=12%`, `weekly_stop=$60`, `obi_reject_thresh=−0.65`; backtest vs original: PnL +85%, MaxDD −28%, Sharpe 3.28 vs 1.97; new analysis scripts: `scripts/analyze_stake_secs.py`, `scripts/backtest_stake_secs.py`, `scripts/calibrate_obi.py`
- **`bot/live_bot.py` + `strategies/polymarket_BTC15M_piste3.json` — configurable market timeframe (15M BTC support)**: `BotConfig` gains `market_tag_id` (default 102892 for 5M) and `market_window_mins` (default 6); strategy JSON controls the tag and window — `"market_tag_id": 102467, "market_window_mins": 16` activates 15M markets; startup log reports the active configuration (`Markets: BTC Up/Down 15M (tag=102467, window=±16min)`); `GAMMA_TAG_5M` and `GAMMA_TAG_15M` constants exposed; legacy `GAMMA_TAG` alias kept for compatibility
- **`bot/strategies/grid.py` + strategy JSON files — grid trail mode**: `trail_mode` parameter accepts `"bull"` (re-centers grid upward when price exceeds `grid_upper`), `"bear"` (re-centers downward when price falls below `grid_lower`), or `"static"` (default, halts in both directions); `_recenter_grid()` cancels all open orders, shifts bounds keeping the same range centered on the current price, and re-initialises; stop-loss check branches per mode; `restore_from_db` restores saved bounds so trail mode survives restarts; new config files: `strategies/grid_BTCUSDT_bull_trailing.json`, `strategies/grid_BTCUSDT_bear_trailing.json`
- **`bot/connectors/__init__.py` + `bot/live_bot.py` — connector/strategy compatibility check**: `validate(connector_module, strategy_type)` raises `RuntimeError` at startup if the connector lacks methods required by the chosen strategy; lists all missing methods in the error message; called in `main()` for non-threshold strategies before the strategy object is created
- **`scripts/update_standalone.sh` — lightweight deploy script**: rsync `bot/` contents flat to `$INSTALL_DIR/` (matching install.sh structure) + rsync `strategies/*.json`; single SSH session for stop + start using PID file approach; single SSH session for verify; options: `--skip-restart`, `--verify-only`

### Fixed
- **`scripts/profile_compare.py` — f-string PRAGMA SQL eliminated**: `c.execute(f"PRAGMA mmap_size = {MMAP_MB * 1024 * 1024};")` replaced with `c.execute("PRAGMA mmap_size = ?", (MMAP_MB * 1024 * 1024,))`; no real injection risk (constant value), but f-string SQL is a pattern to eliminate from the codebase
- **`bot/live_bot.py`, `bot/feed.py`, `bot/account_bot.py` — session-level `ClientTimeout` added**: all three `aiohttp.ClientSession()` instantiations now pass `timeout=aiohttp.ClientTimeout(total=30)`; per-request timeouts in `api_*` modules already cover current calls, but the session-level default prevents any future omission from hanging the event loop indefinitely
- **`bot/api_binance.py`, `bot/api_mexc.py` — error response bodies truncated in logs**: 8 logger calls that passed the full HTTP response body (`data`) now use `%.300s` instead of `%s`, capping the logged representation at 300 characters; affected calls: `order error`, `get_order_status error`, `cancel_order error`, `get_open_orders error` in both modules
- **`bot/indicators.py` — RSI docstring corrected to Cutler**: `compute_rsi` docstring changed from `"Wilder RSI(n)"` to `"Cutler RSI(n): simple-mean gains/losses over the last n bars"`; the implementation uses `sum(...) / n` over a fixed window (Cutler's method), not Wilder's EMA-smoothed average
- **`CLAUDE.md` — `live_bot.py` line count corrected**: `~617 lines` updated to `~1530 lines` (actual: 1531); the stale figure dated from before the major codebase expansion
- **`scripts/install.sh` — grid/connector packages added to install**: "Copying bot files" now includes `api_binance.py`, `api_mexc.py` (flat copies) + `bot/connectors/__init__.py` → `connectors/` + `bot/strategies/__init__.py` and `bot/strategies/grid.py` → `strategies/` (alongside JSON files); a fresh install followed by `connector=binance strategy_type=grid` no longer raises `ModuleNotFoundError`; syntax check loop extended to cover all 8 Python files
- **`bot/live_bot.py` — snapshot commits batched every 30 s**: `save_snapshot()` now only calls `conn.execute()` (the fast path); `handle_book_update()` flushes with `conn.commit()` at most once every `SNAPSHOT_COMMIT_SECS = 30` seconds, reducing blocking commit calls from up to 50× per 5-second window to one per 30 seconds; all SQLite operations stay on the event loop thread — no executor or thread-safety concerns; 3 tests added
- **`bot/strategies/grid.py` — sticky `_no_credentials` flag prevents user-stream task re-spawn**: `_user_stream_loop` now sets `self._no_credentials = True` after `MAX_KEY_FAILURES` (3) consecutive `get_listen_key` failures; `on_book_update()` spawn guard checks `not self._no_credentials` first, so the task is never recreated once credentials are confirmed absent — eliminating an infinite task-creation loop; 3 tests added in `TestNoCredentialsFlag`
- **`bot/account_bot.py` — symlink TOCTOU fix on feed log in `/tmp`**: feed log file now opened with `os.open(O_CREAT|O_WRONLY|O_APPEND|O_NOFOLLOW)` instead of plain `open()`; prevents a malicious symlink placed in the world-writable `/tmp` directory from redirecting log writes to an arbitrary file (e.g. `authorized_keys`); parent closes its fd after `Popen`, child retains it; consistent with the lock file which already used `O_NOFOLLOW`
- **`scripts/test_multibot.conf.example` + `.git-hooks/pre-commit` — generic OS usernames**: real server account names replaced with generic placeholders (`user1 user2 user3`) in the example config and its role comment; pre-commit hook extended so each username from the test config generates two grep patterns — `$u@` (with `@`, existing) and `\b$u\b` (bare word-boundary, new) — blocking both forms in staged diffs
- **`bot/live_bot.py` — ghost trade guard in `enter_live_trade()`**: in live mode (`session + private_key`), if `post_order` returns `None` (CLOB API failure), the function now returns early after incrementing `api_fail_streak`, preventing a ghost DB row with `order_id=NULL` that would permanently lock capital; simulation mode (no `private_key`) is unaffected — the insert still runs with `oid=None`; three regression tests added in `TestEnterLiveTrade`
- **`requirements.txt` — `bcrypt` added**: `bcrypt` was missing from `requirements.txt`; a fresh install left `bot/bot_utils.py` to fall back to unsalted SHA-1 for the web status page password; `bcrypt` is now a listed dependency so `pip install -r requirements.txt` installs it automatically
- **`scripts/start_bot.sh`, `scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh`, `scripts/update_standalone.sh`, `scripts/collect_db.sh` — PID file approach for all stop/start/restart**: root cause: `pkill -f 'pattern'` embedded in an SSH command string kills the remote shell itself — the shell's `/proc/PID/cmdline` contains the full script text passed via `ssh "..."`, which includes the literal process name from the `nohup` line; new pattern: start writes `_pid=$!` → `disown "$_pid"` → `echo "$_pid" > live.pid`; stop uses `kill $(cat live.pid)` — no regex, no pattern match, no self-match risk; liveness check uses `kill -0 $(cat live.pid)`; stale PID files (dead process) cleaned automatically on next start; PID files: `live.pid`, `feed.pid`, `account.pid`, `indicators.pid`
- **`scripts/test_standalone_deploy.sh`, `scripts/test_multibot_deploy.sh` — test scripts upgraded to PID-file stop**: launch commands now write `indicators.pid` and `account.pid` immediately after nohup; cleanup and teardown use PID files for graceful stop; `pkill -9` kept as orphan fallback in kill-only sessions only
- **`scripts/test_multibot_deploy.sh`, `scripts/test_all_accounts.sh` — `pkill` user scope hardening**: added `-u $(id -u)` to all remaining `pkill` calls to scope kills to the current user and prevent accidentally stopping other users' processes
- **`scripts/start_bot.sh`, `scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh` — `nohup` daemonization hardening**: added `</dev/null` to all `nohup` lines so the SSH session can exit cleanly; `disown` added where missing
- **`bot/api_polymarket.py`, `bot/feed.py`, `bot/bot_utils.py`, `bot/api_mexc.py` — English-only code**: remaining French log messages translated to English across 4 files (19 strings total); `api_mexc.py` — 13 occurrences of `erreur` → `error`; `api_polymarket.py:271` CLOB warning now logs `keys=%s` instead of the full response body
- **`bot/api_binance.py`, `bot/strategies/grid.py` — English-only code**: remaining French log messages translated to English
- **`bot/api_binance.py` — `get_markets(**_)` compatibility**: added `**kwargs` to accept Polymarket-specific keyword arguments (`tag_id`, `window_minutes`) passed by `_run_ws` regardless of connector type
- **`scripts/collect_db.sh` — `--rotate` syntax fix**: pre-existing syntax error where `"\$(id -u)"` inside a double-quoted SSH string closed the outer string prematurely; fixed to `\$(id -u)`

### Tests
- **`tests/test_bot.py` — 30 new tests**: `TestComputeStake` (11 cases covering bid scaling, capital cap, seconds penalty, floor/ceiling), `TestWeeklyStopLoss` (3 cases), `TestMarketDiscoveryConfig` (5 cases); `TestPurgeExpiredMarkets` (5 cases — expired token removed, active kept, open-trade guard, signalled cleared on purge, mixed tokens); `TestWsLoopBackoff` (3 cases — doubling, cap at 60 s, reset after success); `TestMarketRefreshLoop` (3 cases — new market registration + subscription, expired purge, API error resilience)
- **`tests/test_grid_trail.py` — 12 new tests**: `TestConnectorValidate` (4 cases), `TestCheckStopLoss` (3 cases), `TestRecenterGrid` (2 cases), `TestRestoreFromDb` (3 cases)
- **Full suite: 659 tests pass**

---

## [0.4.5] - 2026-05-16

### Fixed
- **`scripts/backtest.py` — graceful skip of klines DBs** (`--all`, `--sweep-all`): when the data directory contains BTCUSDT klines files (no `snapshots` table), the script previously crashed with `OperationalError: no such table: snapshots`; now prints a skip notice and continues to the next DB
- **`scripts/profile_compare.py` — `bot.DB_PATH` removed** (E1101): `DB_PATH` no longer exists as a module-level constant in `live_bot.py`; replaced with `os.path.join(PROFILE_DIR, "profile.db")`
- **`scripts/profile_hotpath.py` — missing `config` argument** (E1120 ×2): `bot.init_db()` and `bot.is_trading_hour()` both require a `BotConfig` argument since the config-driven refactor; calls updated to `init_db(BotConfig())` and `is_trading_hour(BotConfig())`; redundant lambda on `is_trading_hour` resolved as a side effect
- **`tests/test_api_cex.py` — unused imports** (W0611): `importlib` and `inspect` removed
- **`tests/test_indicators.py` — unused import `math`** (W0611); reimport of `_shift_addr` inside test method replaced with module-level name (W0404); unused `spec` variable → `_` (W0612); two `NamedTemporaryFile` calls suppressed for R1732
- **`tests/test_multibot.py` — reimport `os as _os`** (W0404/C0411/C0412): replaced with module-level `os`
- **`tests/test_bot.py` — multiple pylint warnings**: `too-many-lines` suppressed at module top (C0302); three local `import time as _time` replaced with module-level `time` (W0404); local `import asyncio` inside method removed (W0404/W0621); three unused `ts` variables replaced with `_` (W0612); four local `from datetime import datetime, timezone` removed (W0404/W0621 ×4); unused `GridLevel`, `GridState` imports removed (W0611); late `import asyncio, unittest.mock` suppressed (C0411/C0412); `TestStrategyLoading` updated — v2 tests replaced with v1 parameter assertions after `polymarket_BTC5M_v2.json` was removed

### Changed
- **`strategies/polymarket_BTC5M.json` — `signal_threshold` 0.96 → 0.95**: sweep-all optimisation across 6 DBs (1,016,186 snapshots, 405 combos); best PnL/MaxDD ratio 4.21 at thr=0.95/secs=45/obi=−0.75/dsl=30
- **`strategies/grid_BTCUSDT.json` — grid bounds ±10% → ±30%**: `grid_lower`/`grid_upper` updated to 70k–130k around 100k midpoint; best avg Calmar across 3 klines DBs (2026 range, 2022 bear, 2024 bull)

### Removed
- **`strategies/polymarket_BTC5M_v2.json`**: deleted — after the threshold update, v1 and v2 were functionally identical; `polymarket_BTC5M.json` is the sole active strategy file

### Documentation
- **`QUICKSTART.md` + `QUICKSTART.fr.md`**: version reference updated `v0.40` → `v0.4.4`; defunct `scripts/install_service.sh` reference replaced with link to INSTALL.md systemd section; `pkill -f live_bot.py` → `pkill -f '[l]ive_bot.py'` (bracket trick)
- **`INSTALL.md` + `INSTALL.fr.md`**: `--detail` flag added to `backtest.py` parameter table; "Feed flags" subsection added with `--verbose` for `feed.py` (distinct from the existing `account_bot.py` entry)

### Chore
- **`.gitignore`**: `.coverage` added

---

## [0.4.4] - 2026-05-16

### Added
- **Shared indicators architecture** (`bot/indicators.py`, `bot/account_bot.py`): `indicators.py` is now a single process per machine, started once under the feed owner user; each `account_bot` registers its desired streams at startup via a ZMQ REP socket (`tcp://127.0.0.1:5561`) and receives indicator messages on the shared PUB socket (`:5559`); replaces the previous per-account process model that caused port conflicts
- **Dynamic stream registration** (`bot/indicators.py` `--reg-addr`): new ZMQ REP socket binds `:5561` and accepts JSON registration requests `{"streams": [...]}` from account bots at startup; `indicators.py` only activates the union of registered streams, eliminating idle Binance WebSocket connections
- **`feed_auto_start=false` support** (`bot/account_bot.py`, `bot/bot_utils.py`): when `config.json` sets `"feed_auto_start": false`, `account_bot` probes the feed with a retry loop (6 attempts × 5 s = 30 s max) instead of forking `feed.py`; required for systemd-managed deployments where `tradinebotte-feed.service` owns the feed process
- **systemd service templates** — three new installer scripts and unit templates:
  - `scripts/install_feed_service.sh` + `scripts/tradinebotte-feed.service`: installs the shared feed as a system service (`After=network-online.target`); detects already-active/enabled unit before overwriting
  - `scripts/install_indicators_service.sh` + `scripts/tradinebotte-indicators.service`: installs the shared indicators process as a user-level service (`Wants=tradinebotte-feed.service`)
  - `scripts/install_account_service.sh` + `scripts/tradinebotte-account.service`: account bot unit with `Requires=tradinebotte-feed.service`, `Wants=tradinebotte-indicators.service`; validates `feed_auto_start=false` in `config.json` before installation
- **`EnvironmentFile=-<credentials>`** in `tradinebotte-feed.service` and `tradinebotte.service`: systemd units now load an optional `credentials` file from the install directory for API key injection, keeping secrets out of the unit file itself
- **`scripts/test_multibot_deploy.sh` — shared indicators phase**: Phase 7 restructured to start one `indicators.py` process under the feed user (not one per account); `TEST_INDICATORS_CONFIG` scalar replaces the old per-account `TEST_INDICATORS_CONFIGS` array; Phase 9 verifies the single process; Phase 12 tears it down cleanly without touching feed ports

### Changed
- **`scripts/start_collector.sh` — `systemd-run --user` transient unit**: replaced `nohup ... &` with `systemd-run --user --description=... --setenv=...` to survive SSH session logout on hosts with `KillUserProcesses=yes` (`loginctl enable-linger` required once per user); liveness check moved to a separate direct SSH call after 15 s
- **`scripts/tradinebotte-feed.service` + `scripts/tradinebotte.service`**: `StartLimitIntervalSec` and `StartLimitBurst` moved from `[Service]` to `[Unit]` (correct systemd section); `EnvironmentFile=-__ENV_FILE__` added

### Fixed
- **Remaining French strings in shell scripts** (`scripts/install.sh`, `scripts/run_tests.sh`, `scripts/setup.py`, `scripts/start_bot.sh`): header comments and echo messages translated to English; `setup.py` bilingual docstring reduced to English-only
- **Stale French test assertion** (`tests/test_bot.py`): `assertIn("Aucun trade", ...)` updated to `assertIn("No resolved trades", ...)` after `generate_status_html()` was migrated to English in 0.4.3
- **Pylint 10.00/10** across all script files: `scripts/backtest_volfilter.py` (unused `Optional` import, redundant `datetime` re-import, non-interpolated f-strings), `scripts/download_btc_history.py` (unused `sys` import, unused `total_expected` variable, disallowed name `bar` → `progress`, non-interpolated f-strings), `scripts/backtest_grid.py` (`if/assign` → `max()`, unused `trail_label` variable, non-interpolated f-string), `scripts/backtest.py` (non-interpolated f-string, unused `best_s` → `_`), `scripts/profile_compare.py` (non-interpolated f-strings, suppress pre-existing `import-error` false positive), `bot/account_bot.py` (`global-statement` suppressed inline)
- **`scripts/test_multibot_deploy.sh` — port conflict bugs**: removed erroneous `fuser -k 5557/tcp` from account teardown loop (would have killed the feed service); removed dead `INDICATORS_CONFIGS` array that was populated but never used after the Phase 7 restructure

### Documentation
- **`docs/design.md` + `docs/design.fr.md`**: process inventory updated with `indicators.py` REP socket (`:5561`); `feed_auto_start=false` subsection added with ASCII retry-loop flow diagram; startup order section updated to reflect shared indicators; env vars scope table corrected (`TRADINEBOTTE_INDICATORS_ADDR` and `TRADINEBOTTE_INDICATORS_REG_ADDR` used by both `indicators.py` and `account_bot.py`)
- **`docs/multi.md` + `docs/multi.fr.md`**: env vars table gains `TRADINEBOTTE_INDICATORS_ADDR` and `TRADINEBOTTE_INDICATORS_REG_ADDR`; per-account `config.json` multi-bot keys table added (`feed_addr`, `feed_auto_start`, `indicators_reg_addr`, `indicators_streams`); launch sequence split into manual and systemd subsections; systemd services table expanded to 3 rows including `install_indicators_service.sh`
- **`INSTALL.md` + `INSTALL.fr.md`**: complete env var reference table added; systemd environment inheritance note with credentials file example; shared indicators architecture section replacing per-account split model; `--config FILE` and `--reg-addr ADDR` flags documented

---

## [0.4.3] - 2026-05-09

### Fixed
- **English-only policy sweep — scripts and bot files** (`bot/feed.py`, `bot/bot_utils.py`, `bot/strategies/grid.py`, `bot/strategies/__init__.py`, `scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh`, `scripts/run_integration_tests.sh`, `scripts/benchmark_api.py`, `scripts/profile_hotpath.py`, `scripts/strategy_compare.sh`, `scripts/backtest_volfilter.py`, `scripts/test_multibot_deploy.sh`, `scripts/test_standalone_deploy.sh`): all remaining French strings in source code, log messages, comments, error output, and script headers were translated to English; the sole exceptions — second arguments of `_t()` bilingual shell helpers and `"FR":` dict values in `setup.py` — are intentional and were preserved; this completes the retroactive enforcement of the language policy introduced in 0.4.2
- **`pgrep`/`pkill` bracket trick extended to feed, account, and indicator processes** (`scripts/start_feed.sh`, `scripts/start_account.sh`, `scripts/start_indicators.sh`): patterns `[f]eed.py`, `[a]ccount_bot.py`, `[i]ndicators.py` prevent the SSH session running the script from matching itself via `pgrep -f`

---


## [0.4.2] - 2026-05-09

### Changed
- **Language policy enforced across all four bot modules** (`bot/live_bot.py`, `bot/feed.py`, `bot/indicators.py`, `bot/account_bot.py`): all French log messages, comments, and docstrings were translated to English; `CLAUDE.md` was updated with a mandatory "Language policy" section codifying that source code (`.py`, `.sh`, `.json`), code comments, log messages, and docstrings must be English-only; documentation files (`*.fr.md`) remain the only place where French belongs; this rule was enforced retroactively across all four modules

### Added
- **`bot/live_bot.py` — log system refactor**: `TimedRotatingFileHandler` (midnight rotation, 30-day backlog) replaces the previous `FileHandler`, preventing unbounded log growth on long-running deployments; `_SESSION_ID` (8-character uppercase hex UUID fragment) is injected into every log line via `_SessionFilter` so all records from one bot lifetime can be grepped as a unit; log format now includes `%(session)s` — every line carries the session identifier; `RejectionStats` dataclass (13 fields, one per `check_signal()` early-exit reason) is logged every 60 seconds as a `[REJECTIONS]` summary line then reset, making it easy to diagnose why the bot is firing fewer trades than expected; `[LATENCY]` log line extended with a `ts_ms=` field carrying the WebSocket message timestamp in milliseconds
- **Docstring audit — 14 functions/methods across 6 modules**: all previously undocumented public functions and methods were given docstrings; affected files: `bot/api_binance.py`, `bot/api_mexc.py`, `bot/feed.py`, `bot/indicators.py`, `bot/strategies/__init__.py`, `bot/strategies/grid.py`
- **Test suite expanded — 27 new tests in 5 new classes** (`tests/test_bot.py`): `TestSessionFilter` (4 tests — filter attaches `session` attribute, `filter()` returns `True`, `session` matches `_SESSION_ID`, value is 8-char uppercase hex); `TestRejectionStats` (3 tests — all 13 fields zero at init, dataclass independence across instances, `BotState` initialises a fresh `RejectionStats`); `TestRejectionCounters` (13 tests — one per rejection reason, plus a no-counter-on-fire guard and a periodic-reset test); `TestLatencyLog` (1 test — `[LATENCY]` line contains `ts_ms=`); `TestLogFormatters` (6 tests — `_PlainFmt` abbreviates `INFO`→`INFO ` and `WARNING`→`WARN `, `_ColorFmt` adds ANSI escape codes, `%(session)s` token present in format string)

### Fixed
- **`pgrep`/`pkill` self-match bug fixed across 7 scripts** (`scripts/start_bot.sh`, `scripts/start_collector.sh`, `scripts/collect_db.sh`, `scripts/monitor.sh`, `scripts/test_all_accounts.sh`, `scripts/test_multibot_deploy.sh`, `scripts/test_standalone_deploy.sh`): all `pgrep -f live_bot.py` / `pkill -f live_bot.py` patterns were replaced with the bracket-trick variant `[l]ive_bot\.py`; the unbracketed pattern matches the SSH session running the command, causing the SSH process itself to be killed instead of the bot process; stale French `grep` patterns in the test scripts were also updated to match the new English log messages: `'Connecte au feed'` → `'Connected to feed'`, `'WebSocket connecte'` → `'WebSocket connected'`, `'Souscription'` → `'Subscribing'`, `'Marches BTC'` → `'BTC 5-min markets'`

### Security
- **Git history purged of infrastructure credentials** via `git filter-repo --replace-text` + `--message-callback`: server hostname and three deployment account passwords that appeared in early commits have been removed from all blobs and commit messages; passwords rotated
- **`bot/bot_utils.py` — SHA-1 → bcrypt for htpasswd** (`_htpasswd_sha1` → `_htpasswd`): unsalted SHA-1 (`{SHA}`) replaced by bcrypt (`$2y$`, Apache 2.4+ native); `bcrypt` added to `requirements.txt`; soft fallback with warning when library absent; `TestHtpasswd` suite updated accordingly
- **`bot/bot_utils.py` — XSS fix in web status page** (`html.escape`): market `question` field from external Polymarket API was inserted raw into HTML attributes and cell content; now escaped with `html.escape()` before any interpolation
- **`bot/account_bot.py` — symlink-safe lock file** (`O_NOFOLLOW`): replaced `open()` with `os.open(O_CREAT|O_WRONLY|O_NOFOLLOW, 0o600)` + `os.fdopen()` to prevent a local attacker from pre-placing a symlink at the lock path and causing the bot to truncate an arbitrary file
- **`bot/account_bot.py` — minimal subprocess env** for `feed.py`: child process no longer inherits the full parent environment (including `POLY_PRIVATE_KEY`); only `PATH`, `HOME`, `LANG`, `VIRTUAL_ENV`, `PYTHONPATH`, `LC_ALL`, `LC_CTYPE` and `TRADINEBOTTE_FEED_ADDR` are passed
- **Shell scripts — `eval echo` removed** (command injection): `INSTALL_DIR="$(eval echo "$INSTALL_DIR")"` replaced with safe `INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"` in all 7 occurrences across `start_bot.sh`, `monitor.sh`, `start_account.sh`, `start_feed.sh`, `install_account_service.sh` (×2) and `install_feed_service.sh`
- **Shell scripts — inline Python path injection fixed**: `$CONFIG` was interpolated into a double-quoted `-c "..."` string; replaced with single-quoted code + `sys.argv[1]` in `start_bot.sh` and `monitor.sh`
- **Deploy scripts — `sshpass -p` removed** (password visible in `ps aux`): replaced with `SSHPASS="$pwd" sshpass -e` in all three deploy scripts (`test_all_accounts.sh`, `test_multibot_deploy.sh`, `test_standalone_deploy.sh`)
- **Deploy scripts — `StrictHostKeyChecking=no` → `accept-new`**: blind host-key acceptance replaced with `accept-new` (trusts on first connect, rejects changed keys) in all 3 deploy scripts

### Changed
- **`SNAPSHOT_INTERVAL` default changed from 5s to 1s**: all bots now write one snapshot row per second by default, eliminating the blind spot that caused ~50 extra LOSS events per 3-month session in aligned backtests; override with `--snapshot-interval N` to reduce disk I/O if needed

### Added
- **`strategies/indicators_4h_bitcoin.json`** + **`strategies/indicators_1d_bitcoin.json`** — per-account indicator configs split from `indicators.json`; account-a gets btc_4h on port 5559, account-b gets btc_1d on port 5560
- **`scripts/start_indicators.sh`** — start/stop script for the indicator service; the account selects its config via `TRADINEBOTTE_INDICATORS_CONFIG`
- **`tests/test_indicators.py`** — added `TestSplitConfigs` (6 tests, 57→63 total) verifying port isolation, stream-id disjointness, and correct config content for each split file
- **`scripts/test_multibot_deploy.sh`** — phase 3b starts `indicators.py` per account before bots; cleanup phases kill `indicators.py` and free ports 5559/5560
- **`scripts/test_multibot.conf.example`** — added `TEST_INDICATORS_CONFIGS` array to map each account to its indicator config
- **`strategies/indicators.json`** — added `btc_1d` stream (daily RSI 14 + volatility 20) alongside `btc_4h`; one `indicators.py` process now serves multiple bots with different timeframe needs
- **`tests/test_indicators.py`** — added `TestMultiBotIndicatorSharing` (5 tests, 52→57 total) verifying ZeroMQ PUB/SUB 1→N broadcast and per-`stream_id` application-level filtering; plain sync ZMQ sockets with `RCVTIMEO=500ms`
- **`strategies/indicators.json`** — JSON configuration file for the indicator service: defines a list of named streams, each specifying `asset`, `source` (`"binance_ws"` or `"feed"`), `timeframe`, `seed_periods`, and a list of `{type, period}` indicator entries; default config computes RSI(14) and volatility(20) on 4h BTC/USDT candles from Binance (`source: "binance_ws"`, `timeframe: "4h"`, `seed_periods: 50`); `indicators.py` is extended with a new `--config FILE` flag that loads this file; `IndicatorSpec.from_dict()` validates type (`rsi|sma|ema|volatility`) and period (≥2); `StreamSpec.from_dict()` validates source (`binance_ws|feed`), parses indicators list; `load_config()` returns `(feed_addr, out_addr, min_ticks, streams)` — env vars take precedence over config addresses; `PriceSeries.compute_indicators(specs)` new method alongside legacy `indicators()` — key format `<abbrev>_<period>` (`vol_20` not `volatility_20`, consistent with legacy); `_binance_kline_task()` opens `wss://stream.binance.com:9443/ws/<symbol>@kline_<tf>`, seeds from REST (`/api/v3/klines`) at startup, pushes close prices of completed candles (`k.x=true`), reconnects with exponential backoff (5s→60s); `_zmq_feed_task()` refactored from legacy `run()` — supports config-driven feed streams; `run()` dispatches tasks per stream from config or falls back to legacy CLI-flag mode (backward compatible); 24 new tests in 4 new classes (`TestIndicatorSpec`, `TestStreamSpec`, `TestLoadConfig`, `TestPriceSeriesComputeIndicators`) — 52 total; pylint 10.00/10
- **`docs/design.md` + `docs/design.fr.md`** — new bilingual process architecture reference (EN+FR): covers all deployment modes (Option A standalone, Option B multi-bot), full process inventory table (live_bot / feed / account_bot / indicators — role, credentials, ZMQ socket), ZeroMQ topology diagram showing all SUB/PUB connections with port numbers, complete message catalog with JSON examples and field tables for all four message types (`market`, `book`, `ping`, `indicators`), feed auto-start mechanism (POSIX lock flowchart, minimal subprocess env, hash-based lock naming), process isolation guarantees table (DB, log, keys, strategy params, capital state, daily PnL cache, signalled set), indicators pipeline internals (ring-buffer → RSI/SMA/EMA/vol → publish-when-ready), startup order with ZMQ connectionless note, environment variables summary
- **`bot/indicators.py` — ZeroMQ technical indicator service** (`compute_rsi`, `compute_sma`, `compute_ema`, `compute_volatility`, `PriceSeries`): new standalone process that subscribes to the feed.py PUB socket (default `tcp://127.0.0.1:5557`) and republishes enriched indicator messages on a second PUB socket (default `tcp://127.0.0.1:5559`); consumes `{"t":"book"}` messages, accumulates a per-token ring-buffer of `best_bid` prices (configurable `maxlen`, default 200), and emits `{"t":"indicators", "token_id":…, "ts":…, "rsi_14":…, "sma_20":…, "ema_9":…, "vol_20":…}` once the minimum tick count is reached and all indicators are computable; indicator math is pure stdlib (no numpy): RSI uses Wilder's formula (avg gain / avg loss over last n deltas), SMA is a simple tail mean, EMA is seeded with SMA then iterated with `k = 2/(n+1)`, volatility is population std-dev of log-returns; all four functions return `None` when insufficient data; `PriceSeries` wraps a `collections.deque` with `push()` and `indicators(rsi_n, sma_n, ema_n, vol_n)` for clean testability; CLI: `--feed ADDR` (SUB target), `--out ADDR` (PUB bind), `--rsi N` (default 14), `--sma N` (default 20), `--ema N` (default 9), `--vol N` (default 20), `--min-ticks N` (default 25), `--verbose`; address overrides via `TRADINEBOTTE_FEED_ADDR` and `TRADINEBOTTE_INDICATORS_ADDR` env vars; pylint 10.00/10; 28 new tests in `tests/test_indicators.py` covering all edge cases: insufficient data (None returns), constant series (SMA/EMA = value, vol = 0), all-gains RSI (100), all-losses RSI (0), EMA k-factor formula, volatility on zero prices (None), PriceSeries maxlen ring behaviour, custom periods, and sma_value_correct integration test
- **Documentation — `backtest_grid.py` and `download_btc_history.py` added to README and INSTALL** (EN + FR): both scripts were entirely absent from the four main user-facing documentation files (`README.md`, `README.fr.md`, `INSTALL.md`, `INSTALL.fr.md`); `README` gains a **Grid Trading** feature bullet and a **Grid Trading Backtest** section with quick-start commands; `INSTALL` gains a full **Grid Trading Backtest** section with all 10 `backtest_grid.py` flags documented, all 6 `download_btc_history.py` flags, complete parameter tables, and links to `docs/AdaptedGridTrading.md`
- **`docs/AdaptedGridTrading.md` + `docs/AdaptedGridTrading.fr.md`** — new bilingual documentation (EN+FR) covering all three grid strategies (static, bear trailing, bull trailing): strategy concept and rationale for each, how the trailing mechanism works step-by-step with real price examples (2022 crash re-center sequence, 2024 bull run re-center sequence), full parameter tables, asymmetry explanation (bear mode stops at exit_high; bull mode stops at exit_low), capital management at re-center, WARNING section on `trail=both` danger in trending markets (−23.9% vs +2.0% on 2022 bear), complete comparison table (3 strategies × 3 regimes), parameter sweep tables for each mode, strategy selection flowchart, reproduce-any-result CLI examples, related files index
- **`scripts/backtest_grid.py` — trailing grid strategies (bear-adapted + bull-adapted)**: engine extended with `--trail bear|bull|both|off`, `--max-recenters N` (default 10), `--compare` (static vs trailing side-by-side per DB); trailing mechanism: when price exits `[grid_lower, grid_upper]`, instead of stopping the bot re-centers the grid at the current close price and resumes — `bear` mode re-centers only downward (follows price down, ignores upward exits), `bull` mode only upward, `both` in either direction (dangerous in strongly trending markets — see below); new metrics: `realized_pnl` (completed cycles net of fees), `unrealized_pnl` (BTC cost basis vs current price), `recenters` count, `recenter_prices` list; at re-center, new buy orders are allocated from remaining USDT budget (prevents overspend after accumulated losses); results (±15%/30L vs static): **bear trailing on 2022 crash** +2.0% vs −3.3% static (102 cycles vs 18, 2 re-centers at $32K/$27K, exits profitably on bounce), **bull trailing on 2024 bull run** +3.7% vs +0.1% static (134 cycles vs 5, 3 re-centers at $76K/$87K/$101K, full 92-day coverage); bear and bull modes are asymmetric: `trail=bear` in a bull run = identical to static (stopped at exit_high); `trail=bull` in a bear market = identical to static (stopped at exit_low); **WARNING**: `trail=both` in a trending bear market = −23.9% loss (9 re-centers oscillating up and down, accumulates $409 unrealized BTC loss) — only use `both` in confirmed ranging conditions; sweep (bear mode): best avg Calmar ±30%/20L, best avg PnL ±15%/30L (+2.5%); sweep (bull mode): best avg Calmar ±20%/20L, best avg PnL ±15%/30L (+1.8%); CLI: `--all`, `--range`, `--levels`, `--size`, `--fee`, `--trail`, `--max-recenters`, `--compare`, `--sweep`, `--sort calmar|pnl`
- **`scripts/backtest_grid.py`** — (initial version) grid trading backtest engine against OHLCV SQLite databases; fill model: price-touch on candle `[low, high]` range — BUY limit orders placed at levels below start price, SELL placed one step above after each BUY fill, BUY placed one step below after each SELL fill; capital = `n_levels × order_size` (worst-case all levels simultaneously filled); stop-loss when candle `low < grid_lower` or `high > grid_upper` — remaining BTC liquidated at close; metrics: net PnL, PnL%, annualized%, gross PnL, fees, max drawdown, Calmar ratio (PnL%/MaxDD%), time-in-grid%; CLI: `--all` (run on all `BTCUSDT_1m*.db` in `data/`), `--range` (±% from start price, default 15), `--levels` (default 30), `--size` (USDT/order, default 50), `--fee`, `--sweep` (sweeps range_pct × levels: 5×3=15 combos), `--sort calmar|pnl`; sweep results (3 DBs × 15 combos): static grid trading performs best in ranging markets (+5%/90d with ±15%/30 levels); bear/bull trending runs blow through any static grid within 10–47% of the period, limiting loss to 3–5% of capital via early stop-loss; best Calmar config across all regimes: ±30%/20 levels (fully survives lateral market, contains bear losses); best absolute PnL config in ranging conditions: ±15%/30 levels
- **`strategies/grid_BTCUSDT_bear_trailing.json`** — bear-adapted strategy: ±15%, 30 levels, $50/order; trail=bear (re-center downward only); calibrated BTC=$80,705: grid [$68,599, $92,811]; backtest: +5.0% lateral, +2.0% bear 2022 (2 re-centers at $32K/$27K, exits on bounce), +0.1% bull (identical to static); use when expecting further downside
- **`strategies/grid_BTCUSDT_bull_trailing.json`** — bull-adapted strategy: same grid params; trail=bull (re-center upward only); backtest: +5.0% lateral (1 re-center, 100% time), +3.7% bull run (3 re-centers at $76K/$87K/$101K, full 92d coverage), −3.3% bear (identical to static); use when expecting further upside
- **`strategies/grid_BTCUSDT_moderate.json`** — backtested strategy: ±20%, 30 levels, $50/order, capital $1,500; calibrated at BTC=$80,705 (2026-05-09): grid [$64,564, $96,846], step $1,113; backtest results: +3.9%/90d (+16%/yr) lateral 2026 (100% time in grid), −4.6% loss bear 2022 (stops after 10% of period), +0.2% bull run 2024 (stops after 28%); recommended for general/uncertain market conditions
- **`strategies/grid_BTCUSDT_tight.json`** — backtested strategy: ±15%, 30 levels, $50/order, capital $1,500; calibrated at BTC=$80,705: grid [$68,599, $92,811], step $829; backtest results: +5.0%/90d (+20%/yr) lateral 2026 (96% time in grid), −3.3% loss bear 2022 (stops after 9%), +0.1% bull run 2024 (stops after 25%); recommended for expected ranging/consolidating conditions — tighter step generates more cycles per day
- **Historical OHLCV databases — three market regimes** (`data/`): three 1-minute BTCUSDT databases now assembled for grid backtest coverage across distinct volatility regimes — (1) `BTCUSDT_1m90d_range_20260208-20260509.db`: current lateral market (129,600 candles, $63K–$83K range, avg candle range $48.5); (2) `BTCUSDT_1m92d_bullrun20241015-20250115.db`: Oct 2024 – Jan 2025 bull run (132,481 candles, $64,800 → ATH $108,353, avg candle range $66.9); (3) `BTCUSDT_1m92d_bearmarket20220501-20220801.db`: May – Aug 2022 LUNA crash bear market (132,481 candles, BTC from ~$38K down to ~$17K, highest avg candle range of the three — extreme volatility period); all files excluded from git (`.gitignore`), regenerate with `python scripts/download_btc_history.py --start YYYY-MM-DD --end YYYY-MM-DD`
- **`scripts/download_btc_history.py`** — download OHLCV kline history from Binance public API; `--start YYYY-MM-DD` / `--end YYYY-MM-DD` flags added for historical date ranges (e.g. bull market periods); output filename now encodes the actual date range (`BTCUSDT_1m92d_range_20241015-20250115.db`); `--days` is used as fallback when `--start` is absent; `BTCUSDT_1m92d_range_20241015-20250115.db` downloaded: 132,481 candles covering the Oct 2024 – Jan 2025 bull run ($64,800 → ATH $108,353); original (no credentials required) into a SQLite database in `data/`; schema: `klines(ts_ms PK, open, high, low, close, volume, close_ms)` + `meta` table storing symbol/interval/download timestamp; supports `--symbol`, `--interval` (1m/5m/15m/1h/…), `--days`, `--out`; resumes from last stored candle on re-run; progress bar with estimated completion; rate-limited to ~8 req/s (Binance limit: 1200 weight/min, weight 2 per klines request); 90 days of 1-minute BTCUSDT downloaded in ~59s → 129,600 rows, 10.2 MB; output DB is excluded from git (`.gitignore`) — regenerate with `python scripts/download_btc_history.py`; intended for grid trading backtesting where fills are detected by price-touch on the `[low, high]` range of each candle
- **Grid trading — WebSocket user data stream** (`bot/strategies/grid.py`, `bot/api_binance.py`, `bot/api_mexc.py`): real-time fill notifications replace REST polling once the stream is connected; `get_listen_key(session)` creates a 60-min TTL user data stream key via `POST /api/v3/userDataStream`; `keepalive_listen_key(session, listen_key)` extends the TTL every 30 min via `PUT /api/v3/userDataStream`; `make_user_stream_url(listen_key)` returns the connector-specific WebSocket URL (`wss://stream.binance.com:9443/ws/<key>` for Binance, `wss://wbs.mexc.com/ws?listenKey=<key>` for MEXC); `parse_user_stream_msg(msg)` extracts fill events — Binance uses `"e":"executionReport"` with string status `"X"`, MEXC uses a nested `"d"` dict with numeric status (2=FILLED, 3=PARTIALLY_FILLED) and numeric side (1=BUY, 2=SELL); `GridStrategy._user_stream_loop(state)` runs as a background `asyncio.Task` started after grid initialization, manages listen-key lifecycle and reconnects with exponential back-off (5 s → 60 s cap), exits after 3 consecutive key-fetch failures (no credentials); `_on_user_stream_fill(state, fill)` matches the `order_id` to the active grid level and dispatches to `_on_buy_filled`/`_on_sell_filled`; when the stream is connected (`_user_ws_connected=True`), REST polling is skipped — it becomes a fallback for the disconnected period only; the stream task is cancelled cleanly on stop-loss; simulation mode (sim_ order IDs) never starts the stream; 14 new tests in `TestUserDataStream` covering Binance/MEXC parse functions, URL generation, fill dispatch, and unknown-order no-op (278 total)
- **Grid trading — SQLite persistence + restart recovery** (`bot/strategies/grid.py`, `bot/live_bot.py` points 4–5): `_save_state(conn)` upserts the `grid_state` row (bounds, step, size, cycles, profit, `initialised`, `halted`) and all `grid_levels` rows (per-level order IDs, prices, status) after any meaningful state change — grid init, stop-loss, and whenever order IDs change after a fill poll; `restore_from_db(state)` is called at startup from `main()` and: (1) loads saved state from DB, (2) validates the config hasn't changed (bounds/step/size within 1-cent tolerance — mismatch triggers a clean re-init), (3) if initialised and not halted, calls `get_open_orders()` to reconcile with the exchange and detects any fills that occurred while the bot was offline — placing the appropriate counter-orders immediately; `grid_state` and `grid_levels` tables added via schema migration v2 (`MIGRATIONS[2]`); `main()` now sets `state.session` before strategy load so `restore_from_db` can call the exchange REST API; 8 new tests in `TestGridPersistence` covering save/upsert, restore-no-state, config-mismatch, halted-restore (no reconciliation), offline-fill detection, and order-still-open (no spurious fill); 264 total tests passing
- **Grid trading — fill detection, counter-orders, stop-loss** (`bot/strategies/grid.py` points 1–3): `GridStrategy` is now fully operational; `_initialise_grid()` places initial BUY orders below `best_ask` and SELL orders above at startup; `_poll_fills()` detects filled orders every `poll_interval` seconds (default 2 s) — simulation path uses price-crossing (`best_ask <= buy_price` / `best_bid >= sell_price`), live path calls `get_open_orders()` once per cycle and treats absent order IDs as filled (40-weight Binance call vs 4×N for individual queries); `_on_buy_filled()` places a counter SELL at `buy_price + grid_step` (or marks idle if above `grid_upper`); `_on_sell_filled()` accounts PnL for full BUY→SELL cycles (`profit = (sell_p − buy_p) × qty − fee_buy − fee_sell`) and places a counter BUY at `sell_price − grid_step` (or marks idle if below `grid_lower`); `_check_stop_loss()` triggers `_cancel_all_orders()` and sets `grid.halted = True` when `best_bid` exits `[grid_lower, grid_upper]`; `GridLevel` gains `buy_price` and `sell_price` fields (actual order prices, distinct from the reference `price` when counter-orders shift the slot); `GridState` gains `halted`, `poll_interval`, and `last_poll_ts`; 29 new tests in `tests/test_bot.py` covering initialisation, fill handlers, simulated fill detection, stop-loss, and CEX sim-mode behaviour (444 total passing)
- **CEX order management functions** (`bot/api_binance.py`, `bot/api_mexc.py`): three new async functions added to both connectors: `get_order_status(session, symbol, order_id)` → `"NEW"|"FILLED"|"CANCELED"|"PARTIALLY_FILLED"` or `None` (GET `/api/v3/order`, weight 4); `cancel_order(session, symbol, order_id)` → `bool` (DELETE `/api/v3/order`, weight 1; returns `True` immediately for `sim_` order IDs or missing credentials); `get_open_orders(session, symbol)` → `list[dict]` with keys `order_id`, `side`, `price`, `qty`, `status` (GET `/api/v3/openOrders`, weight 40 Binance / no stated weight MEXC); both connectors use `str(symbol).split(":", maxsplit=1)[0]` to strip the `:SELL` suffix; header key differs: `X-MBX-APIKEY` (Binance) vs `X-MEXC-APIKEY` (MEXC)
- **Grid trading scaffold — Strategy + Connector abstraction layer** (`bot/connectors/`, `bot/strategies/`): `bot/connectors/__init__.py` provides `load(name)` factory (`"polymarket"`, `"binance"`, `"mexc"`) returning the appropriate `api_*` module via `importlib.import_module`; `bot/strategies/base.py` defines the `Strategy` Protocol (`STRATEGY_TYPE: str`, `async on_book_update(state, ts, _t_ws=None)`) marked `@runtime_checkable`; `bot/strategies/__init__.py` provides `load(name, config)` factory — `"threshold"` returns `None` (built-in path in `live_bot.py`), `"grid"` returns a `GridStrategy` instance; `bot/live_bot.py` gains: `_load_connector(name)` swaps the global `api` module at startup via `global api; api = importlib.import_module(...)` (no-op for `"polymarket"`); `BotState.strategy` field (`None` → threshold backward-compat, `GridStrategy` → grid); `handle_book_update()` routes to `state.strategy.on_book_update()` when non-None; `BotConfig` gains `connector`, `strategy_type`, `grid_symbol`, `grid_lower`, `grid_upper`, `grid_levels`, `grid_order_size_usdt`; existing strategy JSON files gain `"strategy_type": "threshold"` and `"connector": "polymarket"`; `strategies/grid_BTCUSDT.json` created as example config; `.pylintrc` `max-module-lines` 1200 → 1300
- **`docs/GridTrading.md` + `docs/GridTrading.fr.md`** — new bilingual grid trading documentation (333 lines each): algorithm detail (step formula, init logic, full BUY→SELL cycle with worked example, stop-loss), threshold vs grid comparison table, architecture diagram with execution flow, all JSON config parameters with types and constraints, environment variable setup (Binance and MEXC), 5-step setup guide, profitability calculation (gross/net profit formula, fee breakdown, cycle frequency estimate, max-loss scenario), implementation status table (✅/🔲), instructions for adding a new CEX connector
- **US federal holiday filter** (`_us_holidays`, `_is_us_holiday`, `BotConfig.us_holiday_filter`): new `us_holiday_filter` boolean in `BotConfig` (default `false`); when enabled, `is_trading_hour` returns `False` on all 10 NYSE-recognised US federal holidays (New Year's Day, MLK Day, Presidents' Day, Good Friday, Memorial Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas), with Saturday→Friday and Sunday→Monday observed-day shifts; holiday dates are computed via a pure-stdlib algorithm (no external dependency) and cached per year with `functools.lru_cache`; the filter is independent of `hour_filter_enabled` — it activates even when the time-window filter is off; enabled via `"us_holiday_filter": true` in the `hour_filter` block of the strategy JSON; 14 new tests in `TestUsHolidays` cover fixed dates, floating dates, observed shifts, Good Friday (Easter algorithm), and `is_trading_hour` integration
- **Data-collection mode** (`--snapshot-interval N` + `start_collector.sh` + `collect_db.sh` + `schedule_collect.sh` + `.claude/agents/data-collector.md`): first VPS deployment account is repurposed as a passive data collector — `--simulate` (no real orders) + `--snapshot-interval 1` writes one snapshot row per second instead of every 5s, eliminating the blind spot that causes ~50 extra LOSS events in aligned backtests; `scripts/start_collector.sh` deploys code via rsync (single SSH session via `bash -s` stdin to avoid server rate-limiting), creates a minimal `config.json`, and starts the bot from `bot/live_bot.py` with `TRADINEBOTTE_DIR` pointing to the isolated collector directory; `scripts/collect_db.sh` downloads `live.db` as `data/live_YYYY_WNN.db` weekly with `--rotate` to archive the remote DB and restart the collector, `--yes` for non-interactive cron use; `scripts/schedule_collect.sh` installs/removes/shows a crontab entry (`--install`, `--remove`, `--status`, `--run-now`) that runs `collect_db.sh --rotate --yes` every Sunday at 03:00 UTC, logging to `~/tradinebotte/collect.log`; `start_bot.sh` now passes unknown flags through to `live_bot.py`; a new `data-collector` Claude agent orchestrates the full deploy → collect → backtest workflow
- **`bot/live_bot.py` — `--snapshot-interval SECS` flag**: overrides `SNAPSHOT_INTERVAL` (default 5) at launch time without touching `config.json` or the strategy JSON; passed through `make_config(snapshot_interval=...)` into `BotConfig.snapshot_interval`; useful for data collection (1s) or low-disk deployments (60s+)
- **`docs/HOWTO_tests_and_backtests.md` + `.fr.md`** — new bilingual human-readable guide (525 lines each): full glossary of all terms (snapshot, OBI formula, trade outcomes WIN/LOSS/OPEN/STOP/GHOST, all strategy parameters, performance metrics), explicit distinction between *backtest aligné* (simulation with corrected parameters, no real orders) and *bot réel* (real executed trades from the `trades` table); how to run tests and interpret output; all 13 backtest flags documented; sweep table and comparison table columns defined; strategy update decision framework (KEEP/MONITOR/UPDATE with thresholds)
- **`scripts/backtest.py --compare` — user-params column rescaled to match detected stake/capital**: when the bot ran with a different stake/capital than strategy defaults (e.g. paper3: $150/$1000 vs $10/$100), the first column was previously on a different economic base making PnL% incomparable; now, when stake or capital_start diverge, the user-params backtest is re-run with the detected values while keeping the user's signal parameters (threshold, min_secs, obi); the `_stat_block` header still shows the original strategy defaults for reference
- **Documentation — `--sweep-all`, `--sort`, `--top` flags added to README and INSTALL** (EN + FR): the three advanced backtest flags introduced in the previous release were documented in CHANGELOG but missing from the main user-facing reference tables in `README.md`, `README.fr.md`, `INSTALL.md`, `INSTALL.fr.md`; all four files now include the flags in their backtest command examples and parameter tables
- **`scripts/backtest.py` — PnL% added everywhere** (`summarize`, `_stat_block`, `print_aggregate`, `print_comparison`, `print_sweep_table`, `print_recommendations`): every PnL figure now shows the percentage return on `capital_start`; sweep table gains a `PnL%` column; comparison table gains a `PnL%` results row and a `Capital start` config row; `_stat_block` now shows stake and capital start
- **`scripts/backtest.py` — `detect_actual_params` fixes**: `capital_start` now uses the first trade's `capital_before` (ordered by `signal_ts_ms ASC`) instead of `MIN()`, which previously picked a depleted mid-session value; DSL reliability guard lowered from 10× to 5× stake — when estimated DSL exceeds 5× stake the detection is discarded (stake > DSL means a single loss already triggers the daily limit, making the worst-day heuristic unreliable); aligned backtest falls back to user's own DSL when detection fails (previously used `stake × 5` which inflated the aligned PnL by suppressing stop-loss triggers)
- **`.claude/agents/strategy-optimizer.md`** — new dedicated subagent for strategy parameter optimisation: runs the full grid search + per-DB comparison workflow via `scripts/strategy_compare.sh`, interprets results (PnL/MaxDD ratio, drawdown, win rate), produces a structured KEEP/MONITOR/UPDATE verdict, and applies the best configuration by creating a new versioned strategy JSON (`polymarket_BTC5M_v3.json` etc.) and updating the `live_bot.py` default pointer; 5-step workflow: run → read report → interpret → recommend → apply
- **`scripts/strategy_compare.sh`** — new comparison workflow script: runs `--sweep-all` + `--compare` in sequence, tees output to both stdout and a timestamped file in `reports/`; flags: `--top N` (default 10 unique configs), `--sort ratio|pnl|wr`, `--db PATH`, `--out FILE`, `--no-save`
- **`scripts/backtest.py --top N`** — new sweep flag: shows only the top-N unique strategy configurations in the sweep table, deduplicated on `(threshold, min_secs, obi)` — removes the redundant `min_ask` and `dsl` variants that produce identical results; default 0 (show all); the note `(top N configs uniques thr/secs/obi — 405 combos au total)` is appended to the separator line
- **Test suite expanded to 368 tests** — `test_backtest.py` gains 5 new classes covering previously untested functions: `TestRatio` (5 tests for `_ratio`), `TestPercentile` (5 tests for `_percentile`), `TestDetectActualParams` (6 tests for `detect_actual_params`), `TestActualStats` (4 tests for `_actual_stats`), `TestCollectDbs` (4 tests for `_collect_dbs`); `test_bot.py` gains `TestStrategyLoading` (8 tests verifying v2 strategy file exists, loads correct params, v1 still present, missing file returns None)
- **`strategies/polymarket_BTC5M_v2.json` — new default strategy** (sweep-all optimised 2026-05-08): `signal_threshold=0.95` (was 0.96), `min_secs_remaining=45`, `obi_reject_thresh=-0.75`, `daily_stop_loss=30`; PnL/MaxDD ratio 4.42 vs 3.61 for v1 across 5 databases / 912k snapshots; `live_bot.py` default strategy pointer updated to `polymarket_BTC5M_v2.json`; v1 kept for reference
- **`scripts/backtest.py --sweep-all`** — aggregate grid search across all databases: runs each of the 405 parameter combinations (5 thresholds × 3 min_secs × 3 min_ask × 3 OBI × 3 daily_stop_loss) independently per DB and aggregates results (sum PnL, worst MaxDD across sessions); prints the full ranked sweep table plus `print_recommendations()` — top-5 configs by PnL/MaxDD ratio, by total PnL, and by win rate, with the exact CLI command for the best overall config
- **`scripts/backtest.py --sort wr|pnl|ratio`** — sweep table is now sortable; default sort is `ratio` (PnL/MaxDD Calmar-style); `--sort pnl` ranks by total PnL; `--sort wr` ranks by win rate
- **`scripts/backtest.py --sweep` extended grid** — `daily_stop_loss` added as a sweep dimension (`[30, 100, 500]`); grid is now 5×3×3×3×3 = 405 combinations (was 5×3×3 = 45); `ratio` column (PnL/MaxDD) and `dsl` column added to the sweep table
- **`scripts/backtest.py --compare` — three-way comparison table** (`detect_actual_params`): `--compare` now auto-detects the bot's actual runtime config from the `trades` table (modal stake, 5th-percentile threshold and min_secs robust against outliers, min `capital_before`, daily stop-loss estimated from worst observed trading day +20 % headroom), runs a second aligned backtest with those parameters, and prints a side-by-side three-column table: BACKTEST(user params) | BACKTEST(aligned to actual) | ACTUAL BOT RESULTS; also shows STOP/GHOST outcome rows, warns on config mismatches with the exact CLI flags to reproduce, and works with `--all` (per-file comparison in multi mode); handles DBs without a `trades` table (sample DB) without crashing
- **Circuit-breaker on CLOB API failures** (`BotState.api_fail_streak` / `api_cooldown_until`): after 3 consecutive `post_order` failures while a private key is configured, new entries are suspended for 5 minutes; the streak resets to 0 on the first successful order; `check_signal` checks the cooldown before the `signalled.add()` call; 6 new tests in `TestCircuitBreaker`
- **DB schema versioning** (`MIGRATIONS` dict + `_apply_migrations`): `schema_version` table added to SCHEMA; `_apply_migrations(conn)` applies any pending migrations in version order and records the highest applied version; `init_db` calls it after `executescript(SCHEMA)`; `make_db()` in tests updated accordingly; 5 new tests in `TestSchemaVersioning`
- **`.pylintrc` — `[TYPECHECK] ignored-modules = websockets`**: suppresses the pre-existing `E0401 import-error` false positive that occurs when pylint runs under system Python (venv-only package); restores 10.00/10

### Fixed
- **pylint 10.00/10 restored** after security commit: `_today_ms_utc()` helper extracted to `bot_utils.py` (eliminates R0801 duplicate-code between `generate_status_html` and `restore_state_from_db`); stdlib `from`-imports moved before third-party in `live_bot.py` (C0411); `max-module-lines=1200` added to `.pylintrc` (live_bot grew past 1000 after BotConfig + daily PnL cache additions)
- **`scripts/test_all_accounts.sh` — result parser now matches `OK (skipped=N)`**: regex `^OK$` failed when unittest emits `OK (skipped=13)`; changed to `^OK( |$)` so deployments with skipped tests report success correctly
- **`live_bot.py` — restore missing `import aiohttp, websockets`** dropped during the BotConfig refactor; pylint 4.0 detected them as `E0602 undefined-variable`; `global-statement` warning on `_setup_logging` suppressed with inline disable (legitimate process-level singleton); score 9.44 → **10.00/10**

### Refactoring
- **`purge_expired_markets(state)` extracted to `live_bot.py`**: the identical 9-line expired-market cleanup loop (remove from `tokens`, `market_tokens`, `signalled`) that was duplicated between `_market_refresh_loop` and `account_bot._run` is now a single shared function; `account_bot.py` calls `bot.purge_expired_markets(state)`; `pylint: disable=duplicate-code` comment removed

### Performance
- **`check_signal` — daily PnL in-memory cache** (`state.daily_pnl`): eliminates the SQL `SELECT SUM(pnl_net)` executed on every WebSocket book update; `close_trade` increments the counter incrementally; midnight UTC rollover is detected at the top of `check_signal` (runs on every book update, not just signal attempts) and resets the counter to 0; `restore_state_from_db` initialises it from the DB on startup so the cache is accurate after a restart; 7 new tests in `TestDailyPnlCache`

### Fixed
- **`restore_state_from_db` — re-entry guard for recently resolved markets**: trades resolved in the last 10 minutes are now added to `state.signalled` on startup; previously, a restart within the same 5-minute market window could re-enter a market whose price still showed a 96+ bid after resolution; 6 new tests in `TestSignalledRestore`

### Changed
- **`scripts/install.sh` refactor** — `--lang EN|FR` flag for non-interactive runs; safe tilde expansion (`${var/#\~/$HOME}` replaces `eval echo`); `_check_syntax` and `_pip_install` helpers eliminate repeated code; bot file copy and syntax check now driven by a shared loop; `set -eo pipefail` replaces bare `set -e`

### Feature
- **Test suite expanded to 163 tests** — `tests/test_bot.py` now contains 105 tests (up from 95); `test_backtest.py` and `test_multibot.py` unchanged at 28 and 30; confirmed passing on all three VPS deployment accounts in ~11s each
- **`bot/api_binance.py`** — new Binance spot API connector implementing the same public interface as `api_polymarket.py` (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`, market metadata helpers); credentials via `BINANCE_API_KEY` / `BINANCE_API_SECRET` env vars or kwargs; HMAC-SHA256 signing; dry-run mode when credentials absent; fee rate 0.1%; `WS_URL` targets `wss://stream.binance.com:9443/stream` (combined depth stream); switch exchange with a single import change in `live_bot.py`
- **`bot/api_mexc.py`** — new MEXC spot API connector with the same interface; MEXC v3 REST is Binance-compatible but uses different WebSocket framing (`SUBSCRIPTION` method, `spot@public.limit.depth.v3.api@SYMBOL@5` streams, `{"d": {...}, "s": "SYMBOL"}` message envelope); MEXC LIMIT orders do not require `timeInForce` (server defaults to GTC); fee rate 0.2%; credentials via `MEXC_API_KEY` / `MEXC_API_SECRET`
- **`scripts/benchmark_api.py`** — new latency benchmark tool; measures HTTP round-trip time over N sequential requests per endpoint (Polymarket Gamma, Binance, MEXC) and WebSocket time-to-first-message for each exchange; reports min/mean/p50/p90/p99/max/σ with ASCII spark bars; `--rounds N` (default 15) and `--no-ws` options; uses `aiohttp` WS client as built-in fallback when `websockets` is not installed; fetches a live Polymarket token dynamically for the WS subscription test; results on Amsterdam VPS: Polymarket 12–20 ms REST / 52–103 ms WS, MEXC 10–30 ms REST / 880–960 ms WS, Binance 218–235 ms REST / 930–1090 ms WS
- **Bilingual interface** — `scripts/setup.py`, `install.sh`, `start_bot.sh`, and `monitor.sh` now prompt `[E] English / [F] Français` at startup; `setup.py` persists the choice as `"lang": "EN"|"FR"` in `config.json`; subsequent scripts read that key automatically — no re-prompting; all user-facing strings are translated in both directions via a `T` dict (Python) or `_t()` function (bash); SQL column aliases in `monitor.sh` are translated too (`wins`/`victoires`, `current_capital`/`capital_actuel`)
- **`.claude/agents/bilingual-quality.md`** — new Claude Code subagent (Sonnet) that audits, updates, and translates across all 10 bilingual documentation files; three modes: AUDIT (gap report, no edits), UPDATE (adds content to both languages simultaneously), TRANSLATE (EN↔FR with project vocabulary); invoked automatically by the post-commit hook reminder and on demand via `/bilingual-quality`
- **`config.json.example`** — added `binance_api_key`, `binance_api_secret`, `mexc_api_key`, `mexc_api_secret` credential fields with comments; added `lang` field (written by `setup.py`, read by shell scripts)
- **`.claude/agents/doc-sync.md`** — Claude Code subagent (Haiku) that audits all user-facing CLI flags in scripts against the four main doc files (README.md, README.fr.md, INSTALL.md, INSTALL.fr.md); reports gaps only, never edits; integrated into `scripts/run_tests.sh` as a non-blocking post-suite step when the `claude` CLI is present
- **`scripts/start_bot.sh` — `--reset-db` option**: backs up `live.db` to `live.db.bak.YYYYMMDD_HHMMSS`, then deletes it before launch so the bot starts fresh (zero capital, zero trade history); prompts for `yes` confirmation before proceeding; safe no-op if the file does not exist
- **`bot/live_bot.py` v0.41 — optimised default parameters** (grid-search sweep on liveweek.db, 110 952 snapshots):
  - `SIGNAL_THRESHOLD` 0.96 → **0.95** (more trades, same 99.3% WR, +$14.73 vs +$13.14)
  - `MIN_SECS_REMAINING` 45 → **30 s** (gains ~+$7 PnL at equal WR; 45 s was over-restrictive)
  - `OBI_REJECT_THRESH` -0.50 → **-0.25** (tighter order-book filter — flips liveweek PnL from −$2.43 to +$4.97 for the 0.96/45 s config)
  - Same defaults applied to `scripts/backtest.py` (`Params` dataclass + CLI defaults) and `strategies/polymarket_BTC5M.json`
  - Tests updated: `test_blocked_bid_below_threshold` (0.95→0.94), `test_at_min_secs_remaining_blocked` (44s→29s), `test_no_signal_insufficient_secs` (30s→29s)
- **`bot/live_bot.py` — log format overhaul** (5 improvements + uptime):
  - Timestamp without milliseconds: `2026-05-04 20:04:03` (was `2026-05-04 20:04:03,123`)
  - Fixed-width level: `[INFO ]` / `[WARN ]` / `[ERROR]` / `[CRIT ]` — messages align in the file
  - ANSI colors on stdout only (file stays plain): yellow for WARN, red for ERROR, magenta for CRIT
  - Visual separators on trade events: `▶ TRADE`, `✓ WIN `, `✗ LOSS`
  - Cleaner metric spacing: `entry=0.9710  bid=0.9700  secs=52s` (double-space between fields)
  - Uptime in the startup banner: `LIVE BOT v0.40 | start=2026-05-04 19:11:48 UTC | up 1h02m03s | ...`
- **`scripts/test_all_accounts.sh`** — new script that wipes and reinstalls the latest version on all configured test accounts in sequence; reads server and credentials from `~/.tradinebotte-test.conf` (same file as `test_multibot_deploy.sh`); uses `sshpass` throughout; waits a configurable delay between accounts (default 180 s); options: `--delay SECONDS`, `--no-wait`, `--parallel`; prints a final summary with pass/fail per account

### Fix
- **`bot/live_bot.py`** — `QueueHandler.prepare()` was pre-formatting records (calling `self.format()` and storing the result in `record.msg`) before enqueuing; when `logging.basicConfig` assigned the full `_LOG_FMT` formatter to the `QueueHandler`, every record got formatted twice — once by the queue handler, once by the `FileHandler` — producing duplicate timestamps and level tags in `live.log`; fixed by setting a passthrough `Formatter("%(message)s")` on the `QueueHandler` so only the raw message text is stored in the queue
- **`scripts/install.sh`** — added `cd "$REPO_DIR"` after REPO_DIR is computed; relative paths (`bot/live_bot.py`, `tests/`, etc.) now resolve correctly regardless of the working directory from which the script is invoked (was failing on the VPS where the script was called from `~` instead of the repo root)
- **`bot/account_bot.py`** — leftover `getpass` import removed (unused since the `~/tmp` refactor); pylint score restored to 10.00/10
- **`bot/feed.py`** — added `# pylint: disable=duplicate-code` (WebSocket recv loop intentionally mirrors `live_bot.py`)
- **`tests/test_multibot.py`** — `TEST_PORT` was hardcoded to `15557`; when multiple Linux users ran tests in parallel on the same server all processes attempted to `bind()` to `tcp://127.0.0.1:15557` simultaneously, causing `ZMQError: Address already in use`; port is now derived from `os.getuid() % 900 + 15000` so each OS user gets a distinct loopback port in the 15000–15899 range

### Refactoring
- **`bot/live_bot.py` — `BotConfig` dataclass** — module-level side effects eliminated: no `sys.argv` inspection, no file I/O, no logging setup, no `os.makedirs` at import time; all runtime configuration (paths, strategy params, credentials, hour filter, vol filter, timing, web status) now carried by a `BotConfig` dataclass; `make_config(simulate, no_log, no_snapshots)` factory reads env vars, `config.json`, and the strategy JSON — called only from `main()`; `_setup_logging(config)` configures handlers and `QueueListener` — called only from `main()`; `BotState(conn, config)` holds its config so hot-path functions read `state.config.X` instead of module globals; `init_db(config)` and `is_trading_hour(config, ts_ms=None)` now take an explicit config parameter; `bot/account_bot.py` updated to call `bot.make_config()` / `bot.init_db(config)` / `bot.BotState(conn, config)`; `tests/test_bot.py` `TestIsTradingHour` updated to use `BotConfig` objects; 311 tests still pass; importing `live_bot` is now a no-op — multiple bot instances can hold different configs simultaneously
- **Temp directory layout** — only the shared feed uses `/tmp`; all per-user paths moved to `~/tmp/`:
  - `bot/account_bot.py`: `_FEED_TMP_DIR` changed from `/tmp/tradinebotte-<user>` to `/tmp/tradinebotte-feed` (no user suffix); created with chmod 1777 so every Linux user can write lock/log files into it while the sticky bit prevents cross-user deletion; this makes the file lock truly cross-user, ensuring a single `feed.py` instance across all accounts
  - `tests/test_bot.py`, `tests/test_multibot.py`: test sandbox moved to `~/tmp/tradinebotte-test`
  - `scripts/run_tests.sh`: `TRADINEBOTTE_DIR` updated to `${HOME}/tmp/tradinebotte-test`
  - `scripts/profile_hotpath.py`, `scripts/profile_compare.py`: profiling sandbox moved to `~/tmp/profile-bot`
  - `scripts/install_service.sh`, `install_account_service.sh`, `install_feed_service.sh`: generated `.service` staging files moved to `~/tmp/`
  - `scripts/test_standalone_deploy.sh`, `test_multibot_deploy.sh`: cleanup and feed log search paths updated accordingly

---

## [0.40] - 2026-05-02

### Fix
- **`tests/test_bot.py`** — test directory was hardcoded to `/tmp/tradinebotte-test`, causing `PermissionError` when two users ran tests concurrently on the same server; now uses `/tmp/tradinebotte-test-<user>` via `getpass.getuser()`
- **`scripts/install.sh`** — `--with-tests` flag could fail with "source and destination are the same file" when `REPO_DIR == INSTALL_DIR` (repo cloned directly into the install target); copy block now skipped in that case, mirroring the existing `strategies/` guard
- **`scripts/install.sh`** — pip install commands replaced by `-r requirements.txt` so all dependencies (including `pyzmq` for `feed.py` / multibot) are installed automatically without maintaining a duplicate list in the script

---

## [0.32] - 2026-05-02

### Feature
- **`bot/live_bot.py` — volatility filter** — new entry guard that blocks trades when the market has been oscillating heavily over the last 60 seconds; three complementary metrics computed on a 12-sample rolling window (sampled every 5s): `vol_bid` (std dev of best_bid, threshold 0.07), `range_bid` (max−min amplitude, threshold 0.30), `obi_vol` (std dev of OBI, threshold 0.40); a trade is skipped if any metric exceeds its threshold and at least 6 samples are available (30s warm-up); calibrated on live data 2026-04-25→05-01 (301 trades): losses 8→1, win rate 97.3%→99.5%, EV −$0.050→+$0.160 per trade; toggle with `VOL_FILTER_ENABLED`; calibration results in `volstop.txt`
- **`bot/live_bot.py` — `VOL_FILTER_WEEKDAY_ONLY`** — flag that suspends the volatility filter during the weekend session (Fri 20:00 UTC → Mon 13:30 UTC); now defaults to `True` — multi-DB backtest shows better overall EV (+0.1284 vs +0.1196) and avoids over-rejecting valid weekend signals where BTC liquidity patterns differ; new helper `_in_weekend_session()` encapsulates the boundary logic and is covered by 10 unit tests
- **`data/liveweek.db`** — live bot database from VPS London (2026-04-25 to 2026-05-01, 110 883 snapshots, 301 resolved trades) added as a backtest dataset; used for volatility filter calibration
- **`scripts/backtest_volfilter.py`** — simulation script: loads snapshot and trade data, computes per-trade volatility indicators at entry time on a rolling window, sweeps thresholds for `vol_bid`/`range_bid`/`obi_vol`, and reports baseline vs. filtered performance with a top-10 configuration ranking by EV/trade

### Refactoring
- **`bot/bot_utils.py`** — new utility module split out of `live_bot.py`: `print_dashboard`, `generate_status_html`, `write_web_status`, `setup_htaccess`, `_htpasswd_sha1`; configuration injected via module-level variables synced from `live_bot` after config.json is loaded; no circular imports; `live_bot.py` shrinks from 991 to 847 lines
- **`bot/live_bot.py`** — module stays focused on trading logic: signal processing, state classes, WebSocket loop, trade management, DB init

### Fix
- **`scripts/install.sh`** — `sqlite3` CLI check downgraded from a hard error to a non-blocking warning; the bot uses Python's built-in `sqlite3` module (always available) and never calls the CLI — only `monitor.sh` needs it for manual DB queries; the warning still prints the install command but no longer exits 1
- **`docs/CONTEXT_AI.md`** — removed from git tracking entirely; file stays on disk for local AI context but is now listed in `.gitignore` so it is never pushed to the public repo; contains infrastructure details and internal operational notes that have no place in a public repository
- **`docs/CONTEXT_AI.md`** — redacted committed credentials: private key, API key, API secret, passphrase, wallet address, and VPS IP address replaced with `<PLACEHOLDER>` tokens; credentials must be stored only in `config.json` (git-ignored)
- **`scripts/test_standalone_deploy.sh`** — new SSH integration test for Option A multi-user scenario: deploys to 2 Linux users, starts `start_bot.sh` as user 1, then asserts user 2 can also start without being blocked by user 1's process (catches the `pgrep -f` scope class of bugs); verifies both WebSocket connections in logs; 6-phase structure (cleanup, deploy, launch×2, log check, teardown, report)
- **`scripts/run_integration_tests.sh`** — wrapper that runs `test_standalone_deploy.sh` then `test_multibot_deploy.sh` in sequence; `--standalone` / `--multibot` flags to run either alone; final summary with pass/fail count and elapsed time; `INSTALL.md` updated to document both tests and the wrapper
- **`UPDATE.md` / `UPDATE.fr.md`** — new bilingual guide documenting the update workflow for all three scenarios: separate repo and install dir (`git pull` + `install.sh`), repo = install dir (same), and rsync from a dev machine (with the critical `--exclude='config.json'` warning); Option B multi-bot update also covered; added to the bilingual docs table in `CLAUDE.md`
- **`scripts/install.sh`** — detects existing virtualenv on update: if `$INSTALL_DIR/venv/` already exists, skips `python3 -m venv` and runs `pip install --upgrade` only; fresh installs are unchanged; reduces update time from ~2 min (full venv rebuild) to a few seconds
- **`QUICKSTART.md` / `QUICKSTART.fr.md`** — rewritten from ~185 lines to ~40 lines; kept only the two code flows (Option A and B), the decision table, and the "no wallet" simulation note; all detail moved to `INSTALL.md`; link to `UPDATE.md` added in the header
- **`scripts/start_bot.sh`** — single-instance check used `pgrep -f live_bot.py` which matches processes from all Linux users on the same host; on a shared server this prevented any second user from starting their bot; fixed by scoping to the current user with `pgrep -u "$(id -u)" -f live_bot.py`
- **`scripts/install.sh`** — same-file copy guard for `strategies/*.json`: when the repo is cloned directly into `INSTALL_DIR` (e.g. `git clone → ~/tradinebotte`, `install → ~/tradinebotte`), the old `cp strategies/*.json "$INSTALL_DIR/strategies/"` would copy each file onto itself and silently corrupt it; fixed by comparing resolved absolute paths (`_STRAT_SRC` vs `_STRAT_DST`) and skipping the copy when they are identical
- **`bot/account_bot.py`** — lock and log files now created under `/tmp/tradinebotte-<user>/` instead of flat `/tmp/tradinebotte-feed-<hash>.*`; each Linux user owns their subdirectory entirely, eliminating "Operation not permitted" errors on shared hosts where the `/tmp` sticky bit prevents one user from removing another's files
- **`scripts/test_multibot_deploy.sh`** — cleanup (Phase 1) and teardown (Phase 8) now use `rm -rf /tmp/tradinebotte-$USER` matching the new per-user path; feed log detection (Phases 5 and 7) searches each user's subdirectory in turn and records which user's account ran the feed (`FEED_LOG_IDX`) so subsequent reads use the correct SSH account

---

## [0.3] - 2026-05-01

### Bug Fix
- **`scripts/start_bot.sh`** — launch message and log path were displayed as absolute `$HOME`-prefixed paths; replaced with `~`-relative paths using bash parameter substitution (`${VAR/$HOME/\~}`)
- **`scripts/start_bot.sh`** — was using system `python3` instead of the virtualenv's `python3`; the bot would crash immediately because aiohttp/web3/etc. are installed in the venv, not the system Python; fixed to use `$INSTALL_DIR/venv/bin/python3`; also redirected `nohup` output to `live.log` instead of `/dev/null` so startup errors are now visible in the log tail; added a venv existence check with a clear error message

### Improvement
- **`scripts/setup.py`** — pressing Enter without a private key now creates a simulation `config.json` (empty credentials) and exits cleanly; blockchain imports are skipped entirely in this path; prompt updated to mention the option; QUICKSTART + INSTALL docs updated (EN + FR)
- **`scripts/install.sh`** — no longer calls `apt-get` directly; instead detects missing system packages (`python3`, `python3-venv`, `python3.X-venv`, `sqlite3`) and prints the exact `sudo apt-get install` command with the auto-detected Python version; exits with an error if anything is missing, continues silently if all present; docs updated across INSTALL, QUICKSTART, README (EN + FR)

### Code Quality
- `bot/account_bot.py` — pylint 10/10: removed unused `json` import; `open(lock_file)` and `open(log_path)` now carry explicit encoding (`utf-8`) or binary mode (`ab`); intentional non-`with` usages annotated with `# pylint: disable=consider-using-with`; `# pylint: disable=duplicate-code` added at module level (market-expiry purge loop mirrors `feed.py` by design)
- `bot/account_bot.py` — ResourceWarning fixed: `_run()` now wraps its event loop in `try/finally` to call `sock.close(linger=0)` and `ctx.term()` on cancellation; ZMQ socket and context are guaranteed to be released when the task is cancelled during tests or on shutdown

### Testing
- **`scripts/test_multibot_deploy.sh`** — two bugs fixed: (1) `grep -c || echo 0` produced `"0\n0"` because `grep -c` always prints the count to stdout before exiting 1 on no matches, and `|| echo 0` then added a second zero — causing `"0\n0"` to be stored in `BOOK_COUNT`/`ERROR_COUNT` and failing the subsequent `[[ -eq ]]` arithmetic comparison with a syntax error; fixed by replacing `|| echo 0` with `|| true` (grep already printed the count); (2) `ELAPSED=20` was not updated when the Phase 4 stabilisation wait was extended from 20 s to 30 s, causing the heartbeat loop to run one extra iteration in Phase 6; corrected to `ELAPSED=30`
- **`scripts/test_multibot_deploy.sh`** — full end-to-end integration test for the multi-bot Option B setup across configurable test accounts: Phase 1 cleanup (kill processes, remove dirs, clear lock files), Phase 2 deploy (rsync + venv creation + pip install without root), Phase 3 simultaneous launch of all N account_bots in `--verbose` mode to stress-test the race-safe feed auto-start, Phase 4 sustained operation with 30s heartbeat checks, Phase 5 log analysis (feed WebSocket confirmation, book update count per bot, ERROR/CRITICAL line count), Phase 6 teardown and final process count; exits 0 on full pass, 1 on any failure; `--skip-deploy` reuses an existing install; `--duration N` overrides the 3-minute default; server address, SSH port, usernames, and passwords are read from `~/.tradinebotte-test.conf` (or `TEST_MULTIBOT_CONF` env var) — never hardcoded; `scripts/test_multibot.conf.example` provides the template

### Data
- **`data/basicsunday.db`** — 24,870 snapshots from a ~26h live simulation session (2026-04-25 20:57 → 2026-04-26 22:55), 312 distinct markets; backtest result: 42/43 wins (97.7%), PnL -$3.58; combined with `calmsaturday.db` via `--all`: 52 trades, 51 wins, **98.1% aggregate win rate**; `README.md` updated with a dataset comparison table

### Documentation
- **Server admin prerequisites** — confirmed live on Ubuntu 22.04 / Python 3.10: all three packages `python3-venv`, `python3-pip`, and `python3.10-venv` are required; `python3.10-venv` is now a primary requirement (not a fallback) in `INSTALL.md`, `INSTALL.fr.md`, `QUICKSTART.md`, `QUICKSTART.fr.md`, `README.md`, `README.fr.md`; without it venv creation fails with *"ensurepip is not available"*

### Feature
- **Multi-bot WebSocket sharing (Option B — ZeroMQ)** — `bot/feed.py` maintains a single WebSocket connection to Polymarket and publishes every book update over a ZeroMQ PUB socket (`tcp://127.0.0.1:5557` by default, overridable via `TRADINEBOTTE_FEED_ADDR`). `bot/account_bot.py` subscribes to the feed and runs the full trading strategy for one account in isolation. Multiple `account_bot.py` processes can run in parallel, each with its own `TRADINEBOTTE_DIR` (config, DB, log), without opening additional WebSocket connections. `scripts/start_feed.sh` and `scripts/start_account.sh` handle launch and logging. `pyzmq` added to `requirements.txt`.
- **Hour/day filter** — new `hour_filter` block in `strategies/polymarket_BTC5M.json` (disabled by default). When enabled, restricts entries to configurable UTC hour ranges per weekday/weekend, with special handling for the US weekly open (Monday before 13:30 UTC) and close (Friday from 20:00 UTC). Applied identically in the live bot (`is_trading_hour()` guard in `check_signal()`) and in the backtest engine (`_is_trading_hour()` in `run_backtest()`). 15 new unit tests.

### Fix
- `bot/live_bot.py` — `--simulate` no longer overwrites `TRADINEBOTTE_DIR` when already set in the environment; multiple bots can now run in parallel simulation mode with fully isolated directories (`TRADINEBOTTE_DIR=~/account-a python3 live_bot.py --simulate`)

### Documentation
- `INSTALL.md` / `INSTALL.fr.md` — new "Hour / Day Filter" section: rationale table (Asian/EU/US sessions, weekly open/close), full parameter reference, decision logic walkthrough with step-by-step Monday example, 3 ready-to-use preset configs, backtest validation workflow, and startup log example
- `README.md` / `README.fr.md` — new feature bullet for the hour/day filter; test count updated to 123
- `docs/multi.md` / `docs/multi.fr.md` — full bilingual architecture documentation: Option A vs B decision guide (when to use each, tradeoffs table), ASCII diagram, component reference, per-account independent signal evaluation, message protocol (all 3 types with all fields), environment variables, directory layout, launch sequence, per-account monitoring, failure modes, cross-user deployment (different Linux accounts), adding a third account, comparison table with standalone mode; linked from README, INSTALL, QUICKSTART, feed.py, account_bot.py
- `QUICKSTART.md` / `QUICKSTART.fr.md` — restructured deployment choice section: definition-list format for Option A/B, decision table covering single account, two wallets same/different Linux users, strategy comparison, simplicity vs efficiency tradeoffs
- `tests/test_multibot.py` — 30 new tests for `feed.py` and `account_bot.py`: 7 unit tests for `feed.register_market()`, 9 unit tests for `account_bot._register_from_market_msg()`, 8 async ZMQ integration tests for Option A (single bot), 6 async ZMQ integration tests for Option B (two simultaneous bots sharing the same feed); total test count raised to 153

- **`--verbose` diagnostic mode** — both `bot/feed.py` and `bot/account_bot.py` accept `--verbose`; sets logging to DEBUG and emits detailed traces: raw WebSocket messages (200-char truncated), every ZMQ PUB with key fields, ZMQ probe results and timing, `_ensure_feed()` lock race steps and per-second wait loop, book signal threshold comparisons, unknown token skips, market registrations, init params at startup; `feed.py` subprocess inherits `--verbose` when `account_bot.py` is started with it; normal INFO output is unchanged without the flag
- **Self-starting feed in account_bot.py** — `_ensure_feed()` probes the feed address for 5 s on startup; if unreachable, acquires an exclusive file lock (`/tmp/tradinebotte-feed-<hash>.lock`) and launches `feed.py` as a subprocess, waiting up to 30 s for it to be ready before releasing the lock; concurrent account_bots that lose the race block on a shared lock and connect once the winner releases it — all bots can now be started simultaneously with no manual feed management; feed logs go to `/tmp/tradinebotte-feed-<hash>.log`; `docs/multi.md`, `QUICKSTART.md` updated to reflect the simplified launch sequence
- **systemd services for multi-bot (Option B)** — `scripts/tradinebotte-feed.service` and `scripts/tradinebotte-account.service` are unit templates for the ZeroMQ feed and per-account bots. `scripts/install_feed_service.sh` auto-detects the virtualenv (`.venv` for dev, `venv` for prod), validates `feed.py` and `pyzmq`, generates a ready-to-install system service with `User=`, `WorkingDirectory=`, `ExecStart=`, and `TRADINEBOTTE_FEED_ADDR=`. `scripts/install_account_service.sh` derives the service name from the account directory basename (`tradinebotte-account-<name>`), applies the same venv auto-detection, and sets `Requires=tradinebotte-feed.service` so systemd enforces the start/restart ordering. Both scripts print exact `sudo cp / daemon-reload / enable / start` commands. `docs/multi.md` and `docs/multi.fr.md` updated with a full systemd installation walkthrough.

### Previous
- `scripts/tradinebotte.service` — systemd unit template: `After=network-online.target`, `Restart=on-failure`, `RestartSec=30`, `StartLimitBurst=5` (max 5 restarts per 5 min); placeholders `__USER__` and `__TRADINEBOTTE_DIR__` are substituted at install time
- `scripts/install_service.sh` — generator script: reads `TRADINEBOTTE_DIR` (or defaults to `~/tradinebotte`), validates the install exists, substitutes placeholders with `sed`, writes to `/tmp/tradinebotte.service`, and prints the four `sudo` commands needed to enable the service

### Code Quality
- `bot/live_bot.py` — mypy strict: 0 errors; added explicit type annotations for `_log_handlers: list[logging.Handler]`, `_log_queue: queue.Queue[logging.LogRecord]`, and all five `BotState.__init__` dict/set attributes (`tokens`, `market_tokens`, `open_trades`, `traded_direction`, `signalled`); `cur.lastrowid or 0` guards the `int | None` return type
- `tests/test_bot.py` — ResourceWarning fixed: removed global `warnings.filterwarnings` suppression; all seven test classes that create SQLite connections now use explicit `setUp`/`tearDown` or `self.addCleanup(conn.close)`; no unclosed connection warnings on Python 3.13
- `.github/workflows/mypy.yml` — new CI workflow: runs `mypy bot/live_bot.py bot/api_polymarket.py --ignore-missing-imports` on every push and pull request (Python 3.12)
- `requirements-dev.txt` — `mypy` added

### Documentation
- `QUICKSTART.md` / `QUICKSTART.fr.md` — new bilingual quick-start guide: five commands (clone, install, setup, start, monitor) covering the minimal path from zero to a running bot; includes a simulate-mode note and a stop command; cross-linked from `README`, `INSTALL`, and `CLAUDE.md`
- `CLAUDE.md` — bilingual doc rule extended from 6 to 8 files to include `QUICKSTART.md` / `QUICKSTART.fr.md`

### Feature
- `scripts/backtest.py` — multi-file backtest: `--db` now accepts one or more paths (shell glob-expandable, e.g. `--db data/*.db`); new `--all` flag scans the `data/` directory for all `.db` files and prepends `live.db` if it contains ≥ 100 snapshots; capital resets to `capital_start` independently per file; per-file BACKTEST block shows the filename when multiple files are processed; an AGGREGATE block (combined wins/losses/PnL/win-rate/worst-drawdown) is printed after all files when more than one file is processed and `--sweep` is not active

### Security
- `requirements.txt` / `requirements-dev.txt` — dependency manifests: all runtime deps (`aiohttp`, `websockets`, `web3`, `py-clob-client`) and dev deps (`pylint`, `pip-audit`) are now declared in versioned files; all CI workflows install from these files rather than listing packages inline
- `.github/workflows/audit.yml` — new CI workflow: runs `pip-audit -r requirements.txt` on every push and every Monday at 06:00 UTC to detect known CVEs in runtime dependencies before they reach production
- `.github/dependabot.yml` — Dependabot enabled for both `pip` packages and `github-actions`; creates automated PRs every Monday when newer versions are available

### Code Quality
- `bot/live_bot.py` — full type annotations: all 28 functions and class methods now carry parameter and return type hints (`dict[str, Any]`, `list[str]`, `Optional[float]`, `sqlite3.Connection`, `aiohttp.ClientSession`, etc.); the websocket `ws` parameter uses `Any` to stay version-agnostic across websockets releases
- `tests/test_bot.py` — 9 new tests added (71 → 80): `TestHtpasswd` (SHA1 prefix, known value, collision), `TestGenerateStatusHtml` (capital, table, win rate, empty state), `TestHandleBookUpdate` (state update from parsed message, unknown token ignored); uses in-memory SQLite and `unittest.IsolatedAsyncioTestCase` — no network or credentials required
- `.github/workflows/tests.yml` — new CI workflow: runs `unittest discover` on Python 3.10, 3.11, and 3.12 on every push; total suite: 108 tests (80 bot + 28 backtest)

### Feature
- `bot/live_bot.py` — latency tracking: each trade emits a `[LATENCY]` log line with `signal_ms` (WebSocket message received → order decision, includes all signal guards and daily-PnL SQLite query) and `order_rtt_ms` (CLOB API HTTP round-trip); timestamps use `time.monotonic()` and are passed as an optional `_t_ws` parameter through `handle_book_update` → `check_signal` → `enter_live_trade`; zero overhead on non-trade messages
- `scripts/latency.py` — new analysis tool: parses `[LATENCY]` lines from `live.log` and prints min / mean / p50 / p90 / p99 / max for signal_ms, order_rtt_ms, and total_ms; usage: `python3 scripts/latency.py [logfile]`

### Feature
- `bot/live_bot.py` — log writes are now fully asynchronous: a `QueueListener` daemon thread drains the log queue to disk so the asyncio event loop is never blocked by file I/O; no behaviour change for existing deployments
- `bot/live_bot.py` — new `--no-log` flag: suppresses the log file entirely (`NullHandler`); the SQLite DB (trades + snapshots) is unaffected; combine with `--simulate` to keep stdout output; intended for production deployments where minimum disk I/O is critical

### Bugfix
- All scripts, `bot/live_bot.py`, and all documentation — environment variable renamed from `POLYMARKET_DIR` to `TRADINEBOTTE_DIR` to match the project name; update any existing `export POLYMARKET_DIR=...` in your shell profile or systemd unit to `export TRADINEBOTTE_DIR=...`

### Bugfix
- All scripts, `bot/live_bot.py`, and all documentation — default install directory renamed from `~/polymarket` to `~/tradinebotte` to match the bot's name; `TRADINEBOTTE_DIR` still overrides as before; simulation temp dir renamed from `/tmp/polymarket-sim` to `/tmp/tradinebotte-sim`; test temp dir renamed from `/tmp/polymarket-test` to `/tmp/tradinebotte-test`

### Bugfix
- All scripts and `bot/live_bot.py` — default install path changed from `/opt/polymarket-live` (requires root) to `~/polymarket` (no root needed); `TRADINEBOTTE_DIR` still overrides as before; historical CHANGELOG entries referencing `/opt/polymarket-live` reflect the old default and are left unchanged

### Bugfix
- `scripts/backtest.py` — fallback to sample dataset now requires `live.db` to have at least 100 snapshots (previously any non-empty file was accepted, causing a stale 16-snapshot test artifact to shadow the bundled dataset); also prints which database is selected (`(live)` vs `(sample)`) at startup

### Feature
- `bot/live_bot.py` — new `--simulate` flag: redirects all file I/O to `/tmp/polymarket-sim` (overriding `TRADINEBOTTE_DIR`), mirrors logs to stdout in addition to the log file, and logs a visible `MODE SIMULATION` warning; production `live.db` and `live.log` are never touched; safe to run on any machine without credentials
- `INSTALL.md` / `INSTALL.fr.md` — Testing section updated: `timeout 20 python3 bot/live_bot.py --simulate` replaces the previous command that wrote to the production path by default
- `README.md` / `README.fr.md` — Simulation mode feature bullet updated to describe `--simulate`

### Bugfix
- `scripts/start_bot.sh` — refuses to start if an instance of `live_bot.py` is already running (exits with error and prints the existing PID); previously killed the running instance automatically, which could interrupt an open trade mid-resolution
- `INSTALL.md` / `INSTALL.fr.md` — Running section updated to document the new behaviour and the manual stop command (`pkill -f live_bot.py`)

### Data
- `data/backtest_sample_btc5m_range_2026.db` — bundled SQLite sample dataset: 2430 snapshots collected in simulation mode on 2026-04-25 from real Polymarket BTC 5-minute markets (snapshots table only, no credentials or trade data)
- `scripts/backtest.py` — automatic fallback to `data/backtest_sample_btc5m_range_2026.db` when `TRADINEBOTTE_DIR/live.db` is absent; allows running the backtest on any machine without a live bot database

### Performance
- `bot/live_bot.py` — `MARKET_REFRESH` reduced from 90 s to 30 s: the bot now discovers new markets at most 30 s after they enter the ±6-minute window, instead of up to 90 s; the Gamma API call remains a single request (tag_id=102892 filter, no pagination) so the overhead is negligible

### Documentation
- `INSTALL.md` / `INSTALL.fr.md` — sqlite3 CLI added to prerequisites with note that the bot works without it; Python fallback command provided for hosts without sudo

### Feature
- `strategies/polymarket_BTC5M.json` — new strategy file: all backtested signal and capital parameters (`signal_threshold`, `entry_max`, `min_secs_remaining`, `min_ask_vol`, `win_threshold`, `loss_threshold`, `obi_reject_thresh`, `daily_stop_loss`, `stake`, `capital_start`, `gas_fee_usd`) extracted from hardcoded constants into a versioned JSON file; add `"strategy": "<path>"` in `config.json` to switch strategies
- `bot/live_bot.py` — `load_strategy()` loads the JSON file at startup; parameters override hardcoded defaults; falls back silently to defaults if file absent (dev/tests); strategy name logged at startup
- `config.json.example` — new optional key `strategy` pointing to the strategy file
- `scripts/install.sh` — copies `strategies/*.json` to the install directory
- `scripts/install.sh` — new `--with-tests` flag: copies `tests/` and `scripts/backtest.py` to the install directory and runs the full 99-test suite immediately after installation; works with any install path; usage: `bash scripts/install.sh ~/polymarket --with-tests`
- `INSTALL.md` / `INSTALL.fr.md` — `--with-tests` option documented

### Refactoring
- `bot/api_polymarket.py` — new Polymarket API adapter: all exchange-specific code extracted from `live_bot.py` into a dedicated module (`get_markets`, `post_order`, `parse_book_update`, `compute_fee`, `get_market_id/question/end_ts/start_ts/up_token/down_token`, `make_subscribe_msg`, `WS_URL`, `WS_BATCH_SIZE`, `FEE_RATE`). To target a different exchange in the future, create `api_<exchange>.py` with the same public interface and change the single import line in `live_bot.py`.
- `bot/live_bot.py` — imports `api_polymarket as api`; all Polymarket-specific constants and functions replaced by `api.xxx()` calls; `register_market` uses `api.get_market_id/question/etc.`; `enter_live_trade` calls `api.compute_fee` and `api.post_order`; WebSocket loop uses `api.WS_URL`, `api.WS_BATCH_SIZE`, `api.make_subscribe_msg`, `api.get_markets`, `api.parse_book_update`; removed unused top-level imports (`hashlib`, `hmac`, `uuid`)
- `tests/test_bot.py` — `TestComputeFee`, `TestParseBookMessage`, `TestMarketHelpers` now import from `api_polymarket` directly; `insert_trade` helper uses `api_poly.compute_fee`
- `scripts/install.sh` — copies `bot/api_polymarket.py` alongside `live_bot.py`; syntax check extended to both files

### Documentation
- `docs/status_example.html` — static HTML preview of the web status page; illustrates the dark-themed layout, metric cards (capital, PnL, win rate, trades, daily stats, open positions), and resolved-trade table with WIN/LOSS colour coding
- `README.md` / `README.fr.md` — link to `docs/status_example.html` added to the "Optional HTML status page" feature bullet
- `INSTALL.md` / `INSTALL.fr.md` — reference to `docs/status_example.html` added in the "Web Status Page" section
- `README.md` / `README.fr.md` — Features section added listing all 11 bot capabilities with one-line descriptions
- `INSTALL.md` / `INSTALL.fr.md` — expanded "Web Status Page" section with step-by-step Apache prerequisites: `a2enmod userdir auth_basic authn_file`, `AllowOverride AuthConfig` directive, two options for granting the `www-data` process read access to `.htpasswd` (`chmod o+r` vs `usermod -aG`); nginx note explaining that `.htaccess` is not processed and showing the equivalent `auth_basic` / `auth_basic_user_file` server block; note about custom paths outside `~/public_html`
- `README.md` / `README.fr.md` — `webstatuspage_*` config table entries updated to mention required Apache modules, `AllowOverride AuthConfig`, `www-data` permissions, and nginx manual configuration in the description column

### Feature
- `bot/live_bot.py` — new web status page: when `webstatuspage_html` is `true` in `config.json`, the bot writes a self-contained dark-themed HTML status page showing capital, total/daily PnL, win rate, open positions, and the 10 most recent resolved trades; the page is written every `DASHBOARD_INTERVAL` seconds (5 min) and immediately after each trade resolution; the directory is created automatically; the page carries a `<meta http-equiv="refresh" content="60">` tag for browser auto-refresh
- `bot/live_bot.py` — `.htaccess` / `.htpasswd` Basic Auth protection: if `webstatus_password` is set, `setup_htaccess()` writes a `.htpasswd` (Apache `{SHA}` format, no external dependencies) to `TRADINEBOTTE_DIR` (outside the web root) and a `.htaccess` referencing it in the HTML page directory; password changes are applied on the next page write; `.htaccess` is written once and not overwritten if already present to preserve manual edits
- `config.json.example` — four new optional keys: `webstatuspage_html` (bool, default `false`), `webstatuspage_path` (string, default `~/public_html/tradinebot_status.html`), `webstatus_user` (string, default `"tradinebot"`), `webstatus_password` (string, default `""`)

### Performance
- `bot/live_bot.py` — `_market_refresh_loop()` extracted as a background `asyncio.Task`: Gamma API polling (up to 15 s HTTP timeout every 90 s) no longer blocks WebSocket message processing; the `recv()` loop and market discovery now run concurrently within the single event loop
- `bot/live_bot.py` — `_run_ws()`: recv timeout reduced from 90 s to 30 s; `TimeoutError` now triggers `continue` instead of a full reconnect — `ping_interval=20` / `ping_timeout=10` keepalives detect dead connections; reconnect only fires when all tracked markets have expired; `finally` block guarantees cancellation of the refresh task on any WS disconnect
- `bot/live_bot.py` — `fetch_markets()`: `tag_id=102892` (Polymarket `5M` tag) added to `POLY_GAMMA_PARAMS` for server-side pre-filtering; the ±6-minute temporal window now returns ~12–20 markets instead of potentially thousands; pagination loop (up to 20 requests × 100 markets) replaced by a single API call; `BTC_5M_KEYWORDS` Python filter retained as a safety net
- `README.md` / `README.fr.md` — Notes section updated to reflect the 30 s timeout, background refresh task, and Gamma API tag filter

### Feature
- `scripts/backtest.py` — standalone backtest engine: replays `snapshots` table chronologically, applies configurable signal logic, and produces simulated trade statistics; supports `--sweep` grid-search mode (5×3×3×3 = 135 parameter combinations sorted by win rate), `--detail` per-trade table, and `--compare` to show actual bot results alongside the simulation; configurable via `Params` dataclass (`signal_threshold`, `entry_max`, `min_secs_remaining`, `min_ask_vol`, `win_threshold`, `loss_threshold`, `obi_reject_thresh`, `stake`, `daily_stop_loss`)
- `tests/test_backtest.py` — 28 tests for the backtest engine: `TestFeeHelper` (2), `TestRunBacktestBasic` (14, covering all signal guards and resolution paths), `TestRunBacktestMultiMarket` (4, independent markets, direction isolation, expiry resolution), `TestRunBacktestDailyStopLoss` (1), `TestRunBacktestParams` (3, threshold/win/loss sensitivity), `TestSummarize` (6, drawdown, win rate, open trade counting)
- `tests/test_bot.py` — automated test suite (71 tests, zero external services required): `TestComputeFee` (4), `TestParseBookMessage` (14), `TestMarketHelpers` (9), `TestTokenState` (7), `TestRegisterMarket` (5), `TestCheckSignal` (13 guards including all 8 entry conditions, daily stop-loss, and duplicate-entry prevention), `TestCheckResolution` (7), `TestCloseTrade` (6), `TestRestoreState` (5); all tests use an in-memory SQLite database and a fixed `TRADINEBOTTE_DIR=/tmp/polymarket-test` so they never touch production files
- `scripts/run_tests.sh` — test runner now suppresses Python 3.13 `ResourceWarning` for unclosed in-memory SQLite connections (`-W ignore::ResourceWarning`); total suite: 99 tests
- `config.json` / `config.json.example` — new optional key `db_mmap_mb` (integer, default `0`): when set to a non-zero value, activates `PRAGMA mmap_size` so SQLite memory-maps the database file via the kernel page cache; set to e.g. `256` for 256 MB
- `bot/live_bot.py` — `load_config()` refactored to return the full config dict (extensible for future options); `DB_MMAP_MB` derived from config at startup; `init_db()` applies the pragma and logs a confirmation line when mmap is active

### Documentation
- `README.md` — new "Database" section: SQLite/WAL rationale, full `trades` table schema (29 columns with type and description), `snapshots` table schema, and 4 annotated query examples
- `README.fr.md` — French translation of the new Database section
- `bot/live_bot.py` — module docstring translated to English; docstrings added to all functions and classes; inline comments explain non-obvious invariants: temporal filter rationale, all 8 signal guards (including `ask_vol=0` initialization guard and expired-market `best_ask>=1.0` guard), OBI formula, PnL calculation, sysconfig path resolution, lazy ClobClient import, exponential backoff, expired-market cleanup, WAL mode rationale
- `scripts/setup.py` — module docstring translated to English; inline comments explain the security decisions (getpass, exact-amount ERC-20 approvals), Uniswap V3 swap parameters (fee tier 100, 0.5% slippage guard, 5-min deadline), sysconfig dynamic path, API key ECDSA derivation, and chmod 600 rationale

### Feature
- `TRADINEBOTTE_DIR` environment variable now controls the install path across all scripts and the bot itself, defaulting to `/opt/polymarket-live`
- `scripts/install.sh` — accepts install directory as a positional argument or via `TRADINEBOTTE_DIR`; generates a `run.sh` wrapper in the install dir with the path pre-set
- `scripts/start_bot.sh` — reads `TRADINEBOTTE_DIR`, exports it when launching the bot
- `scripts/monitor.sh` — reads `TRADINEBOTTE_DIR` for log and database paths
- `scripts/setup.py` — reads `TRADINEBOTTE_DIR` for `config.json` path and venv site-packages; also fixes hardcoded Python 3.12 venv path (uses `sysconfig` like the bot)
- `bot/live_bot.py` — `DB_PATH`, `LOG_PATH`, `CONFIG_PATH` and venv lookup all derived from `TRADINEBOTTE_DIR`

### Documentation
- `INSTALL` — new English installation guide extracted from README.md (requirements, dependencies, wallet setup, configuration, running, monitoring, virtual environment testing)
- `INSTALL.fr` — French translation of the installation guide
- `README.md` — installation sections replaced by a reference to INSTALL
- `README.fr.md` — installation sections replaced by a reference to INSTALL.fr

---

## [2026-04-23]

### Security — `9e6247c`
**Apply security fixes from audit (4 vulns)**
- `scripts/setup.py` — clé privée lue via `getpass()` au lieu de `sys.argv[1]` : n'apparaît plus dans `ps aux` ni dans l'historique shell
- `scripts/setup.py` — suppression de tout affichage de credentials sur stdout (clé privée partielle, api_key, api_secret, api_passphrase) : la sortie n'affiche plus que l'adresse du wallet
- `scripts/setup.py` — remplacement des approbations ERC-20 illimitées (`2**256-1`) par des montants exacts : `amount_in` pour le swap Uniswap V3, `bal_e` pour le CTF Exchange

### Documentation — `6aa8360`
**Add virtual environment test instructions to README**
- `README.md` — ajout d'une section complète "Testing in a virtual environment" avec toutes les commandes : installation de `uv`, création du venv, vérification de syntaxe, import check, dry-run de 20 secondes, sortie attendue

### Bugfix — `3c0ad40`
**Fix indentation error in WS except clause and hardcoded Python version**
- `bot/live_bot.py` — correction d'une `IndentationError` sur le bloc `except:` dans `_run_ws()` (10 espaces au lieu de 12) qui empêchait le démarrage du bot
- `bot/live_bot.py` — le chemin des site-packages du venv était codé en dur pour Python 3.12 ; remplacé par `sysconfig.get_path()` qui résout le bon chemin dynamiquement selon la version Python installée

### Feature — `cbdbf2a`
**Move credentials from env vars to config.json**
- `bot/live_bot.py` — ajout de `CONFIG_PATH` et d'une fonction `load_config()` qui lit `/opt/polymarket-live/config.json` en priorité, avec fallback sur les variables d'environnement
- `scripts/setup.py` — écrit automatiquement `config.json` (chmod 600) après dérivation des clés API, au lieu d'afficher des `export` à copier manuellement
- `scripts/start_bot.sh` — suppression des `export` de variables ; vérifie l'existence de `config.json` au démarrage
- `config.json.example` — template de référence ajouté au dépôt
- `.gitignore` — `config.json` ajouté pour éviter tout commit accidentel de credentials

### Documentation — `f452161`
**Add comprehensive README**
- `README.md` — réécriture complète avec description de la stratégie, prérequis, dépendances, installation, configuration, monitoring et notes opérationnelles

### Documentation — `beed5e1`
**Add CLAUDE.md with architecture and operational guidance**
- `CLAUDE.md` — documentation de l'architecture pour Claude Code : commandes, flux de données, paramètres critiques à ne pas modifier, décisions de conception, chemins de déploiement

### CI — `d225a5f`
**Add Claude Code GitHub Actions workflow**
- `.github/workflows/claude.yml` — workflow permettant de déclencher Claude Code depuis les issues et pull requests GitHub

### Initial — `85886ea`
**Import base — bot de trading Polymarket v3**
- `bot/live_bot.py` — bot async complet (617 lignes) : state machine WebSocket, signal `best_bid >= 0.96`, placement d'ordres LIMIT via `py_clob_client`, résolution WIN/LOSS, persistance SQLite (WAL), reconnexion automatique avec backoff exponentiel
- `scripts/install.sh` — installation des dépendances système et création du venv `/opt/polymarket-live/venv`
- `scripts/setup.py` — setup wallet : vérification des balances, swap USDC natif → USDC.e via Uniswap V3, approbation CTF Exchange, dérivation des clés API Polymarket
- `scripts/start_bot.sh` — script de lancement avec vérification des prérequis et gestion d'instance unique
- `scripts/monitor.sh` — dashboard de monitoring : statut du processus, logs temps réel, stats SQLite (trades, win rate, PnL)
- `docs/CONTEXT_AI.md` — documentation technique complète : stratégie, architecture, historique des bugs corrigés, résultats de backtest (1663 trades, 98.3% win rate), checklist de déploiement

### Initial — `7c119fd`
**Initial commit**
- Initialisation du dépôt git
