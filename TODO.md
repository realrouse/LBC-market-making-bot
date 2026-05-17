# TODO — Future improvements

> Code, comments, logs, and docstrings are English-only. Documentation files (README, CHANGELOG, INSTALL, QUICKSTART, UPDATE) are bilingual (EN + FR).

---

## Audit 2026-05-17 — Findings to fix
> Full audit report: `audit170526.md`

### CRITICAL

- **[C-1] Implement trail mode in `bot/strategies/grid.py`**
  `_check_stop_loss()` halts the bot in both directions (price < lower OR price > upper) — no
  re-centering branch exists. The `trail_mode` key in `grid_BTCUSDT_bull_trailing.json` is never
  read. Bull and bear trailing JSON files produce identical static grids.
  Fix: read `trail_mode` in `make_config()`, add upward re-center logic in `GridStrategy` when
  `price > grid_upper` (bull) or downward when `price < grid_lower` (bear). (~50 lines)

- **[C-2] Validate connector/strategy compatibility at startup**
  `grid.py` calls 7 methods absent from `api_polymarket.py` (`get_open_orders`, `cancel_order`,
  `get_order_status`, `get_listen_key`, `keepalive_listen_key`, `make_user_stream_url`,
  `parse_user_stream_msg`). Loading `connector=polymarket` + `strategy_type=grid` crashes with
  `AttributeError` at `grid.py:307`.
  Fix: add a compatibility matrix in `bot/connectors/__init__.py` or `bot/strategies/__init__.py`
  that checks the connector exposes the required interface and raises a clear error at startup.
  (~10 lines)

### HIGH

- **[H-1] Guard `post_order` → early return if `None`; make entry atomic**
  In `enter_live_trade()` (`live_bot.py:1078-1106`): if `post_order` returns `None` (CLOB error),
  execution falls through to the SQLite INSERT, creating a ghost row with `order_id=NULL` that
  permanently locks `cfg.stake` of capital. On crash between `post_order` and `conn.commit()`, a
  duplicate order may be placed on restart.
  Fix: add `if order_id is None: return` after `post_order`; consider a `pending` DB status set
  before sending the order, updated to `open` on success. (~5 lines)

- **[H-2] Translate remaining French strings to English**
  Violations of the mandatory language policy (CLAUDE.md §Language policy):
  - `bot/api_polymarket.py:274` — `"Erreur CLOB : %s"` → `"CLOB error: %s"`
  - `bot/strategies/grid.py:474` — `"hors [%.2f, %.2f]"` → `"outside [%.2f, %.2f]"`
  - `bot/feed.py:259` — `"book updates publies"` → `"book updates published"`
  - `bot/feed.py:292` — `"[WS ERREUR]"` → `"[WS ERROR]"`
  - `bot/bot_utils.py:90` — `"htaccess cree"` → `"htaccess created"`
  - `bot/bot_utils.py:198` — `"Erreur page web statut"` → `"Web status page error"`
  - `bot/api_mexc.py` — 13 occurrences of `"MEXC API erreur"`, `"MEXC fetch erreur"`, etc.
  (~20 lines total)

### MEDIUM — Security

- **[M-1] Replace real OS usernames in `scripts/test_multibot.conf.example`**
  Lines 18-20 contain `TEST_USERS=(claude1 claude2 claude3)` — real server account names.
  The pre-commit hook blocks the `username@` form (with `@`) but not the bare username form.
  Fix: replace with `TEST_USERS=(user1 user2 user3)`; extend hook PATTERNS to block bare usernames.
  (1 line + hook extension)

- **[M-2] Fix symlink TOCTOU on feed log file in `/tmp`**
  `bot/account_bot.py:150` opens the feed log with `open(log_path, "ab")` in a world-writable
  `/tmp` directory. The lock file correctly uses `O_NOFOLLOW` (line 125) but the log file does not.
  Fix: `os.open(log_path, os.O_CREAT|os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW, 0o644)` then
  `os.fdopen(...)`. (4 lines)

- **[M-3] Add `bcrypt>=4.0` to `requirements.txt`**
  `bot/bot_utils.py:58-67` falls back to unsalted SHA-1 when `bcrypt` is absent. `bcrypt` is not
  in `requirements.txt`, so SHA-1 is the default on a fresh install.
  Fix: add `bcrypt>=4.0` to `requirements.txt`. (1 line)

- **[M-4] Truncate full CLOB response in log**
  `bot/api_polymarket.py:271` logs the complete API response body on unexpected answers.
  Fix: `logger.warning("CLOB resp without orderID: keys=%s", list(_resp.keys()))`. (1 line)

### MEDIUM — Architecture

- **[M-5] Improve test coverage on critical paths**
  Not covered: `ws_loop`/reconnect logic, `_market_refresh_loop`/`purge_expired_markets`,
  `grid.restore_from_db` offline reconciliation, `_user_stream_loop` retry, `bot/feed.py` (entire),
  `bot/account_bot.py` (entire), `bot/indicators.py` (entire).

- **[M-6] Fix user stream task re-spawned ~500×/s when no credentials**
  `bot/strategies/grid.py:675-686`: when `BINANCE_API_KEY` is absent, `_user_stream_loop` exits
  immediately but `on_book_update()` recreates the task on every message (~500/s at 100ms cadence).
  Fix: set a sticky `_no_credentials` flag after first exit, or add `is_sim` guard before task
  creation. (~3 lines)

- **[M-7] Make SQLite snapshot commits async**
  Snapshot inserts call synchronous `conn.commit()` up to 50× per 5-second window, blocking the
  asyncio event loop under sustained WebSocket load.
  Fix: `await asyncio.get_event_loop().run_in_executor(None, conn.commit)` for snapshot commits,
  or batch snapshots before committing.

### LOW — Deployment / Code

- **[L-1] Complete `install.sh` to include grid/connector packages**
  `scripts/install.sh` does not copy `api_binance.py`, `api_mexc.py`, `bot/connectors/`,
  `bot/strategies/` (Python packages). A fresh install + grid deployment fails with
  `ModuleNotFoundError`. Fix: add copy commands for these files/dirs. (~5 lines)

- **[L-2] Update CLAUDE.md line count for `live_bot.py`**
  CLAUDE.md states "~617 lines"; actual is 1518. (1 line)

- **[L-3] Fix RSI label in `bot/indicators.py`**
  `indicators.py:130-141` uses a simple arithmetic mean (Cutler's RSI) but the docstring describes
  Wilder's EMA-smoothed RSI. Update the docstring to reflect the actual implementation.

- **[L-4] Truncate Binance/MEXC error responses in logs**
  `bot/api_binance.py:252,294,334,373` and `bot/api_mexc.py:268,310,349,385` log full response
  bodies. Fix: `"%.300s" % data` format in all logger.error calls. (~8 lines)

- **[L-5] Add session-level default timeout to `aiohttp.ClientSession`**
  `bot/live_bot.py:1497`, `bot/feed.py:283`, `bot/account_bot.py:375` create sessions without a
  default timeout. Per-request timeouts in api_* modules cover current calls, but a future omission
  would hang the event loop. Fix: add `timeout=aiohttp.ClientTimeout(total=30)`. (3 lines)

- **[L-6] Fix `ssh-keyscan` duplicate key appending in deploy scripts**
  `scripts/collect_db.sh`, `scripts/start_collector.sh`, `scripts/test_all_accounts.sh`,
  `scripts/test_multibot_deploy.sh`, `scripts/test_standalone_deploy.sh` append unconditionally
  to `known_hosts` without checking for existing entries. Add existence check before appending.

- **[L-7] Replace f-string PRAGMA SQL in `scripts/profile_compare.py:58`**
  `c.execute(f"PRAGMA mmap_size = {MMAP_MB * 1024 * 1024};")` — no real injection risk (constant),
  but f-string SQL is a pattern to eliminate.
  Fix: `c.execute("PRAGMA mmap_size = ?", (MMAP_MB * 1024 * 1024,))`. (1 line)

### INFO — No immediate action required

- **[I-1] ZMQ without CURVE/ZAP authentication** — mitigated by loopback-only binding. Required
  before any external network exposure (see Roadmap v0.3 section below).

- **[I-2] Move tracked `.db` files out of git** — `data/*.db` are simulation-only but binary blobs
  complicate secret scanning and inflate repo size. Add `data/*.db` to `.gitignore` and distribute
  sample databases separately.

- **[I-3] Pin GitHub Actions to SHA** — `anthropics/claude-code-action@v1` is used by tag, not SHA.
  Pin to a specific commit SHA for supply-chain integrity.

---

## Logging system — deferred items (priorities 3–4)

These were scoped out of the log-system refactor session (priorities 1+2 + English unification were implemented):

- **3a — JSON log mode**: Add `--log-json` flag to `live_bot.py` to emit newline-delimited JSON records
  (`{"ts":…,"level":…,"session":…,"msg":…}`) instead of plain text. Useful for ingestion into
  Elasticsearch / Loki / Datadog without a log parser.

- **3b — Standardize tag prefixes**: Audit all `[TAG]` prefixes across the four bot modules
  (`[LATENCY]`, `[REJECTIONS]`, `[PROBE]`, `[ENSURE_FEED]`, `[BOOK]`, `[FEED]`, `[MARKET]`, …)
  and define a canonical list in `docs/logging.md`. Ensures log parsers can match on a stable
  prefix vocabulary.

- **4a — OBI + ask_vol in trade entry log**: Add `obi=%.3f ask_vol=%.0f` to the `▶ TRADE` line in
  `enter_live_trade()` (`live_bot.py`) so the entry log is self-contained without joining with snapshots.

- **4b — Trade duration in resolution log**: Add `duration=%ds` (elapsed seconds from
  `signal_ts_ms` to `resolution_ts_ms`) to the `✓ WIN` / `✗ LOSS` line in `close_trade()`
  (`live_bot.py`). Lets log-grep workflows spot unusually long holds without a DB query.

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

- **Dynamic position sizing** — fractional Kelly on stake size instead of fixed $10; adapts risk to
  signal confidence.
- **Weekly stop-loss** — complement to the daily stop-loss to limit multi-day drawdown streaks.

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
