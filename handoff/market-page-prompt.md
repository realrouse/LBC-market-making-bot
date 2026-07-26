# Ready-to-run prompt — market page upgrades

Paste into a Grok session that owns **lbcrevival.com** (not the bot repo).

---

## Prompt

```
You are working on the LBC Revival website at /root/AI/grokbuild/lbcrevival.com.

Context:
- Market page lives at site/market.php (and drmalvin/market.php variants).
- Chart/ticker logic is in site/assets/market.js and uses site proxy.php for MEXC public data.
- Today ranges include 7d (4h candles), 30d, 90d (1d).
- Community wants: clearer 1D and 4h "action price" views + candles, and a ±2% depth meter for LBC/USDT.

Tasks:
1. Add explicit range controls for 1D and 4h action-price oriented views (and candle rendering if not already candle-style).
2. Fetch public MEXC depth (via existing proxy pattern only — no new secrets) and display:
   - mid price
   - sum of bid notional within mid−2%
   - sum of ask notional within mid+2%
   - goal markers at $100 each side
3. Keep styling consistent with the existing green dark theme.
4. Do not implement trading. Do not call private MEXC APIs.
5. Optionally link to LBC-market-making-bot install docs (see /root/AI/grokbuild/lbc-market-making-bot/handoff/lbcrevival-site.md).

Acceptance:
- Staging market page shows 1D and 4h views + live-ish ±2% depth numbers.
- Works without login.
- Mobile-usable.
```

## Depth formula (CoinGecko-style approximation)

```
mid = (best_bid + best_ask) / 2
bid_depth_2pct = sum(price * qty for bids where price >= mid * 0.98)
ask_depth_2pct = sum(price * qty for asks where price <= mid * 1.02)
```

MEXC public: `GET /api/v3/depth?symbol=LBCUSDT&limit=100` (via site proxy).

## Related paths

- `/root/AI/grokbuild/lbcrevival.com/site/market.php`
- `/root/AI/grokbuild/lbcrevival.com/site/assets/market.js`
- `/root/AI/grokbuild/lbcrevival.com/site/proxy.php`
