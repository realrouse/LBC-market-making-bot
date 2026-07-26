# Handoff package — lbcrevival.com & community channels

This folder is the **bridge** from the bot repo to:

- **lbcrevival.com** publishing  
- **Discord** announcements  
- **Market page** enhancements  
- Optional note to **neofutur**  

Bot implementation lives in `../lbcmm/`. **Site sessions should not rewrite the bot.**

---

## Start a website Grok session

1. Open a **new** Grok session that can edit `/root/AI/grokbuild/lbcrevival.com`.  
2. Paste the full prompt from **[GROK_SESSION_PROMPT.md](GROK_SESSION_PROMPT.md)**.  
3. Let it read this folder and publish using **[lbcrevival-site.md](lbcrevival-site.md)** + **[website-copy-blocks.md](website-copy-blocks.md)**.

---

## Files

| File | Purpose |
|---|---|
| **[GROK_SESSION_PROMPT.md](GROK_SESSION_PROMPT.md)** | Copy-paste prompt for the website session |
| **[lbcrevival-site.md](lbcrevival-site.md)** | Placement, product facts, checklist |
| **[website-copy-blocks.md](website-copy-blocks.md)** | Paste-ready marketing + install + FAQ + HTML |
| **[discord-announcement.md](discord-announcement.md)** | Discord draft |
| **[market-page-prompt.md](market-page-prompt.md)** | 1D/4h + candles + ± meter |
| **[neofutur-opinion.md](neofutur-opinion.md)** | Codebase opinion for neofutur |

User-facing install (keep site in sync):

- `../install-lbcmm.sh` — one-command installer  
- `../QUICKSTART-LBCMM.md` — full quick start  
- `../README-LBCMM.md` — product README  

---

## Canonical install (for any public page)

```bash
git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot
bash install-lbcmm.sh
./bin/lbcmm gui
```

Then open **http://127.0.0.1:8787/**

### Background (VPS / leave running)

```bash
mkdir -p logs
nohup ./bin/lbcmm gui > logs/gui.log 2>&1 &
echo $! > logs/gui.pid
# stop: kill "$(cat logs/gui.pid)"
```

Full options (nohup + systemd): **[QUICKSTART-LBCMM.md § Run in the background](../QUICKSTART-LBCMM.md#run-in-the-background)**.

---

## Rules

- No secrets, API keys, or PATs in this folder  
- Website must **never** collect MEXC keys  
- Attribution + GPL + safety always visible  
- Prefer linking to GitHub / QUICKSTART over duplicating outdated steps  

Working tree: `/root/AI/grokbuild/lbc-market-making-bot`  
GitHub: `https://github.com/realrouse/LBC-market-making-bot`
