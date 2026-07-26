# LBC-market-making-bot — Quick Start

> Forked from **neofutur’s** multibot design ([tradinebotte](https://github.com/neofutur/tradinebotte))  
> LBC-only standalone bot · **GPL-3.0**

Contribute liquidity to **MEXC LBC/USDT** with a simple local bot (CLI + browser GUI).

---

## Super-easy install (recommended)

**Needs:** Linux or macOS, Python 3.10+, internet once for packages.

```bash
# 1) Get the code
git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot

# 2) Install (venv + deps + launcher)
bash install-lbcmm.sh

# 3) Open the control panel
./bin/lbcmm gui
```

Then open **http://127.0.0.1:8787/** in your browser.

That’s it. The first visit walks you through a **setup wizard** (paper or live, capital, depth).

| Goal | Command |
|---|---|
| Browser GUI | `./bin/lbcmm gui` |
| See market depth | `./bin/lbcmm depth` |
| Paper bot (terminal) | `./bin/lbcmm run --paper` |
| Status | `./bin/lbcmm status` |

If `install-lbcmm.sh` linked into `~/.local/bin`, you can also run `lbcmm gui` from anywhere (ensure `~/.local/bin` is on your `PATH`).

---

## What you do in the GUI

1. Finish the **first-time setup wizard** (paper recommended first).  
2. Set **USDT** (buy side) and **LBC** (sell side).  
3. Set **Depth %** (default ±2% — CoinGecko-style) and **Steps** (order levels).  
4. Press **Start bot**.  

Menu bar:

- **Market Config** — capital, depth, start/stop  
- **Status** — market + bot snapshot  
- **Settings** — paper/live, API keys, strategy  

---

## Live trading (optional, real money)

1. Create a key at **https://www.mexc.com/user/openapi**  
2. Under **Spot**, enable only:
   - **View Order Details**
   - **Trade**  
   (optional: View Account Details)  
3. **Never enable Withdraw.** Prefer IP allowlisting.  
4. In the GUI: **Settings → Live**, or the setup wizard “Live” path.  
   Enter **Access Key** then **Secret Key**.  

Or via environment:

```bash
export MEXC_API_KEY='your-access-key'
export MEXC_API_SECRET='your-secret-key'
./bin/lbcmm run --live   # only after setup confirmed LIVE
```

---

## Manual install (if you prefer)

```bash
cd LBC-market-making-bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lbcmm.txt
export PYTHONPATH=.
python3 -m lbcmm gui
```

---

## Safety

- **Paper by default** — simulated orders until you opt into live  
- Market making can lose money if price trends through your orders  
- API keys stay on **your machine** — never paste them into Discord or websites  
- Not financial advice  

---

## Attribution

When you mention this project:

> “I forked @neofutur’s multibot design to build an LBC-only bot.”

Community goal: help **±2% depth** on MEXC LBC/USDT toward **~$100 per side**.

---

## More docs

| Doc | Audience |
|---|---|
| [README-LBCMM.md](README-LBCMM.md) | Product overview |
| [docs/lbcmm/SECURITY.md](docs/lbcmm/SECURITY.md) | Operator security checklist |
| [handoff/](handoff/README.md) | **For lbcrevival.com Grok / website editors** |
| [UPSTREAM.md](UPSTREAM.md) | License / fork relationship |
