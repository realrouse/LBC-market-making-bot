# Ready-to-paste prompt — lbcrevival.com Grok session

Copy everything inside the fence into a **new Grok session** that owns the website (not the bot repo).

---

```
You manage the LBC Revival website at /root/AI/grokbuild/lbcrevival.com.

## Your only bot-related source of truth
Read the entire handoff package first (do not invent install steps):

  /root/AI/grokbuild/lbc-market-making-bot/handoff/

Especially:
  - handoff/README.md
  - handoff/lbcrevival-site.md          ← page copy, install, FAQs, attribution
  - handoff/website-copy-blocks.md      ← paste-ready HTML/Markdown blocks
  - handoff/discord-announcement.md
  - handoff/market-page-prompt.md       ← optional market page upgrades
  - ../QUICKSTART-LBCMM.md              ← user install (keep in sync with site)

Bot code lives at:
  /root/AI/grokbuild/lbc-market-making-bot
GitHub:
  https://github.com/realrouse/LBC-market-making-bot

## Goals
1. Publish a clear “Contribute liquidity” / LBC-market-making-bot section on lbcrevival.com
   so community members can install and run the bot in minutes.
2. Use the EXACT install path from handoff (bash install-lbcmm.sh → ./bin/lbcmm gui).
3. Always show attribution: forked from @neofutur’s multibot design (tradinebotte), GPL-3.0.
4. Always show safety: paper by default; no withdraw on API keys; not financial advice.
5. Do NOT implement trading or collect API keys on the website.
6. Optional: market page ±2% depth meter + 1D/4h views (see market-page-prompt.md).

## Product facts (do not contradict)
- Pair/venue: MEXC spot LBC/USDT
- Default depth band: ±2% (CoinGecko-style); adjustable 0.5%–15%
- Steps (levels per side): 1–30
- Community depth goal: ~$100 per side at ±2%
- GUI: http://127.0.0.1:8787/ after `./bin/lbcmm gui`
- First-time setup wizard: paper vs live, capital, depth
- Menu: Market Config | Status | Settings

## Deliverables
- Site page or section with install + FAQ + attribution
- Links to GitHub + QUICKSTART
- Checklist from lbcrevival-site.md marked done in your summary

Begin by reading handoff/README.md and handoff/lbcrevival-site.md, then implement on the site.
```

---
