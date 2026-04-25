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
  ~/tradinebotte/venv/bin/python3 -c \
    "import sqlite3; c=sqlite3.connect('live.db'); \
     print(c.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0])"
  ```


## Dependencies

The following Python packages are installed automatically by `scripts/install.sh`
into a virtualenv at `~/tradinebotte/venv/`:

- `aiohttp`
- `websockets`
- `web3`
- `py-clob-client`

The canonical list is `requirements.txt` at the project root. CVEs in these
packages are detected automatically on every push via `pip-audit` (GitHub Actions)
and Dependabot opens PRs when newer versions are available.


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


## Installation

Run the install script from the repository root:

```bash
bash scripts/install.sh [install_dir] [--with-tests]
```

**Options:**
- `--with-tests` — Also copy `tests/`, `scripts/backtest.py`, and
  `data/backtest_sample_btc5m_range_2026.db`, then run the
  full test suite (108 tests) immediately after installation.
  The backtest uses `live.db` only if it contains ≥ 100 snapshots;
  otherwise it falls back to the bundled sample dataset automatically.

This will:
- Install system packages (python3, pip, venv, sqlite3)
- Create the install directory
- Copy `bot/live_bot.py` and `bot/api_polymarket.py` to `<TRADINEBOTTE_DIR>/`
- Copy `strategies/*.json` to `<TRADINEBOTTE_DIR>/strategies/`
- Create a virtualenv at `<TRADINEBOTTE_DIR>/venv/`
- Install Python dependencies into the virtualenv
- Generate `<TRADINEBOTTE_DIR>/run.sh` (wrapper with `TRADINEBOTTE_DIR` pre-set)
- Verify bot syntax


## Wallet Setup (one-time)

Run `setup.py` once with your Polygon wallet. It will:
- Prompt for your private key interactively (masked stdin)
- Check USDC.e and USDC native balances
- Swap USDC native → USDC.e via Uniswap V3 if needed
- Approve CTF Exchange allowance
- Derive your Polymarket API credentials
- Write credentials to `<TRADINEBOTTE_DIR>/config.json` (chmod 600)

```bash
TRADINEBOTTE_DIR=~/tradinebotte python3 scripts/setup.py
```

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
A static preview of the rendered page is available at [docs/status_example.html](docs/status_example.html).

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


## Running

```bash
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/start_bot.sh
```

**Flags:**
- *(no flag)* — normal mode: log writes are asynchronous (daemon thread, never blocks the event loop)
- `--no-log` — suppress the log file entirely for minimum disk I/O; SQLite DB (trades + snapshots) is unaffected; combine with `--simulate` to keep stdout output
- `--simulate` — isolate all file I/O to `/tmp/tradinebotte-sim`, no real orders placed

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
pkill -f live_bot.py
```

- Logs: `<TRADINEBOTTE_DIR>/live.log`
- Trades: `<TRADINEBOTTE_DIR>/live.db` (SQLite)


## Latency analysis

Each trade emits one `[LATENCY]` line in `live.log`. Run the analysis tool after a trading session:

```bash
python3 scripts/latency.py                           # default path
python3 scripts/latency.py ~/tradinebotte/live.log   # explicit path
TRADINEBOTTE_DIR=~/tradinebotte python3 scripts/latency.py
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
python3 scripts/backtest.py

# One or more explicit files (shell glob supported)
python3 scripts/backtest.py --db ~/tradinebotte/live.db
python3 scripts/backtest.py --db data/session1.db data/session2.db
python3 scripts/backtest.py --db data/*.db

# Scan data/ automatically (includes live.db if it has ≥ 100 snapshots)
python3 scripts/backtest.py --all

# Grid search across 135 threshold/stake combinations
python3 scripts/backtest.py --sweep
python3 scripts/backtest.py --all --sweep
```

When more than one file is processed, each file runs with capital reset to `capital_start` (independent simulation), and an AGGREGATE block summarises combined wins, losses, PnL, win rate, and worst drawdown across all files.


## Monitoring

Live dashboard:

```bash
TRADINEBOTTE_DIR=~/tradinebotte bash scripts/monitor.sh
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
.venv/bin/python3 -m py_compile bot/live_bot.py && echo "SYNTAX OK"
```

Import check (verifies module-level code runs without errors):

```bash
.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
import bot.live_bot as b
print('CONFIG_PATH:', b.CONFIG_PATH)
print('PRIVATE_KEY set:', bool(b.PRIVATE_KEY))
print('SIGNAL_THRESHOLD:', b.SIGNAL_THRESHOLD)
"
```

Run the bot for 20 seconds in isolated simulate mode (logs to stdout,
writes to `/tmp/tradinebotte-sim` — production data is never touched):

```bash
timeout 20 .venv/bin/python3 bot/live_bot.py --simulate
```

Expected output (printed directly to the terminal):

```
[WARNING]  MODE SIMULATION — donnees isolees dans /tmp/tradinebotte-sim
[INFO]     LIVE BOT v3 — Threshold=0.96 Stake=$10 MinAskVol=10
[WARNING]  POLY_PRIVATE_KEY non definie — ordres SIMULES
[INFO]     DB initialisee : /tmp/tradinebotte-sim/live.db
[INFO]     State : capital=$100.00 | 0 trades | WR=0.0%
[INFO]     Marches BTC 5-min : 2
[INFO]     Souscription 2 tokens...
[INFO]     WebSocket connecte
```

The `.venv/` directory is listed in `.gitignore` and must not be committed.
