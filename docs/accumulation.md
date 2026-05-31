# BTC Accumulation Strategy — Design Document

> Bot: `tradinebotte-cex/accumulation_bot.py` v1.3  
> Strategy: `strategies/accumulation/btc_accumulation.json`  
> Conviction: long-only BTC spot, bull-run horizon (weeks to months)

---

## 1. Philosophy

The bot is not a scalper. It is a systematic position-builder that:

- Deploys capital upfront (conviction buy at startup).
- Adds to the position only on genuine dip signals (OBI + VWAP).
- Partially harvests profits at fixed bands without ever fully exiting.
- Buys back sold quantity at a discount, creating a ratchet effect.
- Parks idle USDT in Binance Flexible Earn between trades.

The core bet is that BTC is in a multi-month uptrend. Losses are paper losses until
the bot sells; the min_holdings_pct floor ensures a minimum BTC position is always held
regardless of profit-taking activity.

---

## 2. Capital Structure

| Parameter               | Value  | Role                                          |
|-------------------------|--------|-----------------------------------------------|
| `capital_usdt`          | 1000   | Total capital envelope                        |
| `initial_stake_usdt`    | 500    | Deployed unconditionally at startup           |
| `scale_in_usdt`         | 100    | Base OBI dip buy (adaptive: 100–300 USDT)     |
| `max_invested_pct`      | 0.90   | Hard cap: never invest more than 900 USDT     |
| `min_holdings_pct`      | 0.50   | Floor: never sell below 50% of peak BTC held  |

After the initial buy, ~500 USDT remains free. With max_invested_pct=90%, the bot can
deploy up to 400 USDT more in scale-ins before hitting the cap (at current BTC price).

---

## 3. Signal Pipeline

The bot consumes two ZMQ streams from the shared indicators service:

```
btc_scalping_spot   →  mid price, obi_ema, spread_bps
btc_vwap_context    →  dip_score, dip_zone
```

All signal computation lives in the indicators service; the bot only reads and acts.

### 3.1 Initial Buy

Triggered on the **first price tick** after startup (or first tick after restore if
`initial_done=False` in DB). No OBI or VWAP condition. Deploys `initial_stake_usdt`
at market.

Rationale: the bot has a bull-run conviction. Waiting for a signal before the first
deployment risks missing the primary trend.

### 3.2 OBI Dip Scale-in

Triggered when **all** of:

1. `obi_ema < -obi_entry_thresh` (`-0.50`) for `obi_confirm_n` (`20`) consecutive ticks  
   — sustained sell-side order-book imbalance, not a flash dip
2. `vwap_dip_score >= 0` (VWAP gate): price is below the 4h VWAP  
   — buys only on dips, never chases price above VWAP
3. Time since last buy > `min_scale_interval_s` (3600 s = 1h)  
   — cooldown prevents rapid DCA into a free-fall
4. `invested + scale_usdt <= max_invest` — position cap not breached
5. `scale_usdt <= free_usdt` — cash available

**Adaptive scale-in amount** (larger buys when deeper in the dip):

```
mult       = 1.0 + scale_in_dip_factor × max(dip_pct, 0)
scale_usdt = min(base × mult, base × scale_in_max_mult)
           = clamped to [100, 300] USDT
```

Where `dip_pct = (avg_entry - price) / avg_entry × 100`. At -2% below avg_entry,
mult=2.0 (200 USDT buy); at -4%, mult=3.0 (300 USDT, the cap).

After a scale-in, `pending_count` resets to 0 — another full 20-tick confirmation
is required before the next scale-in.

---

## 4. Exit Logic — Profit Ladder

Checked on **every price tick**, before OBI scale-in check.

```
target_price = avg_entry × (1 + band_pct / 100)
```

Bands: `[5%, 10%, 20%, 30%, 50%]`. When `price >= target`:

1. Calculate `qty = holdings_btc × sell_fraction` (15%)
2. Apply floor: `qty = min(qty, holdings_btc - peak_holdings_btc × min_holdings_pct)`
3. If qty < 1e-6: skip (floor prevents selling)
4. Sell `qty` at market
5. Mark band as `active` (prevents re-triggering at same band while position is above it)
6. Create `PendingRebuy` at `sell_price × (1 - discount)`

Bands only re-arm when the corresponding rebuy fills (price dips back down and the
rebuy executes). This creates a repeating staircase:

```
sell at +5% → wait → rebuy at ~+2% → sell again at +5% → ...
```

---

## 5. Rebuy Logic

Checked on **every tick**, before profit band check.

```
rebuy_price = sell_price × (1 - discount)
discount    = clamp(spread_ema × rebuy_spread_mult, min_d, max_d)
            = clamp(spread_ema × 3.0, 3%, 10%)
```

**Spread EMA** is an exponential moving average of `spread_bps` from the ZMQ stream
(alpha=0.1). It is a volatility proxy: wide spreads indicate choppy markets → larger
rebuy discount → more patient re-entry.

| Market condition    | spread_ema  | discount  | rebuy offset |
|---------------------|-------------|-----------|--------------|
| Calm trending       | ~0.01%      | 3% (min)  | -3% from sell|
| Normal              | ~0.05%      | ~15% → 10%| -10%         |
| Volatile            | ~0.15%      | 10% (max) | -10% from sell|

When rebuy fills: `active_bands.discard(band_pct)` — the band re-arms for the next
profit-taking cycle.

**Note**: the rebuy checks `price <= rb.rebuy_price` and `usdt_needed <= free_usdt`.
If the bot is fully invested and free_usdt is low, rebuys can be skipped.

---

## 6. Adaptive Mechanisms

### 6.1 Spread EMA (volatility proxy)

```python
spread_ema = alpha × spread_pct + (1 - alpha) × spread_ema
```

Alpha=0.1 → half-life ≈ 6.6 ticks (~6.6 minutes). Drives rebuy discount dynamically.
Initialized to 0.002% (typical calm Binance spread) so the first discount starts at minimum.

### 6.2 Adaptive Scale-in

The deeper the price is below avg_entry, the larger the scale-in. Controlled by
`scale_in_dip_factor` and `scale_in_max_mult`. This implements a simplified Kelly
bet-sizing: more conviction at more extreme dislocations.

### 6.3 Peak Holdings Floor

`peak_holdings_btc` tracks the all-time maximum BTC holdings since start (or restore).
`min_holdings_pct=0.50` prevents selling below 50% of that peak. This protects
against the profit ladder selling down to near-zero BTC in a strong rally.

---

## 7. Binance Simple Earn Integration

After each sell: `park_idle(free_usdt)` → subscribes `free_usdt - 20` USDT to Flexible Earn.  
Before each buy: `ensure_liquid(needed)` → redeems from Earn if spot wallet is short.

Binance Flexible redemptions settle in < 1 second. The 20 USDT liquid buffer
(`MIN_LIQUID_USDT`) is hardcoded in `earn_manager.py`.

Sim mode is active when `BINANCE_API_KEY` / `BINANCE_API_SECRET` are not set
(logs the actions without making real API calls).

---

## 8. State Persistence

SQLite WAL mode. Three tables:

| Table              | Content                                    | Rows                 |
|--------------------|--------------------------------------------|----------------------|
| `accum_state`      | Current position snapshot (id=1, upserted) | Always 1             |
| `accum_trades`     | Every buy/sell with full context           | Grows indefinitely   |
| `accum_snapshots`  | Position snapshot every 20 ticks           | ~4/hour              |

On restart: `_restore_state()` recovers holdings, avg_entry, pending_rebuys, active_bands.
If no state row: fresh start (initial buy fires on first tick).

---

## 9. Identified Weaknesses

### W1 — Initial buy ignores all signals
The bot deploys 500 USDT at the first tick regardless of OBI, VWAP, or market context.
In a downtrend, this locks in a high avg_entry and increases the unrealized loss before
any scale-in improves the average.

### W2 — No macro bear protection
The max_invested_pct cap (90%) helps but does not prevent the bot from DCA-ing into a
prolonged bear market. At -40% the bot may exhaust capital buying all the way down.
There is no circuit breaker for macro bearish regimes.

### W3 — Cooldown is purely time-based
min_scale_interval_s=3600 ignores signal strength. A very strong OBI signal (−0.9)
waits the same 1h as a barely-qualifying signal (−0.51). A deeper dip is a better
opportunity, but the cooldown does not shorten for stronger signals.

### W4 — Rebuy uses fixed qty at sell time
If BTC price crashes after a sell, the rebuy fills at a much lower price, buying the
same BTC qty for less USDT. This is actually correct (buy-low behavior). But if price
never returns to the rebuy level, the pending rebuy hangs indefinitely, tying up the
band slot and preventing that band from re-arming for profit-taking in a new leg up.

### W5 — No macro OBI filter
The bot only checks spot OBI (btc_scalping_spot). The indicators service also publishes
`btc_macro_obi` (multi-TF aggregate). During macro bearish phases (macro OBI << 0), the
bot will still scale in if the 1-minute spot OBI dips. This adds to a losing position
in a trend that the bot cannot reverse.

### W6 — earn_manager MIN_LIQUID_USDT is hardcoded
The 20 USDT liquid buffer in `earn_manager.py` is not configurable from the strategy JSON.
If capital_usdt is changed significantly, this fixed value may be inappropriate.

### W7 — avg_entry not recalculated after sells
Strictly speaking, avg_entry represents the FIFO cost basis of remaining holdings.
The bot does NOT update avg_entry after sells (the `_sell` function leaves it unchanged).
This means bands remain anchored to the cost basis of the entire position history, not
just remaining holdings. For the profit ladder this is intentional (you want to realize
profit vs original cost), but it can cause bands to retrigger at artificially high prices
after significant selling has lowered the actual cost basis of remaining shares.

### W8 — No ATR-based scale-in guard
Extreme volatility (flash crashes, sudden gaps) can trigger the OBI confirm_n threshold
without representing a genuine accumulation opportunity. An ATR or volatility filter
could gate out these events.

---

## 10. Proposed Improvements (Prioritized)

### P1 — Macro OBI gate (HIGH — 1–2h)

Add subscription to `btc_macro_obi` stream. Block scale-ins when macro OBI < threshold
(e.g., `macro_obi_block_thresh: -0.3`). Same pattern as `orderbook_bot.py`.

This prevents DCA-ing into a macro downtrend while still allowing opportunistic buys
during brief spot dips in a broadly sideways or bullish regime.

```python
# New DEFAULTS entry
"macro_obi_gate":         True,
"macro_obi_block_thresh": -0.30,
"macro_obi_stream_id":    "btc_macro_obi",

# In _check_obi_scale_in
if p.get("macro_obi_gate", True) and state.macro_obi < p.get("macro_obi_block_thresh", -0.30):
    return
```

### P2 — Configurable initial buy gate (MEDIUM — 30min)

Add `vwap_gate_initial: False` param. When `True`, the initial buy also respects the
VWAP gate. Useful for deployments in uncertain market conditions.

```python
# In _handle_indicator, initial buy block:
if p.get("vwap_gate_initial", False):
    if p.get("vwap_gate", True) and state.vwap_dip_score < 0.0:
        return  # defer initial buy until below VWAP
```

### P3 — Signal-adaptive cooldown (MEDIUM — 1h)

Replace the flat 1h cooldown with a signal-strength-based shortening:

```python
"scale_in_cooldown_base_s":   3600,   # base cooldown (1h)
"scale_in_cooldown_min_s":    900,    # floor at 15min for very strong signals
"scale_in_obi_strong_thresh": 0.80,   # OBI below -0.80 → halved cooldown

# In _check_obi_scale_in:
cooldown = p["scale_in_cooldown_base_s"]
if abs(state.obi_ema) >= p["scale_in_obi_strong_thresh"]:
    cooldown = max(p["scale_in_cooldown_min_s"], cooldown // 2)
if (ts_ms - state.last_buy_ts) / 1000.0 < cooldown:
    return
```

### P4 — Pending rebuy expiry (MEDIUM — 1h)

Pending rebuys that never fill leave a band permanently locked. Add an expiry:

```python
"rebuy_max_age_days": 30,  # cancel pending rebuys older than 30 days

# Store ts_ms in PendingRebuy, prune in _check_rebuys
```

When a rebuy expires: `active_bands.discard(rb.band_pct)` so the band can retrigger
in the next rally.

### P5 — Trailing rebuy price (LOW — 2h)

Instead of a fixed rebuy_price, trail it downward if price continues falling past the
initial rebuy target:

```python
"rebuy_trail_pct": 0.005,  # if price falls >0.5% past rebuy_price, trail down

# In _check_rebuys:
for rb in state.pending_rebuys:
    if price < rb.rebuy_price * (1 - p.get("rebuy_trail_pct", 0)):
        rb.rebuy_price = price * (1 - small_buffer)  # trail
```

This avoids catching a falling knife on the rebuy.

### P6 — Configurable earn liquid buffer (LOW — 20min)

Move `MIN_LIQUID_USDT` to strategy JSON:

```python
"earn_min_liquid_usdt": 20.0,
```

Pass it through to `park_idle()` / `ensure_liquid()` calls.

### P7 — Periodic position report to DB (LOW — 30min)

Add a daily summary row to `accum_state` with realized PnL, Earn yield, and total
portfolio value (BTC × price + free_usdt + earn_parked). Currently Earn yield is
not tracked at all — the bot doesn't know how much USDT interest has accrued.

---

## 11. Live Performance Notes (as of 2026-05-30)

| Account        | Holdings   | Avg Entry | uPnL      | Free USDT |
|----------------|------------|-----------|-----------|-----------|
| Test account A | 0.009468   | 73929.91  | +0.18%    | 299.86    |
| Test account B | 0.010885   | 77183.37  | −4.05%    | 159.70    |

Both in paper mode (no real orders). Account B's higher avg_entry reflects an earlier
deployment when price was higher. Both have no pending rebuys or triggered bands,
meaning BTC has not yet reached any of the +5/10/20/30/50% profit targets from their
respective avg_entries.
