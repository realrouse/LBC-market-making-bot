# Indicators — Reference Guide

> 🇫🇷 [Version française](indicators.fr.md)

`tradinebotte-indicators/indicators.py` is an optional pipeline stage that subscribes to market
data (ZeroMQ feed and/or Binance WebSocket) and publishes enriched messages
on a dedicated PUB socket. It supports three categories of data:

- **Computed indicators** — applied to a rolling price series (RSI, SMA, EMA,
  volatility). Require at least `min_ticks` data points before publishing.
- **WebSocket-based sources** — maintain a real-time connection to Binance
  WebSocket streams and publish on each event batch. No REST polling required.
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

| `source` | Category | External dependency | Default interval |
|---|---|---|---|
| `feed` | computed | ZeroMQ feed.py (local) | event-driven |
| `binance_ws` | computed | Binance kline WebSocket | event-driven (closed candle) |
| `binance_scalping` | WebSocket | Binance depth20@100ms + aggTrade | event-driven (every N depth events) |
| `cex_scalping` | ZeroMQ | shared `cex_feed` (local, any exchange) | event-driven (every N book updates) |
| `binance_full_depth` | WebSocket | Binance depth@100ms + REST snapshot | event-driven (every N depth events) |
| `binance_funding` | poll | `fapi.binance.com` REST | 900 s (15 min) |
| `deribit_iv` | poll | `www.deribit.com` REST | 300 s (5 min) |
| `fear_greed` | poll | `api.alternative.me` REST | 3600 s (1 h) |
| `binance_oi` | poll | `fapi.binance.com` REST | 300 s (5 min) |
| `binance_ls_ratio` | poll | `fapi.binance.com` REST | 300 s (5 min) |
| `binance_liquidations` | poll | `fapi.binance.com` REST | 300 s (5 min) |
| `binance_vwap_context` | poll | `api.binance.com` REST | 3600 s (1 h) |
| `binance_volume_profile` | poll | `api.binance.com` REST | 3600 s (1 h) |
| `binance_macro_obi` | poll | `api.binance.com` REST | 60 s (1 min) |

**`cex_scalping`** consumes the shared `cex_feed` data-plane service (which fetches each CEX order book once and fans it out over ZeroMQ) instead of opening its own exchange WebSocket, and republishes a scalping stream (`mid` / `obi` / `obi_ema` / `spread_bps`) shaped like `binance_scalping`. Params: `exchange` + `symbol` (the cex_feed tags, e.g. `mexc` / `BTCUSDT`), `cex_feed_addr`, `obi_ema_alpha`, `publish_every_n`. Used e.g. for `btc_scalping_mexc` (MEXC spot, decoded from MEXC's protobuf WS by cex_feed).

**On-demand registration:** beyond the static config, a bot can declare the streams it needs (`indicators_streams`) and register them with the REP socket (`zmq_reg_addr`), re-registering periodically so a stream self-heals if the indicators service restarts — no static-config edit required.

---

## 3. Computed indicators

These are applied to a `PriceSeries` ring buffer. They are configured via the
`indicators` list inside a stream spec.

### 3.1 Price-based indicators (`feed` and `binance_ws`)

These work with any source that provides a price series.

#### RSI — Relative Strength Index

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

#### SMA — Simple Moving Average

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

#### EMA — Exponential Moving Average

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

#### Volatility — Rolling volatility

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

### 3.2 OHLCV-based indicators (`binance_ws` only)

These indicators require High, Low, and Volume data from Binance klines. They
are **not available** for `feed`-source streams (which only carry `best_bid`).

#### ATR — Average True Range

**Key:** `atr_N` (e.g. `atr_14`)  
**Config:** `{"type": "atr", "period": 14}`  
**Source requirement:** `binance_ws`

Average true range over the last N bars. True range = max(H−L, |H−prev_C|,
|L−prev_C|). Measures raw volatility in price units.

---

#### Bollinger Bands

**Keys:** `bb_upper_N`, `bb_mid_N`, `bb_lower_N` (e.g. `bb_upper_20`)  
**Config (three separate entries):**
```json
{"type": "bollinger_upper", "period": 20},
{"type": "bollinger_mid",   "period": 20},
{"type": "bollinger_lower", "period": 20}
```
**Source requirement:** `binance_ws`  
**Band multiplier:** fixed at k = 2.0

`bb_mid_N` = SMA(close, N); `bb_upper_N` = mid + 2σ; `bb_lower_N` = mid − 2σ.
Width = (upper − lower) / mid measures volatility regime.

---

#### VWAP — Volume-Weighted Average Price

**Key:** `vwap_N` (e.g. `vwap_50`)  
**Config:** `{"type": "vwap", "period": 50}`  
**Source requirement:** `binance_ws`

VWAP of the last N closed candles using close price × base volume. Rolling
(not intraday-reset). Useful as a trend-anchoring reference.

---

#### vol_zscore — Volume Z-score

**Key:** `vol_z_N` (e.g. `vol_z_20`)  
**Config:** `{"type": "vol_zscore", "period": 20}`  
**Source requirement:** `binance_ws`

Z-score of current bar volume relative to the rolling mean and standard
deviation over N bars: `(current_vol − mean) / std`. Positive = elevated
volume; negative = below-average volume.

---

#### rolling_max — Rolling Maximum of Highs

**Key:** `rmax_N` (e.g. `rmax_20`)  
**Config:** `{"type": "rolling_max", "period": 20}`  
**Source requirement:** `binance_ws`

Maximum high price over the last N bars. Useful for breakout detection.

---

## 4. WebSocket-based sources

These sources maintain a persistent Binance WebSocket connection and publish
batched output without REST polling. `indicators: []` is mandatory.

### `binance_scalping` — Real-time OBI and trade flow

**Streams:** Binance combined `depth20@100ms` + `aggTrade`  
**Publish trigger:** every `publish_every_n` depth updates (default 10)  
**Auth required:** no

```json
{
  "t":                 "indicators",
  "stream_id":         "btc_scalping_spot",
  "asset":             "BTCUSDT",
  "market":            "spot",
  "obi":               0.12,
  "obi_ema":           0.10,
  "obi_decel":         -0.003,
  "spread_bps":        1.8,
  "tfi":               0.23,
  "realized_vol_bps":  4.7,
  "ts":                1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `obi` | float | Raw order book imbalance at `obi_levels` depth: `(bid_vol − ask_vol) / (bid_vol + ask_vol)` ∈ [−1, +1] |
| `obi_ema` | float | EMA-smoothed OBI (`obi_ema_alpha`, default 0.05) — spoofing filter |
| `obi_decel` | float | First difference of `obi_ema` — OBI acceleration/deceleration signal |
| `spread_bps` | float | `(best_ask − best_bid) / mid × 10000` in basis points |
| `tfi` | float | Trade flow imbalance over `tfi_window_s`: `(buy_vol − sell_vol) / total_vol` ∈ [−1, +1] |
| `realized_vol_bps` | float | Rolling population std-dev of mid-price log-returns in basis points (absent when insufficient data) |

**Stream parameters:**

| Param | Default | Description |
|---|---|---|
| `market` | `"spot"` | `"spot"` or `"perp"` — selects the Binance WebSocket endpoint |
| `obi_levels` | 10 | Number of top-of-book levels summed for OBI |
| `obi_ema_alpha` | 0.05 | EMA smoothing coefficient for `obi_ema` |
| `tfi_window_s` | 60.0 | Rolling window in seconds for TFI aggregation |
| `vol_window_n` | 200 | Mid-price sample count for realized vol |
| `publish_every_n` | 10 | Throttle: publish once every N depth events |

---

### `binance_full_depth` — Full order book reconstruction

**Streams:** Binance `btcusdt@depth@100ms` (incremental diffs) + REST snapshot  
**Publish trigger:** every `publish_every_n` depth updates (default 10)  
**Auth required:** no

Maintains the complete spot order book (up to 5 000 levels) using the
documented Binance reconnect+resync algorithm: on connect, buffer WebSocket
events while fetching a REST snapshot (`GET /api/v3/depth?limit=5000`); apply
the snapshot; replay buffered events (discard stale, validate sequence
`U == lastUpdateId + 1`); then stream live. On any gap or error: resync from
scratch.

```json
{
  "t":                  "indicators",
  "stream_id":          "btc_full_depth",
  "asset":              "BTCUSDT",
  "best_bid":           67420.10,
  "best_ask":           67421.50,
  "mid":                67420.80,
  "spread_bps":         2.08,
  "obi_10":             0.15,
  "obi_100":            0.08,
  "obi_500":            0.03,
  "cum_bid_vol_1.0pct": 12.45,
  "cum_ask_vol_1.0pct": 9.87,
  "wall_bid_price":     67300.00,
  "wall_bid_qty":       4.2,
  "wall_ask_price":     67500.00,
  "wall_ask_qty":       3.8,
  "book_levels_bid":    4872,
  "book_levels_ask":    4651,
  "ts":                 1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `best_bid` | float | Best bid price |
| `best_ask` | float | Best ask price |
| `mid` | float | `(best_bid + best_ask) / 2` |
| `spread_bps` | float | Spread in basis points |
| `obi_N` | float | OBI at N levels for each N in `obi_levels_list` (e.g. `obi_10`, `obi_100`, `obi_500`) |
| `cum_bid_vol_Xpct` | float | Cumulative bid quantity within X% below mid |
| `cum_ask_vol_Xpct` | float | Cumulative ask quantity within X% above mid |
| `wall_bid_price` | float | Price of the largest single bid level within `wall_range_pct` of mid |
| `wall_bid_qty` | float | Quantity at `wall_bid_price` |
| `wall_ask_price` | float | Price of the largest single ask level within `wall_range_pct` of mid |
| `wall_ask_qty` | float | Quantity at `wall_ask_price` |
| `book_levels_bid` | int | Number of price levels currently tracked on the bid side |
| `book_levels_ask` | int | Number of price levels currently tracked on the ask side |

**Stream parameters:**

| Param | Default | Description |
|---|---|---|
| `obi_levels_list` | `[10, 100, 500]` | List of depths for which OBI is computed |
| `cum_vol_range_pct` | 1.0 | Percentage range around mid for cumulative volume fields |
| `wall_range_pct` | 2.0 | Percentage range around mid to search for wall levels |
| `publish_every_n` | 10 | Throttle: publish once every N depth events |

---

## 5. Poll-based sources

These sources fetch a value from an external REST API at a configurable
interval. They do not require a price series. `indicators: []` is mandatory.

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

### `binance_vwap_context` — VWAP price context

**Endpoint:** `https://api.binance.com/api/v3/klines` + `https://api.binance.com/api/v3/ticker/price`  
**Default interval:** 3600 s (1 h)  
**Auth required:** no

Fetches the last `vwap_period` closed candles at `timeframe` (default: 24 × 4h
candles = 4 days of data), computes VWAP using typical price
`(H + L + C) / 3 × base_volume`, then fetches the current spot price.

```json
{
  "t":         "indicators",
  "stream_id": "btc_vwap_context",
  "vwap":      67150.40,
  "price":     67420.10,
  "dip_score": -0.00402,
  "dip_zone":  "above_vwap",
  "ts":        1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `vwap` | float | VWAP of the last `vwap_period` closed candles |
| `price` | float | Current spot price |
| `dip_score` | float | `(vwap − price) / vwap`. Positive = price below VWAP (dip); negative = price above VWAP |
| `dip_zone` | string | `"below_vwap"` or `"above_vwap"` |

**Stream parameters:**

| Param | Default | Description |
|---|---|---|
| `vwap_period` | 24 | Number of closed candles for VWAP (24 × 4h = 4 days) |
| `timeframe` | `"4h"` | Kline timeframe |

---

### `binance_volume_profile` — Taker volume profile

**Endpoint:** `https://api.binance.com/api/v3/klines` + `https://api.binance.com/api/v3/ticker/price`  
**Default interval:** 3600 s (1 h)  
**Auth required:** no

Fetches the last `kline_limit` closed candles, aggregates taker buy/sell volume
into `bucket_size_usd`-wide price buckets (using candle midpoint), and
identifies the top `hvn_top_n` High-Volume Nodes (HVN) by total volume.

```json
{
  "t":               "indicators",
  "stream_id":       "btc_volume_profile",
  "price":           67420.10,
  "price_bucket":    67000.0,
  "bucket_buy_vol":  145.3,
  "bucket_sell_vol": 98.7,
  "bucket_net_vol":  46.6,
  "price_zone":      "buy_hvn",
  "zone_score":      0.32,
  "hvn_buckets":     [65000.0, 66500.0, 67000.0, 68000.0, 69500.0],
  "ts":              1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `price` | float | Current spot price |
| `price_bucket` | float | Lower bound of the bucket containing `price` |
| `bucket_buy_vol` | float | Taker buy volume accumulated in the current bucket |
| `bucket_sell_vol` | float | Taker sell volume accumulated in the current bucket |
| `bucket_net_vol` | float | `bucket_buy_vol − bucket_sell_vol` |
| `price_zone` | string | `"buy_hvn"` / `"sell_hvn"` / `"neutral"` — whether current bucket is a top HVN and its dominant side |
| `zone_score` | float | `bucket_net_vol / (bucket_buy_vol + bucket_sell_vol)` ∈ [−1, +1] |
| `hvn_buckets` | list[float] | Sorted list of the top `hvn_top_n` HVN bucket lower bounds |

**Stream parameters:**

| Param | Default | Description |
|---|---|---|
| `bucket_size_usd` | 500.0 | Width of each price bucket in USD |
| `hvn_top_n` | 5 | Number of top HVN buckets to identify |
| `kline_limit` | 288 | Number of closed candles to fetch (288 × 5m = 24 h) |
| `timeframe` | `"5m"` | Kline timeframe |

---

### `binance_macro_obi` — Macro order book imbalance from klines

**Endpoint:** `https://api.binance.com/api/v3/klines`  
**Default interval:** 60 s (1 min)  
**Auth required:** no

Fetches the last `kline_limit` closed 1m candles and computes a per-candle
taker-flow imbalance: `(taker_buy_vol / total_vol − 0.5) × 2`, range [−1, +1].
The series is EMA-smoothed with `ema_alpha` to produce `macro_obi`.

```json
{
  "t":                   "indicators",
  "stream_id":           "btc_macro_obi",
  "macro_obi":           0.18,
  "macro_obi_raw":       0.24,
  "macro_obi_direction": "bullish",
  "ts":                  1745664125000
}
```

| Field | Type | Description |
|---|---|---|
| `macro_obi` | float | EMA-smoothed taker flow imbalance ∈ [−1, +1]. +1 = pure buy pressure; −1 = pure sell pressure. |
| `macro_obi_raw` | float | Raw taker flow imbalance of the most recent closed candle |
| `macro_obi_direction` | string | `"bullish"` / `"neutral"` / `"bearish"` based on `neutral_threshold` |

**Stream parameters:**

| Param | Default | Description |
|---|---|---|
| `kline_limit` | 60 | Number of closed 1m candles to fetch |
| `ema_alpha` | 0.20 | EMA smoothing coefficient |
| `neutral_threshold` | 0.10 | `|macro_obi|` below this threshold → `"neutral"` |
| `timeframe` | `"1m"` | Kline timeframe |

---

## 6. Pre-built configuration files

### Production unified config

`tradinebotte-indicators/strategies/indicators_all.json` is the production configuration.
It runs all 14 streams in a single `indicators.py` process on ports 5559/5561
(the `tradinebotte-indicators` systemd service). Individual config files exist
for standalone testing only.

| `stream_id` | Source | Category |
|---|---|---|
| `btc_4h` | `binance_ws` | WebSocket (closed candle) |
| `btc_1d` | `binance_ws` | WebSocket (closed candle) |
| `btc_funding` | `binance_funding` | REST poll (15 min) |
| `btc_dvol` | `deribit_iv` | REST poll (5 min) |
| `fear_greed` | `fear_greed` | REST poll (1 h) |
| `btc_oi` | `binance_oi` | REST poll (5 min) |
| `btc_ls_ratio` | `binance_ls_ratio` | REST poll (5 min) |
| `btc_scalping_spot` | `binance_scalping` | WebSocket (depth20 + aggTrade, spot) |
| `btc_scalping_perp` | `binance_scalping` | WebSocket (depth20 + aggTrade, perp) |
| `btc_full_depth` | `binance_full_depth` | WebSocket (full book reconstruction) |
| `btc_vwap_context` | `binance_vwap_context` | REST poll (1 h) |
| `btc_volume_profile` | `binance_volume_profile` | REST poll (1 h) |
| `btc_macro_obi` | `binance_macro_obi` | REST poll (1 min) |

### Standalone per-stream configs (testing)

Each file declares a single stream on a dedicated port pair.

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

### Starting the unified production instance

```bash
# Production: all 14 streams (managed by tradinebotte-indicators systemd service)
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_all.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh

# Testing a single stream in isolation
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_oi_bitcoin.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh
```

---

## 7. Dynamic registration

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

For poll-based and WebSocket sources, `asset` and `timeframe` are optional:

```python
req.send_json({"cmd": "subscribe", "source": "binance_oi", "asset": "BTCUSDT"})
resp = req.recv_json()
# {"status": "ok", "stream_id": "binance_oi"}
```

**Limitation:** `source="feed"` streams cannot be registered dynamically —
declare them in the JSON config file. All other sources support dynamic
registration: `"binance_ws"`, `"binance_scalping"`, `"binance_full_depth"`,
`"binance_funding"`, `"deribit_iv"`, `"fear_greed"`, `"binance_oi"`,
`"binance_ls_ratio"`, `"binance_liquidations"`, `"binance_vwap_context"`,
`"binance_volume_profile"`, and `"binance_macro_obi"`.

---

## 8. Port configuration

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
bash tradinebotte-indicators/scripts/start_indicators.sh

# Second independent stack — all ports shifted by +1000
TRADINEBOTTE_PORT_BASE=6557 \
TRADINEBOTTE_INDICATORS_CONFIG=tradinebotte-indicators/strategies/indicators_4h_bitcoin.json \
  bash tradinebotte-indicators/scripts/start_indicators.sh
```

---

## 9. Planned indicators (TODO)

These will be added as new `type` values in `_VALID_INDICATOR_TYPES` and
implemented in `PriceSeries.compute_indicators`. They reuse existing Binance
kline data — no new REST or WebSocket source needed.

| Indicator | Keys | Notes |
|---|---|---|
| **MACD** (12/26/9) | `macd`, `macd_signal`, `macd_hist` | `macd = EMA12 − EMA26`; `signal = EMA9(macd)`; `hist = macd − signal` |
| **Stochastic RSI** | `stoch_rsi_k`, `stoch_rsi_d` | `k = (RSI − min_RSI) / (max_RSI − min_RSI)` smoothed × 3; `d = SMA3(k)` |

---

## 10. Related files

| File | Role |
|---|---|
| `tradinebotte-indicators/indicators.py` | Implementation: all sources, `PriceSeries`, config loader, ZMQ tasks |
| `tradinebotte-indicators/strategies/indicators_all.json` | Unified production config (all 14 streams) |
| `tradinebotte-indicators/strategies/indicators_*.json` | Per-stream config files for standalone testing |
| `tradinebotte-indicators/tests/test_indicators.py` | Unit and integration tests (117 tests) |
| `docs/design.md` | ZeroMQ topology, message catalog, ZeroMQ vs MQTT analysis |
