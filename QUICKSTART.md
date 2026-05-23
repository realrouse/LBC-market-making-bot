# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · Updating: [UPDATE.md](UPDATE.md)

## Prerequisites

- Python 3.8+ on Linux/Mac (dedicated server or local machine)
- Polygon EOA wallet — MATIC > 0.1 (gas) and USDC.e > $10
- **No wallet yet?** Press Enter at the `setup.py` prompt → simulation mode, no real orders

---

## Install from official release (tar.gz)

```bash
# Download the latest release (replace v0.44 with the current version)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.44.tar.gz
tar -xzf v0.44.tar.gz
cd tradinebotte-0.44
bash scripts/install.sh        # detects missing system packages; prompts for language (E/F)
python3 scripts/setup.py       # prompts for language (saved to config.json); Enter = simulation mode
bash scripts/start_bot.sh
tail -f ~/tradinebotte/live.log
```

Monitor: `bash scripts/monitor.sh`  
Auto-restart on reboot: see [INSTALL.md — systemd setup](INSTALL.md#auto-start-with-systemd-recommended-for-dedicated-servers)

**Stop:** `kill $(cat ~/tradinebotte/live.pid)` · or `sudo systemctl stop tradinebotte` if using systemd

---

## Other installation methods

See [INSTALL.md](INSTALL.md) for:
- **git clone** — recommended when GitHub is accessible from the target machine
- **rsync** — recommended for servers without git (deploy from a local dev machine)
- Full details on multi-account setup (Option B — ZeroMQ shared WebSocket)
