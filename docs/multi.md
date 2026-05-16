# Multi-bot WebSocket Architecture

> 🇫🇷 [Version française](multi.fr.md)

This document describes **Option B**: sharing a single WebSocket connection across
multiple independent trading bots via ZeroMQ.

For the single-account setup, see [QUICKSTART.md — Option A](../QUICKSTART.md).

## When to use Option B

| Situation | Option A (standalone) | Option B (multi-bot) |
|---|---|---|
| Single account | ✅ simpler | possible but unnecessary |
| Two wallets, same Linux user | works (two processes, two WS connections) | ✅ one WS connection |
| Two wallets, different Linux users | works | ✅ one WS connection, cross-user |
| One account, two strategies to compare | two standalone bots | ✅ one feed, two account bots |
| Priority: simplicity and easy debugging | ✅ | more moving parts |
| Priority: minimal exchange connections | more connections | ✅ |

**Rule of thumb:** start with Option A.  Move to Option B when you have two or more
accounts to run simultaneously, or when you want to compare strategies without
opening multiple WebSocket connections.

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
| Signal evaluation | Calls `live_bot.handle_book_update()` → `check_signal()` on every `book` message, using **its own** strategy parameters |
| Order placement | Calls `live_bot.enter_live_trade()` → Polymarket CLOB API |
| Trade resolution | WIN/LOSS/expiry resolved via `check_resolution()` as in the standalone bot |
| Persistence | Own `live.db`, `account.log`, `config.json` under `TRADINEBOTTE_DIR` |

The account bot is a **separate OS process**.  At startup it sets `TRADINEBOTTE_DIR`
then imports `live_bot`, which reads all strategy parameters from that directory's
`strategies/polymarket_BTC5M.json` at import time.  Because each process has its
own copy of the `live_bot` module, **each account bot can run a completely
different strategy** — different threshold, different stake, different hour filter,
different stop-loss — while sharing the same raw WebSocket feed.

The feed is entirely **signal-agnostic**: it broadcasts every raw book update
without any filtering.  Signal evaluation happens independently inside each
account bot process.

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
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:5557` | feed.py, account_bot.py | ZeroMQ PUB/SUB address for the shared feed. |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:5559` | indicators.py, account_bot.py | ZeroMQ PUB address of the shared indicators service. `account_bot` subscribes here when `indicators_streams` is configured. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:5561` | indicators.py, account_bot.py | ZeroMQ REP address for dynamic stream registration. Each `account_bot` sends subscribe requests here at startup. |
| `TRADINEBOTTE_DIR` | `~/tradinebotte` | account_bot.py only | Per-account data directory (DB, log, config). |

`TRADINEBOTTE_FEED_ADDR` must be set consistently between the feed and all account bots.
The indicators variables default to sensible values and only need to be overridden
when running multiple independent stacks on the same machine.

### Per-account `config.json`

Each account directory requires its own `config.json` generated by `scripts/setup.py`:

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py
```

The config contains the private key, API credentials, and optional strategy
overrides.  Files are chmod 600 and never shared between accounts.

**Keys relevant to multi-bot mode:**

| Key | Default | Description |
|---|---|---|
| `feed_addr` | `tcp://127.0.0.1:5557` | ZMQ address of the shared feed. |
| `feed_auto_start` | `true` | Set to `false` when the feed is managed by systemd — account_bot will probe with retries instead of forking feed.py. |
| `indicators_reg_addr` | `tcp://127.0.0.1:5561` | ZMQ REP address of the shared indicators service. |
| `indicators_streams` | `[]` | List of stream subscriptions to register with the indicators service at startup. Leave empty to skip indicators. |

Example with indicators enabled:

```json
{
  "feed_addr":         "tcp://127.0.0.1:5557",
  "feed_auto_start":   false,
  "indicators_reg_addr": "tcp://127.0.0.1:5561",
  "indicators_streams": [
    {
      "source":     "binance_ws",
      "asset":      "BTCUSDT",
      "timeframe":  "4h",
      "indicators": [{"type": "rsi", "period": 14},
                     {"type": "vol", "period": 20}]
    }
  ]
}
```

### Strategy parameters — each account bot is independent

Strategy parameters (`strategies/polymarket_BTC5M.json`) are read from
`TRADINEBOTTE_DIR/strategies/` by each account bot **at process startup**.
Because each `account_bot.py` is a separate OS process with its own copy of the
`live_bot` module, every account bot evaluates signals independently against
**its own** parameters.  The feed broadcasts raw book updates with no filtering —
it has no knowledge of any threshold or strategy.

This means you can run genuinely different strategies in parallel:

| Account | `signal_threshold` | `stake` | `hour_filter` | Purpose |
|---|---|---|---|---|
| `~/account-conservative` | `0.98` | `$5` | US session only | Low-risk, fewer entries |
| `~/account-standard` | `0.96` | `$10` | disabled | Backtested default |
| `~/account-aggressive` | `0.94` | `$20` | 24/7 | Higher frequency |

Each account needs its own `strategies/` directory with a JSON file configured
for that strategy:

```bash
# Set up a conservative account with a custom threshold
mkdir -p ~/account-conservative/strategies
cp strategies/polymarket_BTC5M.json ~/account-conservative/strategies/
# Edit ~/account-conservative/strategies/polymarket_BTC5M.json:
#   "signal_threshold": 0.98, "stake": 5, "daily_stop_loss": 15
TRADINEBOTTE_DIR=~/account-conservative python3 scripts/setup.py
```

Signal guards that differ per account bot (all read from the strategy JSON):

| Parameter | Key in JSON | Effect |
|---|---|---|
| Entry threshold | `signal_threshold` | Minimum `best_bid` to enter |
| Stake | `stake` | USD per trade |
| Daily stop-loss | `daily_stop_loss` | Max daily loss before halting |
| Min time remaining | `min_secs_remaining` | Minimum seconds until market close |
| Min ask volume | `min_ask_vol` | Minimum liquidity at entry |
| OBI reject threshold | `obi_reject_thresh` | Order book imbalance floor |
| Hour / day filter | `hour_filter` | UTC trading windows per weekday/weekend |

---

## Directory layout

### Same Linux user (simplest)

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

### Different Linux users (cross-user)

Each user manages their own installation; they share only the ZeroMQ address.
See [Cross-user deployment](#cross-user-deployment-different-linux-accounts) below.

```
/home/user1/tradinebotte/     ← feed user: venv, feed.log (no credentials)
/home/user1/account-1/        ← user1's trading account

/home/user2/tradinebotte/     ← user2's own venv (separate install)
/home/user2/account-2/        ← user2's trading account (own key, own DB)
```

---

## Launch sequence

### Manual launch (feed_auto_start=true, default)

**account_bot.py auto-starts the feed** — you do not need to start feed.py
separately.  Just launch all account bots at once:

```bash
# All three can be started simultaneously — no ordering required
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-c bash scripts/start_account.sh
```

**How it works (race-safe):**

1. Every account_bot probes the feed address for 5 seconds on startup.
2. If no feed is found, it races for an exclusive file lock
   (`/tmp/tradinebotte-feed-<hash>.lock`).
3. The winner starts `feed.py` as a subprocess and waits up to 30 s for it to
   be ready, then releases the lock.
4. The losers block on the lock, see the feed is ready when they unblock, and
   proceed without starting a second feed.

Feed logs go to `/tmp/tradinebotte-feed-<hash>.log`.

If you prefer to start the feed explicitly (e.g. for systemd or monitoring):

```bash
# Optional: manual feed start — account_bots will find it automatically
bash scripts/start_feed.sh

# Then account bots (they will skip the auto-start and connect directly)
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

To use a non-default feed address:

```bash
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

### Systemd launch (feed_auto_start=false, recommended for production)

When the feed is managed by systemd, disable auto-start in each account's `config.json`:

```json
{ "feed_auto_start": false }
```

Then start account bots normally — they probe the already-running feed service
instead of forking it:

```bash
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

If the feed is not reachable after 30 s (6 × 5 s probes), the bot exits with an
error and systemd restarts it automatically.

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

## Cross-user deployment (different Linux accounts)

The ZeroMQ PUB socket binds to a **TCP loopback address** (`127.0.0.1`).
On Linux, loopback TCP is accessible to every process on the machine regardless
of which user runs it — no special configuration, no shared filesystem, no sudo
required.  This means the feed can run as `user1` and account bots can run as
`user2`, `user3`, etc.

### Prerequisites per user

Each Linux user needs their own independent installation:

```bash
# As user2 — one-time setup
git clone https://github.com/neofutur/tradinebotte.git ~/tradinebotte
bash ~/tradinebotte/scripts/install.sh          # creates ~/tradinebotte/venv with pyzmq
TRADINEBOTTE_DIR=~/account-2 python3 ~/tradinebotte/scripts/setup.py   # wallet setup
```

> Each user's `config.json` (private key, API credentials) stays in their own home
> directory under their own Unix permissions.  No credentials are ever shared.

### Who runs the feed?

The feed has no credentials and no trading logic — any user can run it.
Typical choices:

| Who runs feed.py | When to use |
|---|---|
| The same user as one of the accounts (e.g. `user1`) | 2–3 accounts, simple setup |
| A dedicated system account (`tradebotte-feed`) | Production; clear responsibility separation |

### Launch sequence (cross-user)

```bash
# As user1 — start the shared feed (binds tcp://127.0.0.1:5557)
bash ~/tradinebotte/scripts/start_feed.sh

# As user2 — start their account bot (connects to the same address)
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
TRADINEBOTTE_DIR=~/account-2 \
bash ~/tradinebotte/scripts/start_account.sh

# As user3 — another account bot
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
TRADINEBOTTE_DIR=~/account-3 \
bash ~/tradinebotte/scripts/start_account.sh
```

Each user uses **their own venv** (`~/tradinebotte/venv/`) via their own
`start_account.sh`.  The `TRADINEBOTTE_FEED_ADDR` must match across all users.

### Directory layout (cross-user)

```
/home/user1/
  tradinebotte/
    venv/                     ← user1's venv (has pyzmq, aiohttp, etc.)
    feed.log                  ← feed diagnostics
    strategies/
      polymarket_BTC5M.json
  account-1/
    config.json               (chmod 600, user1 only)
    live.db
    account.log

/home/user2/
  tradinebotte/
    venv/                     ← user2's own venv (independent install)
    strategies/
      polymarket_BTC5M.json
  account-2/
    config.json               (chmod 600, user2 only)
    live.db
    account.log

/home/user3/
  tradinebotte/
    venv/
  account-3/
    config.json               (chmod 600, user3 only)
    live.db
    account.log
```

### Monitoring across users

Each user monitors their own logs independently:

```bash
# user1 monitors feed + own account
tail -f ~/tradinebotte/feed.log
tail -f ~/account-1/account.log

# user2 monitors only their account
tail -f ~/account-2/account.log
```

To check if the feed is alive from any user:

```bash
# Any user can run this — checks if feed.py process exists
pgrep -u user1 -f feed.py && echo "feed running" || echo "feed down"
```

### Security model

| What is shared | Who can access | Risk |
|---|---|---|
| ZMQ feed messages (`market`, `book`, `ping`) | Any local user who knows the port | Market data only — no credentials, no keys |
| `config.json` | Owner only (chmod 600) | Never exposed |
| `live.db` | Owner only | Never exposed |
| `account.log` | Owner only | Never exposed |

The feed deliberately holds no credentials.  Any local user could connect to the
ZMQ port and receive market data, but that data is public (Polymarket's order book
is public) and contains nothing sensitive.  Private keys never leave each user's
own `TRADINEBOTTE_DIR`.

### Port conflicts

If port 5557 is already in use on the machine:

```bash
# Check what is using the port
ss -tlnp | grep 5557

# Use a different port for all participants
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/start_feed.sh
# — every account bot must use the same address
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/account-2 bash scripts/start_account.sh
```

### systemd services

The project ships three dedicated generator scripts:

| Script | Generates | Purpose |
|---|---|---|
| `scripts/install_feed_service.sh` | `tradinebotte-feed.service` | System-level WebSocket feed (one per machine) |
| `scripts/install_indicators_service.sh` | `tradinebotte-indicators.service` | Shared indicators pipeline (one per machine, optional) |
| `scripts/install_account_service.sh` | `tradinebotte-account-<name>.service` | Per-account trading bot (one per wallet) |

**Step 1 — install the feed service (run once per machine, as any user):**

```bash
bash scripts/install_feed_service.sh
# optional: use a non-default ZMQ address
TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 bash scripts/install_feed_service.sh

# Follow the printed sudo commands:
sudo cp /tmp/tradinebotte-feed.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte-feed
sudo systemctl start tradinebotte-feed
```

**Step 1b — install the indicators service (optional, run once per machine):**

The indicators service is a **shared process** — one instance runs on the machine
(like the feed). Each `account_bot` registers the streams it needs at startup via
the REP socket; the indicators service starts the corresponding tasks dynamically.

```bash
INDICATORS_CONFIG=~/tradinebotte/strategies/indicators_base.json \
bash scripts/install_indicators_service.sh

# Follow the printed sudo commands:
sudo cp ~/tmp/tradinebotte-indicators.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte-indicators
sudo systemctl start tradinebotte-indicators
journalctl -u tradinebotte-indicators -f
```

**Step 2 — install an account service (once per wallet directory):**

Make sure `config.json` has `"feed_auto_start": false` before running this
step — the script warns if it is missing.

```bash
# Each wallet owner runs this for their own directory:
TRADINEBOTTE_DIR=~/account-a bash scripts/install_account_service.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/install_account_service.sh

# Follow the printed sudo commands for each:
sudo cp /tmp/tradinebotte-account-<username>.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable tradinebotte-account-<username>
sudo systemctl start tradinebotte-account-<username>
```

The account unit declares:
- `Requires=tradinebotte-feed.service` — systemd refuses to start it if the feed is not running.
- `Wants=tradinebotte-indicators.service` — systemd starts the indicators service first if it is installed (optional; the account bot continues without indicators if the service is absent).

**Cross-user**: the feed service runs as whichever user ran `install_feed_service.sh`.
Account services run as their respective wallet owners. All connect via
`127.0.0.1` — no extra Linux permissions needed.

```bash
# Useful monitoring commands:
sudo systemctl status tradinebotte-feed
sudo systemctl status tradinebotte-account-account-a
journalctl -u tradinebotte-feed -f
journalctl -u tradinebotte-account-account-a -f
```


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
