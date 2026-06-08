# Logging — canonical tag vocabulary

All structured log lines emitted by tradinebotte services use a `[TAG]`
bracket prefix so log parsers and alerting rules can match on a stable token
rather than on a fragile substring of the human-readable message.

Two exceptions use visual-marker format instead of brackets (see
[Visual-marker lines](#visual-marker-lines) below).

---

## Tag reference

### `live_bot.py`

| Tag | Level | Emitted | Description |
|-----|-------|---------|-------------|
| `[REJECTIONS]` | INFO | every 60 s | Aggregated signal rejection counters for the last interval. Fields: `signalled`, `ended`, `hour`, `bid`, `emax`, `ask`, `askvol`, `secs`, `obi`, `vol`, `capital`, `dstop`, `wstop`, `cooldown`. |
| `[LATENCY]` | INFO | per trade | Signal-to-order latency. Fields: `signal_ms`, `order_rtt_ms`, `total_ms`, `ts_ms`, `direction`, `market`. Parsed by `scripts/latency.py`. |
| `[VOL_FILTER]` | DEBUG | per skip | Volatility filter rejection. Fields: `bid_vol`, `range`, `obi_vol`, market id. Only emitted when `vol_filter_enabled=true`. |
| `[KELLY]` | INFO | per skip | Kelly criterion returned f*≤0 — no positive edge at current ask/win-rate. |
| `[CIRCUIT_BREAKER]` | WARNING | on streak | CLOB API failure streak reached `api_fail_threshold`. Entries suspended for `api_cooldown_secs`. |
| `[GHOST_GUARD]` | WARNING | per failure | `post_order` returned `None` in live mode — entry aborted to prevent a ghost row with `order_id=NULL`. |

### Visual-marker lines (`live_bot.py`)

These two line types do not use brackets. They carry a trade ID (`#N`) and
are identifiable by their leading icon character.

| Pattern | Level | Description |
|---------|-------|-------------|
| `▶ TRADE #N \| …` | INFO | Trade entry. Fields after `\|`: `direction`, `market_id[:12]`, `entry`, `bid`, `secs`, `stake`, `order`. |
| `✓ WIN  #N \| …` | INFO | Trade resolved as WIN. Fields: `direction`, `market_id[:12]`, `pnl`, `roi%`, `WR%`, `capital`. |
| `✗ LOSS #N \| …` | INFO | Trade resolved as LOSS. Same field layout as WIN. |

Grep patterns: `'▶ TRADE'`, `'✓ WIN'`, `'✗ LOSS'` (UTF-8).

---

### `feed.py`

| Tag | Level | Description |
|-----|-------|-------------|
| `[REGISTER]` | DEBUG | New market registered from Gamma API. Fields: `market`, `q` (question, truncated), `new_tokens`. |
| `[REFRESH]` | DEBUG | Gamma API poll lifecycle (start / count returned / done). |
| `[WS]` | DEBUG | WebSocket lifecycle events: `get_markets`, token list, 30-s timeout, recv exception, batch stats, traceback on error. |
| `[ZMQ]` | DEBUG | ZMQ PUB socket bound. |
| `[PUB book]` | DEBUG | Order-book update published on ZMQ. |
| `[PUB market]` | DEBUG | Market-registration message published on ZMQ. |
| `[PUB ping]` | DEBUG | Keepalive ping published on ZMQ. |

---

### `account_bot.py`

| Tag | Level | Description |
|-----|-------|-------------|
| `[INIT]` | DEBUG | Initialization parameters: `TRADINEBOTTE_DIR`, thresholds, capital, open trades. |
| `[PROBE]` | DEBUG | Feed probe attempt (ZMQ SUB with timeout). |
| `[ENSURE_FEED]` | DEBUG | Feed auto-start path: command, log file, wait loop. |
| `[IND]` | INFO / WARNING | Indicator stream registration result (registered, failed, timeout, error). |
| `[MARKET]` | DEBUG | Incoming market message from feed: incomplete / expired / registered. |
| `[FEED]` | DEBUG | Incoming feed message routing: book tick, ping, market update. |
| `[BOOK]` | DEBUG | Book update signal check: unknown token skip, signal threshold check. |

---

### `indicators.py`

Indicators uses two tag conventions:

**Static tags** — fixed structural events:

| Tag | Level | Description |
|-----|-------|-------------|
| `[seed]` | INFO / WARNING | REST seed on startup for a kline stream. Fields: `stream_id/timeframe`, candle count. |
| `[feed]` | INFO | ZMQ SUB socket connected to `feed.py`. |
| `[reg]` | INFO / ERROR | Stream registration via REP socket: already-active skip, start confirmed, bad source. |
| `[PUB <stream_id>]` | DEBUG | Indicator message published. Fields: stream id, indicator values. |

**Dynamic stream tags** — `[<stream_id>]` where `stream_id` matches the `id`
field in the stream config (e.g. `[btc_full_depth_perp]`, `[btc_scalping]`,
`[btc_kline_4h]`). Used for WS lifecycle, errors, resync, and data events
specific to that stream.

---

## Adding a new structured log line

1. Pick a tag from the table above if the event fits an existing category.
2. Otherwise introduce a new `[UPPER_SNAKE]` tag and add it to this file in
   the same commit.
3. Never use `[TAG]` brackets for prose status lines (startup banners,
   "Subscribing to N tokens…", DB init). Brackets signal machine-parseable
   events only.
4. Keep tags in `live_bot.py` and `account_bot.py` English-only per the
   project language policy.
