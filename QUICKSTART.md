# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · Updating: [UPDATE.md](UPDATE.md)

## Prerequisites

- Python 3.8+ on Linux/Mac (dedicated server or local machine)
- Polygon EOA wallet — MATIC > 0.1 (gas) and USDC.e > $10
- **No wallet yet?** Press Enter at the `setup.py` prompt → simulation mode, no real orders

---

## Install from official release (tar.gz)

```bash
# Download the latest release (replace v0.63 with the current version)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.63.tar.gz
tar -xzf v0.63.tar.gz
cd tradinebotte-0.63
bash scripts/install.sh        # detects missing system packages; prompts for language (E/F)
python3 scripts/setup.py       # prompts for language (saved to config.json); Enter = simulation mode
~/tradinebotte/run.sh
tail -f ~/tradinebotte/live.log
```

Monitor: `bash tradinebotte-polymarket/scripts/monitor.sh`  
Auto-restart on reboot: see [INSTALL.md — systemd setup](INSTALL.md#auto-start-with-systemd-recommended-for-dedicated-servers)

**Stop:** `kill $(cat ~/tradinebotte/live.pid)` · or `systemctl --user stop tradinebotte-live.service` (user unit, no sudo)

---

## Other installation methods

See [INSTALL.md](INSTALL.md) for:
- **git clone** — recommended when GitHub is accessible from the target machine
- **rsync** — recommended for servers without git (deploy from a local dev machine)
- Full details on multi-account setup (Option B — ZeroMQ three-service architecture)

### Multi-account setup (Option B) — summary

Option B runs three systemd user services per deployment: **indicators**, **feed**, and **account_bot**.
All three communicate over IPC sockets in `/run/user/$UID/` — no TCP port conflicts between Linux users.

One-time admin step per VPS user (root required):

```bash
sudo loginctl enable-linger <bot_username>
```

Then as the bot user (no sudo needed):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user enable --now tradinebotte-indicators.service
systemctl --user enable --now tradinebotte-feed.service
systemctl --user enable --now tradinebotte-account.service
```

Full procedure (unit file templates, tradinetools install, config): [INSTALL.md — Multi-bot WebSocket sharing](INSTALL.md#multi-bot-websocket-sharing-option-b--zeromq)
