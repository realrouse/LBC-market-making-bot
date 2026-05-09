# Indicators — Reference Guide

> 🇫🇷 [Version française](indicators.fr.md)

`bot/indicators.py` is an optional pipeline stage that subscribes to market
data (ZeroMQ feed and/or Binance WebSocket) and publishes enriched messages
on a dedicated PUB socket. It supports two categories of data:

- **Computed indicators** — applied to a rolling price series (RSI, SMA, EMA,
  volatility). Require at least `min_ticks` data points before publishing.
- **Poll-based sources** — fetch a raw scalar value from an external REST API
  at a configurable interval. No price history required.

All output is broadcast on one PUB socket; consumers filter by `stream_id`.
See [design.md](design.md) for the ZeroMQ topology and dynamic registration
protocol.

---

## 1. Configuration format

Each stream is declared in a JSON config file:

```json
{
  "zmq_feed_addr": "tcp://127.0.0.1:5557",
  "zmq_out_addr":  "tcp://127.0.0.1:5559",
  "zmq_reg_addr":  "tcp://127.0.0.1:5561",
  "min_ticks": 25,
  "streams": [
    {
      "id":              "btc_4h",
      "asset":           "BTCUSDT",
      "source":          "binance_ws",
      "timeframe":       "4h",
      "indicators":      [
        {"type": "rsi",        "period": 14},
        {"type": "volatility", "period": 20}
      ],
      "seed_periods":    50,
      "poll_interval_s": 0
    }
  ]
}
```

### Top-level fields

| Field | Default | Description |
|---|---|---|
| `zmq_feed_addr` | `tcp://127.0.0.1:5557` | Address of the `feed.py` PUB socket to subscribe to (feed source only) |
| `zmq_out_addr` | `tcp://127.0.0.1:5559` | PUB address this indicators instance binds |
| `zmq_reg_addr` | `tcp://127.0.0.1:5561` | REP address for dynamic stream registration |
| `min_ticks` | 25 | Minimum price ticks in buffer before publishing (computed indicators only) |

### Stream fields

| Field | Required | Default | Description |
|---|---|---|---|
| `id` | yes | — | Unique stream identifier; used as `stream_id` in published messages |
| `asset` | yes¹ | `""` | Trading pair (e.g. `"BTCUSDT"`) |
| `source` | yes | `"feed"` | Data source — see section 2 |
| `timeframe` | yes¹ | `"tick"` | Candle timeframe for `binance_ws`; `"n/a"` for poll sources |
| `indicators` | yes² | `[]` | List of `{"type": "...", "period": N}` objects |
| `seed_periods` | no | `50` | Historical REST candles to pre-load at startup (`binance_ws` only) |
| `poll_interval_s` | no | `0` | Poll interval in seconds; `0` = use source default |

¹ Optional for poll-based sources (`binance_funding`, `deribit_iv`, `fear_greed`,
`binance_oi`, `binance_ls_ratio`, `binance_liquidations`).  
² Empty `[]` is allowed for poll-based sources only; required for `feed` and
`binance_ws`.

---

## 2. Data sources

| `source` | Category | External dependency | Default poll |
|---|---|---|---|
| `feed` | computed | ZeroMQ feed.py (local) | event-driven |
| `binance_ws` | computed | Binance kline WebSocket | event-driven |
| `binance_funding` | poll | `fapi.binance.com` REST | 900 s (15 min) |
| `deribit_iv` | poll | `www.deribit.com` REST | 300 s (5 min) |
| `fear_greed` | poll | `api.alternative.me` REST | 3600 s (1 h) |
| `binance_oi` | poll | `fapi.binance.com` REST | 300 s (5 min) |
| `binance_ls_ratio` | poll | `fapi.binance.com` REST | 300 s (5 min) |
| `binance_liquidations` | poll | `fapi.binance.com` REST | 300 s (5 min) |

---

## 3. Computed indicators

These are applied to a `PriceSeries` ring buffer. They are configured via the
`indicators` list inside a stream spec and work with both `feed` and
`binance_ws` sources.

### RSI — Relative Strength Index

**Key:** `rsi_N` (e.g. `rsi_14`)  
**Config:** `{"type": "rsi", "period": 14}`  
**Minimum prices needed:** N + 1  
**Output range:** 0 – 100 (float)

Wilder's RSI over the last N price deltas:

```
avg_gain = mean(positive deltas over last N)
avg_loss = mean(|negative deltas| over last N)
RSI = 100 − 100 / (1 + avg_gain / avg_loss)
```

Returns `None` when fewer than N + 1 prices are in the buffer. Returns 100.0
when `avg_loss == 0` (all gains).

**Interpretation:** >70 = overbought, <30 = oversold. For 5-min binary
markets, a high RSI alongside `best_bid ≥ 0.96` may indicate overextension —
the UP outcome is already priced in. A low RSI with a high bid is rarer and
may be a stronger entry signal.

---

### SMA — Simple Moving Average

**Key:** `sma_N` (e.g. `sma_20`)  
**Config:** `{"type": "sma", "period": 20}`  
**Minimum prices needed:** N  
**Output range:** same scale as input prices (0 – 1 for Polymarket bids)

```
SMA(N) = mean(prices[-N:])
```

Returns `None` when fewer than N prices are in the buffer.

**Interpretation:** Price above SMA = upward trend; price crossing SMA from
below = potential support. For feed-source streams (Polymarket `best_bid`),
SMA smooths out tick noise. Useful as a trend-confirmation filter alongside
the 0.96 threshold.

---

### EMA — Exponential Moving Average

**Key:** `ema_N` (e.g. `ema_9`)  
**Config:** `{"type": "ema", "period": 9}`  
**Minimum prices needed:** N  
**Output range:** same scale as input prices

```
k = 2 / (N + 1)
EMA₀ = SMA(prices[:N])          # seed with SMA
EMAᵢ = priceᵢ × k + EMAᵢ₋₁ × (1 − k)
```

Returns `None` when fewer than N prices are in the buffer.

**Interpretation:** More reactive than SMA (gives more weight to recent
prices). Typical pairs: EMA 9 + EMA 20 for crossover signals, or EMA 12 +
EMA 26 for MACD (see TODO). A short EMA crossing above a long EMA = momentum
shift.

---

### Volatility — Rolling volatility

**Key:** `vol_N` (e.g. `vol_20`)  
**Config:** `{"type": "volatility", "period": 20}`  
**Minimum prices needed:** N + 1  
**Output range:** 0 – ∞ (dimensionless log-return std-dev)

Population standard deviation of log-returns over the last N + 1 prices:

```
log_returns = [log(p[i] / p[i-1]) for i in 1..N]
vol = sqrt( mean( (r - mean(log_returns))² ) )
```

Returns `None` when fewer than N + 1 prices are in the buffer, or when any
price ≤ 0. Returns 0.0 for a constant series.

**Interpretation:** Low volatility = stable trending market; high volatility =
uncertainty or rapid reversals. A volatility spike before a binary market
closes raises the risk that the current bid price is unstable.

---

## 4. Poll-based sources

These sources fetch a single value from an external REST API. They do not
require a price series. `indicators: []` is mandatory in the JSON config.

### `binance_funding` — Perpetual funding rate

**Endpoint:** `https://fapi.binance.com/fapi/v1/premiumIndex`  
**Default interval:** 900 s (15 min)  
**Auth required:** no

```json
{
  "t":               "indicators",
  "stream_id":       "btc_funding",
  "funding_rate":    0.0001,
  "next_funding_ms": 1746000000000,
  "ts":              1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `funding_rate` | float | Current Binance perp funding rate. Positive = longs pay shorts. Typical range: ±0.03 % per 8 h. |
| `next_funding_ms` | int | Next funding settlement (Unix ms) |

**Interpretation:** High positive funding (> 0.01 %) = crowded long side,
slight bearish lean. Negative funding = crowded short side, slight bullish
lean. Useful as a macro tilt, not a trade-by-trade signal.

---

### `deribit_iv` — Implied volatility (DVOL)

**Endpoint:** `https://www.deribit.com/api/v2/public/get_index_price?index_name=dvol_btc`  
**Default interval:** 300 s (5 min)  
**Auth required:** no

```json
{
  "t":         "indicators",
  "stream_id": "btc_dvol",
  "dvol":      62.5,
  "ts":        1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `dvol` | float | Deribit BTC annualised implied volatility (e.g. 62.5 ≈ 62.5 %) |

**Interpretation:** High DVOL (> 80 %) = options market expects large moves;
low DVOL (< 40 %) = calm market. A spike in DVOL while `best_bid` is near
0.96 may indicate the "certainty" is fragile. Useful for position sizing
(reduce size in high-IV regime).

---

### `fear_greed` — Fear & Greed Index

**Endpoint:** `https://api.alternative.me/fng/?limit=1`  
**Default interval:** 3600 s (1 h)  
**Auth required:** no

```json
{
  "t":                "indicators",
  "stream_id":        "fear_greed",
  "fear_greed":       72,
  "fear_greed_label": "Greed",
  "ts":               1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `fear_greed` | int | Index value 0–100 |
| `fear_greed_label` | string | `"Extreme Fear"` / `"Fear"` / `"Neutral"` / `"Greed"` / `"Extreme Greed"` |

**Interpretation:** Extreme Greed (> 80) historically precedes corrections;
Extreme Fear (< 20) historically precedes recoveries. Macro context only —
far too slow for 5-min prediction markets.

---

### `binance_oi` — Futures open interest

**Endpoint:** `https://fapi.binance.com/futures/data/openInterestHist`  
**Params:** `period=5m&limit=2`  
**Default interval:** 300 s (5 min)  
**Auth required:** no

```json
{
  "t":             "indicators",
  "stream_id":     "btc_oi",
  "oi_btc":        45690.57,
  "oi_usd":        4569056780.0,
  "oi_change_btc": 12.44,
  "oi_change_usd": 1244440.0,
  "ts":            1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `oi_btc` | float | Total open interest in BTC contracts |
| `oi_usd` | float | Total open interest in USD |
| `oi_change_btc` | float | Change since previous poll (+= positions opened, −= positions closed) |
| `oi_change_usd` | float | Same in USD |

`oi_change_*` is 0.0 on the first poll (no previous reference).

**Interpretation:** Rising OI + rising price = genuine trend (new longs
entering). Rising OI + falling price = new shorts entering (bearish). Falling
OI = position unwinding regardless of direction (reversal risk). A large OI
drop immediately before a binary market closes may indicate smart money
exiting the directional bet.

---

### `binance_ls_ratio` — Long/short account ratio

**Endpoint:** `https://fapi.binance.com/futures/data/topLongShortAccountRatio`  
**Params:** `period=5m&limit=1`  
**Default interval:** 300 s (5 min)  
**Auth required:** no

```json
{
  "t":                "indicators",
  "stream_id":        "btc_ls_ratio",
  "long_short_ratio": 1.2345,
  "long_pct":         0.5523,
  "short_pct":        0.4477,
  "ts":               1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `long_short_ratio` | float | `long_pct / short_pct` |
| `long_pct` | float | Fraction of top-trader accounts that are net long (0–1) |
| `short_pct` | float | Fraction of top-trader accounts that are net short (0–1) |

**Note:** This measures *account count*, not *position size*. A single
large short position held by one account is not reflected here.

**Interpretation:** Contrarian signal. When top traders are overwhelmingly
long (ratio > 1.5), price often reverses down (crowded trade). Ratio < 0.7
(mostly short) tends to precede squeezes upward. Most predictive during
trending regimes with high OI.

---

### `binance_liquidations` — Forced liquidation orders

**Endpoint:** `https://fapi.binance.com/fapi/v1/forceOrders`  
**Params:** `startTime = now − interval`  
**Default interval:** 300 s (5 min)  
**Auth required:** no

```json
{
  "t":             "indicators",
  "stream_id":     "btc_liquidations",
  "liq_long_usd":  1250000.0,
  "liq_short_usd": 80000.0,
  "liq_net_usd":   -1170000.0,
  "liq_count":     45,
  "ts":            1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `liq_long_usd` | float | USD value of long positions liquidated in the interval (`SELL` forced orders) |
| `liq_short_usd` | float | USD value of short positions liquidated in the interval (`BUY` forced orders) |
| `liq_net_usd` | float | `liq_short_usd − liq_long_usd`. Negative = long liquidation dominant (bearish cascade). |
| `liq_count` | int | Total number of forced orders in the interval |

**Interpretation:** A large `liq_long_usd` spike means longs are being
force-sold — this drives price down and can trigger further cascading. Large
`liq_short_usd` = short squeeze in progress. Either event increases realized
volatility in the next 5-min window, which is directly relevant to predicting
binary outcome certainty.

---

## 5. Pre-built configuration files

Each file declares one stream on a dedicated PUB + REP port pair, ready to
run as an independent `indicators.py` instance.

| Config file | Source | `stream_id` | PUB port | REP port | Default poll |
|---|---|---|---|---|---|
| `indicators_4h_bitcoin.json` | `binance_ws` | `btc_4h` | 5559 | 5561 | event-driven |
| `indicators_1d_bitcoin.json` | `binance_ws` | `btc_1d` | 5560 | 5562 | event-driven |
| `indicators_funding_bitcoin.json` | `binance_funding` | `btc_funding` | 5563 | 5564 | 900 s |
| `indicators_deribit_iv_bitcoin.json` | `deribit_iv` | `btc_dvol` | 5565 | 5566 | 300 s |
| `indicators_fear_greed.json` | `fear_greed` | `fear_greed` | 5567 | 5568 | 3600 s |
| `indicators_oi_bitcoin.json` | `binance_oi` | `btc_oi` | 5569 | 5570 | 300 s |
| `indicators_ls_ratio_bitcoin.json` | `binance_ls_ratio` | `btc_ls_ratio` | 5571 | 5572 | 300 s |
| `indicators_liquidations_bitcoin.json` | `binance_liquidations` | `btc_liquidations` | 5573 | 5574 | 300 s |

`indicators.json` is a combined config (both `btc_4h` and `btc_1d`) for
single-instance multi-stream setups.

### Starting a dedicated indicators instance

```bash
# 4h klines (account-a)
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
  bash scripts/start_indicators.sh

# Open interest (separate process, separate port)
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_oi_bitcoin.json \
  bash scripts/start_indicators.sh
```

---

## 6. Dynamic registration

Any bot can request a new stream at runtime by sending a REQ to the REP
socket (`:5561` by default). The server starts the task if not already
running and replies immediately.

```python
import zmq, json
ctx = zmq.Context()
req = ctx.socket(zmq.REQ)
req.connect("tcp://127.0.0.1:5561")

req.send_json({
    "cmd":        "subscribe",
    "source":     "binance_ws",
    "asset":      "BTCUSDT",
    "timeframe":  "4h",
    "indicators": [{"type": "rsi", "period": 14}],
})
resp = req.recv_json()
# {"status": "ok", "stream_id": "btc_4h"}
```

For poll-based sources, `asset` and `timeframe` are optional:

```python
req.send_json({"cmd": "subscribe", "source": "binance_oi", "asset": "BTCUSDT"})
resp = req.recv_json()
# {"status": "ok", "stream_id": "binance_oi"}
```

**Limitation:** `source="feed"` streams cannot be registered dynamically —
declare them in the JSON config file.

---

## 7. Port configuration

All port addresses are computed from `TRADINEBOTTE_PORT_BASE` (default: 5557).
Setting this variable shifts the entire default port layout uniformly, enabling
two independent stacks on the same machine without editing any JSON config.

| Variable | Default | Description |
|---|---|---|
| `TRADINEBOTTE_PORT_BASE` | `5557` | Base port. All default addresses shift by `PORT_BASE − 5557`. |
| `TRADINEBOTTE_FEED_ADDR` | `tcp://127.0.0.1:{PORT_BASE}` | Feed PUB address. Overrides `PORT_BASE` for the feed only. |
| `TRADINEBOTTE_INDICATORS_ADDR` | `tcp://127.0.0.1:{PORT_BASE+2}` | Indicators PUB address. |
| `TRADINEBOTTE_INDICATORS_REG_ADDR` | `tcp://127.0.0.1:{PORT_BASE+4}` | Indicators REP address for dynamic registration. |

When `PORT_BASE` is set, addresses declared in JSON config files (`zmq_out_addr`,
`zmq_reg_addr`, `zmq_feed_addr`) are shifted by the same offset. Per-service env
vars override everything without shifting.

```bash
# Default stack — ports 5557 / 5559 / 5561 …
bash scripts/start_indicators.sh

# Second independent stack — all ports shifted by +1000
TRADINEBOTTE_PORT_BASE=6557 \
TRADINEBOTTE_INDICATORS_CONFIG=strategies/indicators_4h_bitcoin.json \
  bash scripts/start_indicators.sh
```

---

## 8. Planned indicators (TODO)

These will be added as new `type` values in `_VALID_INDICATOR_TYPES` and
implemented in `PriceSeries.compute_indicators`. They reuse existing Binance
kline data — no new REST or WebSocket source needed.

| Indicator | Keys | Notes |
|---|---|---|
| **MACD** (12/26/9) | `macd`, `macd_signal`, `macd_hist` | `macd = EMA12 − EMA26`; `signal = EMA9(macd)`; `hist = macd − signal` |
| **Bollinger Bands** (20, ±2σ) | `bb_upper`, `bb_lower`, `bb_width` | `width = (upper − lower) / middle` — measures volatility regime |
| **VWAP** | `vwap` | Requires kline volume (`v` field); intraday reset at midnight UTC |
| **Stochastic RSI** | `stoch_rsi_k`, `stoch_rsi_d` | `k = (RSI − min_RSI) / (max_RSI − min_RSI)` smoothed × 3; `d = SMA3(k)` |

---

## 9. Related files

| File | Role |
|---|---|
| `bot/indicators.py` | Implementation: all sources, `PriceSeries`, config loader, ZMQ tasks |
| `strategies/indicators_*.json` | Per-stream config files |
| `tests/test_indicators.py` | Unit and integration tests (112 tests) |
| `docs/design.md` | ZeroMQ topology, message catalog, ZeroMQ vs MQTT analysis |
