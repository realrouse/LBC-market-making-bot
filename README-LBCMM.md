# LBC-market-making-bot

**Standalone LBC/USDT market-making bot for MEXC** — help keep LBC tradeable.

> Forked from **neofutur’s** multibot design ([tradinebotte](https://github.com/neofutur/tradinebotte))  
> to build an LBC-only bot. **GPL-3.0**.

## Install in 3 commands

```bash
git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot
bash install-lbcmm.sh
```

Then:

```bash
./bin/lbcmm gui
```

Open **http://127.0.0.1:8787/** — setup wizard → set capital → **Start bot**.

Full guide: **[QUICKSTART-LBCMM.md](QUICKSTART-LBCMM.md)**.

### Run in the background

Keep the GUI or bot running after you close the terminal:

```bash
mkdir -p logs
nohup ./bin/lbcmm gui > logs/gui.log 2>&1 &
echo $! > logs/gui.pid
# stop later: kill "$(cat logs/gui.pid)"
```

Or install the user systemd units (`systemd/lbcmm-gui.service`, `systemd/lbcmm.service`) — step-by-step in **[QUICKSTART-LBCMM.md § Run in the background](QUICKSTART-LBCMM.md#run-in-the-background)**.

## Mission

Raise MEXC **LBC/USDT ±2% depth** toward **~$100 per side** so ordinary trades don’t move price much (healthier market, delisting risk down, relisting stories up). Even ~$10 helps.

## Features

| | |
|---|---|
| **GUI** | Local browser UI — wizard, sliders, depth diagram, menu (Market Config / Status / Settings) |
| **CLI** | `depth`, `status`, `run`, `cancel`, `gui` |
| **Paper mode** | Default — simulated orders, no real money |
| **Live** | MEXC maker orders with Access Key + Secret Key (Spot Trade only) |
| **Depth** | 0.5%–15% band (default ±2%), quick presets ±1 / ±2 / ±3 / ±5 |
| **Steps** | 1–30 order levels per side |
| **Strategies** | Depth Provider (default), BAMM, Grid (Settings) |
| **Standalone** | No multi-account ZMQ fleet required |

## Commands

```bash
./bin/lbcmm gui          # browser control panel (foreground)
./bin/lbcmm depth        # public ±2% depth + your plan
./bin/lbcmm status
./bin/lbcmm run --paper
./bin/lbcmm cancel       # cancel open live orders (this bot’s IDs)

# background (example)
nohup ./bin/lbcmm gui > logs/gui.log 2>&1 &
```

## Website / community editors

Do **not** re-implement the bot on the site. Use the **handoff pack**:

→ **[handoff/README.md](handoff/README.md)**  
→ Ready session prompt: **[handoff/GROK_SESSION_PROMPT.md](handoff/GROK_SESSION_PROMPT.md)**  
→ Copy for lbcrevival.com: **[handoff/lbcrevival-site.md](handoff/lbcrevival-site.md)**

## Layout

| Path | Role |
|---|---|
| `install-lbcmm.sh` | One-command installer |
| `bin/lbcmm` | Launcher (created by installer) |
| `lbcmm/` | Product package |
| `handoff/` | Docs for site / Discord sessions |
| `tradinebotte-*/` | Upstream multi-bot tree (reference) |
| `UPSTREAM.md` | License & attribution |

## License

GNU GPL v3 — see [LICENSE](LICENSE). Preserve attribution to tradinebotte / neofutur.
