# Discord announcement draft (LBRY Foundation / LBC Revival)

**Status:** Draft — human reviews before posting.

---

## Short

**LBC-market-making-bot** is ready for the community to try.

**Why:** MEXC LBC/USDT ±2% depth is still thin. Goal ≈ **$100 each side** so small trades don’t thrash the price.

**What:** Free local bot — browser UI or CLI. Assign USDT + LBC, set depth % (default ±2%), press **Start bot**. **Paper mode** first (no real money).

**Install:**
```bash
git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot
bash install-lbcmm.sh
./bin/lbcmm gui
```
Open http://127.0.0.1:8787/

**Attribution:** Forked **@neofutur’s** tradinebotte multibot design into an LBC-only standalone bot (GPL-3.0).

**Safety:** Paper by default. Live keys = Spot **View Order Details + Trade** only — **never Withdraw**. Not financial advice.

Make LBC great again — liquidity is a team sport.

---

## Longer (optional)

If people want to join LBC Revival with capital, resting USDT bids and LBC asks near mid is more useful than idle bags — but manual laddering is tedious. This bot does the boring part.

neofutur already proved real-money LBC strategies on MEXC. This community fork focuses on **easy depth contribution** first (Depth Provider), with advanced strategies in Settings.

Docs: repo `QUICKSTART-LBCMM.md` · Site: *(link when published on lbcrevival.com)*
