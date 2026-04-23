# tradinebotte

Automated trading bot for [Polymarket](https://polymarket.com) prediction markets, targeting Bitcoin Up/Down 5-minute markets on Polygon. Uses a quantitative signal strategy (`best_bid >= 0.96`) backtested at **98.3% win rate** across 1663 trades (April 2026).

## Strategy

- Monitors "Bitcoin Up or Down — 5 minutes" markets with `endDate` within ±6 minutes of now
- Entry signal: `best_bid >= 0.96` on a UP or DOWN token
- Executes LIMIT BUY at `best_ask` via Polymarket CLOB API
- Resolves WIN at bid >= 0.99, LOSS at bid <= 0.01, or at market expiry (bid >= 0.50 = WIN)
- Daily stop-loss: $30 | Stake per trade: $10 | Fee: 2%

## Requirements

- Python 3.8+
- A Polygon mainnet wallet (EOA — **not** Safe/Gnosis multisig)
- MATIC > 0.1 (gas fees)
- USDC.e > $10 on Polygon (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
  - **Not** USDC native (`0x3c499c...`) — `setup.py` handles the swap automatically

## Dependencies

Installed automatically by `scripts/install.sh` into a virtualenv at `/opt/polymarket-live/venv/`:

```
aiohttp
websockets
web3
py-clob-client
```

## Installation

```bash
bash scripts/install.sh
```

## Configuration

Run `setup.py` once with your Polygon private key. It will:
- Check USDC.e and USDC native balances
- Swap USDC native → USDC.e via Uniswap V3 if needed
- Approve CTF Exchange allowance
- Derive your Polymarket API credentials and write them to `/opt/polymarket-live/config.json`

```bash
python3 scripts/setup.py
```

La clé privée est saisie de manière interactive (stdin masqué, non visible dans `ps aux` ni dans l'historique shell).

The bot reads credentials from `config.json` at startup (falls back to env vars `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE` if the file is absent).

See `config.json.example` for the expected structure.

> **Never commit `config.json`.** It is listed in `.gitignore`.

## Running

```bash
bash scripts/start_bot.sh
```

Verify it's running:

```bash
pgrep -fa live_bot.py
```

Only one instance should run at a time. The bot logs to `/opt/polymarket-live/live.log` and persists all trades to `/opt/polymarket-live/live.db` (SQLite).

## Monitoring

```bash
# Live dashboard
bash scripts/monitor.sh

# Follow logs
tail -f /opt/polymarket-live/live.log

# Recent trades
sqlite3 /opt/polymarket-live/live.db \
  "SELECT id, direction, entry_price, outcome, ROUND(pnl_net,3), capital_after \
   FROM trades ORDER BY id DESC LIMIT 10;"

# Today's stats
sqlite3 /opt/polymarket-live/live.db \
  "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), ROUND(SUM(pnl_net),2) \
   FROM trades WHERE resolved=1 AND created_at > (strftime('%s','now')-86400)*1000;"

# Confirm real on-chain orders (not simulated)
grep "order=" /opt/polymarket-live/live.log | grep -v "order=sim" | tail -20
```

## Testing in a virtual environment

Use [uv](https://github.com/astral-sh/uv) to create an isolated test environment without touching the system Python or the production venv.

**Install uv** (if not already installed):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

**Create the venv and install dependencies:**

```bash
uv venv .venv --python 3.13
uv pip install aiohttp websockets web3 py-clob-client --python .venv/bin/python3
```

**Syntax check:**

```bash
.venv/bin/python3 -m py_compile bot/live_bot.py && echo "SYNTAX OK"
```

**Import check** (verifies module-level code runs without errors):

```bash
.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
import bot.live_bot as b
print('CONFIG_PATH:', b.CONFIG_PATH)
print('PRIVATE_KEY set:', bool(b.PRIVATE_KEY))
print('SIGNAL_THRESHOLD:', b.SIGNAL_THRESHOLD)
"
```

**Run the bot for 20 seconds** (no credentials needed — orders are simulated):

```bash
timeout 20 .venv/bin/python3 bot/live_bot.py
```

Then inspect the log:

```bash
cat /opt/polymarket-live/live.log
```

Expected output confirms the bot starts, connects to the Polymarket API, finds active BTC 5-min markets, subscribes to the WebSocket, and enters simulated mode:

```
[INFO] LIVE BOT v3 — Threshold=0.96 Stake=$10 MinAskVol=10
[WARNING] POLY_PRIVATE_KEY non definie — ordres SIMULES
[INFO] DB initialisee : /opt/polymarket-live/live.db
[INFO] State : capital=$100.00 | 0 trades | WR=0.0%
[INFO] Marches BTC 5-min : 2
[INFO] Souscription 2 tokens...
[INFO] WebSocket connecte
```

The `.venv/` directory is listed in `.gitignore` and should not be committed.

## Notes

- WebSocket timeouts at ~90s during quiet periods are **normal** — the bot reconnects automatically
- If `POLY_PRIVATE_KEY` is not set, orders are simulated (no on-chain execution)
- Signals can be infrequent during low-volatility BTC periods — this is expected
- Do not modify `SIGNAL_THRESHOLD` (0.96) without re-running the full backtest

## License

See [LICENSE](LICENSE).
