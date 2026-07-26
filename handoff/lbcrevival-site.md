# lbcrevival.com — publish LBC-market-making-bot

**Audience:** Grok / human editing `/root/AI/grokbuild/lbcrevival.com`  
**Bot source tree:** `/root/AI/grokbuild/lbc-market-making-bot`  
**GitHub:** https://github.com/realrouse/LBC-market-making-bot  

## Rule

| You do | You do not |
|---|---|
| Add pages/sections, install docs, links | Rewrite trading engine |
| Copy install commands from this handoff | Collect API keys on the website |
| Show attribution + safety | Claim guaranteed price / profits |

Paste-ready blocks: **[website-copy-blocks.md](website-copy-blocks.md)**  
Session prompt: **[GROK_SESSION_PROMPT.md](GROK_SESSION_PROMPT.md)**

---

## Placement ideas

1. **Hub / Work** — card “Contribute liquidity”  
2. **Dedicated page** e.g. `/liquidity` or `/bot` (recommended)  
3. **Market page** footer + optional ±2% depth meter ([market-page-prompt.md](market-page-prompt.md))  
4. Link from Discord announcement ([discord-announcement.md](discord-announcement.md))

---

## Product snapshot (keep accurate)

| Item | Value |
|---|---|
| Name | LBC-market-making-bot |
| Venue / pair | MEXC spot **LBC/USDT** |
| Install | `bash install-lbcmm.sh` then `./bin/lbcmm gui` |
| GUI URL | http://127.0.0.1:8787/ |
| Default mode | **Paper** (simulated) |
| Depth % | 0.5–15 (default **2**); presets 1 / 2 / 3 / 5 |
| Steps | 1–30 levels per side |
| Community goal | ~$100 bid + ~$100 ask inside ±2% of mid |
| Attribution | Forked **neofutur** tradinebotte multibot → LBC-only · GPL-3.0 |
| MEXC keys page | https://www.mexc.com/user/openapi |
| Key permissions | Spot: **View Order Details** + **Trade** only; never Withdraw |

### User journey (describe on site)

1. Clone + `bash install-lbcmm.sh`  
2. `./bin/lbcmm gui` → browser wizard  
3. Choose paper (recommended) or live  
4. Set USDT + LBC capital, depth %, steps  
5. **Start bot**  

---

## Canonical install (do not “simplify” into wrong commands)

```bash
git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot
bash install-lbcmm.sh
./bin/lbcmm gui
```

Open **http://127.0.0.1:8787/**

Full user doc in repo: `QUICKSTART-LBCMM.md`

---

## Messaging

**Headline:** Help keep LBC tradeable — provide MEXC liquidity  

**Body:**  
LBC/USDT needs healthy ±2% order-book depth. Thin books make price easy to push and hurt listing optics. The open **LBC-market-making-bot** lets anyone rest maker bids (USDT) and asks (LBC) near mid. Default band ±2%. Goal: roughly **$100 each side**.

**Attribution line (required):**  
Forked from **@neofutur’s** multibot design (tradinebotte) to build an LBC-only standalone bot. GPL-3.0.

**Safety (required):**  
Paper by default · Spot trade keys only, never withdraw · Keys stay local · Not financial advice · Optional participation  

---

## Implementation checklist

- [ ] Section or page with Blocks A–F from `website-copy-blocks.md`  
- [ ] Install commands match this file exactly  
- [ ] GitHub link works  
- [ ] Attribution visible  
- [ ] Safety / no-keys-on-site visible  
- [ ] Optional: market page depth meter + 1D/4h ([market-page-prompt.md](market-page-prompt.md))  
- [ ] Optional: Discord post from `discord-announcement.md`  

---

## Out of scope

- Hosting the bot in the cloud for users  
- Proxying private MEXC order APIs  
- Changing `lbcmm/` trading code (report bugs to bot session / GitHub issues)  
