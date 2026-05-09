# TODO — Future improvements

> Code, comments, logs, and docstrings are English-only. Documentation files (README, CHANGELOG, INSTALL, QUICKSTART, UPDATE) are bilingual (EN + FR).

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
