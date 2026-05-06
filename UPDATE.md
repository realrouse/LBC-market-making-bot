# tradinebotte — Update Guide

> 🇫🇷 [Version française](UPDATE.fr.md) · Full reference: [INSTALL.md](INSTALL.md)

---

## What `install.sh` does on an update

When the virtualenv already exists, `install.sh` **skips creation** and only upgrades
pip and packages. It never touches `config.json`, `live.db`, or log files.

---

## Scenario 1 — Repo and install dir are separate

Typical layout: repo cloned to `~/src/tradinebotte`, bot installed to `~/tradinebotte`.

```bash
cd ~/src/tradinebotte
git pull
bash scripts/install.sh      # reuses ~/tradinebotte/venv, upgrades packages only

pkill -f live_bot.py
bash scripts/start_bot.sh
# or: sudo systemctl restart tradinebotte
```

`config.json` is untouched — no need to re-run `setup.py`.

---

## Scenario 2 — Repo = install dir

Typical layout: repo cloned directly into `~/tradinebotte`.

```bash
cd ~/tradinebotte
git pull
bash scripts/install.sh

pkill -f live_bot.py
bash scripts/start_bot.sh
```

Same guarantees — `config.json` and `live.db` are safe.

---

## Scenario 3 — Deploying from a dev machine via rsync

```bash
rsync -az --delete \
    --exclude='config.json' \
    --exclude='live.db' \
    --exclude='*.log' \
    --exclude='venv/' \
    /path/to/tradinebotte/ user@server:~/tradinebotte/

ssh user@server 'cd ~/tradinebotte && bash scripts/install.sh'
ssh user@server 'pkill -f live_bot.py; bash ~/tradinebotte/scripts/start_bot.sh'
```

**Critical exclusions:**
- `--exclude='config.json'` — prevents wiping live credentials **and language preference** (`"lang"` field set by `setup.py` / `install.sh`)
- `--exclude='live.db'` — preserves trade history
- `--exclude='venv/'` — avoids transferring hundreds of MB over the network

Without `--exclude='config.json'`, `rsync --delete` will delete the file and
`start_bot.sh` will refuse to start. Re-run `setup.py` if that happens (it will
re-prompt for language and regenerate the file).

---

## Option B — Multi-bot update

Update the shared repo and restart. Account dirs (`~/account-a`, etc.) are not touched.

```bash
cd ~/src/tradinebotte   # or wherever the repo lives
git pull
bash scripts/install.sh

pkill -f feed.py
pkill -f account_bot.py

TRADINEBOTTE_DIR=~/account-a bash scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash scripts/start_account.sh
```

---

## Verify the update

```bash
pgrep -fa live_bot.py          # confirm the process is running
tail -5 ~/tradinebotte/live.log  # confirm clean startup, no errors
```
