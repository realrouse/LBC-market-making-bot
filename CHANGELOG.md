# Changelog

> 🇫🇷 [Version française](CHANGELOG.fr.md)

All notable changes to this project are documented here.

---

## [Unreleased]

### Improvement
- **`scripts/install.sh`** — no longer calls `apt-get` directly; instead detects missing system packages (`python3`, `python3-venv`, `python3.X-venv`, `sqlite3`) and prints the exact `sudo apt-get install` command with the auto-detected Python version; exits with an error if anything is missing, continues silently if all present; docs updated across INSTALL, QUICKSTART, README (EN + FR)

### Code Quality
- `bot/account_bot.py` — pylint 10/10: removed unused `json` import; `open(lock_file)` and `open(log_path)` now carry explicit encoding (`utf-8`) or binary mode (`ab`); intentional non-`with` usages annotated with `# pylint: disable=consider-using-with`; `# pylint: disable=duplicate-code` added at module level (market-expiry purge loop mirrors `feed.py` by design)
- `bot/account_bot.py` — ResourceWarning fixed: `_run()` now wraps its event loop in `try/finally` to call `sock.close(linger=0)` and `ctx.term()` on cancellation; ZMQ socket and context are guaranteed to be released when the task is cancelled during tests or on shutdown

### Testing
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
