# tradinebotte — Quick Start

> 🇫🇷 [Version française](QUICKSTART.fr.md) · Full guide: [INSTALL.md](INSTALL.md) · Updating: [UPDATE.md](UPDATE.md)

## Prerequisites

- Python 3.8+ on Linux/Mac (dedicated server or local machine)
- Per strategy family, real trading needs exchange credentials (see
  [docs/going-live.md](docs/going-live.md)) — Polymarket needs a Polygon EOA
  wallet (MATIC > 0.1 for gas, USDC.e > $10); CEX strategies (grid/swing/DCA/
  accumulation) need a Binance/MEXC/Bitstamp API key instead.
- **No credentials yet?** Every strategy family runs in **simulation mode**
  by default (no key = paper trading, no real orders) — nothing here is
  Polymarket-specific.

---

## Install from official release (tar.gz)

```bash
# Download the latest release (replace v0.89.1 with the current version)
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.89.1.tar.gz
tar -xzf v0.89.1.tar.gz
cd tradinebotte-0.89.1
bash scripts/install.sh        # detects missing system packages; prompts for language (E/F)
python3 scripts/setup.py       # prompts for language (saved to config.json); Enter = simulation mode
~/tradinebotte/run.sh
tail -f ~/tradinebotte/live.log
```

Monitor: `bash tradinebotte-polymarket/scripts/monitor.sh`  
Multi-bot status page: `python3 tradinebotte-status/generate_status.py` → `~/public_html/tradinebottestatus.html`  
Auto-restart on reboot: see [INSTALL.md — systemd setup](INSTALL.md#auto-start-with-systemd-recommended-for-dedicated-servers)

**Stop:** `kill $(cat ~/tradinebotte/live.pid)` · or `systemctl --user stop tradinebotte-live.service` (user unit, no sudo)

---

## Other installation methods

See [INSTALL.md](INSTALL.md) for:
- **git clone** — recommended when GitHub is accessible from the target machine
- **rsync** — recommended for servers without git (deploy from a local dev machine)

## Running more than one bot (multi-account / multi-strategy)

Every trading bot — whatever the strategy family, across as many accounts
as needed — deploys natively into a single shared `~/tradinebotte/` tree,
driven by `inventory.toml` (one `[[bot]]` row per bot, the fleet's single
source of truth — local and git-ignored, since it describes your own
accounts/bots):

```bash
cp inventory.toml.example inventory.toml                 # once, then edit it for your fleet
bash tradinebotte-cex/scripts/deploy_all.sh              # deploy/redeploy the whole fleet
bash tradinebotte-cex/scripts/deploy_all.sh --only <tok>  # target one account/bot
```

Add a bot by adding a `[[bot]]` row to `inventory.toml`, then redeploy. See
[INSTALL.md — Multi-bot / multi-account deployment](INSTALL.md#multi-bot--multi-account-deployment)
for the full schema and the retired "Option B" architecture this replaced.
