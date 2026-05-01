# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · Updating: [UPDATE.md](UPDATE.md)

## Prerequisites

- Python 3.8+ on Linux/Mac (VPS recommended)
- Polygon EOA wallet — MATIC > 0.1 (gas) and USDC.e > $10
- **No wallet yet?** Press Enter at the `setup.py` prompt → simulation mode, no real orders

---

## Option A — One account (standalone)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh        # detects missing system packages automatically
python3 scripts/setup.py       # Enter = simulation mode
bash scripts/start_bot.sh
tail -f ~/tradinebotte/live.log
```

Monitor: `bash scripts/monitor.sh`  
Auto-restart on reboot: `bash scripts/install_service.sh` (then follow the printed `sudo` commands)

**Stop:** `pkill -f live_bot.py` · or `sudo systemctl stop tradinebotte` if using systemd

---

## Option B — Multiple accounts (shared WebSocket)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py   # key for account A
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py   # key for account B
TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

The feed (`feed.py`) starts automatically — no manual step needed.

**Stop:** `pkill -f feed.py; pkill -f account_bot.py`

Full architecture: [docs/multi.md](docs/multi.md)

---

## Which option?

| Situation | Use |
|---|---|
| Single account | **Option A** |
| Multiple wallets or Linux users | **Option B** |
| Comparing strategies in parallel | **Option B** |
| Simplest possible setup | **Option A** |
