# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**tradinebotte** is a Polymarket prediction market trading bot targeting Bitcoin Up/Down 5-minute markets on Polygon. It uses a quantitative signal-based strategy (best_bid >= 0.96 threshold) backtested at 98.3% win rate across 1663 trades.

## Documentation bilingue — règle obligatoire

Ce projet maintient six fichiers de documentation en deux langues :

| Anglais | Français |
|---|---|
| `README.md` | `README.fr.md` |
| `CHANGELOG.md` | `CHANGELOG.fr.md` |
| `INSTALL.md` | `INSTALL.fr.md` |

**Les six fichiers doivent être mis à jour dans le même commit.** Ne jamais modifier l'un sans mettre à jour son équivalent dans l'autre langue. Un hook Claude Code (`.claude/settings.json`) rappelle cette règle après chaque `git commit`.

## Commands

**Install dependencies** (creates venv at `/opt/polymarket-live/venv`):
```bash
bash scripts/install.sh
```

**One-time wallet setup** (derives API keys, checks/swaps USDC.e balance, approves CTF Exchange):
```bash
python3 scripts/setup.py 0xYOUR_PRIVATE_KEY
```

**Start the bot** (requires env vars: `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE`):
```bash
bash scripts/start_bot.sh
```

**Monitor live status and stats**:
```bash
bash scripts/monitor.sh
tail -f /opt/polymarket-live/live.log
sqlite3 /opt/polymarket-live/live.db "SELECT * FROM trades ORDER BY id DESC LIMIT 10;"
```

No test suite exists yet. There is no linter configured.

## Architecture

The bot is a single-file async state machine (`bot/live_bot.py`, ~617 lines) driven by WebSocket market data.

**Core data flow:**
1. `main()` initializes SQLite DB and `BotState`, restores unresolved trades from DB
2. `ws_loop()` manages WebSocket reconnections with exponential backoff (1s → 60s)
3. `_run_ws()` fetches active markets from Gamma API filtered to ±6min endDate window, subscribes to their order book tokens (50 per WebSocket batch)
4. Each WebSocket message → `parse_book_message()` → `handle_book_update()` → `check_signal()` → optionally `enter_live_trade()`
5. Open trades are monitored on every book update via `check_resolution()` → `close_trade()` on WIN/LOSS threshold

**Key classes:**
- `TokenState` — per-token market data (bid/ask, volumes, OBI, time remaining)
- `BotState` — global runtime state: capital, session, active/open trades, maps from token_id and market_id

**State persistence** (SQLite WAL mode, `/opt/polymarket-live/live.db`):
- `trades` table: 21 columns, entry signal through resolution
- `snapshots` table: 5-second interval book snapshots for post-analysis

## Critical Parameters — Do Not Modify

These are backtested. Changing them without re-running the full backtest invalidates the strategy:

| Parameter | Value | Purpose |
|---|---|---|
| `SIGNAL_THRESHOLD` | 0.96 | Core entry signal: best_bid >= 0.96 |
| `MIN_SECS_REMAINING` | 45s | Minimum time left at entry |
| `WIN_THRESHOLD` | 0.99 | Auto-resolve as WIN |
| `LOSS_THRESHOLD` | 0.01 | Auto-resolve as LOSS |
| `DAILY_STOP_LOSS` | $30 | Max daily loss before halting |
| `STAKE` | $10 | Per-trade capital allocation |
| `FEE_RATE` | 2% | Polymarket taker fee |

## Key Architecture Decisions

**Temporal window filter is critical**: The Gamma API query uses `end_date_min = now - 6min` and `end_date_max = now + 6min`. Without this, the bot subscribes to hundreds of expired markets with frozen prices (0.01/0.99) that falsely trigger the signal.

**Never reimplement signing**: Order placement uses `py_clob_client` which handles EIP-712 + HMAC signing internally. The API secret is base64-encoded; `create_or_derive_api_creds()` is the authoritative derivation path.

**Async + sync hybrid**: The bot loop is fully async (WebSocket, HTTP); SQLite operations are synchronous but safe (`check_same_thread=False`). All callbacks run in a single event loop — no true parallelism.

**Graceful restart**: On startup, unresolved trades are restored from DB and capital is rebuilt from historical PnL to prevent duplicate orders.

## External APIs

- **Gamma API**: `https://gamma-api.polymarket.com/markets` — market discovery
- **CLOB API**: `https://clob.polymarket.com` — order placement
- **WebSocket**: `wss://ws-subscriptions-clob.polymarket.com/ws/market` — real-time order books
- **Polygon RPC**: `https://polygon.drpc.org`

## Runtime Paths (VPS Deployment)

- Bot source: `/opt/polymarket-live/live_bot.py`
- Database: `/opt/polymarket-live/live.db`
- Log file: `/opt/polymarket-live/live.log`
- Virtualenv: `/opt/polymarket-live/venv/`

## Normal Behavior

- WebSocket timeouts at ~90s during quiet periods are normal — auto-reconnect handles them
- `POLY_PRIVATE_KEY` missing → simulated orders (no on-chain execution)
- The `best_ask >= 1.0` guard blocks already-resolved markets that slip through the time filter

## Further Context

See `docs/CONTEXT_AI.md` for: complete strategy rationale, full bug history (11 bugs logged), backtest results across 11 days, and live performance data.
