# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · Updating: [UPDATE.md](UPDATE.md)

## Prerequisites

- Python 3.8+ on Linux/Mac (VPS recommended)
- Polygon EOA wallet — MATIC > 0.1 (gas) and USDC.e > $10
- **No wallet yet?** Press Enter at the `setup.py` prompt → simulation mode, no real orders

---

## Install from official release (tar.gz)

```bash
# Download the latest release (replace v0.4.4 with the current version)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.4.4.tar.gz
tar -xzf v0.4.4.tar.gz
cd tradinebotte-0.4.4
bash scripts/install.sh        # detects missing system packages; prompts for language (E/F)
python3 scripts/setup.py       # prompts for language (saved to config.json); Enter = simulation mode
bash scripts/start_bot.sh
tail -f ~/tradinebotte/live.log
```

Monitor: `bash scripts/monitor.sh`  
Auto-restart on reboot: see [INSTALL.md — systemd setup](INSTALL.md#auto-start-with-systemd-recommended-for-vps)

**Stop:** `pkill -f '[l]ive_bot.py'` · or `sudo systemctl stop tradinebotte` if using systemd

---

## Other installation methods

See [INSTALL.md](INSTALL.md) for:
- **git clone** — recommended when GitHub is accessible from the target machine
- **rsync** — recommended for VPS without git (deploy from a local dev machine)
- Full details on multi-account setup (Option B — ZeroMQ shared WebSocket)
