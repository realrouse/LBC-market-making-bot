# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · CI: pylint 10/10 · mypy 0 errors · 123 tests

## Before you start

- Python 3.8+ on a Linux/Mac machine (VPS recommended)
- A Polygon **EOA** wallet (MetaMask key — not Safe/Gnosis multisig)
- On that wallet: **MATIC > 0.1** (gas) and **USDC.e > $10** (`0x2791Bca1...`)
  - USDC native (`0x3c499c...`) also works — `setup.py` swaps it automatically

---

## 1 — Clone and install

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

This creates `~/tradinebotte/` with a virtualenv and all dependencies. No root needed.

---

## 2 — Connect your wallet (one-time)

```bash
python3 scripts/setup.py
```

You will be prompted for your private key (masked, never stored in history).  
The script checks balances, swaps USDC if needed, approves the exchange, and writes
`~/tradinebotte/config.json` (chmod 600).

---

## 3 — Start the bot

```bash
bash scripts/start_bot.sh
```

---

## 4 — Monitor

```bash
bash scripts/monitor.sh          # live dashboard
tail -f ~/tradinebotte/live.log  # raw log stream
```

---

## Test without real money first

```bash
bash scripts/start_bot.sh --simulate
```

All file I/O goes to `~/tradinebotte-sim`. No orders are placed on-chain.
To run multiple bots in parallel, set `TRADINEBOTTE_DIR` first: `TRADINEBOTTE_DIR=~/account-a bash scripts/start_bot.sh --simulate`

---

## Auto-restart on reboot (systemd)

```bash
bash scripts/install_service.sh   # generates unit file and prints install commands
```

Then follow the printed `sudo` commands to enable the service.

---

## Multiple accounts — shared WebSocket (ZeroMQ)

Run a single WebSocket feed and multiple independent account bots:

```bash
bash scripts/start_feed.sh                               # shared feed (one WS connection)
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

Each account needs its own directory with a `config.json` (run `TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py` for each).  Full guide: [INSTALL.md — Multi-bot section](INSTALL.md#multi-bot-websocket-sharing-option-a--zeromq).

---

## Stop the bot

```bash
pkill -f live_bot.py        # if running manually
sudo systemctl stop tradinebotte  # if running via systemd
pkill -f feed.py            # multi-bot feed
pkill -f account_bot.py     # multi-bot account bots
```
