# Opinion on tradinebotte — design, protocol, security

**For:** neofutur  
**From:** Grok analysis of the forked tree (realrouse/LBC-market-making-bot @ v0.90)  
**Date:** 2026-07-26  

---

## Executive summary

tradinebotte is a **serious multi-process trading platform**, not a script. The neutral `botcore` + connector registry + pure strategy planners (especially **BAMM**) are high quality. The cost is **operational complexity**: ZMQ data plane, multi-account inventory, systemd fleet — excellent on Apollo for operators, heavy for community “$10 liquidity” users.

**Recommendation we took for LBC public share:** extract a **standalone LBC-only bot** (in-process MEXC feed, Depth Provider UX) while keeping full tradinebotte as the operator/multibot research stack. GPL-3.0 + attribution preserved.

---

## Architecture — strengths

1. **Neutral core (`botcore`)**  
   Strategy protocol, connector `load`/`validate`, persistence helpers — no exchange imports. Plan D decoupling is the right long-term shape.

2. **Connector contract**  
   Shared methods (`post_order`, book parse, precision, open orders) with `validate()` against strategy requirements. Fail-closed precision for LBC sub-cent ticks is battle-tested (the “0.00 price” class of bugs is handled).

3. **Shared feed model**  
   One public WS per venue/symbol → ZMQ fan-out. Correct for multi-wallet hosts (rate limits + connection caps). MEXC protobuf depth + app-level PING shows real production friction was absorbed.

4. **Pure planners**  
   `bamm.py` is exemplary: geometric rungs, deploy-at-floor, stash cycle, `desired_orders(mid)` — unit-testable, shadow/live parity. Grid/accumulation follow similar discipline to varying degrees.

5. **Sim-by-default**  
   Missing keys → `sim_…` order IDs. Going live is explicit (env + inventory `is_live`). Strong operational safety culture.

6. **Observability**  
   Heartbeats, status page, inventory as single source of truth for fleet topology.

---

## Architecture — costs / risks

1. **Complexity tax**  
   ~47k LOC Python, multiple services (feed, indicators, status, bots). New contributors and community users will not boot this happily.

2. **Docs lag**  
   README still describes retired pieces (e.g. orderbook scalping bot) in places. Inventory/deploy docs are dense.

3. **Live path concentration**  
   Large `accumulation.py` + `live_bot.py` hold a lot of money-path logic. BAMM pure core is small; wiring is not.

4. **BAMM ≠ CoinGecko depth**  
   BAMM is **bullish accumulating** (floor, stash, downside weight). Community relisting narrative wants **two-sided ±2% depth**. Both are valid; they are different products. Symmetric “balanced MM” is hard when the thesis is price-up — your Discord point is correct.

5. **Cross-user ZMQ + SSH status**  
   Powerful on a shared host; wrong default for a single Windows/Mac user.

---

## Protocol / exchange notes

- MEXC spot public WS: protobuf on `wbs-api.mexc.com` (JSON depth retired).  
- LIMIT_MAKER can accept-then-cancel if it would cross — callers must not trust order id alone.  
- Account-read scope ≠ trade scope; `get_account` None means unknown, not zero.  
- Fees: maker 0 measured on your stack; taker effective depends on MX tier.

These details belong in any LBC-only extract (we carried them into `lbcmm/connectors/mexc.py`).

---

## Security opinion

| Area | Assessment |
|---|---|
| Credential handling | Good: env / chmod 600 files; not in git inventory |
| Default mode | Excellent: simulation without keys |
| Order types | Prefer post-only for maker bots — correct |
| Surface area | Large multiproc attack surface vs single process |
| Dependency set | Reasonable; Polymarket/web3 heavy for a CEX-only user |
| Key permissions | Docs should scream: **no withdraw** on API keys |

No critical “keys in repo” issue in the tree reviewed. Main risk for community forks is **users enabling live without understanding inventory risk** — UX must force paper-first and confirmations (we do this in `lbcmm`).

---

## What we extracted for LBC-market-making-bot

- MEXC connector + precision + depth ±2% helper  
- Depth Provider strategy (new, simple mode)  
- BAMM pure planner (advanced)  
- Single-process engine + CLI + local GUI  
- Handoff docs for site/Discord — site not implemented in bot session  

Out of default product: Polymarket, Binance, multi-account inventory, indicators service, fleet SSH status.

---

## Optional future collaboration

- Shared pure modules (BAMM/grid) as a tiny library both trees import  
- DCRDEX connector behind the same `botcore.connectors` registry  
- Optional “Apollo mode” that reattaches to shared `cex_feed`  

---

## Bottom line

**Production-grade operator platform** with a real-money LBC proof point. For “make LBC great again” community distribution, a **thin standalone extract** is the right product cut — not a simplified README on the full multiproc beast.

Thanks for GPL + the invitation to fork an LBC-only version.
