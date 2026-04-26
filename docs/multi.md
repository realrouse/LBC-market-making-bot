# Multi-bot WebSocket Architecture

> 🇫🇷 [Version française](multi.fr.md)

This document describes **Option B**: running multiple independent trading accounts
on the same server while sharing a single WebSocket connection to Polymarket.

For the single-account setup, see [QUICKSTART.md — Option A](../QUICKSTART.md).

---

## Why share a WebSocket?

Polymarket's WebSocket endpoint is a persistent connection that receives every
order-book update for all subscribed markets.  Opening one connection per account
creates three problems:

1. **Redundant bandwidth** — every message is received N times instead of once.
2. **Duplicate API load** — each connection polls the Gamma API independently every 30 s.
3. **Rate-limit risk** — multiple connections from the same IP may be throttled.

The solution: one process (`feed.py`) holds the single connection and
**broadcasts** every event to all account bots via ZeroMQ PUB/SUB.

---

## Architecture

```
Polymarket WebSocket
        │
        ▼
   ┌─────────┐
   │ feed.py │  ← single process, no credentials, no trading logic
   └────┬────┘
        │ ZeroMQ PUB  tcp://127.0.0.1:5557
        │
   ┌────┴────────────────────────┐
   │                             │
   ▼                             ▼
┌──────────────────┐   ┌──────────────────┐
│  account_bot.py  │   │  account_bot.py  │   (N instances)
│  TRADINEBOTTE_   │   │  TRADINEBOTTE_   │
│  DIR=~/account-a │   │  DIR=~/account-b │
│                  │   │                  │
│  live.db         │   │  live.db         │   ← isolated DBs
│  account.log     │   │  account.log     │   ← isolated logs
│  config.json     │   │  config.json     │   ← isolated keys
└──────────────────┘   └──────────────────┘
```

Each account bot runs the **full live_bot.py trading stack** — signal evaluation,
order placement, trade resolution — as if it were a standalone bot, but receives
market data from the shared feed instead of maintaining its own WebSocket.

---

## Components

### `bot/feed.py`

| Responsibility | Details |
|---|---|
| WebSocket connection | One persistent connection to Polymarket, reconnects with exponential backoff (1 s → 60 s) |
| Market discovery | Polls Gamma API every 30 s in a background task (same logic as standalone bot) |
| Market purge | Removes expired markets from internal state so the token dict stays bounded |
| ZMQ publisher | Binds a PUB socket; publishes `market`, `book`, and `ping` messages |
| Credentials | **None** — feed.py has no private key and places no orders |

The feed holds no trading state.  It can be restarted at any time without
affecting account bots (they will miss updates during the gap but will not
place duplicate orders on reconnection).

### `bot/account_bot.py`

| Responsibility | Details |
|---|---|
| ZMQ subscriber | Connects a SUB socket to the feed address; subscribes to all messages |
| Market registration | Builds `TokenState` pairs from feed `market` messages |
| Signal evaluation | Calls `live_bot.handle_book_update()` → `check_signal()` on every `book` message |
| Order placement | Calls `live_bot.enter_live_trade()` → Polymarket CLOB API |
| Trade resolution | WIN/LOSS/expiry resolved via `check_resolution()` as in the standalone bot |
| Persistence | Own `live.db`, `account.log`, `config.json` under `TRADINEBOTTE_DIR` |

The account bot imports `live_bot` for its entire trading pipeline.  All
strategy parameters, signal guards, and fee calculations are identical to
the standalone mode.

---

## Message protocol

The feed publishes JSON messages over ZeroMQ PUB.  All three types are
single-frame JSON objects.

### `market` — new market discovered

Sent once when a market is first registered, and again when the feed
reconnects (account bots treat duplicates as no-ops).

```json
{
  "t": "market",
  "market_id":    "0xabc…",
  "question":     "Bitcoin Up or Down — 5 minutes (13:00 UTC)",
  "up_token_id":  "1234…",
  "dn_token_id":  "5678…",
  "start_ms":     1745664000000,
  "end_ms":       1745664300000
}
```

| Field | Type | Description |
|---|---|---|
| `market_id` | string | Polymarket condition ID |
| `question` | string | Market title (truncated to 80 chars) |
| `up_token_id` | string | Token ID for the UP/YES outcome |
| `dn_token_id` | string | Token ID for the DOWN/NO outcome |
| `start_ms` | integer | Market open Unix timestamp (ms) |
| `end_ms` | integer | Market close Unix timestamp (ms) |

### `book` — order-book update

Emitted on every `book`, `price_change`, or `last_trade_price` WebSocket event.
This is the high-frequency message that drives signal evaluation.

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
| `obi` | float | Order book imbalance: `(bid_vol − ask_vol) / (bid_vol + ask_vol)`, range −1 to +1 |

### `ping` — keepalive

Sent every 10 seconds.  Account bots ignore it; useful for monitoring feed health
(absence of pings indicates feed crash or network issue).

```json
{"t": "ping", "ts": 1745664123456}
```

---

## Configuration

### Environment variables

| Variable | Default | Scope | Description |
|---|---|---|---|
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | feed.py + account_bot.py | ZeroMQ bind/connect address |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py only | Per-account data directory (DB, log, config) |

Both variables must be set consistently between the feed and all account bots
that connect to it.

### Per-account `config.json`

Each account directory requires its own `config.json` generated by `scripts/setup.py`:

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py
```

The config contains the private key, API credentials, and optional strategy
overrides.  Files are chmod 600 and never shared between accounts.

### Strategy parameters

Strategy parameters (`strategies/polymarket_BTC5M.json`) are read from
`TRADINEBOTTE_DIR/strategies/` by each account bot independently.  This means
each account can use a different threshold, stake, or hour filter.

---

## Directory layout

```
~/tradinebotte/               ← shared venv + feed log (no credentials here)
  venv/
  feed.log
  strategies/
    polymarket_BTC5M.json     ← shared strategy (or symlink per account)

~/account-a/                  ← account A: own DB, log, config, credentials
  config.json                 (chmod 600)
  live.db
  account.log
  strategies/
    polymarket_BTC5M.json     ← optional account-specific strategy

~/account-b/                  ← account B: own DB, log, config, credentials
  config.json
  live.db
  account.log
```

---

## Launch sequence

Order matters: the feed must bind its PUB socket before account bots connect.

```bash
# Step 1 — start the shared feed
bash scripts/start_feed.sh

# Step 2 — start each account bot (separate terminals or nohup)
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

The scripts print the PID and the first lines of each log.  A 2-second startup
delay is built in to catch immediate crashes before reporting success.

To use a non-default feed address (e.g. a different port):

```bash
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
```

---

## Monitoring

### Feed health

```bash
tail -f ~/tradinebotte/feed.log
```

Expected output every 10 s (ping loop) and on every market refresh:

```
[INFO]  Feed PUB bind sur tcp://127.0.0.1:5557
[INFO]  WebSocket connecte — diffusion sur tcp://127.0.0.1:5557
[INFO]  Marches BTC 5-min : 4
[INFO]  Nouveaux tokens : 8
```

Absence of log lines for > 60 s (two refresh cycles) indicates a problem.

### Account bot health

```bash
tail -f ~/account-a/account.log
tail -f ~/account-b/account.log
```

Normal output mirrors the standalone bot: signal guards, trade entries, resolutions.

If a bot prints `Aucun message du feed depuis Xs` it is connected but the feed is
not publishing — check `feed.log`.

### SQLite queries (per account)

```bash
sqlite3 ~/account-a/live.db "SELECT id, direction, outcome, pnl_net FROM trades ORDER BY id DESC LIMIT 5;"
sqlite3 ~/account-b/live.db "SELECT COUNT(*), ROUND(SUM(pnl_net),2) FROM trades WHERE resolved=1;"
```

---

## Failure modes

### Feed crashes

Account bots detect silence: if no message arrives within 60 s
(`FEED_TIMEOUT`), they log a warning and keep waiting.  They do not exit.

When the feed restarts, it re-publishes `market` messages for all active markets.
Account bots process these as no-ops (duplicate registrations are idempotent).
Book updates resume immediately — no manual restart needed.

### Account bot crashes

The feed is unaffected.  Only the crashed account misses updates during the gap.
On restart, `restore_state_from_db()` reloads open trades from the SQLite database
(same crash-recovery path as the standalone bot) and the bot resumes from current state.

### Network interruption

The feed handles WebSocket reconnection with exponential backoff (1 s → 60 s cap).
Account bots wait silently during this period.

---

## Adding a third account

```bash
# Set up the new account directory
TRADINEBOTTE_DIR=~/account-c python3 scripts/setup.py

# Start its bot (feed is already running)
TRADINEBOTTE_DIR=~/account-c bash scripts/start_account.sh
```

No restart of the feed or existing account bots required.

---

## Comparison with standalone mode

| Feature | Standalone (`live_bot.py`) | Multi-bot (`feed.py` + `account_bot.py`) |
|---|---|---|
| WebSocket connections | 1 per account | 1 total |
| Gamma API polls | 1 per account every 30 s | 1 total every 30 s |
| SQLite DB | `~/tradinebotte/live.db` | `~/account-X/live.db` per account |
| Log file | `live.log` | `account.log` per account, `feed.log` for feed |
| Config / key | `~/tradinebotte/config.json` | `~/account-X/config.json` per account |
| Strategy file | `TRADINEBOTTE_DIR/strategies/` | `TRADINEBOTTE_DIR/strategies/` per account |
| Crash recovery | `restore_state_from_db()` on restart | same, per account |
| Signal logic | `live_bot.check_signal()` | identical (imported from `live_bot`) |
| systemd service | `tradinebotte.service` | separate units for feed + each account |

---

## Tests

The multi-bot architecture is covered by `tests/test_multibot.py` (30 tests):

```bash
bash scripts/run_tests.sh
```

| Class | Tests | What is verified |
|---|---|---|
| `TestFeedRegisterMarket` | 7 | `feed.register_market()`: new tokens, duplicate, expired, missing fields, multiple markets, metadata |
| `TestAccountBotRegister` | 9 | `_register_from_market_msg()`: token states, directions, market_tokens map, expired/missing skips, idempotence |
| `TestSingleBotIntegration` | 8 | ZMQ round-trip with one bot: market registration, book update, signal threshold, ping |
| `TestTwoBotIntegration` | 6 | ZMQ round-trip with two simultaneous bots: both receive updates, isolated DBs and capital, duplicate market idempotency |

All tests use in-memory SQLite and loopback TCP ZMQ sockets — no network or
exchange credentials required.
