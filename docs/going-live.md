# Going Live — Switching from Simulation to Real Money

> 🇫🇷 [Version française](going-live.fr.md)

All bots detect credentials at startup via environment variables. When the
relevant variables are absent the bot runs in **simulation mode**: order
functions return `sim_...` IDs, no real orders are placed, and no funds move.
Switching to live is a three-step process: set credentials on the remote →
wire them into the systemd service → flip `is_live` for that bot in
`inventory.toml`.

---

## 1. Credentials required per bot

Every strategy family runs inside the same host process, `live_bot.py`
(native single-tree deploy — see `docs/plan_D_decoupling.md`); which
connector it loads is config-driven (`strategy_type` / `connector` in the
bot's strategy JSON), not a separate binary per strategy.

| Strategy family | Connector | Environment variables |
|-----|-----------|----------------------|
| Polymarket (`pm_strategy` plugin) | `polymarket` | `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET` |
| Grid / Swing / DCA (Binance) | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| Grid / Swing (MEXC spot) | `mexc` | `MEXC_API_KEY`, `MEXC_API_SECRET` |
| Grid (MEXC Futures) | `mexc_futures` | `MEXC_FUTURES_API_KEY`, `MEXC_FUTURES_API_SECRET` |
| Accumulation / BAMM (MEXC or Binance) | `mexc` / `binance` | `MEXC_API_KEY`/`SECRET` or `BINANCE_API_KEY`/`SECRET` |

**Polymarket note:** `POLY_API_KEY`, `POLY_API_SECRET`, and `POLY_API_PASSPHRASE`
are derived from the wallet private key. Run `python3 scripts/setup.py` once
on the account to generate them; they are written to `~/.polymarket_creds` by
that script, which you can then source into the environment.

---

## 2. Create a credentials file on the remote account

SSH into the target account and create `~/.tradinebotte-creds`:

```bash
cat > ~/.tradinebotte-creds << 'EOF'
# Binance — used by live_bot.py for grid/swing/DCA/accumulation on the binance connector
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here

# MEXC Futures — used by live_bot.py for grid on the mexc_futures connector
# MEXC_FUTURES_API_KEY=your_key_here
# MEXC_FUTURES_API_SECRET=your_secret_here

# Polymarket — used by live_bot.py via the pm_strategy plugin
# POLY_PRIVATE_KEY=0x...
# POLY_API_KEY=...
# POLY_API_SECRET=...
# POLY_API_PASSPHRASE=...
EOF
chmod 600 ~/.tradinebotte-creds
```

This file is **never rsynced** by any deploy script — it lives only on the
remote account and must be set manually.

---

## 3. Wire credentials into the systemd service (drop-in override)

The service templates ship without an `EnvironmentFile=` directive. Use a
**drop-in override** so it survives future daemon-reloads and deploys:

```bash
# On the remote account — run once per service that needs credentials
export XDG_RUNTIME_DIR=/run/user/$(id -u)

systemctl --user edit tradinebotte-live.service
```

An editor opens `~/.config/systemd/user/tradinebotte-live.service.d/override.conf`.
Add:

```ini
[Service]
EnvironmentFile=%h/.tradinebotte-creds
```

Save, then reload:

```bash
systemctl --user daemon-reload
systemctl --user restart tradinebotte-live.service
```

Repeat for other service units if needed
(`tradinebotte-accumulation.service`, `tradinebotte-grid.service`).

> **Why a drop-in?** The base service file is overwritten on every rsync
> deploy. A drop-in under `service.d/` is never touched by rsync and merges
> automatically on reload.

---

## 4. Redeploy to pick up the change

```bash
# Redeploy the whole fleet (inventory-driven)
bash tradinebotte-cex/scripts/deploy_all.sh

# Or target one account/bot directly, e.g.:
bash tradinebotte-cex/scripts/deploy_all.sh --only account-2
```

---

## 5. Verify — confirm live mode in the startup log

After the restart, the "orders SIMULATED" warning must be **absent** from the
startup log:

```bash
# On the remote account
grep -iE "simul|LIVE BOT|credentials" ~/tradinebotte/live.log | tail -10
```

In live mode the startup banner shows the connector and strategy without any
simulation notice. In simulation mode it prints:

```
[INFO] POLY_PRIVATE_KEY not set — orders SIMULATED
# or
[WARN] Binance — simulated order (BINANCE_API_KEY/SECRET not set)
# or
[WARN] MEXC Futures — order simulated (MEXC_FUTURES_API_KEY/SECRET not set)
```

---

## 6. Flip `is_live` in inventory.toml

The status page's LIVE/SIM badge is **not** hand-edited in
`generate_status.py` — it's derived from `inventory.toml`, the fleet's
single source of truth (`live_bots()` in
`tradinebotte-status/inventory_labels.py`, keyed on each bot's `is_live`
flag). Editing a `_LIVE_BOTS` set directly in `generate_status.py` has no
effect; that set is computed fresh from `inventory.toml` on every run.

`inventory.toml` itself is local and git-ignored (it describes your real
accounts/bots — see `inventory.toml.example` if you haven't created yours
yet: `cp inventory.toml.example inventory.toml`). If it's missing,
`generate_status.py` prints a loud warning and fails soft to **no** LIVE
badges at all — never assume "no warning printed" without checking the
file exists.

Find the bot's `[[bot]]` row (matched by `account_idx` + its generated
`bot_name`/bot_id) and flip:

```toml
is_live       = true   # ⚠ real money — leave a comment: budget, connector, date armed
```

Regenerate, or wait for the next `statuspage.timer` tick (~2 min):

```bash
python3 tradinebotte-status/generate_status.py
```

`is_live` is load-bearing beyond the status page: it's also what makes
`botctl.sh` refuse destructive commands (reset/wipe) against that bot. See
the idx7 BAMM row in `inventory.toml` for the comment pattern this flag
expects.

---

## Quick reference — simulation detection by connector

| Connector | Simulated when… | Log warning |
|-----------|-----------------|-------------|
| `polymarket` | `POLY_PRIVATE_KEY` empty | `orders SIMULATED` |
| `binance` | `BINANCE_API_KEY` or `BINANCE_API_SECRET` empty | `simulated order (BINANCE_API_KEY/SECRET not set)` |
| `mexc` | `MEXC_API_KEY` or `MEXC_API_SECRET` empty | `order simulated (MEXC_API_KEY/SECRET not set)` |
| `mexc_futures` | `MEXC_FUTURES_API_KEY` or `MEXC_FUTURES_API_SECRET` empty | `order simulated (MEXC_FUTURES_API_KEY/SECRET not set)` |

All simulation checks happen at the connector level — the strategy engine and
heartbeat system are identical in both modes.
