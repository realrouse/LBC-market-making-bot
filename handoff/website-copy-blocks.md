# Paste-ready copy blocks for lbcrevival.com

Use these as-is or lightly restyle. Keep attribution + safety.

---

## Block A — Hero / card (short)

**Title:** Contribute liquidity  

**Subtitle:** LBC-market-making-bot  

**Body:**  
Help keep MEXC LBC/USDT healthy. Run a small open-source bot on your computer that rests buy and sell orders near the mid price. Default targets CoinGecko-style **±2% depth**. Community goal: about **$100 on each side**. Even $10 helps.

**CTA button:** Get the bot  

**CTA link:** `https://github.com/realrouse/LBC-market-making-bot`  
(or site-local docs page that embeds Block B)

**Fine print:**  
Forked from @neofutur’s multibot design · GPL-3.0 · Paper mode by default · Not financial advice

---

## Block B — Install (keep exact commands)

### Install (3 steps)

```bash
git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot
bash install-lbcmm.sh
```

### Run the control panel

```bash
./bin/lbcmm gui
```

Open **http://127.0.0.1:8787/** in your browser.  
Complete the setup wizard → set USDT / LBC → press **Start bot**.

### Optional terminal commands

```bash
./bin/lbcmm depth      # see public ±2% depth
./bin/lbcmm status
./bin/lbcmm run --paper
```

**Requirements:** Python 3.10+, Linux or macOS.

---

## Block C — Why this matters

LBC/USDT on MEXC has been **thin** on the order book. CoinGecko-style metrics look at how much size sits within **±2% of mid**.

| | |
|---|---|
| **Problem** | Small depth → easy to move price → delisting / delist-risk narratives |
| **Goal** | ~**$100** resting on **each** side inside ±2% |
| **How** | Community members optionally run the bot with their own capital |

This does **not** guarantee price; it improves **tradeability**.

---

## Block D — Safety (always include)

- Starts in **paper mode** (simulated) until you choose live  
- Create MEXC API keys at [mexc.com/user/openapi](https://www.mexc.com/user/openapi)  
- Under **Spot**, enable only **View Order Details** + **Trade**  
- **Never enable Withdraw**  
- Keys stay on **your computer** — we never ask for them on the website  
- You can lose money if price trends through your orders  
- **Not financial advice** — participation is optional  

---

## Block E — Attribution (always include)

> This bot was **forked from @neofutur’s multibot design (tradinebotte)** to build an **LBC-only standalone** tool for the community.  
> License: **GNU GPL v3**. When you talk about it: *“I forked @neofutur’s multibot design to build an LBC-only bot.”*

---

## Block F — FAQ

**Is this a website wallet / cloud bot?**  
No. It runs on **your** machine. The site only explains how to install it.

**Do I need API keys for paper mode?**  
No.

**What is Depth %?**  
How far from the mid price your resting orders may sit. Default **±2%**. Range **0.5%–15%**.

**What are Steps?**  
How many order levels on each side (1–30).

**Can the site trade for me?**  
No. Never enter API keys on lbcrevival.com.

**Where is full documentation?**  
[QUICKSTART-LBCMM.md](https://github.com/realrouse/LBC-market-making-bot/blob/main/QUICKSTART-LBCMM.md) in the repo.

---

## Block G — Market page footer blurb

Depth is community-powered. Run the open **LBC-market-making-bot** to rest maker orders on MEXC LBC/USDT (paper mode available). Goal: ~$100 at ±2% each side. [Install →]

---

## Suggested HTML skeleton (optional)

```html
<section class="liquidity-bot" id="liquidity">
  <h2>Contribute liquidity</h2>
  <p class="lede">Help keep MEXC LBC/USDT tradeable with a free local market-making bot.</p>

  <div class="goal">
    <strong>Community depth goal</strong>
    <span>~$100 on each side within ±2% of mid</span>
  </div>

  <h3>Install</h3>
  <pre><code>git clone https://github.com/realrouse/LBC-market-making-bot.git
cd LBC-market-making-bot
bash install-lbcmm.sh
./bin/lbcmm gui</code></pre>
  <p>Then open <code>http://127.0.0.1:8787/</code></p>

  <h3>Safety</h3>
  <ul>
    <li>Paper mode by default</li>
    <li>MEXC Spot: View Order Details + Trade only — never Withdraw</li>
    <li>Not financial advice</li>
  </ul>

  <p class="attr">
    Forked from <strong>@neofutur</strong>’s multibot design (tradinebotte) · GPL-3.0
  </p>
  <p>
    <a href="https://github.com/realrouse/LBC-market-making-bot">GitHub</a>
    ·
    <a href="https://www.mexc.com/exchange/LBC_USDT">Trade LBC/USDT on MEXC</a>
  </p>
</section>
```
