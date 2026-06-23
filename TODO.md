# TODO — Future improvements

> Code, comments, logs, and docstrings are English-only. Documentation files (README, CHANGELOG, INSTALL, QUICKSTART, UPDATE) are bilingual (EN + FR).

---

## Audit 2026-05-17 — Findings to fix
> Full audit report: `audit170526.md`

### CRITICAL — Done

- **[C-1] DONE — Trail mode implemented in `bot/strategies/grid.py`**
  `trail_mode` reads `"bull"` / `"bear"` / `"static"` from JSON; `_recenter_grid()` cancels all
  orders and shifts bounds centered on current price; stop-loss check branches per mode; bounds
  restored from DB on restart. Tests: `TestCheckStopLoss`, `TestRecenterGrid`. (branch `dev`)

- **[C-2] DONE — Connector/strategy compatibility check at startup**
  `validate(connector_module, strategy_type)` in `bot/connectors/__init__.py` raises `RuntimeError`
  listing all missing methods if the connector does not expose the interface required by the strategy.
  Called in `main()` before strategy instantiation. Tests: `TestConnectorValidate`. (branch `dev`)

### HIGH

- **[H-1] DONE — Ghost trade guard in `enter_live_trade()`**
  In live mode (`session + private_key`), `enter_live_trade()` now returns early after incrementing
  `api_fail_streak` when `post_order` returns `None`, preventing ghost rows with `order_id=NULL`.
  Simulation mode (no `private_key`) still inserts with `oid=None` as before.
  3 regression tests added: `test_no_db_insert_on_live_clob_failure`,
  `test_db_insert_on_live_clob_success`, `test_db_insert_in_simulation_no_session`. (branch `dev`)
  Note: crash-between-post_order-and-commit (Mode A) not yet addressed — requires `pending` status.

- **[H-2] DONE — All French strings translated to English**
  All violations of the mandatory language policy are now fixed across the codebase:
  `bot/api_binance.py`, `bot/strategies/grid.py` (previous session) +
  `bot/api_polymarket.py` (2 strings), `bot/feed.py` (2 strings), `bot/bot_utils.py` (2 strings),
  `bot/api_mexc.py` (13 occurrences — `erreur` → `error` replace-all). 639 tests pass. (branch `dev`)
  Also fixes M-4: CLOB response now logged as `keys=%s` instead of full body.

### MEDIUM — Security

- **[M-1] DONE — Real OS usernames removed from `scripts/test_multibot.conf.example`**
  Real server account names replaced with generic placeholders (`user1 user2 user3`);
  role comment updated accordingly. Pre-commit hook extended: each username now generates two
  patterns — `$u@` (with `@`) and `\b$u\b` (bare word-boundary). (branch `dev`)

- **[M-2] DONE — Symlink TOCTOU fixed on feed log file in `/tmp`**
  `bot/account_bot.py:150` — `open(log_path, "ab")` replaced with
  `os.open(..., O_CREAT|O_WRONLY|O_APPEND|O_NOFOLLOW, 0o644)` + `os.fdopen()`; parent closes
  its fd after `Popen`, child keeps it open. Consistent with the lock file (line 125). (branch `dev`)

- **[M-3] DONE — `bcrypt` added to `requirements.txt`**
  `bcrypt` now in `requirements.txt` (line 6); `bot_utils.py` uses `bcrypt.hashpw` by default,
  SHA-1 fallback logs a warning. (branch `dev`)

- **[M-4] DONE — CLOB response truncated in log** *(fixed with H-2)*
  `bot/api_polymarket.py:271` now logs `keys=%s` instead of the full response body. (branch `dev`)

### MEDIUM — Architecture

- **[M-5] DONE — Test coverage added for `ws_loop`, `_market_refresh_loop`, `purge_expired_markets`**
  11 new tests in `tests/test_bot.py`: `TestPurgeExpiredMarkets` (5 tests — expired removal,
  active retention, open-trade guard, signal cleared on purge, mixed tokens); `TestWsLoopBackoff`
  (3 tests — doubling, cap at 60 s, reset on success); `TestMarketRefreshLoop` (3 tests — new
  market registration + subscription, expired purge, API error resilience). Total: 659 tests.
  (branch `dev`)

- **[M-6] DONE — User stream task no longer re-spawned after credential failure**
  `bot/strategies/grid.py` — `_no_credentials: bool = False` added to `__init__`; set to `True`
  in `_user_stream_loop` after `MAX_KEY_FAILURES` (3) consecutive failures; `on_book_update()`
  spawn guard now checks `not self._no_credentials` first, preventing infinite task recreation.
  3 tests added in `TestNoCredentialsFlag`. (branch `dev`)

- **[M-7] DONE — SQLite snapshot commits batched (one per 30 s)**
  `save_snapshot()` no longer calls `conn.commit()` — it only does the fast `execute()`.
  `handle_book_update()` flushes once every `SNAPSHOT_COMMIT_SECS = 30` seconds, keeping all
  SQLite operations on the event loop thread (no executor, no thread-safety concerns).
  3 tests added: `test_no_commit_after_save_snapshot`, `test_batch_commit_fires_after_interval`,
  `test_batch_commit_deferred_within_interval`. (branch `dev`)

### LOW — Deployment / Code

- **[L-1] DONE — `install.sh` now copies all grid/connector packages**
  Added to "Copying bot files" section: `api_binance.py`, `api_mexc.py` (flat copy);
  `bot/connectors/__init__.py` → `$INSTALL_DIR/connectors/`; `bot/strategies/__init__.py`
  and `bot/strategies/grid.py` → `$INSTALL_DIR/strategies/` (alongside JSON files).
  Syntax check loop extended to cover all 8 Python files. (branch `dev`)

- **[L-2] DONE — CLAUDE.md line count updated for `live_bot.py`**
  `~617 lines` → `~1530 lines` (actual: 1531). (branch `dev`)

- **[L-3] DONE — RSI docstring corrected in `bot/indicators.py`**
  `compute_rsi` docstring changed from `"Wilder RSI(n)"` to `"Cutler RSI(n): simple-mean
  gains/losses over the last n bars"` — accurately describing the `sum(...) / n` implementation.
  (branch `dev`)

- **[L-4] DONE — Binance/MEXC error responses truncated in logs**
  8 log calls across `bot/api_binance.py` (lines 252, 294, 334, 373) and `bot/api_mexc.py`
  (lines 268, 310, 349, 385) changed from `%s` to `%.300s` on the `data` argument.
  (branch `dev`)

- **[L-5] DONE — Session-level `ClientTimeout(total=30)` added to all three `ClientSession` calls**
  `bot/live_bot.py:1511`, `bot/feed.py:283`, `bot/account_bot.py:377` — all three session
  instantiations now pass `timeout=aiohttp.ClientTimeout(total=30)` as a safety net against
  future requests omitting a per-request timeout. (branch `dev`)

- **[L-6] DONE — `ssh-keyscan` already guarded in all 5 deploy scripts** *(already fixed)*
  All scripts already have `if ! ssh-keygen -F "[$SERVER]:$PORT" &>/dev/null && ! ssh-keygen -F
  "$SERVER" &>/dev/null; then ssh-keyscan ...` — the check was present before the audit.
  No code change needed.

- **[L-7] DONE — f-string PRAGMA SQL replaced in `scripts/profile_compare.py:58`**
  `c.execute(f"PRAGMA mmap_size = {MMAP_MB * 1024 * 1024};")` →
  `c.execute("PRAGMA mmap_size = ?", (MMAP_MB * 1024 * 1024,))`. (branch `dev`)

### INFO — No immediate action required

- **[I-1] ZMQ without CURVE/ZAP authentication** — mitigated by loopback-only binding. Required
  before any external network exposure (see Roadmap v0.3 section below).

- **[I-2] DONE — Move tracked `.db` files out of git** — removed `!data/*.db` exceptions from
  `.gitignore`; four sample databases untracked with `git rm --cached`; all `.db` files now
  uniformly ignored. (branch `dev`, v0.55)

- **[I-3] DONE — Pin GitHub Actions to SHA** — all five workflow files (`claude.yml`, `tests.yml`,
  `pylint.yml`, `mypy.yml`, `audit.yml`) now pin `actions/checkout`, `actions/setup-python`, and
  `anthropics/claude-code-action` to their exact commit SHAs; tag kept as comment. (branch `dev`, v0.55)

---

## Security review 2026-06-08 — Findings to fix

### CRITICAL — Open

- **[C-2] DONE — Private key replaced by SHA-256 digest as cache key**
  `api_polymarket.py` — `cache_key = (private_key, install_dir)` replaced with
  `cache_key = (hashlib.sha256(private_key.encode()).hexdigest()[:16], install_dir)`.
  Raw key material is no longer stored as a Python dict key. (dev branch, audit session 2026-06-12)

### HIGH — Open

- **[H-2]** ZeroMQ sockets have no authentication on multi-tenant host
  `feed.py` and `indicators.py` bind PUB/REP sockets on `127.0.0.1` without CURVE or ZAP auth.
  On a multi-tenant VPS any local process can subscribe to the raw market data stream and send
  commands to the REP socket. Fix: enable CURVE auth or add a ZAP handler before exposing data
  to multiple user accounts.

- **[H-3]** SSH passwords appear in `/proc` and remote heredoc
  Deploy scripts pass SSH credentials via environment variables or heredocs that briefly appear in
  `/proc/<pid>/cmdline`. Fix: use SSH key-based auth for all deploy operations; eliminate password
  arguments from command lines.

### LOW — Open

- **[L-1]** Private key stored as plain `str` in `BotConfig` (unzeroable)
  Python strings are immutable and cannot be securely zeroed. The private key in `BotConfig.private_key`
  remains in memory until garbage-collected. Fix: use a `bytearray` or a `SecretStr` wrapper that
  can be explicitly zeroed after use, and clear on bot shutdown.

- **[+]** Add `|` guard to `install_service.sh`, `install_feed_service.sh`, `install_indicators_service.sh`
  Pipe (`|`) in service unit names can break `systemctl enable`; add a guard that aborts with a
  clear error if the computed service name contains `|`.

### LOW — Done

- **[L-2] DONE** — Remove dead `PASS=` line from remote heredoc in `update_standalone.sh`
  Dead `PASS=` variable removed to eliminate credential exposure in heredoc.

- **[L-3] DONE** — Convert `_has_creds` from module-level bool to function in `api_bitstamp.py`
  Module-level `_has_creds` was evaluated at import time before env vars were set.
  Replaced with a `_has_creds()` function called at runtime.

---

## Backtest analysis 2026-06-08 — Completed

All 7 analysis tasks from the production-database backtest session are done.
Full results and parameter recommendations in `notes/backtest_20260608.txt`.

- **[B-1] DONE** — Polymarket baseline + walk-forward (c2 5M primary)
  Confirmed: `thr=0.95 secs=30 obi=-0.25 dsl=$50` — Sharpe 10.10, WR 99.5%. Stable.

- **[B-2] DONE** — Polymarket vol filter + stake sizing (c2)
  `vol_bid <= 0.03` filter: +43% EV/trade, +$40 PnL. Step stake $15/$12/$10/$8: Sharpe +2 pts.
  Both need OOS walk-forward before live deployment.

- **[B-3] DONE** — OB scalping sweep (c4, 576 combos)
  All negative — 92% timeout exits. Strategy has no detectable edge on current data.
  **Do not deploy orderbook_bot in live mode with current params.**

- **[B-4] DONE** — CEX grid/swing backtests (3 regimes: bull 2024 / bear 2022 / range 2026)
  Grid: ±15% / 20 levels / trail=bear most robust across regimes (Calmar 1.71).
  Swing: only profitable in bull market. DCA consistently positive across all regimes.

- **[B-5] DONE** — `--sweep-all` contamination identified and documented
  CEX databases included in sweep skew OBI recommendation to -0.75; c2 alone gives Sharpe 6.4
  vs 10.1 with current -0.25. Warning added to `docs/HOWTO_tests_and_backtests.md`.

- **[B-6] DONE** — Accumulation strategy parameter improvements validated OOS
  `btc_accumulation.json` → v2.0: `max_invested_pct` 0.90→0.65, new `max_avg_entry_mult=1.20`
  guard, `sell_fraction` 0.15→0.10, `rebuy_max_age_days` 30→60.
  OOS test (2026-01 → 2026-06): Alpha +6.0% → +9.8%, drawdown -21.6% → -17.8%.

- **[B-7] DONE** — All backtest scripts smoke-tested + docs updated (bilingual)
  9 scripts verified (all pass `--help`). Section 12 added to `HOWTO_tests_and_backtests.md/.fr.md`
  covering the 8 non-Polymarket scripts.

---

## Audit 2026-06-05 — Findings to fix

### HIGH — Done

- **[A05-H1] DONE — Cache `ClobClient` across trades** (`api_polymarket.py`)
  `post_order` was rebuilding an authenticated ClobClient on every order, triggering EIP-712 key
  derivation each time. The client is now initialised once via `_init_clob_client()` and cached in
  `_clob_clients` by `(private_key, install_dir)`. `sys.path` injection moved inside the one-time
  init block. (v0.57)

- **[A05-H2] DONE — Stable feed lock path across processes** (`account_bot.py`)
  `abs(hash(addr))` is randomised per process since Python 3.3+; two concurrent account_bots
  computed different lock paths for the same feed address, silently breaking coordination.
  Replaced with `_feed_id(addr) = hashlib.md5(addr.encode()).hexdigest()[:8]`. (v0.57)

### MEDIUM — Done

- **[A05-M3] DONE — Remove dead `warn_if_external_bind` from `bot_utils`**
  Duplicate of `tradinetools.zmq.warn_if_external_bind`; no callers remained in production code
  (all callers import from tradinetools directly). Function deleted. (v0.57)

- **[A05-M1] DONE — Pass `config.vol_window` to `TokenState.__init__`**
  `live_bot.py` and `account_bot.py` — `TokenState.__init__` now accepts `vol_window: int = VOL_WINDOW`
  keyword arg; `register_market()` and `_register_from_market_msg()` both pass
  `vol_window=state.config.vol_window`. `deque(maxlen=vol_window)` now reflects the JSON config
  instead of the hardcoded constant. (dev branch, audit session 2026-06-12)

- **[A05-M5] DONE — Weekly stop-loss boundary already Monday-aligned**
  Code audit (2026-06-12) confirmed `live_bot.py` already uses
  `today_week = (_dt - timedelta(days=_dt.weekday())).toordinal()` — ISO-week Monday boundary,
  not the epoch-Thursday window cited in the original finding. No code change required.

### MEDIUM — Open

- **[A05-M2] DONE — Capital guard uses worst-case effective stake**
  `live_bot.py` — replaced `cfg.stake` with
  `min(cfg.stake_max, stake_max_pct_capital × capital) if pct > 0 else cfg.stake_max`,
  mirroring the pattern already used in `compute_stake()`. Guard now assumes the maximum
  possible stake per open trade, preventing entry when capital would be exhausted under
  dynamic scaling. 360 tests pass. (dev branch, 2026-06-12)

- **[A05-M4] DONE (partial) — `MarketMessage` and `PingMessage` migrated to production callers**
  `feed.py` now uses `MarketMessage(...).to_dict()` and `PingMessage(...).to_dict()` for all
  publish sites; `account_bot.py._register_from_market_msg` uses `MarketMessage.from_dict()`.
  Wire format gains `"v": 1` (backward-compatible — all consumers use `.get()`).
  Intentionally skipped: `BookMessage` (hot path — `get_type_hints()` overhead per tick);
  `IndicatorsMessage` (14+ polymorphic stream types, marginal gain).
  Stale schemas — **cannot migrate without breaking the live protocol**: `RegisterRequest` /
  `RegisterReply` define `{t, stream_id, bot_id}` but the live protocol uses
  `{cmd, asset, timeframe, source, indicators}` / `{status, stream_id}`. These need to either
  be corrected to match the live protocol or deleted. (dev branch, audit session 2026-06-12)

- **[A05-M6] `sys.path.insert` inside `post_order` — already mitigated by H-1 cache**
  The `if _site not in sys.path` guard in `_init_clob_client` prevents duplicate inserts.
  Residual concern: `sysconfig` / `sys` are now top-level imports in `api_polymarket.py` (added
  with H-1). No further action required unless the lazy-import approach is revisited.

### LOW — Done

- **[A05-L3] DONE — `account_bot.py` error message corrected to `systemctl --user`**
  Hardcoded `(sudo systemctl start tradinebotte-feed)` replaced with
  `(systemctl --user start tradinebotte-feed)`. (dev branch, audit session 2026-06-12)

### LOW — Open

- **[A05-L1] DONE — `bot_utils` module globals removed; config passed explicitly**
  Removed `WEBSTATUS_ENABLED / PATH / USER / PASSWORD` and `INSTALL_DIR` module-level globals
  from `bot_utils.py`. `write_web_status(state, config)`, `setup_htaccess(html_path, config)`,
  and `print_dashboard(state, config)` now receive `config: Any` directly. `make_config()` in
  `live_bot.py` no longer mutates module state — the 5 injection lines removed. Call sites
  updated: `write_web_status(state, state.config)` and `print_dashboard(state, state.config)`.
  (dev branch, audit session 2026-06-12)

- **[A05-L2] DONE — Status HTML and dashboard log timestamps now UTC**
  `bot_utils.py` — added `tz=timezone.utc` to all three non-UTC calls: `datetime.now()` in
  `print_dashboard` (log line), `datetime.fromtimestamp(ts_ms / 1000)` in
  `_status_html_trade_rows` (trade table), and `datetime.now()` in `generate_status_html`
  (page header/footer). HTML table header updated to "Time (UTC)"; footer label updated to
  "Last updated: … UTC". (dev branch, audit session 2026-06-12)

- **[A05-L4] DONE — `BotConfig` hour-filter range annotations typed**
  `live_bot.py` — `weekday_utc_ranges: list` and `weekend_utc_ranges: list` changed to
  `list[tuple[int, int]]`, matching the module-level constant declarations at lines 76-77.
  (dev branch, audit session 2026-06-12)

---

## Logging system — deferred items (priorities 3–4)

These were scoped out of the log-system refactor session (priorities 1+2 + English unification were implemented):

- **3a — JSON log mode**: Add `--log-json` flag to `live_bot.py` to emit newline-delimited JSON records
  (`{"ts":…,"level":…,"session":…,"msg":…}`) instead of plain text. Useful for ingestion into
  Elasticsearch / Loki / Datadog without a log parser.

- **3b — DONE — Standardize tag prefixes**: audited all `[TAG]` prefixes across the four bot
  modules; standardized four unbracketed structured lines in `live_bot.py` (`[VOL_FILTER]`,
  `[KELLY]`, `[CIRCUIT_BREAKER]`, `[GHOST_GUARD]`); removed spurious `[VERBOSE]` and `[WS ERROR]`
  tags from `feed.py`; canonical vocabulary published in `docs/logging.md`. (branch `dev`, v0.52)

- **4a — DONE — OBI + ask_vol in trade entry log**: added `obi=%.3f ask_vol=%.0f` to the `▶ TRADE`
  line in `enter_live_trade()`. (branch `dev`, v0.56)

- **4b — DONE — Trade duration in resolution log**: added `duration=%ds` (from `signal_ts_ms` to
  `resolution_ts_ms`) to `✓ WIN` / `✗ LOSS` lines in `close_trade()`. (branch `dev`, v0.56)

## Roadmap v0.3

### Operations / infrastructure

- **External network broadcast for the indicator server** — `feed.py` and `indicators.py` currently
  bind to `127.0.0.1` (loopback only). Allow broadcasting on an external interface (`0.0.0.0` or a
  dedicated IP) so a bot running on another machine can subscribe.

  Prerequisites before enabling:
  - **ZMQ authentication** — enable CURVE or plain auth (ZAP) to avoid exposing the raw data stream
    to arbitrary hosts on the network.
  - **TLS** — ZMQ CURVE provides point-to-point encryption without a proxy; otherwise tunnel through
    SSH (`-L`) or WireGuard.
  - **Configurable bind address** — replace the hardcoded `127.0.0.1` with a
    `TRADINEBOTTE_BIND_ADDR` env var (default: `127.0.0.1` to preserve current behavior) in
    `feed.py` and `indicators.py`.
  - **Firewall** — document the iptables/nftables rules to open (PUB and REP ports per service) and
    those to keep closed by default.
  - **`TRADINEBOTTE_PORT_BASE`** is already multi-stack compatible; this change slots in naturally.

- **Telegram notifications** — alert on each trade, daily stop-loss trigger, WebSocket reconnect.
- **HTTP health-check** — lightweight local server (e.g. port 9090) returning raw stats; monitorable
  from a reverse proxy or an external cron.
  > `aiohttp.web` on `127.0.0.1:8765`, `GET /health` → `{"status":"ok","capital":…,"wins":…,"losses":…,"open_trades":…,"uptime_s":…}`

### Strategy / risk management

- ~~**Dynamic position sizing** — fractional Kelly on stake size instead of fixed $10; adapts risk to
  signal confidence.~~ ✓ Done (v0.5.1): `kelly_fraction` / `kelly_min_trades` in `BotConfig`; `compute_stake()` Kelly path; 8 tests added.
- ~~**Weekday vol filter (Priority 1)**~~ ✓ Done (2026-05-19): `vol_filter_enabled=true` /
  `vol_filter_weekday_only=true` now explicit in `strategies/polymarket_BTC5M.json`; `make_config()`
  reads `"vol_filter"` JSON section; startup log added. Backtest: Sharpe +2.67 → +6.76.
- ~~**Step-function stake (Priority 2)**~~ ✓ Done (2026-05-19): `stake_step_enabled/s0/s1/s2/s3` in
  `BotConfig`; step path added to `compute_stake()` (priority: Kelly > step > bid×secs > flat);
  JSON: `"stake_step": {"enabled": true, "s0": 15, "s1": 12, "s2": 6, "s3": 6}`.
  Best Sharpe config from Curve B grid: Sharpe +8.40, DD $38.57, +$118 PnL vs flat $80.
- **Weekly stop-loss** — complement to the daily stop-loss to limit multi-day drawdown streaks.
- **Threshold=0.98 investigation** — walk-forward consistently selects `thr=0.98` over `0.95` in
  training folds. Validate OOS when ≥8 weeks of live data are available (current: 3 weeks,
  walk-forward too noisy). Do not change without running `backtest.py --walk-forward 4`.
- **Curve A (bid×secs) alternative** — `bid_α=1.0 secs_ref=45 secs_α=1.00 vol=weekday` gives
  Sharpe +7.85 vs Curve B's +8.40 but EV is lower ($0.0344 vs $0.0509). Revisit if step-function
  produces unexpected behaviour in live data (e.g. if secs bucketing introduces noise).
- **Kelly live validation** — Curve C (Kelly/bucket) showed Sharpe +12.02 in-sample. Requires
  out-of-sample walk-forward on ≥8 weeks of data before enabling live (`kelly_fraction > 0`).
  Current 3-week dataset is insufficient for robust (p, b) estimation per bucket.

### Technical indicators — indicators.py

The following sources are computable from the Binance klines already integrated, without new network
dependencies. They require new entries in `_VALID_INDICATOR_TYPES` and implementation in
`PriceSeries.compute_indicators`.

- **MACD** (12/26/9) — EMA 12 / EMA 26 crossover; signal = EMA 9 of MACD.
  Published fields: `macd`, `macd_signal`, `macd_hist`.
- **Bollinger Bands** (20, ±2σ) — `bb_upper`, `bb_lower`, `bb_width`.
  Width (`bb_width = (upper - lower) / middle`) measures the volatility regime.
- **VWAP** — intraday Volume Weighted Average Price. Requires volume from klines (`v` field in
  Binance kline WebSocket). Published field: `vwap`.
- **Stochastic RSI** — RSI of RSI, more reactive at short timeframes.
  `stoch_rsi_k = (rsi - min_rsi) / (max_rsi - min_rsi)`, smoothed over 3 periods.
  Fields: `stoch_rsi_k`, `stoch_rsi_d`.

### Backtest / analysis

- **Sharpe / Sortino ratio** — metrics missing from the current report; important for comparing
  strategies on a risk-adjusted basis.
- **Walk-forward optimization** — train on N weeks, validate on the next, slide the window; reduces
  overfitting risk from the `--sweep`.

### Time-scaled stake sizing (stake ∝ secs_remaining)

**Hypothesis**: win rate is higher when fewer seconds remain at signal time — the price is more
"locked in" and BTC has less time to reverse. A stake that decreases with secs_remaining should
improve EV without increasing overall risk.

The `signal_secs_remaining` column is already stored in every `trades` row (paper3.db, liveweek.db).

#### Phase 1 — Validate the hypothesis (DONE: see `scripts/analyze_stake_secs.py`)
- SQL bucketing of trades by `signal_secs_remaining` (30–45s, 45–60s, 60–90s, 90–120s, 120s+)
- Win rate, average EV, and trade count per bucket
- **Gate**: proceed only if win rate shows a meaningful drop (≥ 1 pp) across buckets

#### Phase 2 — Define stake curve candidates
Three families to grid-search:
- **A. Inverse-proportional**: `stake = min(stake_max, base_stake * ref_secs / secs_remaining)`
  — intuitive, continuous; `ref_secs` anchors the nominal stake at a chosen time.
- **B. Step function** (3–4 thresholds): e.g. `<45s → $12`, `45–60s → $10`, `60–90s → $7`, `90s+ → $5`
  — operationally transparent, no floating-point math on the hot path.
- **C. Half-Kelly per bucket**: `p = WR(bucket)`, `b = avg_payout_ratio`, `kelly = (p*b-(1-p))/b`;
  stake = `0.5 * kelly * capital` — theoretically optimal but requires fresh WR estimates per bucket.

#### Phase 3 — Grid search on paper3.db / liveweek.db
Replay all resolved trades chronologically with each curve variant.
Report: total PnL, per-day Sharpe, max drawdown, and comparison vs flat $10.
Key sweep params for curve A: `ref_secs ∈ {30,45,60}`, `stake_max ∈ {10,15,20}`.

#### Phase 4 — Implementation
If Phase 3 shows a clear winner:
- Add `stake_time_scaling: bool`, `stake_ref_secs: float`, `stake_max: float` to `BotConfig`
- Add `compute_stake(cfg, secs_remaining) → float` helper used in `enter_live_trade()`
- Expose params in the strategy JSON; validate with 1-week paper run before going live

---

## Alternative exchanges

- **Kalshi** — binary event markets (US), documented REST+WS API, structure very close to
  Polymarket (binary CLOB, YES/NO resolution). Priority candidate for a second `api_kalshi.py`.

- **MEXC Prediction Markets** — their "Prediction Markets" product (beta March 2026) is structurally
  similar to Polymarket but has no public API documented yet. Revisit when the API ships.
  Note: their spot/futures WebSocket uses protobuf (not JSON).

## Market discovery

- **Predictive polling** (option 2) — instead of polling every 30 s, compute the exact time the
  next market enters the ±6 min window (`next_boundary = ceil(now/300)*300 - 360`) and schedule a
  targeted poll. Eliminates the residual 30 s lag without extra API load.

---

## Backtest system audit 2026-06-23 — Findings

Audit of the 8 `analysis/backtest_*.py` scripts. Lens: can the backtests be trusted to
validate a strategy before it trades? Prioritized by trustworthiness.

### Done

- **[BT-1] DONE — Shared realized-PnL math; kill live↔backtest drift** (commit 5889029, dev)
  Every backtest reimplemented its PnL/fee math independently of the live engines, and they had
  silently diverged (the swing SL fee bug: live omitted a fee leg, backtest didn't). Extracted
  `tradinetools/pnl.py::round_trip_pnl` (both fee legs always charged), routed all 7 live-engine
  PnL sites + the swing/DCA/SwingHold backtests through it. Fixed a second real bug: those
  backtests' `realized_pnl` deducted only the SELL fee (cost basis excluded the buy fee),
  overstating PnL by one leg (range-DB DCA realized +78.22 → +75.52; portfolio unchanged). Grid
  was already correct (`gross − total_fee`). Parity test (`test_pnl_parity.py`) locks
  round_trip_pnl ≡ live assembly ≡ backtest model so future drift fails CI.

### Open — by priority

- **[BT-2] Signal-tape record/replay so the accumulation (and other gated) strategies are
  backtestable.** The flagship accumulation strategy (OBI dip-buy + 6 macro gates: F&G,
  liquidations, L/S ratio, RSI 4h, macro-OBI, VWAP) has NO backtest, because its inputs are the
  live indicators *streams*, which are never recorded historically. Fix: record the indicator
  stream messages to a "signal tape" (the orderbook/scalping backtests already replay live-recorded
  OBI/TFI from `ob_snapshots` — same idea, extended to all streams), then replay the tape to drive
  the accumulation strategy against its real inputs. Highest-value coverage gap.

- **[BT-3] Enforced out-of-sample / regime reporting.** bull / bear / range datasets exist and the
  tooling supports them (`backtest_grid.py --all`, `--trail bull/bear`) but it's opt-in — no
  discipline that a strategy is reported across all three before deployment. Add a wrapper/convention
  that runs each price strategy across all regime DBs and prints one comparison table (a "+5% on
  range" headline is meaningless without the bear number).

- **[BT-4] Overfitting discipline for the calibration studies.** `backtest_stake_secs.py` and
  `backtest_volfilter.py` are grid searches over thresholds (calibrated in `volstop.txt`). Mined
  params were NOT found in any live strategy config (appear confined to the studies / the disabled
  order-book scalper), so this is currently methodology not live risk — but: keep mined params out
  of live configs unless validated out-of-sample; report train-vs-holdout for any grid search.

- **[BT-5] Shared backtest harness (lowest priority — cosmetic).** Data loading and metrics
  (Sharpe/Calmar/MaxDD/CAGR) are re-defined per file with inconsistent formulas; fees are a single
  `fee_rate` with no maker/taker or per-exchange split (relevant now MEXC spot is maker-0%/taker-0.2%).
  Add a small shared `backtest_common` (OHLCV loader + one metrics module + a fee schedule), adopt
  incrementally. Also: `docs/HOWTO_tests_and_backtests.md` is heavily Polymarket-centric — the CEX
  backtests are thinly documented there.
