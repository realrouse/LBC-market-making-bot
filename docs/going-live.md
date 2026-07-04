# Going Live — Switching from Simulation to Real Money

> 🇫🇷 [Version française](going-live.fr.md)

All bots detect credentials at startup via environment variables. When the
relevant variables are absent the bot runs in **simulation mode**: order
functions return `sim_...` IDs, no real orders are placed, and no funds move.
Switching to live is a three-step process: set credentials on the remote →
wire them into the systemd service → update the status page.

---

## 1. Credentials required per bot

| Bot | Connector | Environment variables |
|-----|-----------|----------------------|
| `live_bot` / `account_bot` (Polymarket) | `polymarket` | `POLY_PRIVATE_KEY`, `POLY_API_KEY`, `POLY_API_SECRET` |
| `grid_bot` / `swing_bot` (Binance) | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| `accumulation_bot` | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| `orderbook_bot` | `binance` | `BINANCE_API_KEY`, `BINANCE_API_SECRET` |
| `grid_bot` (MEXC Futures) | `mexc_futures` | `MEXC_FUTURES_API_KEY`, `MEXC_FUTURES_API_SECRET` |
| `grid_bot` / `swing_bot` (MEXC spot) | `mexc` | `MEXC_API_KEY`, `MEXC_API_SECRET` |

**Polymarket note:** `POLY_API_KEY`, `POLY_API_SECRET`, and `POLY_API_PASSPHRASE`
are derived from the wallet private key. Run `python3 scripts/setup.py` once
on the account to generate them; they are written to `~/.polymarket_creds` by
that script, which you can then source into the environment.

---

## 2. Create a credentials file on the remote account

SSH into the target account and create `~/.tradinebotte-creds`:

```bash
cat > ~/.tradinebotte-creds << 'EOF'
# Binance — used by accumulation_bot, orderbook_bot, grid_bot (binance), swing_bot
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here

# MEXC Futures — used by grid_bot (mexc_futures connector)
# MEXC_FUTURES_API_KEY=your_key_here
# MEXC_FUTURES_API_SECRET=your_secret_here

# Polymarket — used by live_bot / account_bot
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
(`tradinebotte-accumulation.service`, `tradinebotte-orderbook.service`).

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

## 6. Update the status page

In `tradinebotte-status/generate_status.py`, add the bot to `_LIVE_BOTS`:

```python
# Default: all bots are SIM. Add entries here when a bot goes live.
_LIVE_BOTS: set[tuple[str, str]] = {
    ("acct-2", "live_bot"),           # example: acct-2 Polymarket bot now live
    ("acct-4", "accumulation_bot"),   # example: acct-4 accumulation bot now live
}
```

`acct_short` is the first word of the matching `_ACCOUNT_LABELS` entry
(e.g. `"acct-2"` from `"acct-2 [poly]"`). `bot_name` matches the heartbeat
`bot_name` field exactly.

Regenerate after editing:

```bash
python3 tradinebotte-status/generate_status.py
```

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
