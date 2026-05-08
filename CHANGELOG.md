# Changelog

> 🇫🇷 [Version française](CHANGELOG.fr.md)

All notable changes to this project are documented here.

---

## [Unreleased]

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

### Added
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
