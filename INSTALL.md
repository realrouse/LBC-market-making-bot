# tradinebotte — Installation Guide

> 🇫🇷 [Version française](INSTALL.fr.md) · First time? Start with [QUICKSTART.md](QUICKSTART.md)


## Requirements

- Python 3.8+
- A Polygon mainnet wallet (EOA — NOT Safe/Gnosis multisig)
- MATIC > 0.1 (gas fees)
- USDC.e > $10 on Polygon (`0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174`)
  Note: NOT USDC native (`0x3c499c...`) — setup.py handles the swap automatically
- `sqlite3` CLI (optional but recommended for monitoring queries)
  The bot uses Python's built-in sqlite3 module and works without the CLI.
  Install on Debian/Ubuntu: `sudo apt install sqlite3`
  Without sudo, use the Python alternative:
  ```bash
  ~/tradinebotte/.venv/bin/python3 -c \
    "import sqlite3; c=sqlite3.connect('live.db'); \
     print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"
  ```

### Server admin prerequisites (Debian/Ubuntu)

`scripts/install.sh` **detects missing packages automatically** and prints
the exact `sudo apt-get install` command to run as root — no need to know
the package names in advance.

Just run the script as a normal user:

```bash
bash scripts/install.sh
```

If anything is missing, you will see:

```
ERREUR : paquets système manquants. Lance cette commande en root (une seule fois par machine) :

  sudo apt-get install -y python3-venv python3.10-venv
```

The version number (`3.10`) is detected from the system Python — no manual
substitution needed. Run the printed command as root, then re-run `install.sh`.

Once installed, `install.sh` puts all Python dependencies inside an
isolated venv and **never touches the system Python again**.


## Dependencies

The following Python packages are installed automatically by `scripts/install.sh`
into a virtualenv at `~/tradinebotte/.venv/`:

- `aiohttp`
- `websockets`
- `web3`
- `py-clob-client`
- `pyzmq`
- `bcrypt`

The `tradinetools` shared library is installed separately as an editable package:

```bash
pip install -e tradinetools/
```

The canonical list is `requirements.txt` at the project root. CVEs in these
packages are detected automatically on every push via `pip-audit` (GitHub Actions)
and Dependabot opens PRs when newer versions are available.

Dev dependencies (`pylint`, `pip-audit`, `mypy`) are declared in `requirements-dev.txt`.


## Obtaining the source code

Three methods are available depending on your setup. All three lead to the same
`bash scripts/install.sh` step.

### Method 1 — Git clone (recommended when GitHub is accessible)

```bash
git clone https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

To install a specific release:

```bash
git clone --branch v0.63 https://github.com/neofutur/tradinebotte.git
cd tradinebotte
bash scripts/install.sh
```

### Method 2 — rsync from a local dev machine (recommended for servers without git)

From your local machine where the repo is already cloned:

```bash
rsync -a --exclude='*.db' --exclude='__pycache__' --exclude='.git' --exclude='venv' --exclude='.venv' \
  /path/to/tradinebotte/ user@server:~/tradinebotte/
ssh user@server "cd ~/tradinebotte && bash scripts/install.sh"
```

To update an existing install (preserves `config.json`):

```bash
rsync -a --exclude='*.db' --exclude='__pycache__' --exclude='.git' --exclude='venv' --exclude='.venv' \
  --exclude='config.json' \
  /path/to/tradinebotte/ user@server:~/tradinebotte/
ssh user@server "cd ~/tradinebotte && bash scripts/install.sh"
```

> The `--exclude='config.json'` flag is critical on updates — without it rsync
> overwrites the live credentials file.

For bot-only updates (no full repo sync), use `tradinebotte-polymarket/scripts/update_standalone.sh`:

```bash
bash tradinebotte-polymarket/scripts/update_standalone.sh            # rsync tradinebotte-polymarket/ + strategies/*.json, then restart
bash tradinebotte-polymarket/scripts/update_standalone.sh --skip-restart   # rsync only
bash tradinebotte-polymarket/scripts/update_standalone.sh --verify-only    # check files and process, no transfer
```

This stops the bot via `live.pid`, syncs only the necessary files, and restarts in a
single SSH session. See [UPDATE.md](UPDATE.md) for the full scenario.

### Method 3 — Official release tar.gz (no git required)

Download the latest release archive from the
[Releases page](https://github.com/neofutur/tradinebotte/releases):

```bash
# Replace v0.63 with the version you want
wget https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.63.tar.gz
tar -xzf v0.63.tar.gz
cd tradinebotte-0.63
bash scripts/install.sh
```

Or with `curl`:

```bash
curl -L https://github.com/neofutur/tradinebotte/archive/refs/tags/v0.63.tar.gz \
  | tar -xz
cd tradinebotte-0.63
bash scripts/install.sh
```

The directory is named `tradinebotte-<version>` after extraction. The install
script detects its own location automatically — no path adjustment needed.


## Installation directory

All scripts read the `TRADINEBOTTE_DIR` environment variable to determine
where to install and run the bot. If unset, it defaults to:

```
~/tradinebotte
```

No root access required — the default is in the user's home directory.

Examples:

```bash
# Default (installs in ~/tradinebotte, no root needed)
bash scripts/install.sh

# Custom path as argument
bash scripts/install.sh ~/tradinebotte

# Custom path via environment variable
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/install.sh
```

The same variable must be set consistently for setup, start, and monitor:

```bash
export TRADINEBOTTE_DIR=~/tradinebotte
```


## Environment variables

All environment variables are optional — each has a sensible default.
For persistent values, most can also be stored in `config.json` (see
[Wallet Setup](#wallet-setup-one-time)) instead of being set in the shell.

### Resolution order

When a value can come from multiple sources, the priority is:

```
config.json  >  environment variable  >  built-in default
```

Environment variables always win over built-in defaults, but `config.json`
takes precedence over both.

### Reference table

| Variable | config.json key | Default | Scope | Description |
|---|---|---|---|---|
| `TRADINEBOTTE_DIR` | — | `~/tradinebotte` | all scripts | Runtime directory: where `config.json`, `live.db`, `live.log`, the venv, and strategy files are stored. **No config.json key** — this is the bootstrap path needed to locate the file in the first place. |
| `TRADINEBOTTE_FEED_ADDR` | `feed_addr` | IPC auto-detected (`/run/user/$UID/tradinebotte-feed.sock`) | feed, account\_bot, indicators | ZeroMQ PUB/SUB address for the shared WebSocket feed (Option B multi-bot). Leave unset for IPC (single-host). Set to `tcp://127.0.0.1:5557` to force TCP, e.g. when running multiple independent stacks or cross-host. |
| `TRADINEBOTTE_PORT_BASE` | — | (unset) | feed, account\_bot, indicators | When set, switches all address defaults to TCP and shifts ports by `PORT_BASE − 5557`. E.g. `TRADINEBOTTE_PORT_BASE=6557` runs a second independent TCP stack at 6557/6559/6561. Leave unset for IPC (recommended). |
| `TRADINEBOTTE_INDICATORS_ADDR` | `indicators_addr` | IPC auto-detected (`/run/user/$UID/tradinebotte-indicators.sock`) | indicators, account\_bot | ZeroMQ PUB address where the shared indicators service publishes enriched messages. `account_bot` subscribes here when `indicators_streams` is set. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `indicators_reg_addr` | IPC auto-detected (`/run/user/$UID/tradinebotte-ind-reg.sock`) | account\_bot | ZeroMQ REP address of the shared indicators service for dynamic stream registration. Each `account_bot` sends subscribe requests here at startup. |
| — | `feed_auto_start` | `true` | account\_bot | When `false`, `account_bot` expects `feed.py` to be managed externally (e.g. systemd); probes with retries instead of auto-starting it. Exits if the feed is unreachable after 30 s. |
| — | `indicators_streams` | `[]` | account\_bot | List of stream subscription specs sent to the shared indicators service at startup. See [Technical Indicator Service](#technical-indicator-service). |
| `TRADINEBOTTE_INSTALL_DIR` | — | auto-detected | install scripts | Override the install directory the deploy engine uses when searching for the virtualenv. |
| `POLY_PRIVATE_KEY` | `private_key` | `""` | live\_bot, account\_bot | Polygon wallet private key (`0x` + 64 hex chars). If empty, orders are simulated with no on-chain execution. |
| `POLY_API_KEY` | `api_key` | `""` | live\_bot, account\_bot | Polymarket CLOB API key (derived by `setup.py`). |
| `POLY_API_SECRET` | `api_secret` | `""` | live\_bot, account\_bot | Polymarket CLOB API secret. |
| `POLY_PASSPHRASE` | `api_passphrase` | `""` | live\_bot, account\_bot | Polymarket CLOB API passphrase. |
| `MEXC_API_KEY` | — | `""` | api\_mexc | MEXC exchange API key. Env var only — no `config.json` key. |
| `MEXC_API_SECRET` | — | `""` | api\_mexc | MEXC exchange API secret. Env var only. |
| `BINANCE_API_KEY` | — | `""` | api\_binance | Binance exchange API key. Env var only. |
| `BINANCE_API_SECRET` | — | `""` | api\_binance | Binance exchange API secret. Env var only. |

### Systemd services and environment inheritance

Systemd system services do **not** inherit the shell environment (`.bashrc`,
`.profile`, etc.).  The generated unit files handle this in two ways:

1. **Inline `Environment=`** — non-sensitive runtime paths (`TRADINEBOTTE_DIR`,
   `TRADINEBOTTE_FEED_ADDR`) are baked into the unit file by the install script.
2. **`EnvironmentFile=`** — each service loads `<TRADINEBOTTE_DIR>/credentials`
   if it exists (the `-` prefix makes it optional — a missing file is silently
   ignored).  Create this file for API keys that are not stored in `config.json`:

```bash
# Example: ~/.../tradinebotte/credentials  (chmod 600)
MEXC_API_KEY=...
MEXC_API_SECRET=...
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
# POLY_* credentials are usually in config.json — add here only as override
```

```bash
chmod 600 ~/tradinebotte/credentials
```

> Polymarket credentials (`POLY_*`) are written to `config.json` by `setup.py`
> and loaded automatically.  The `credentials` file is only needed for secrets
> that have no `config.json` key (MEXC, Binance) or to override `config.json`
> values without editing the file.


## Installation

Run the install script from the repository root:

```bash
bash scripts/install.sh [install_dir] [--lang EN|FR] [--with-tests]
```

**Options:**
- `--lang EN|FR` — Set language non-interactively (useful for CI or automated deploys).
  Without this flag the script prompts at startup as before.
- `--with-tests` — Also copy `tests/`, `analysis/backtest.py`, and
  run the test suite immediately after installation.
  The backtest uses `live.db` only if it contains ≥ 100 snapshots;
  otherwise it falls back to the bundled sample dataset automatically.

This will:
- Install system packages (python3, pip, venv, sqlite3)
- Create the install directory
- Copy the entrypoint + plugin modules flat to `<TRADINEBOTTE_DIR>/`: `live_bot.py`, `api_polymarket.py`, the Polymarket plugin (`pm_types.py`, `pm_calendar.py`, `pm_strategy.py`, `pm_data.py`), the CEX glue (`api_binance.py`, `api_mexc.py`, `cex_consumer.py`), the whole `botcore/` package, and `connectors/__init__.py`
- Copy `tradinebotte-polymarket/strategies/*.json` to `<TRADINEBOTTE_DIR>/strategies/`
- Create a virtualenv at `<TRADINEBOTTE_DIR>/.venv/`
- Install Python dependencies into the virtualenv
- Generate `<TRADINEBOTTE_DIR>/run.sh` (wrapper with `TRADINEBOTTE_DIR` pre-set)
- Verify bot syntax


## Wallet Setup (one-time)

Run `setup.py` once before starting the bot — it creates `config.json`:

```bash
python3 scripts/setup.py
```

When prompted for a private key:
- **Real wallet:** enter `0x` + 64 hex characters — the script checks balances,
  swaps USDC native → USDC.e if needed, approves the CTF Exchange, derives your
  Polymarket API credentials, and writes `<TRADINEBOTTE_DIR>/config.json` (chmod 600).
- **Simulation (no wallet):** press Enter without typing a key — the script writes
  a minimal `config.json` with empty credentials; the bot runs with simulated orders
  and no on-chain transactions occur.

The private key is entered interactively and is never visible in `ps aux`
or shell history.

The bot reads credentials from `config.json` at startup. If the file is
absent it falls back to environment variables:
`POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_PASSPHRASE`

See `config.json.example` for the expected file structure.

> **WARNING:** Never commit `config.json`. It is listed in `.gitignore`.


## Web Status Page (optional)

The bot can publish a static HTML status page readable in any browser.
Enable it by adding these keys to `config.json`:

```json
"webstatuspage_html": true,
"webstatuspage_path": "~/public_html/tradinebot_status.html",
"webstatus_user":     "tradinebot",
"webstatus_password": "yourpassword"
```

The page displays capital, total and daily PnL, win rate, open positions,
and the 10 most recent resolved trades. It auto-refreshes every 60 s.
The bot creates the HTML directory automatically and writes a `.htaccess` in
it. The `.htpasswd` file is stored at `TRADINEBOTTE_DIR/.webstatus_htpasswd`
(outside the web root).

### Prerequisites — Apache

1. Enable required modules (Debian/Ubuntu):

   ```bash
   sudo a2enmod userdir       # serves ~/public_html — skip if using a
                              #   custom path already under DocumentRoot
   sudo a2enmod auth_basic    # HTTP Basic Auth support
   sudo a2enmod authn_file    # reads credentials from .htpasswd files
   sudo systemctl reload apache2
   ```

2. Allow `.htaccess` overrides in the HTML directory.
   Edit `/etc/apache2/mods-enabled/userdir.conf` (or your VirtualHost):

   ```apache
   <Directory /home/*/public_html>
       AllowOverride AuthConfig
       Options Indexes FollowSymLinks
       Require all granted
   </Directory>
   ```

   Then reload: `sudo systemctl reload apache2`

3. Grant the Apache process read access to the `.htpasswd` file.
   The file is written as chmod 640 (owner read/write, group read).
   Apache runs as `www-data` and must be able to read it:

   ```bash
   # Option A — world-readable (simpler, exposes hash to local users)
   chmod o+r $TRADINEBOTTE_DIR/.webstatus_htpasswd

   # Option B — add www-data to the bot user's primary group (safer)
   sudo usermod -aG $(id -gn $USER) www-data
   sudo systemctl reload apache2
   ```

### Prerequisites — nginx

nginx does not process `.htaccess` files. The bot still writes the page
to disk, but the password protection has no effect. Configure Basic Auth
directly in your nginx server block instead:

```nginx
location /tradinebot_status.html {
    auth_basic "Tradinebot Status";
    auth_basic_user_file /path/to/.webstatus_htpasswd;
}
```

The `.htpasswd` generated by the bot uses Apache `{SHA}` format, which nginx
also supports. Use the `TRADINEBOTTE_DIR/.webstatus_htpasswd` path.

If you use a custom `webstatuspage_path` outside `~/public_html`, make sure
your web server is configured to serve that directory.


## Multi-bot status dashboard

`tradinebotte-status/generate_status.py` connects to all deployment accounts via SSH,
reads `heartbeat.db` from the status-collector service, and produces a single HTML page
showing the live health of every bot across all accounts.

### Prerequisites

- The `tradinebotte-status.service` systemd user unit must be running on the first
  deployment account (the heartbeat collector). It listens on `tcp://127.0.0.1:5562`
  for ZMQ heartbeat messages from every bot and writes them to `heartbeat.db`.
- SSH credentials for all accounts must be present in `~/.tradinebotte-test.conf`
  (same format used by all deploy scripts).

### Quick start

```bash
python3 tradinebotte-status/generate_status.py
```

Without `--out` or `$TRADINEBOTTE_STATUS_OUT` the page is written to
`~/public_html/tradinebottestatus.html`. The directory is created automatically if absent.

### Output path

Three levels of configuration, evaluated in priority order:

| Priority | Method | Notes |
|---|---|---|
| 1 | `--out /path/to/file.html` | CLI flag — overrides everything |
| 2 | `TRADINEBOTTE_STATUS_OUT=/path/to/file.html` | Environment variable — set in shell or systemd `Environment=` |
| 3 | `~/public_html/tradinebottestatus.html` | Built-in default |

To write to stdout (e.g. for piping):

```bash
python3 tradinebotte-status/generate_status.py --out /dev/stdout
```

### Credentials file

Default: `~/.tradinebotte-test.conf`. Override with `--conf`:

```bash
python3 tradinebotte-status/generate_status.py --conf /path/to/other.conf
```

### What the page shows

- **Heartbeat table** — one row per bot: account label, bot name, age of last heartbeat,
  status, bounds flag, deployed version, and a DETAILS column with bot-type-specific
  payload fields:
  - `live_bot` / `account_bot` — daily PnL, capital (live_bot only), open trades,
    last book update timestamp
  - `accumulation_bot` — BTC holdings, free USDT, average entry price, total realised PnL
  - `orderbook_bot` — open positions, total PnL, last price
  - `feed` — WebSocket connected flag, total messages processed, last book timestamp
  - `indicators` — last publication timestamp
- **Per-account cards** — active trade list, recent resolved trades, CEX metrics
- **Generation timestamp** and total collection time in seconds

### Scheduling — systemd `--user` timer (recommended)

Install the versioned timer; it regenerates the page every 2 minutes and is reproducible
from the checkout (no hand-maintained crontab):

```bash
bash tradinebotte-status/scripts/install_statuspage_timer.sh
```

The installer self-locates the repo and venv, enables + starts
`tradinebotte-statuspage.timer`, and warns if user *linger* is disabled (without linger the
timer pauses when you log out — enable it with `loginctl enable-linger <user>`). Inspect it
with `systemctl --user list-timers tradinebotte-statuspage.timer` and
`journalctl --user -u tradinebotte-statuspage.service`.

### Scheduling with cron (alternative)

If you prefer cron over a systemd timer, add to crontab (`crontab -e`). Use only one of the
two methods, or the two runs race on the output file:

```
*/5 * * * * TRADINEBOTTE_STATUS_OUT=~/public_html/tradinebottestatus.html \
    /home/<user>/tradinebotte/.venv/bin/python3 \
    /home/<user>/tradinebotte/generate_status.py \
    >> ~/tradinebotte/status_gen.log 2>&1
```

### Web server — Apache mod_userdir

If the output goes to `~/public_html/`, enable `mod_userdir` so Apache serves the page
at `http://server/~<user>/tradinebottestatus.html`:

```bash
sudo a2enmod userdir
sudo systemctl reload apache2
```

For password protection on the directory, see
[Web Status Page — Prerequisites — Apache](#prerequisites--apache).


## Running

```bash
~/tradinebotte/run.sh
```

### Auto-start with systemd

Bots run as `systemctl --user` units installed by the native deploy engine — there is no
separate hand-run installer. Deploy a bot (or the whole fleet) with:

```bash
bash tradinebotte-cex/scripts/deploy_all.sh          # whole fleet (thin shim over scripts/deploy.py)
```

The engine writes each unit, enables it (linger keeps it across reboots), and restarts it. Units
restart on failure and come back on reboot once the network is up. Inspect a running bot with
`systemctl --user status <unit>` and `journalctl --user -u <unit> -f`.

> **Shared infrastructure services** (feed, indicators, collector) are installed natively by `scripts/deploy.py`. See [docs/multi.md](docs/multi.md) for the shared-feed architecture.
>
> **Multi-account server deployments** use `~/.config/systemd/user/` units (`systemctl --user`) instead of system units — no sudo required at deploy time. Deploy them with `scripts/deploy.py` (or `bash tradinebotte-cex/scripts/deploy_all.sh`), which installs every unit natively.

**Flags:**
- *(no flag)* — normal mode: log writes are asynchronous (daemon thread, never blocks the event loop)
- `--no-log` — suppress the log file entirely for minimum disk I/O; SQLite DB (trades + snapshots) is unaffected; combine with `--simulate` to keep stdout output
- `--no-snapshots` — skip writing 5-second price snapshots to the DB; trades are still recorded; reduces write pressure during long sessions; use when snapshot data is not needed for post-analysis
- `--reset-db` — back up `live.db` to `live.db.bak.YYYYMMDD_HHMMSS` then delete it before launch; bot starts from zero capital and trade history; prompts for `yes` confirmation; safe no-op if DB is absent
- `--snapshot-interval SECS` — override the snapshot write interval in seconds (default: 5); use `1` for data-collection mode where 1-second resolution is needed for strategy research
- `--simulate` — isolate all file I/O to `~/tradinebotte-sim` by default, no real orders placed. If `TRADINEBOTTE_DIR` is already set in the environment, that path is used instead — allowing multiple bots to run in parallel without conflict:
  ```bash
  TRADINEBOTTE_DIR=~/account-a python3 live_bot.py --simulate
  TRADINEBOTTE_DIR=~/account-b python3 live_bot.py --simulate
  ```

Or using the generated wrapper (`TRADINEBOTTE_DIR` already embedded):

```bash
~/tradinebotte/run.sh
```

Verify the bot is running:

```bash
pgrep -fa live_bot.py
```

`start_bot.sh` refuses to start if an instance is already running (to avoid
interrupting an open trade). Stop it manually first if needed:

```bash
kill $(cat ~/tradinebotte/live.pid)
```

The start script writes `live.pid` automatically. Similarly, `feed.pid`, `account.pid`,
and `indicators.pid` are written by their respective start scripts. Use
`kill $(cat <path>.pid)` to stop any of these processes. Stale PID files from a
crashed process are cleaned automatically on the next start.

- Logs: `<TRADINEBOTTE_DIR>/live.log`
- Trades: `<TRADINEBOTTE_DIR>/live.db` (SQLite)


## Latency analysis

Each trade emits one `[LATENCY]` line in `live.log`. Run the analysis tool after a trading session:

```bash
python3 analysis/latency.py                           # default path
python3 analysis/latency.py ~/tradinebotte/live.log   # explicit path
TRADINEBOTTE_DIR=~/tradinebotte python3 analysis/latency.py
```

Example output:
```
==============================================================
  LATENCY REPORT — /home/botte/tradinebotte/live.log
  Trades: 42  (UP=27  DOWN=15)
==============================================================
  Metric             min    mean     p50     p90     p99     max
  ----------------------------------------------------------
  signal (ms)        1.2     2.1     1.9     3.4     5.1     6.0
  order RTT (ms)    98.3   143.2   138.7   201.4   310.2   340.5
  total (ms)        99.8   145.3   140.9   204.1   314.8   345.0
==============================================================
```

- **signal_ms** — time from WebSocket message received to order decision (includes all signal guards + daily-PnL SQLite query)
- **order_rtt_ms** — CLOB API HTTP round-trip
- **total_ms** — end-to-end: WebSocket message → order confirmed

## Backtesting

Run the backtest engine to replay recorded snapshots against any parameter set:

```bash
# Single file (default: live.db, or bundled sample if live.db has < 100 snapshots)
python3 analysis/backtest.py

# One or more explicit files (shell glob supported)
python3 analysis/backtest.py --db ~/tradinebotte/live.db
python3 analysis/backtest.py --db data/session1.db data/session2.db
python3 analysis/backtest.py --db data/*.db

# Scan data/ automatically (includes live.db if it has ≥ 100 snapshots)
python3 analysis/backtest.py --all

# Grid search across 135 threshold/stake combinations
python3 analysis/backtest.py --sweep
python3 analysis/backtest.py --all --sweep

# Extended grid search (405 combos) across all DBs — strategy optimisation
python3 analysis/backtest.py --sweep-all
python3 analysis/backtest.py --sweep-all --sort pnl   # sort by pnl|ratio|wr
python3 analysis/backtest.py --sweep-all --top 10     # top-10 unique configs (deduped)
```

When more than one file is processed, each file runs with capital reset to `capital_start` (independent simulation), and an AGGREGATE block summarises combined wins, losses, PnL, win rate, and worst drawdown across all files.

**Parameter flags** (override strategy JSON defaults for a single run):

| Flag | Default | Description |
|---|---|---|
| `--threshold FLOAT` | 0.95 | Entry signal threshold (`best_bid >= threshold`) |
| `--min-secs FLOAT` | 30.0 | Minimum seconds remaining at entry |
| `--min-ask FLOAT` | 10.0 | Minimum ask-side volume in USD at entry |
| `--obi FLOAT` | −0.25 | OBI reject threshold (entries with OBI below this are skipped) |
| `--stake FLOAT` | 10.0 | USD stake per trade |
| `--sweep-all` | — | Extended 405-combo grid search across all DBs (adds OBI and DSL axes) |
| `--sort METRIC` | `ratio` | Sort sweep results by `ratio` (PnL/MaxDD), `pnl`, or `wr` |
| `--top N` | 0 (all) | Show only top-N unique configs in sweep table (deduped on threshold/min_secs/obi) |
| `--detail` | — | Print the individual simulated trade table (one row per trade) |


## Technical Indicator Service

`tradinebotte-indicators/indicators.py` is a ZeroMQ pipeline stage that sits between feed.py and any consumer. It subscribes to the feed PUB socket, accumulates a price history per token, and republishes enriched indicator messages on a second PUB socket. All three Binance WebSocket loops are protected by a 120-second recv watchdog — if Binance stops sending data while keeping the TCP connection alive, the service detects the stall and reconnects automatically.

```
feed.py  PUB (IPC)  ──SUB──▶  indicators.py  ──PUB (IPC)──▶  consumers
```

```bash
# Start with default settings (IPC auto-detected from /run/user/$UID/)
python3 tradinebotte-indicators/indicators.py

# Custom periods
python3 tradinebotte-indicators/indicators.py --rsi 7 --sma 10 --ema 5 --vol 10

# Custom ZMQ addresses (TCP override example)
python3 tradinebotte-indicators/indicators.py --feed tcp://127.0.0.1:5558 --out tcp://127.0.0.1:5560

# Verbose (prints each indicator publish to stdout)
python3 tradinebotte-indicators/indicators.py --verbose
```

**Output message format:**

```json
{"t": "indicators", "token_id": "...", "ts": 1746800000000,
 "rsi_14": 72.3, "sma_20": 0.9612, "ema_9": 0.9634, "vol_20": 0.0021}
```

Messages are only published once `--min-ticks` (default: 25) price updates have been received **and** all indicator periods are satisfied.

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--config FILE` | — | Path to the indicators JSON config file (recommended) |
| `--feed ADDR` | IPC auto-detected | ZMQ address to subscribe to (feed.py PUB) |
| `--out ADDR` | IPC auto-detected | ZMQ PUB address to bind and publish on |
| `--reg-addr ADDR` | IPC auto-detected | ZMQ REP address for dynamic stream registration |
| `--rsi N` | 14 | RSI period |
| `--sma N` | 20 | SMA period |
| `--ema N` | 9 | EMA period |
| `--vol N` | 20 | Volatility window (std-dev of log-returns) |
| `--min-ticks N` | 25 | Minimum price ticks before any publish |
| `--verbose` | — | Print each publish at DEBUG level |

**Environment variables:** `TRADINEBOTTE_FEED_ADDR` and `TRADINEBOTTE_INDICATORS_ADDR` override the defaults for `--feed` and `--out`.

### Shared architecture — one instance, all bots register dynamically

The indicators service is a **shared process**: one instance runs on the machine (managed like the feed), and every `account_bot` registers the streams it needs at startup via the REP socket.

Each account declares its needs in `config.json`:

```json
{
  "indicators_reg_addr": "ipc:///run/user/1000/tradinebotte-ind-reg.sock",
  "indicators_streams": [
    {
      "source": "binance_ws",
      "asset":  "BTCUSDT",
      "timeframe": "4h",
      "indicators": [{"type": "rsi", "period": 14},
                     {"type": "vol", "period": 20}]
    }
  ]
}
```

`account_bot` connects to the REP socket at startup, sends each entry as a `{"cmd":"subscribe", ...}` request, and logs the assigned `stream_id`. A timeout is logged as a warning — the bot continues running without indicators.

Available sources: `binance_ws`, `binance_scalping`, `binance_funding`, `deribit_iv`, `fear_greed`, `feed`.

### Systemd service (recommended)

The indicators service is installed and managed natively by the deploy engine
(`scripts/deploy.py`, infra target `indicators`) — no separate install script:

```bash
bash tradinebotte-cex/scripts/deploy_all.sh --only "<account> — indicators"
```

Set `INDICATORS_LABEL=btc` in the inventory to name the service
`tradinebotte-indicators-btc` when running two independent indicator instances.

### Manual start

```bash
python3 tradinebotte-indicators/indicators.py --config tradinebotte-indicators/strategies/indicators_4h_bitcoin.json
```

Ready-to-use config files in `tradinebotte-indicators/strategies/`:

| File | Sources |
|---|---|
| `tradinebotte-indicators/strategies/indicators_all.json` | 9-stream unified config: Binance 4h klines (EMA50, EMA200, ATR14), 1d klines, funding rate, Deribit DVOL, Fear & Greed, scalping (depth20 + aggTrade) |
| `tradinebotte-indicators/strategies/indicators_4h_bitcoin.json` | Binance BTC/USDT 4h klines |
| `tradinebotte-indicators/strategies/indicators_1d_bitcoin.json` | Binance BTC/USDT 1d klines |
| `tradinebotte-indicators/strategies/indicators_funding_bitcoin.json` | Binance perpetual funding rate |
| `tradinebotte-indicators/strategies/indicators_deribit_iv_bitcoin.json` | Deribit DVOL implied volatility |
| `tradinebotte-indicators/strategies/indicators_fear_greed.json` | Alternative.me Fear & Greed index |

Streams added in v0.50 (`btc_full_depth` and `btc_full_depth_perp`) are configured inline with the following stream-level parameters:

| Parameter | Default | Description |
|---|---|---|
| `market` | `"spot"` | `"spot"` or `"perp"` — selects the Binance REST and WebSocket endpoints for the full-depth book |
| `bid_depth_pct` | `0` | Trim bids to this percentage window below mid-price; `0` = disabled |
| `ask_depth_pct` | `0` | Trim asks to this percentage window above mid-price; `0` = disabled |
| `db_path` | `""` | Path to the shared SQLite orderbook database; empty string disables DB writes |
| `bucket_size_usd` | `50` | Price bucket width in USD for the `orderbook_current` table |
| `db_write_every_n` | `60` | Write to the DB every N publish cycles (approximately once per minute at 1 Hz) |
| `history_retention_h` | `24` | Retention period for the `orderbook_snapshots` ring-buffer, in hours |


## Grid Trading Backtest

Replay historical BTC/USDT OHLCV data against a configurable grid strategy. Fill model: price-touch on the candle `[low, high]` range. Requires 1-minute SQLite databases in `data/` — download with `analysis/download_btc_history.py`.

```bash
# Static grid (default) — all DBs in data/
python3 analysis/backtest_grid.py --all

# Bear-adapted trailing — re-center grid downward on each exit_low
python3 analysis/backtest_grid.py --all --trail bear

# Bull-adapted trailing — re-center grid upward on each exit_high
python3 analysis/backtest_grid.py --all --trail bull

# Side-by-side comparison: static vs trailing
python3 analysis/backtest_grid.py --all --trail bear --compare
python3 analysis/backtest_grid.py --all --trail bull --compare

# Parameter sweep (range × levels combos)
python3 analysis/backtest_grid.py --all --sweep
python3 analysis/backtest_grid.py --all --sweep --sort pnl

# Explicit DB file
python3 analysis/backtest_grid.py data/BTCUSDT_1m90d_range_20260208-20260509.db
```

**Parameter flags:**

| Flag | Default | Description |
|---|---|---|
| `--all` | — | Use all `BTCUSDT_1m*.db` files found in `data/` |
| `--range FLOAT` | 15.0 | Grid ±% from start/re-center price (`grid_lower = price × (1 − range/100)`) |
| `--levels INT` | 30 | Number of evenly-spaced grid levels; capital = `levels × size` |
| `--size FLOAT` | 50.0 | USDT per order |
| `--fee FLOAT` | 0.1 | Fee rate % per side |
| `--trail MODE` | `off` | Trailing mode: `off` (static), `bear` (re-center down), `bull` (re-center up), `both` (both — dangerous in trending markets) |
| `--max-recenters INT` | 10 | Max re-centers before treating as stop-loss |
| `--compare` | — | Run static alongside trailing and print side-by-side per DB |
| `--sweep` | — | Sweep `range_pct × levels` (5×3 = 15 combos) |
| `--sort METRIC` | `calmar` | Sort sweep results by `calmar` (PnL%/MaxDD) or `pnl` |

### Download historical OHLCV data

```bash
# Last 90 days (default)
python3 analysis/download_btc_history.py

# Historical range — 2022 bear market (LUNA crash)
python3 analysis/download_btc_history.py --start 2022-05-01 --end 2022-08-01

# Historical range — 2024 bull run
python3 analysis/download_btc_history.py --start 2024-10-15 --end 2025-01-15

# Custom output path
python3 analysis/download_btc_history.py --out data/my_range.db
```

**Flags:**

| Flag | Default | Description |
|---|---|---|
| `--symbol STR` | `BTCUSDT` | Trading pair |
| `--interval STR` | `1m` | Candle interval (`1m`, `5m`, `15m`, `1h`, …) |
| `--days INT` | 90 | Days to download (used when `--start` is absent) |
| `--start DATE` | — | Start date `YYYY-MM-DD`; overrides `--days` |
| `--end DATE` | today | End date `YYYY-MM-DD` |
| `--out FILE` | auto | Output SQLite path (default: `data/BTCUSDT_1m<N>d_range_<dates>.db`) |

Output databases are excluded from git (`.gitignore`). The download resumes from the last stored candle on re-run. See [`docs/AdaptedGridTrading.md`](docs/AdaptedGridTrading.md) for backtest results, strategy selection, and parameter sweep tables.


## Hour / Day Filter

The bot can restrict trade entries to specific UTC hour ranges depending on the day of the week. The filter is configured in the strategy JSON file (`tradinebotte-polymarket/strategies/polymarket_BTC5M.json`) and is **disabled by default** — existing behaviour is preserved until you explicitly enable it.

### Rationale

BTC volatility follows daily and weekly patterns driven by institutional flows:

| Period | UTC window | Characteristic |
|---|---|---|
| Asian session | 00:00–08:00 | Moderate volume, directional moves |
| European dead zone | 08:00–13:00 | Low volume, noisy signals |
| US session | 13:00–22:00 | High volume, clearest signals |
| US weekly open | Mon 13:30 | Institutional re-entry after weekend; strong directional move |
| US weekly close | Fri 20:00 | Position squaring; volatility spike then drop |
| Weekend | Sat–Sun | Retail-driven, higher noise, lower predictability |

### Configuration

Add or edit the `hour_filter` block in your strategy JSON:

```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [[0, 8], [13, 22]],
    "weekend_utc_ranges": [],
    "us_weekly_open": true,
    "us_weekly_close": true
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. `false` = no filtering, all hours allowed. |
| `weekday_utc_ranges` | list of `[start, end]` | `[]` | UTC hour ranges allowed Mon–Fri. Empty = all hours allowed on weekdays. |
| `weekend_utc_ranges` | list of `[start, end]` | `[]` | UTC hour ranges allowed Sat–Sun. Empty = **all weekend trading blocked**. |
| `us_weekly_open` | bool | `true` | When `true`, blocks entries on **Monday before 13:30 UTC** (US markets have not yet opened for the week). |
| `us_weekly_close` | bool | `true` | When `true`, blocks entries on **Friday from 20:00 UTC** (US markets have closed for the week). |

Hour ranges use the convention `[start, end)` — `[13, 22]` means 13:00 ≤ hour < 22:00.
The `us_weekly_open` and `us_weekly_close` constraints are applied **in addition to** `weekday_utc_ranges`, and take precedence over them on their respective days.

### Decision logic (Monday example, filter enabled)

```
Monday 07:00 UTC
  → weekday range check: 07 is in [0, 8) → would be OK
  → us_weekly_open check: 07 < 13:30 → BLOCKED

Monday 13:45 UTC
  → us_weekly_open check: 13:45 ≥ 13:30 → passes
  → weekday range check: 13 is in [13, 22) → ALLOWED

Saturday 15:00 UTC
  → weekend_utc_ranges is [] → BLOCKED
```

### Preset examples

**Conservative — US session only, no weekends:**
```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [[13, 22]],
    "weekend_utc_ranges": [],
    "us_weekly_open": true,
    "us_weekly_close": true
}
```

**Extended — Asian + US sessions, no weekends:**
```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [[0, 8], [13, 22]],
    "weekend_utc_ranges": [],
    "us_weekly_open": true,
    "us_weekly_close": true
}
```

**24/7 — all hours, all days (same as disabled):**
```json
"hour_filter": {
    "enabled": true,
    "weekday_utc_ranges": [],
    "weekend_utc_ranges": [[0, 24]],
    "us_weekly_open": false,
    "us_weekly_close": false
}
```

### Backtest with filter

The backtest engine applies the same filter logic when replaying snapshots, so you can measure its effect before enabling it live:

```bash
# Edit hour_filter.enabled = true in the strategy JSON, then:
python3 analysis/backtest.py --all
```

Compare win rate and trade count with and without the filter to validate your chosen windows against your snapshot dataset.

### Startup log

When the filter is active the bot logs the effective configuration at startup:

```
[INFO]   Filtre horaire : sem=0-8h 13-22h | we=bloque ouv.lun=13h30 ferm.ven=20h00
```


## Multi-bot WebSocket sharing (Option B — ZeroMQ)

> Full architecture reference and decision guide: **[docs/multi.md](docs/multi.md)**

Use Option B when running two or more accounts simultaneously, when accounts belong
to different Linux users, or when comparing different strategies in parallel.
For a single account, Option A (`live_bot.py` standalone) is simpler.

The ZeroMQ architecture uses **three** processes per deployment:

| Process | File | Role |
|---|---|---|
| Indicators | `indicators.py` | Computes signal data; publishes via ZMQ PUB; registers markets on REP socket |
| Feed | `feed.py` | Single WebSocket connection; broadcasts book updates via ZMQ PUB |
| Account bot | `account_bot.py` | Subscribes to feed and indicators; executes trades for one account |

All three communicate over IPC sockets (`ipc://`) placed in `/run/user/$UID/`
(kernel-enforced mode 0700 per Linux user).  No TCP port conflicts between
Linux users sharing the same server.  The fallback location is
`/tmp/tradinebotte-$UID/` on systems without `systemd-logind`.

### Prerequisites

`pyzmq` is already included in `requirements.txt`.  Install it with the rest of
the dependencies:

```bash
bash scripts/install.sh
```

Install `tradinetools` (the shared ZMQ utility library).  `pip install -e` may
fail on Python 3.14 venvs; the copy fallback is always reliable:

```bash
cd ~/tradinebotte
PYVER=$(.venv/bin/python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
SITE=.venv/lib/python${PYVER}/site-packages
if .venv/bin/pip install --quiet -e tradinetools 2>/dev/null; then
    echo 'tradinetools ok (pip)'
else
    rm -rf "$SITE/tradinetools"
    cp -r tradinetools/tradinetools "$SITE/tradinetools"
    echo 'tradinetools ok (copy)'
fi
.venv/bin/python3 -c 'from tradinetools.zmq import ipc_socket_dir, make_pub; print("tradinetools ok")'
```

**Account config** — add `feed_auto_start: false` to each account's `config.json`
so the account bot does not try to spawn a private feed when the shared feed is
a managed service:

```json
{ "feed_auto_start": false }
```

### Directory layout (example — two accounts)

```
~/tradinebotte/          ← shared install: venv, all bot files, tradinetools/
  .venv/
  live_bot.py            ← account_bot imports this (sys.path includes ~/tradinebotte/)
  pm_*.py                ← Polymarket plugin (pm_types/pm_calendar/pm_strategy/pm_data)
  cex_consumer.py        ← CEX glue (grid/swing feed consumer)
  botcore/               ← neutral core (strategy/connectors/persistence/schema)
  connectors/            ← connector registry shim (re-exports botcore.connectors)
  feed.py                ← feed service ExecStart
  indicators.py          ← indicators service ExecStart
  account_bot.py         ← account service ExecStart
  tradinetools/
  feed.log
  indicators.log
~/account-a/             ← account A: own DB, log, config
  config.json            ← "feed_auto_start": false
  live.db
  account.log
~/account-b/             ← account B: own DB, log, config
  config.json
  live.db
  account.log
```

Set up each account directory first:

```bash
TRADINEBOTTE_DIR=~/account-a python3 scripts/setup.py   # enter account A key
TRADINEBOTTE_DIR=~/account-b python3 scripts/setup.py   # enter account B key
```

### Systemd user services (recommended)

User services run without `sudo`, restart automatically on crash, and persist
across reboots when linger is enabled.

**One-time admin step** — enable linger so services survive after SSH logout
(requires root or `sudo`; run once per VPS user, not by the bot user itself):

```bash
sudo loginctl enable-linger <bot_username>
```

**Install unit files** — run as the bot user:

```bash
mkdir -p ~/.config/systemd/user/

cat > ~/.config/systemd/user/tradinebotte-indicators.service << 'EOF'
[Unit]
Description=tradinebotte indicators
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/indicators.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/tradinebotte-feed.service << 'EOF'
[Unit]
Description=tradinebotte feed
After=tradinebotte-indicators.service
Requires=tradinebotte-indicators.service

[Service]
Type=simple
# IPC address auto-detected from /run/user/%U/ — no override needed.
# To force TCP: Environment=TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/feed.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

cat > ~/.config/systemd/user/tradinebotte-account.service << 'EOF'
[Unit]
Description=tradinebotte account bot
After=tradinebotte-feed.service
Requires=tradinebotte-feed.service

[Service]
Type=simple
WorkingDirectory=%h/tradinebotte
ExecStart=%h/tradinebotte/.venv/bin/python3 %h/tradinebotte/account_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user daemon-reload
systemctl --user enable --now tradinebotte-indicators.service
systemctl --user enable --now tradinebotte-feed.service
systemctl --user enable --now tradinebotte-account.service
```

**Verify** (wait ~10 s after start):

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user status tradinebotte-indicators.service
systemctl --user status tradinebotte-feed.service
systemctl --user status tradinebotte-account.service
```

**Note:** `XDG_RUNTIME_DIR` must be set explicitly in non-interactive SSH
sessions.  The `export XDG_RUNTIME_DIR=/run/user/$(id -u)` line above is
required whenever you run `systemctl --user` over SSH.

### Stopping

With systemd user services:

```bash
export XDG_RUNTIME_DIR=/run/user/$(id -u)
systemctl --user stop tradinebotte-account.service
systemctl --user stop tradinebotte-feed.service
systemctl --user stop tradinebotte-indicators.service
```

Without systemd (PID files):

```bash
kill $(cat ~/tradinebotte/feed.pid)
kill $(cat ~/account-a/account.pid)
kill $(cat ~/account-b/account.pid)
```

### Message protocol

The feed publishes three JSON message types over ZeroMQ PUB:

| Type | Fields | Purpose |
|---|---|---|
| `market` | `market_id`, `question`, `up_token_id`, `dn_token_id`, `start_ms`, `end_ms` | New market registered |
| `book` | `token_id`, `best_bid`, `best_ask`, `spread`, `bid_vol`, `ask_vol`, `obi` | Book update |
| `ping` | `ts` | Keepalive every 10 s |

### Architecture notes

- The feed has no trading logic and holds no credentials — it is safe to restart without affecting account state.
- Each `account_bot.py` process writes to its own SQLite database; the `handle_book_update` / `check_signal` / `enter_live_trade` path (defined in `pm_data` / `pm_strategy`, re-exported by `live_bot`) runs unmodified.
- If the feed restarts, account bots automatically recover — they will miss book updates during the gap but will not place duplicate orders because the `signalled` set is persisted to the DB between sessions.
- The ZeroMQ PUB/SUB pattern is one-way: account bots never send messages back to the feed.
- IPC sockets are placed in `/run/user/$UID/` (managed by systemd-logind, mode 0700).  The fallback for systems without `systemd-logind` is `/tmp/tradinebotte-$UID/` (mode 0700).
- `account_bot.py` inserts its own directory into `sys.path` and imports `live_bot` from there.  With the flat-dir layout (`ExecStart` pointing to `~/tradinebotte/account_bot.py`), both files live in `~/tradinebotte/` and stay in sync automatically on every rsync update.

### Integration tests

Two SSH integration tests cover the shared-server scenarios. Both read from the same `~/.tradinebotte-test.conf`:

```bash
cp scripts/test_multibot.conf.example ~/.tradinebotte-test.conf
editor ~/.tradinebotte-test.conf
```

**Run all integration tests (recommended):**

```bash
bash scripts/run_integration_tests.sh              # both tests in sequence
bash scripts/run_integration_tests.sh --standalone # Option A only
bash scripts/run_integration_tests.sh --multibot   # Option B only
```

**`test_standalone_deploy.sh`** — Option A multi-user (standalone `live_bot.py`):
- Deploys to 2 Linux users on the same server
- User 1 starts `start_bot.sh` → must succeed
- User 2 starts `start_bot.sh` while user 1 is running → must also succeed
- Verifies no "une instance est déjà en cours" error in either log (catches the `pgrep` scope class of bugs)
- Both WebSocket connections confirmed in logs

**`test_multibot_deploy.sh`** — Option B multi-user (ZeroMQ feed + account bots):
- Feed auto-starts when 3 bots launch simultaneously (race-safe file lock)
- Exactly one `feed.py` process visible across all Linux users
- All 3 `account_bot.py` processes connect and receive book updates
- No ERROR/CRITICAL log lines during the 3-minute test window
- All processes stopped cleanly after the test

```bash
# Individual runs with options:
bash tradinebotte-polymarket/scripts/test_standalone_deploy.sh --skip-deploy
bash scripts/test_multibot_deploy.sh --skip-deploy --duration 300
```


## Monitoring

Live dashboard:

```bash
bash tradinebotte-polymarket/scripts/monitor.sh
```

Follow logs in real time:

```bash
tail -f ~/tradinebotte/live.log
```

Recent trades:

```bash
sqlite3 ~/tradinebotte/live.db \
  "SELECT id, direction, entry_price, outcome, ROUND(pnl_net,3), capital_after \
   FROM trades ORDER BY id DESC LIMIT 10;"
```

Today's stats:

```bash
sqlite3 ~/tradinebotte/live.db \
  "SELECT COUNT(*), SUM(CASE WHEN outcome='WIN' THEN 1 ELSE 0 END), ROUND(SUM(pnl_net),2) \
   FROM trades WHERE resolved=1 AND created_at > (strftime('%s','now')-86400)*1000;"
```

Confirm real on-chain orders (not simulated):

```bash
grep "order=" ~/tradinebotte/live.log | grep -v "order=sim" | tail -20
```


## Data collection

The first deployment account runs the bot in simulate mode with 1-second snapshot intervals to build a high-resolution dataset for strategy research and backtesting.

> ⚠ **Dormant since 2026-05.** This pipeline is not currently deployed — no collector directory on the account, no cron entry installed, newest archive `data/live_2026_W19.db`. The scripts are kept because this is the tooling that produces the backtest datasets; the instructions below describe how to reactivate it.

### Collector scripts

**`tradinebotte-polymarket/scripts/start_collector.sh`** — deploy and manage the data-collection process:

| Flag | Description |
|---|---|
| *(no flag)* | Deploy source files to the collector account and start `live_bot.py --simulate --snapshot-interval 1` |
| `--status` | Check whether the collector process is running and print the remote snapshot row count |
| `--stop` | Stop the running collector process |

```bash
bash tradinebotte-polymarket/scripts/start_collector.sh           # deploy + launch
bash tradinebotte-polymarket/scripts/start_collector.sh --status  # check if running
bash tradinebotte-polymarket/scripts/start_collector.sh --stop    # stop
```

**`tradinebotte-polymarket/scripts/collect_db.sh`** — download and archive the weekly snapshot database:

| Flag | Description |
|---|---|
| `--status` | Show remote row counts for `snapshots` and `trades` tables without downloading |
| `--rotate` | Download `live.db` from the collector, archive it to `data/` with a datestamp, then restart the collector with a fresh database |

```bash
bash tradinebotte-polymarket/scripts/collect_db.sh --status    # remote row counts
bash tradinebotte-polymarket/scripts/collect_db.sh --rotate    # download + archive + restart
```

The downloaded file is archived to `data/collect_YYYYMMDD.db`. Collector log: `~/tradinebotte/collect.log`.

**`tradinebotte-polymarket/scripts/schedule_collect.sh`** — automate the weekly rotation with cron:

| Flag | Description |
|---|---|
| `--install` | Install a cron entry that runs `collect_db.sh --rotate` every Sunday at 03:00 UTC |
| `--status` | Print the current cron entry for the collect job |
| `--run-now` | Run the rotation immediately (same as `collect_db.sh --rotate`) |

```bash
bash tradinebotte-polymarket/scripts/schedule_collect.sh --install    # every Sunday 03:00 UTC
bash tradinebotte-polymarket/scripts/schedule_collect.sh --status     # show cron entry
bash tradinebotte-polymarket/scripts/schedule_collect.sh --run-now    # run immediately
```


## Testing in a Virtual Environment

Use [uv](https://github.com/astral-sh/uv) to create an isolated test
environment without touching the system Python or the production venv.

Install uv (if not already installed):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
```

Create the venv and install dependencies:

```bash
uv venv .venv --python 3.13
uv pip install aiohttp websockets web3 py-clob-client --python .venv/bin/python3
```

Syntax check:

```bash
.venv/bin/python3 -m py_compile tradinebotte-polymarket/live_bot.py && echo "SYNTAX OK"
```

Import check (verifies module-level code runs without errors):

```bash
.venv/bin/python3 -c "
import sys, importlib.util
spec = importlib.util.spec_from_file_location('live_bot', 'tradinebotte-polymarket/live_bot.py')
b = importlib.util.module_from_spec(spec); spec.loader.exec_module(b)
print('CONFIG_PATH:', b.CONFIG_PATH)
print('PRIVATE_KEY set:', bool(b.PRIVATE_KEY))
print('SIGNAL_THRESHOLD:', b.SIGNAL_THRESHOLD)
"
```

Run the bot for 20 seconds in isolated simulate mode (logs to stdout,
writes to `~/tradinebotte-sim` — production data is never touched):

```bash
timeout 20 .venv/bin/python3 tradinebotte-polymarket/live_bot.py --simulate
```

Expected output (printed directly to the terminal):

```
[WARNING]  MODE SIMULATION — donnees isolees dans ~/tradinebotte-sim
[INFO]     LIVE BOT v3 — Threshold=0.95 Stake=$10 MinAskVol=10
[WARNING]  POLY_PRIVATE_KEY non definie — ordres SIMULES
[INFO]     DB initialisee : ~/tradinebotte-sim/live.db
[INFO]     State : capital=$100.00 | 0 trades | WR=0.0%
[INFO]     Marches BTC 5-min : 2
[INFO]     Souscription 2 tokens...
[INFO]     WebSocket connecte
```

The `.venv/` directory is listed in `.gitignore` and must not be committed.


## Bilingual interface

All interactive scripts prompt for a language at startup:

```
Language / Langue :  [E] English   [F] Français
>>>
```

The choice is persisted as `"lang": "EN"` or `"lang": "FR"` in `config.json` by `setup.py`.
Subsequent scripts (`start_bot.sh`, `monitor.sh`) read this key automatically — no re-prompting.

If `config.json` is absent (before the first `setup.py` run), `start_bot.sh` and `monitor.sh`
default to English. `install.sh` always asks interactively since it runs before `setup.py`.

To change the language after initial setup, edit `config.json`:

```json
{ "lang": "FR" }
```

or re-run `python3 scripts/setup.py` and choose again.


## CEX connectors (Binance, MEXC, Bitstamp)

Three additional exchange adapters are included as drop-in replacements for `api_polymarket.py`:

| File | Exchange | Fee | WebSocket stream |
|---|---|---|---|
| `tradinebotte-cex/api_binance.py` | Binance spot | 0.1% taker | `btcusdt@depth5@100ms` |
| `tradinebotte-cex/api_mexc.py` | MEXC spot | 0.2% taker | `spot@public.limit.depth.v3.api@BTCUSDT@5` |
| `tradinebotte-cex/api_bitstamp.py` | Bitstamp spot | 0.1% taker | `wss://ws.bitstamp.net` live order book |

All three implement the identical public interface: `get_markets`, `post_order`,
`parse_book_update`, `compute_fee`, and market metadata helpers.

**Credentials** — set via environment variables or `config.json`:

```bash
export BINANCE_API_KEY=...
export BINANCE_API_SECRET=...
export MEXC_API_KEY=...
export MEXC_API_SECRET=...
export BITSTAMP_API_KEY=...
export BITSTAMP_API_SECRET=...
export BITSTAMP_CUSTOMER_ID=...
```

**Switch exchange** — set the `connector` name in the **strategy JSON** (the file
`config.json`'s `"strategy"` key points to); no code edit. There is no module-global `api`:
the connector is loaded from the registry (`botcore.connectors.load(config.connector)`) and
injected as `state.connector`.

```json
// strategies/<your_strategy>.json
{ "strategy_type": "grid", "connector": "binance" }   // or "mexc", "bitstamp", "polymarket" (default)
```

**Important**: the Polymarket signal (`best_bid >= 0.95`) operates on a 0–1 probability
scale. Binance/MEXC prices are absolute USDT values (e.g. 65000). Strategy thresholds in
`strategies/*.json` must be recalibrated before using a CEX connector.


## API latency benchmark

Compare REST and WebSocket latency across all three exchanges:

```bash
python3 analysis/benchmark_api.py             # 15 rounds, all exchanges
python3 analysis/benchmark_api.py --rounds 30 # more samples
python3 analysis/benchmark_api.py --no-ws     # REST only (faster)
```

Results are saved to `latence_api.txt` if redirected:

```bash
python3 analysis/benchmark_api.py 2>&1 | tee latence_api.txt
```

Reference latencies measured from a dedicated server in Amsterdam:

| Exchange | REST mean | REST p99 | WS mean |
|---|---|---|---|
| Polymarket Gamma | ~14 ms | ~20 ms | ~65 ms |
| MEXC | ~15 ms | ~80 ms | ~905 ms |
| Binance | ~225 ms | ~232 ms | ~990 ms |

Binance latency from Europe is high due to geographic routing; from a server
hosted in Asia the numbers would be reversed.
