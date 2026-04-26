# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · CI: pylint 10/10 · mypy 0 errors · 153 tests

## Before you start

- Python 3.8+ on a Linux/Mac machine (VPS recommended)
- A Polygon **EOA** wallet (MetaMask key — not Safe/Gnosis multisig)
- On that wallet: **MATIC > 0.1** (gas) and **USDC.e > $10** (`0x2791Bca1...`)
  - USDC native (`0x3c499c...`) also works — `setup.py` swaps it automatically

---

## Choose your deployment mode

**Option A — Standalone** (`live_bot.py`)
: Each bot opens its own WebSocket connection to Polymarket.
: **Use when:** one account, or a small number of accounts where simplicity and ease of debugging matter more than connection efficiency.

**Option B — Multi-bot** (`feed.py` + `account_bot.py`)
: One shared WebSocket feed; each account bot subscribes via ZeroMQ.
: **Use when:** two or more accounts on the same machine, multiple Linux users each with their own account, or running different strategies in parallel (each bot evaluates signals independently with its own parameters).

Quick decision guide:

| Situation | Recommended |
|---|---|
| First setup, single account | **Option A** |
| Two wallets, same Linux user | **Option B** |
| Two wallets, different Linux users (`/home/user1`, `/home/user2`) | **Option B** |
| One account but two strategies to compare simultaneously | **Option B** |
| Want simplest possible operation and debugging | **Option A** |

Both modes share the same strategy JSON format, database schema, signal logic, and backtesting tools. Switching from A to B later requires no changes to existing data.

---

## Option A — Standalone (one account)

### 1 — Clone and install

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

This creates `~/tradinebotte/` with a virtualenv and all dependencies. No root needed.

### 2 — Connect your wallet (one-time)

```bash
python3 scripts/setup.py
```

You will be prompted for your private key (masked, never stored in history).  
The script checks balances, swaps USDC if needed, approves the exchange, and writes
`~/tradinebotte/config.json` (chmod 600).

### 3 — Start the bot

```bash
bash scripts/start_bot.sh
```

### 4 — Monitor

```bash
bash scripts/monitor.sh          # live dashboard
tail -f ~/tradinebotte/live.log  # raw log stream
```

### Auto-restart on reboot (systemd)

```bash
bash scripts/install_service.sh   # generates unit file and prints install commands
```

Then follow the printed `sudo` commands to enable the service.

### Stop

```bash
pkill -f live_bot.py              # if running manually
sudo systemctl stop tradinebotte  # if running via systemd
```

---

## Option B — Multi-bot (shared WebSocket, multiple accounts)

One `feed.py` process opens a single WebSocket connection to Polymarket and
broadcasts every book update via ZeroMQ.  Each `account_bot.py` subscribes to
this feed and trades one account independently — with its own database, log file,
and private key.  No extra exchange connections are opened.

```
feed.py  →  ZMQ PUB (tcp://127.0.0.1:5557)
              ├── account_bot.py  [~/account-a]
              └── account_bot.py  [~/account-b]
```

### 1 — Clone and install (shared venv)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh           # creates ~/tradinebotte/venv
```

### 2 — Set up each account (one-time per account)

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py   # enter account A key
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py   # enter account B key
```

Each account gets its own `~/account-X/config.json` (chmod 600).

### 3 — Start the feed, then each account bot

```bash
bash scripts/start_feed.sh                                # shared feed
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

### 4 — Monitor each account

```bash
tail -f ~/account-a/account.log
tail -f ~/account-b/account.log
tail -f ~/tradinebotte/feed.log   # feed diagnostics
```

### Stop

```bash
pkill -f feed.py
pkill -f account_bot.py
```

Full architecture documentation: [docs/multi.md](docs/multi.md) · INSTALL reference: [INSTALL.md — Multi-bot section](INSTALL.md#multi-bot-websocket-sharing-option-a--zeromq).

---

## Test without real money first

Works for both modes — just add `--simulate` (standalone) or set a directory
without a real private key (multi-bot):

```bash
# Standalone simulate
bash scripts/start_bot.sh --simulate        # writes to ~/tradinebotte-sim

# Multi-bot simulate (each account in its own directory)
TRADINEBOTTE_DIR=~/sim-a bash scripts/start_bot.sh --simulate
TRADINEBOTTE_DIR=~/sim-b bash scripts/start_bot.sh --simulate
```

No orders are placed on-chain in either case.
