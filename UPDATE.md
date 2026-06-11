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
bash scripts/install.sh      # reuses ~/tradinebotte/.venv, upgrades packages only

kill $(cat ~/tradinebotte/live.pid)
~/tradinebotte/run.sh
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

kill $(cat ~/tradinebotte/live.pid)
~/tradinebotte/run.sh
```

Same guarantees — `config.json` and `live.db` are safe.

---

## Scenario 3 — Deploying from a dev machine via rsync

```bash
rsync -az --delete \
    --exclude='config.json' \
    --exclude='live.db' \
    --exclude='*.log' \
    --exclude='venv/' --exclude='.venv/' \
    /path/to/tradinebotte/ user@server:~/tradinebotte/

ssh user@server 'cd ~/tradinebotte && bash scripts/install.sh'
ssh user@server 'kill $(cat ~/tradinebotte/live.pid); ~/tradinebotte/run.sh'
```

**Critical exclusions:**
- `--exclude='config.json'` — prevents wiping live credentials **and language preference** (`"lang"` field set by `setup.py` / `install.sh`)
- `--exclude='live.db'` — preserves trade history
- `--exclude='venv/' --exclude='.venv/'` — avoids transferring hundreds of MB over the network (covers both `venv/` and `.venv/` layouts)

Without `--exclude='config.json'`, `rsync --delete` will delete the file and
`start_bot.sh` will refuse to start. Re-run `setup.py` if that happens (it will
re-prompt for language and regenerate the file).

---

## Scenario 4 — Lightweight deploy with `update_standalone.sh`

For deploying only the bot files (no full repo sync), use the dedicated script:

```bash
bash tradinebotte-polymarket/scripts/update_standalone.sh
```

This rsync-copies `tradinebotte-polymarket/` contents flat to the install directory,
`tradinebotte-polymarket/strategies/*.json`, `tradinebotte-cex/connectors/`,
`tradinebotte-cex/strategy_engines/`, `requirements.txt`, and `tradinetools/`, then runs
`pip install -r requirements.txt` to update Python dependencies before stopping the running
bot (via `live.pid`) and starting the new version in a single SSH session. Useful when
working from a dev machine without pushing to git first.

**Options:**
- `--skip-restart` — rsync only, do not stop/start the bot
- `--verify-only` — check that the deployed files are present and the bot is running; no file transfer

---

## Scenario 5 — Deploying the swing strategy account

For the dedicated swing trading deployment account, use the swing-specific deploy script:

```bash
bash tradinebotte-cex/scripts/update_swing.sh
```

This script rsync-copies the swing strategy engine and config to the swing account's install directory, writes its `config.json`, then restarts the bot — preferring `systemctl --user restart tradinebotte-live.service` if the user service is active or enabled, falling back to nohup otherwise. Verify step uses `pgrep`/`/proc/$P/exe` filtering, matching the approach of `update_standalone.sh`.

---

## Scenario 6 — Deploying a CEX strategy

Three deploy scripts cover the CEX sub-service strategies. Each rsync-copies the relevant engine and config, restarts the running bot via its PID file, and verifies the process — all in a single SSH session.

```bash
# Scalping bot (Binance OBI)
bash tradinebotte-cex/scripts/deploy_scalping_claude4.sh

# BTC accumulation bot v1.5
bash tradinebotte-cex/scripts/deploy_accumulation_claude4.sh

# Swing strategy
bash tradinebotte-cex/scripts/update_swing.sh
```

---

## v0.50 — Indicators service update notes

v0.50 adds new streams and parameters to `tradinebotte-indicators/indicators.py`. When updating from v0.49:

1. Restart the shared indicators service **before** restarting any dependent bots (accumulation, scalping, swing):

```bash
# With systemd user service (recommended — no sudo):
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user restart tradinebotte-indicators.service
# or, using the PID file:
kill $(cat ~/tradinebotte/indicators.pid)
bash tradinebotte-indicators/scripts/start_indicators.sh
```

2. To enable the shared SQLite orderbook database for a stream, add the following keys to the stream entry in the indicators JSON config (all are optional — omitting `db_path` or leaving it empty disables DB writes):

```json
{
  "stream_id": "btc_full_depth_perp",
  "market": "perp",
  "db_path": "",
  "bucket_size_usd": 50,
  "db_write_every_n": 60,
  "history_retention_h": 24
}
```

The DB file is created with `0o644` permissions. Ensure the path's parent directory is writable by the indicators service user.

3. No changes to `config.json`, `live.db`, or any bot strategy JSON files are required for this update.

---

## Option B — Multi-bot update

Update the shared repo and restart. Account dirs (`~/account-a`, etc.) are not touched.

```bash
cd ~/src/tradinebotte   # or wherever the repo lives
git pull
bash scripts/install.sh

kill $(cat ~/tradinebotte/feed.pid)
kill $(cat ~/account-a/account.pid)
kill $(cat ~/account-b/account.pid)

bash tradinebotte-polymarket/scripts/start_feed.sh
TRADINEBOTTE_DIR=~/account-a bash tradinebotte-polymarket/scripts/start_account.sh
TRADINEBOTTE_DIR=~/account-b bash tradinebotte-polymarket/scripts/start_account.sh
```

---

## Multi-bot status dashboard — output path

Since v0.80, `generate_status.py` writes to `~/public_html/tradinebottestatus.html`
by default instead of stdout. If you were piping the output in a script or cron job,
add `--out /dev/stdout` (or `--out -`) to restore the previous behaviour, or set
`TRADINEBOTTE_STATUS_OUT` to your preferred path.

---

## Verify the update

```bash
pgrep -fa live_bot.py          # confirm the process is running
tail -5 ~/tradinebotte/live.log  # confirm clean startup, no errors
```
