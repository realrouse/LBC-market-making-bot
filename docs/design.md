# Process Architecture & ZeroMQ Message Flow

> 🇫🇷 [Version française](design.fr.md)

This document describes the multi-process architecture of tradinebotte: how
processes are structured, what each one does, and how they communicate over
ZeroMQ.

---

## 1. Deployment modes

Two deployment modes exist. They share the same codebase and strategy files.

### Option A — Standalone (single process)

```
Polymarket WebSocket
        │
        ▼
  ┌───────────────┐
  │  live_bot.py  │  ← WebSocket + signal eval + orders + DB
  └───────────────┘
```

One process does everything: maintains the WebSocket connection, evaluates
signals, places orders, writes the database. Used for single-account setups.

```bash
python3 bot/live_bot.py
```

### Option B — Multi-bot (feed + N account bots)

```
Polymarket WebSocket
        │
        ▼
  ┌──────────┐
  │ feed.py  │  ← WebSocket only, no keys, no trading
  └────┬─────┘
       │ ZeroMQ PUB  tcp://127.0.0.1:5557
       │
  ┌────┴──────────────────────┐
  ▼                           ▼
┌──────────────────┐  ┌──────────────────┐
│  account_bot.py  │  │  account_bot.py  │  (N instances, isolated)
│  ~/account-a     │  │  ~/account-b     │
└──────────────────┘  └──────────────────┘
```

The feed holds the single WebSocket connection and broadcasts all events. Each
account bot runs the full trading stack independently with its own keys,
database, and strategy parameters.

```bash
python3 bot/feed.py &
TRADINEBOTTE_DIR=~/account-a python3 bot/account_bot.py &
TRADINEBOTTE_DIR=~/account-b python3 bot/account_bot.py &
```

**Feed auto-start:** the first `account_bot.py` to launch starts `feed.py`
automatically. A POSIX file lock (`/tmp/tradinebotte-feed/feed-<hash>.lock`)
ensures exactly one account bot starts the feed; other account bots wait until
the feed is reachable before connecting.

---

## 2. Process inventory

| Process | File | Role | Credentials | ZMQ socket |
|---|---|---|---|---|
| `live_bot` | `bot/live_bot.py` | Standalone bot: WebSocket + signal + orders + DB | Private key required | None (no ZMQ) |
| `feed` | `bot/feed.py` | Broadcast-only WebSocket relay | **None** | PUB bind `:5557` |
| `account_bot` | `bot/account_bot.py` | Per-account trading logic | Private key required | SUB connect `:5557` |
| `indicators` | `bot/indicators.py` | Technical indicator pipeline | None | SUB connect `:5557`, PUB bind `:5559` |

---

## 3. ZeroMQ topology

```
                     ┌─────────────────────────────────────────────┐
                     │              EXTERNAL SYSTEMS               │
                     │  Polymarket WebSocket (wss://ws-*.clob...)  │
                     │  Gamma REST API (https://gamma-api.poly...) │
                     │  Binance kline WebSocket + REST API         │
                     └──────────────────┬──────────────────────────┘
                                        │ single WS connection
                                        ▼
                              ┌──────────────────┐
                              │    feed.py        │
                              │  PUB bind :5557   │
                              └─────────┬─────────┘
                                        │ broadcasts: market / book / ping
                          ┌─────────────┼─────────────────┐
                          ▼             ▼                  ▼
               ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐
               │ account_bot  │  │ account_bot  │  │    indicators.py     │
               │ SUB :5557    │  │ SUB :5557    │  │    SUB :5557         │
               │ REQ :5561 ───┼──┼──────────────┼─▶│    REP bind :5561   │
               │ ~/account-a  │  │ REQ :5561 ───┼─▶│    PUB bind :5559   │
               └──────────────┘  └──────────────┘  └──────────┬───────────┘
                                                               │ indicators
                                                               ▼
                                                    ┌─────────────────────┐
                                                    │   any consumer      │
                                                    │   SUB :5559         │
                                                    └─────────────────────┘
```

**Dynamic registration flow** — at startup, each `account_bot` sends a REQ
to `:5561` declaring which streams it needs. `indicators.py` starts the
corresponding asyncio task if it is not already running and replies
`{"status":"ok","stream_id":"..."}`. All output is broadcast on the single
PUB `:5559`; consumers filter by `stream_id`.

```
account_bot startup:
  REQ → {"cmd":"subscribe","asset":"BTCUSDT","timeframe":"4h",
         "source":"binance_ws","indicators":[{"type":"rsi","period":14}]}
  REP ← {"status":"ok","stream_id":"btc_4h"}
  SUB → :5559  (filter: stream_id == "btc_4h")
```

### Socket types used

| Pattern | Direction | Used by |
|---|---|---|
| `zmq.PUB` bind | 1 → N broadcast | `feed.py`, `indicators.py` |
| `zmq.SUB` connect | N → 1 receive | `account_bot.py`, `indicators.py` |
| `zmq.REP` bind | request/reply server | `indicators.py` |
| `zmq.REQ` connect | request/reply client | `account_bot.py` (at startup) |

All messages are single-frame JSON objects. ZeroMQ guarantees atomic delivery
of each frame — no partial messages.

### Default addresses

| Variable | Default | Bound by | Connected by |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | `feed.py` | `account_bot.py`, `indicators.py` |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:5559` | `indicators.py` PUB | any consumer |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:5561` | `indicators.py` REP | `account_bot.py` (startup REQ) |

Both can be overridden with environment variables to run multiple independent
feed+account stacks on the same machine (e.g. port 5557 for stack A, 5558
for stack B).

---

## 4. Message catalog

All messages share a `"t"` discriminator field.

### `market` — new market discovered

Published by `feed.py` when a market enters the ±6-minute window. Also
re-published after each WebSocket reconnect (consumers treat duplicates as
no-ops).

```json
{
  "t":           "market",
  "market_id":   "0xabc…",
  "question":    "Bitcoin Up or Down — 5 minutes (13:00 UTC)",
  "up_token_id": "1234…",
  "dn_token_id": "5678…",
  "start_ms":    1745664000000,
  "end_ms":      1745664300000
}
```

| Field | Type | Description |
|---|---|---|
| `market_id` | string | Polymarket condition ID |
| `question` | string | Market title (≤80 chars) |
| `up_token_id` | string | Token ID for the UP/YES outcome |
| `dn_token_id` | string | Token ID for the DOWN/NO outcome |
| `start_ms` | int | Market open timestamp (Unix ms) |
| `end_ms` | int | Market close timestamp (Unix ms) |

Consumers: `account_bot.py` (registers the market into `BotState`).

---

### `book` — order-book update

Published by `feed.py` on every `book`, `price_change`, or
`last_trade_price` WebSocket event. High-frequency; drives signal evaluation.

```json
{
  "t":        "book",
  "token_id": "1234…",
  "best_bid": 0.97,
  "best_ask": 0.975,
  "spread":   0.005,
  "bid_vol":  120.50,
  "ask_vol":  80.00,
  "obi":      0.20
}
```

| Field | Type | Description |
|---|---|---|
| `token_id` | string | Token whose book changed |
| `best_bid` | float | Best bid price (0–1) |
| `best_ask` | float | Best ask price (0–1) |
| `spread` | float | `best_ask − best_bid` |
| `bid_vol` | float | Aggregate top-5 bid depth (USD) |
| `ask_vol` | float | Aggregate top-5 ask depth (USD) |
| `obi` | float | Order book imbalance: `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |

Consumers: `account_bot.py` (signal evaluation), `indicators.py` (price
accumulation).

---

### `ping` — keepalive

Published by `feed.py` every 10 seconds. Absence of pings indicates a feed
crash or network issue.

```json
{"t": "ping", "ts": 1745664123456}
```

Consumers: ignored by `account_bot.py`; useful for external monitoring.

---

### `indicators` — technical indicators

Published by `indicators.py` once a per-token price history buffer reaches
`--min-ticks` (default 25) and all indicator periods are satisfied.

```json
{
  "t":        "indicators",
  "token_id": "1234…",
  "ts":       1745664125000,
  "rsi_14":   72.3,
  "sma_20":   0.9612,
  "ema_9":    0.9634,
  "vol_20":   0.0021
}
```

| Field | Type | Description |
|---|---|---|
| `token_id` | string | Token the indicators relate to |
| `ts` | int | Publish timestamp (Unix ms) |
| `rsi_N` | float | RSI(N) — Wilder's formula, 0–100 |
| `sma_N` | float | Simple moving average of last N `best_bid` values |
| `ema_N` | float | Exponential moving average (k = 2/(N+1)), seeded with SMA |
| `vol_N` | float | Rolling volatility: population std-dev of log-returns over last N prices |

The key suffix encodes the configured period (e.g. `rsi_14`, `sma_20`).
Fields are absent if the period is not reached yet; the message is not
published at all until every indicator has a valid value.

**Binance kline stream** — `source="binance_ws"`, fields include `asset`,
`timeframe`, and `stream_id`; no `token_id`.

---

### `indicators` — Binance perpetual funding rate (`source="binance_funding"`)

Polled from `https://fapi.binance.com/fapi/v1/premiumIndex` every 15 min
(default). No indicator math; raw rate is published as-is.

```json
{
  "t":               "indicators",
  "stream_id":       "btc_funding",
  "funding_rate":    0.0001,
  "next_funding_ms": 1746000000000,
  "ts":              1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `stream_id` | string | As declared in the config (e.g. `"btc_funding"`) |
| `funding_rate` | float | Current Binance perpetual funding rate (typically ±0.01 %) |
| `next_funding_ms` | int | Next funding settlement timestamp (Unix ms) |
| `ts` | int | Publish timestamp (Unix ms) |

---

### `indicators` — Deribit DVOL implied volatility (`source="deribit_iv"`)

Polled from `https://www.deribit.com/api/v2/public/get_index_price` every
5 min (default). Provides the BTC annualised implied volatility index.

```json
{
  "t":         "indicators",
  "stream_id": "btc_dvol",
  "dvol":      62.5,
  "ts":        1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `stream_id` | string | As declared in the config (e.g. `"btc_dvol"`) |
| `dvol` | float | Deribit DVOL annualised implied volatility (e.g. 62.5 ≈ 62.5 %) |
| `ts` | int | Publish timestamp (Unix ms) |

---

### `indicators` — Fear & Greed Index (`source="fear_greed"`)

Polled from `https://api.alternative.me/fng/` every 1 hour (default).
The index ranges from 0 (Extreme Fear) to 100 (Extreme Greed).

```json
{
  "t":                "indicators",
  "stream_id":        "fear_greed",
  "fear_greed":       72,
  "fear_greed_label": "Greed",
  "ts":               1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `stream_id` | string | Always `"fear_greed"` (asset-agnostic) |
| `fear_greed` | int | Index value 0–100 |
| `fear_greed_label` | string | Text label: `"Extreme Fear"`, `"Fear"`, `"Neutral"`, `"Greed"`, `"Extreme Greed"` |
| `ts` | int | Publish timestamp (Unix ms) |

Consumers: any process subscribing to the indicators PUB port.

---

### `indicators` — Binance futures open interest (`source="binance_oi"`)

Polled from `https://fapi.binance.com/futures/data/openInterestHist` every
5 min (default). Provides absolute OI and the signed change since the previous
poll.

```json
{
  "t":             "indicators",
  "stream_id":     "btc_oi",
  "oi_btc":        45690.57,
  "oi_usd":        4569056780.0,
  "oi_change_btc": 12.44,
  "oi_change_usd": 1244440.0,
  "ts":            1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `oi_btc` | float | Open interest in BTC contracts |
| `oi_usd` | float | Open interest in USD |
| `oi_change_btc` | float | OI delta since previous poll (+ = new longs/shorts opened) |
| `oi_change_usd` | float | OI delta in USD since previous poll |
| `ts` | int | Publish timestamp (Unix ms) |

Rising OI with rising price = trend continuation; falling OI = position
unwinding / reversal risk.

---

### `indicators` — Binance long/short ratio (`source="binance_ls_ratio"`)

Polled from `https://fapi.binance.com/futures/data/topLongShortAccountRatio`
every 5 min (default). Reflects positioning of top-trader accounts (not
position size).

```json
{
  "t":                "indicators",
  "stream_id":        "btc_ls_ratio",
  "long_short_ratio": 1.2345,
  "long_pct":         0.5523,
  "short_pct":        0.4477,
  "ts":               1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `long_short_ratio` | float | `long_pct / short_pct` |
| `long_pct` | float | Fraction of top-trader accounts that are net long (0–1) |
| `short_pct` | float | Fraction of top-trader accounts that are net short (0–1) |
| `ts` | int | Publish timestamp (Unix ms) |

Contrarian signal: ratio > 1.5 or < 0.7 often precedes a reversal.

---

### `indicators` — Binance forced liquidations (`source="binance_liquidations"`)

Aggregates `https://fapi.binance.com/fapi/v1/forceOrders` over the last poll
interval (5 min by default). `SELL` orders = long positions liquidated; `BUY`
orders = short positions liquidated.

```json
{
  "t":             "indicators",
  "stream_id":     "btc_liquidations",
  "liq_long_usd":  1250000.0,
  "liq_short_usd": 80000.0,
  "liq_net_usd":   -1170000.0,
  "liq_count":     45,
  "ts":            1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `liq_long_usd` | float | Total USD value of long positions liquidated in the interval |
| `liq_short_usd` | float | Total USD value of short positions liquidated in the interval |
| `liq_net_usd` | float | `liq_short_usd − liq_long_usd` (negative = more longs blown out) |
| `liq_count` | int | Number of forced orders in the interval |
| `ts` | int | Publish timestamp (Unix ms) |

Consumers: any process subscribing to the indicators PUB port.

---

## 5. Feed auto-start mechanism

When running in multi-bot mode, manual feed management is not required. The
first `account_bot.py` to start automatically launches `feed.py`.

```
account_bot starts
    │
    ├─── probe feed (TCP connect, recv within 5 s)?
    │       YES ──▶ connect SUB, begin trading
    │
    │       NO
    │        │
    │        ├─── try LOCK_EX | LOCK_NB on lock file
    │        │       FAIL (another bot got lock) ──▶ wait LOCK_SH ──▶ connect SUB
    │        │
    │        │       SUCCESS (we hold exclusive lock)
    │        │            │
    │        │            ├─── subprocess.Popen(feed.py, env=minimal_env)
    │        │            ├─── probe until feed is up (up to 30 s)
    │        │            └─── release lock ──▶ connect SUB
    │        │
    └─────────────────────────────────────────────────────────
```

**Lock file:** `/tmp/tradinebotte-feed/feed-<hash(addr)>.lock`
The hash is derived from `TRADINEBOTTE_FEED_ADDR` so each independent stack
(different port) gets its own lock.

**Minimal environment:** `feed.py` inherits only `PATH`, `HOME`, `LANG`,
`VIRTUAL_ENV`, `PYTHONPATH`, `LC_ALL`, `LC_CTYPE`, and `TRADINEBOTTE_FEED_ADDR`.
The parent's `POLY_PRIVATE_KEY` is never passed to the feed subprocess.

---

## 6. Process isolation

Each `account_bot.py` instance is a **separate OS process** with its own copy
of the `live_bot` module. This guarantees:

| Resource | Isolated? | Notes |
|---|---|---|
| SQLite database (`live.db`) | ✅ | Separate `TRADINEBOTTE_DIR` per account |
| Log file (`account.log`) | ✅ | Separate `TRADINEBOTTE_DIR` per account |
| Private key | ✅ | Read from per-account `config.json` |
| Strategy parameters | ✅ | Separate `strategies/` dir per account |
| Capital state | ✅ | In-memory `BotState`, rebuilt from own DB |
| Daily stop-loss counter | ✅ | Per-process `state.daily_pnl` cache |
| Signal deduplication | ✅ | Per-process `state.signalled` set |

The feed is entirely **signal-agnostic**: it publishes every raw book update
without filtering. Signal evaluation, order placement, and trade tracking all
happen independently inside each account bot process.

---

## 7. Indicators pipeline

`indicators.py` is an optional, stateless pipeline stage. It does not trade.

```
Per-token ring buffer (deque, maxlen=200)
    │
    │  push(best_bid) on every "book" message
    │
    ├── RSI(N)       Wilder's: avg_gain / avg_loss over last N deltas
    ├── SMA(N)       mean(prices[-N:])
    ├── EMA(N)       iterative: ema = price*k + ema*(1−k),  k = 2/(N+1)
    └── Vol(N)       std-dev of log-returns over last N+1 prices
         │
         └── publish "indicators" when all four are non-None
```

Indicators return `None` until the buffer has enough history. No message is
published until every configured indicator has a valid value, so consumers
never receive partial data.

**Multi-consumer pattern** — `indicators.py` runs one asyncio task per stream. All output is broadcast on one PUB socket; consumers filter by `stream_id`.

**Dynamic registration** — streams can be added at runtime without restarting `indicators.py`. Any bot sends a REQ to the REP socket (`:5561`) with its indicator needs; the server starts the task if new and replies with the `stream_id` to subscribe to. Streams declared in the JSON config are pre-loaded at startup; bot-requested streams are added on top.

**Limitation** — dynamic registration is supported for `source="binance_ws"`, `"binance_funding"`, `"deribit_iv"`, and `"fear_greed"`. Feed-source streams (Polymarket ticks) must be declared statically in the JSON config.

**Starting the pipeline:**

```bash
# Start feed first (or let account_bot auto-start it)
python3 bot/feed.py &

# Start indicators (subscribes to feed, publishes on :5559)
python3 bot/indicators.py &

# A consumer subscribes to :5559
# (any python script with zmq.SUB connecting to tcp://127.0.0.1:5559)
```

---

## 8. Startup order

For Option B with indicators:

```
1. feed.py          binds  :5557   (or auto-started by first account_bot)
2. indicators.py    SUB→   :5557   / binds :5559
3. account_bot(s)   SUB→   :5557
4. indicator consumers  SUB→  :5559
```

ZeroMQ PUB/SUB is **connectionless from the publisher's perspective**: the
PUB socket continues whether or not SUB clients are connected. Messages
published before a SUB connects are dropped (no buffering on the publisher
side). This means starting indicators after the feed causes no data loss —
any missed `market` messages are re-published on the next 30-second refresh.

---

## 9. Environment variables summary

| Variable | Default | Scope | Description |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | feed.py, account_bot.py, indicators.py | ZMQ address feed.py binds and consumers connect to |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:5559` | indicators.py | ZMQ address indicators.py binds and consumers connect to |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py, live_bot.py | Per-account data directory (DB, log, config, strategies) |

---

## 11. ZeroMQ vs MQTT — trade-off analysis for this project

Both ZeroMQ and MQTT implement publish/subscribe messaging, but they make
opposite architectural choices. This section explains why ZeroMQ was chosen
and what the concrete trade-offs are in the context of tradinebotte.

### Key difference: broker vs brokerless

MQTT is **broker-based**: every message transits through a central server
(Mosquitto, EMQX, …). Publishers and subscribers never talk directly — the
broker mediates all exchanges, stores retained messages, and enforces QoS
levels.

ZeroMQ is **brokerless**: the publisher binds a TCP port; subscribers connect
to it directly. No intermediary process exists.

```
MQTT topology             ZeroMQ topology (ours)
──────────────────        ──────────────────────────
feed.py                   feed.py
  │  publish              PUB bind :5557
  ▼                         │
[Mosquitto broker]          ├──▶ account_bot A (SUB connect)
  │  subscribe             ├──▶ account_bot B (SUB connect)
  ├──▶ account_bot A       └──▶ indicators.py (SUB connect)
  ├──▶ account_bot B
  └──▶ indicators.py
```

### Advantages of ZeroMQ in our case

| Criterion | Detail |
|---|---|
| **No broker process** | No extra daemon to deploy, configure, monitor, or restart. 3 fewer failure points per server. |
| **Latency** | Loopback TCP, no broker hop: ~10–50 µs vs ~1 ms through a local MQTT broker. Critical for `book` messages that drive signal evaluation at 0.96 threshold. |
| **No stale data** | ZeroMQ PUB/SUB has no message retention. A late-connecting SUB misses old messages — exactly what we want: an `account_bot` restarting after a crash should not receive hundreds of stale book prices queued during the downtime. |
| **Simplicity** | `pip install pyzmq` only; no broker package, no config files, no ACL rules. One line to bind, one to connect. |
| **High-water mark (HWM)** | If a slow subscriber falls behind, ZeroMQ silently drops at HWM. For streaming market data, dropping is correct behaviour: a stale book price is worse than no price. |
| **Localhost deployment** | All processes run on the same dedicated server. ZeroMQ's `tcp://127.0.0.1:*` requires no auth, TLS, or network ACLs. MQTT's security model (usernames, TLS, ACLs) adds zero value here. |

### Disadvantages of ZeroMQ in our case

| Criterion | Detail |
|---|---|
| **No message persistence** | A subscriber that is not connected when a message is sent will never receive it. This is usually fine (`book` is continuous; `market` is re-published every 30 s) but means a freshly restarted `account_bot` may miss the first cycle of market announcements. |
| **No broker-side topic filtering** | ZeroMQ PUB sends every message to every connected SUB. Application-level filtering (`if msg["stream_id"] != "btc_4h": continue`) is the only mechanism. With MQTT, topic-based filtering at the broker saves CPU on high-frequency streams when many subscribers exist. Not a concern today (N ≤ 3 subscribers). |
| **No built-in monitoring** | MQTT brokers provide a `$SYS` topic tree with connection counts, throughput, queue depth. ZeroMQ has no equivalent — diagnostics require application-level instrumentation or external tools. |
| **Reconnect message loss** | After a feed restart, SUBs reconnect automatically but lose messages published during the gap. The 30-second `market` refresh mitigates this; `book` gaps are acceptable. |

### Why MQTT would be worse here

| MQTT feature | Our situation |
|---|---|
| **Retained messages** | We explicitly do *not* want a freshly connected account_bot to receive the last cached bid price: it may be minutes old and would corrupt the `min_ticks` warmup phase. |
| **QoS 1 / QoS 2** | Adds acknowledgment round-trips and at-least-once / exactly-once delivery guarantees. For streaming prices, duplicate delivery is harmful (double-counted ticks inflate indicator history). |
| **Broker HA / clustering** | Our design runs on a single dedicated server. MQTT's broker HA adds complexity with no benefit. |
| **WAN / IoT focus** | MQTT was designed for constrained devices over unreliable networks. Our pipes are localhost TCP — there is no packet loss, bandwidth limit, or keep-alive problem to solve. |

### Verdict

ZeroMQ is the right choice for this project: it removes the broker from the
critical path, eliminates a class of deployment failures, and delivers the
no-persistence, low-latency semantics that high-frequency order-book streaming
requires. The only scenario where MQTT would become attractive is if
subscribers were distributed across multiple hosts (different dedicated server instances)
and topic-based filtering at the broker level were needed to manage bandwidth —
a scenario that is currently out of scope.

---

## 10. Related files

| File | Role |
|---|---|
| `bot/live_bot.py` | Standalone bot (Option A) |
| `bot/feed.py` | ZMQ feed broadcaster (Option B) |
| `bot/account_bot.py` | Per-account subscriber + trading logic |
| `bot/indicators.py` | Technical indicator pipeline stage |
| `docs/multi.md` | Option B setup guide and per-account strategy configuration |
| `docs/GridTrading.md` | Grid strategy architecture and JSON config |
| `tests/test_multibot.py` | ZMQ integration tests for feed + account_bot |
| `tests/test_indicators.py` | Unit tests for indicator math and PriceSeries |
