# Snapshots — Technical Reference

The `snapshots` table is a time-series log of every active token's order-book
state, written at a fixed interval while the bot is running. It is the primary
data source for backtesting and strategy analysis.

---

## Table Schema

```sql
CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms          INTEGER,   -- Unix timestamp in milliseconds (wall clock)
    market_id      TEXT,      -- Polymarket market CLOB ID
    token_id       TEXT,      -- Outcome token ID (YES or NO side)
    direction      TEXT,      -- "UP" or "DOWN" (derived from market title)
    secs_remaining REAL,      -- Seconds until market closes at snapshot time
    best_bid       REAL,      -- Highest bid price on the order book (0–1 scale)
    best_ask       REAL,      -- Lowest ask price on the order book (0–1 scale)
    spread         REAL,      -- best_ask − best_bid (≥ 0)
    ask_vol        REAL,      -- Total volume offered at the best ask level
    obi            REAL,      -- Order Book Imbalance (see below, range −1 to +1)
    has_open_trade INTEGER DEFAULT 0  -- 1 if the bot had an open trade on this
                                      -- market at snapshot time, 0 otherwise
);
```

### Indexes

```sql
CREATE INDEX IF NOT EXISTS idx_trades_market ON trades(market_id);
CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved);
```

No index is created on `snapshots` by default. For large analytical queries,
add one on `(ts_ms)` or `(market_id, ts_ms)` manually:

```sql
CREATE INDEX idx_snap_ts ON snapshots(ts_ms);
```

---

## Column Definitions

| Column | Type | Source | Notes |
|---|---|---|---|
| `id` | INTEGER | SQLite autoincrement | Monotonically increasing; gaps possible after rotations |
| `ts_ms` | INTEGER | `int(time.time() * 1000)` at write time | Wall-clock time, UTC, millisecond precision |
| `market_id` | TEXT | Gamma API → `TokenState.market_id` | Stable for the lifetime of the market |
| `token_id` | TEXT | Gamma API → `TokenState.token_id` | Identifies the YES/NO outcome token |
| `direction` | TEXT | Derived from market title | `"UP"` or `"DOWN"` |
| `secs_remaining` | REAL | Computed from market `end_date_ts` | Decreases to 0; negative after close |
| `best_bid` | REAL | WebSocket order book | The entry signal fires when `best_bid >= 0.95` |
| `best_ask` | REAL | WebSocket order book | Guard: if `best_ask >= 1.0` the market is resolved |
| `spread` | REAL | `max(0, best_ask − best_bid)` | High spread → thin book, slippage risk |
| `ask_vol` | REAL | Top-of-book ask quantity | Used in OBI computation (see below) |
| `obi` | REAL | Computed from order book | Order Book Imbalance; see formula below |
| `has_open_trade` | INTEGER | `1 if market_id in state.open_trades` | Flags rows where a trade was live |

**What is NOT stored:**

- `bid_vol` — the numerator component of OBI is not persisted directly; derive
  it from OBI and `ask_vol` if needed (see formula below).
- Individual order book levels beyond the top of book.
- Raw WebSocket message payloads.
- Bot configuration at snapshot time (capital, thresholds, etc.).

---

## OBI Formula

OBI (Order Book Imbalance) is computed in `api_polymarket.py:parse_book_message()`:

```python
bv = sum(float(e["size"]) for e in bids)  # total bid-side volume
av = sum(float(e["size"]) for e in asks)  # total ask-side volume
tv = bv + av
obi = (bv - av) / tv if tv > 0 else 0.0
```

Range: **−1.0** (all volume is on the ask side, bearish pressure) to **+1.0**
(all volume is on the bid side, bullish pressure). A value near 0 indicates a
balanced book.

**Recovering `bid_vol` from stored columns** (approximate, top-of-book only):

```
bid_vol = ask_vol * (1 + obi) / (1 - obi)   [when obi ≠ 1]
```

This reconstruction is only approximate because `bid_vol` spans all bid levels
while `ask_vol` stored here is the top-of-book ask quantity only.

---

## Write Timing

A snapshot is written once per active token per `SNAPSHOT_INTERVAL` seconds.

The check runs inside `handle_book_update()` after every WebSocket message:

```python
now = time.time()
if now - ts.last_snapshot_ts >= state.config.snapshot_interval:
    ts.bid_history.append(ts.best_bid)
    ts.obi_history.append(ts.obi)
    if state.config.enable_snapshots:
        save_snapshot(state, ts)
    ts.last_snapshot_ts = now
```

Key points:
- The interval is **event-driven**, not clock-driven. A snapshot fires on the
  first WebSocket update that arrives at least `snapshot_interval` seconds after
  the previous snapshot for that token.
- If the WebSocket is quiet for a period, snapshots pause accordingly.
- Each `save_snapshot()` call issues one `INSERT` and one `COMMIT`.

### Configuring the Interval

| Method | How |
|---|---|
| Default (1 s) | Compile-time constant `SNAPSHOT_INTERVAL = 1` in `live_bot.py` |
| `config.json` | `"snapshot_interval": N` under `[hour_filter]` section |
| CLI flag | `--snapshot-interval N` (overrides all other settings) |
| Disable entirely | `--no-snapshots` flag |

The data-collection account uses `--snapshot-interval 1` (already the default).
To run with 5 s for lighter I/O, pass `--snapshot-interval 5`.

---

## Storage Estimates

At any given moment, 2–4 active markets are typically tracked (Bitcoin UP/DOWN
pairs within the ±6 min end-date window).

| Interval | Rows/min | Rows/day | Rows/week | Approx. DB size/week |
|---|---|---|---|---|
| 1 s | ~120 | ~172 800 | ~1 210 000 | ~200 MB |
| 5 s | ~24 | ~34 560 | ~242 000 | ~40 MB |

These are estimates assuming 2 active tokens and continuous WebSocket
connectivity. Quiet periods (few markets) produce fewer rows.

SQLite in WAL mode handles this volume without issue. The weekly
`collect_db.sh --rotate` workflow archives the DB before it grows unbounded.

---

## Data Isolation

Each bot instance writes to its own database:

| Account | Database path |
|---|---|
| Live bot | `~/tradinebotte/live.db` |
| Data-collection bot | `~/tradinebotte-collector/live.db` |

The collection account runs `--simulate --snapshot-interval 1` (no real orders,
maximum snapshot density).

---

## How the Backtest Uses Snapshots

The backtest engine replays the `snapshots` table to simulate the strategy:

| Column | Backtest use |
|---|---|
| `ts_ms` | Chronological ordering; compute `secs_remaining` drift |
| `best_bid` | Entry signal: `best_bid >= SIGNAL_THRESHOLD (0.95)` |
| `best_ask` | Resolved-market guard: `best_ask >= 1.0` → skip |
| `secs_remaining` | Entry gate: must be `>= MIN_SECS_REMAINING (45 s)` |
| `obi` | Filter: positive OBI confirms directional momentum |
| `ask_vol` | Liquidity check: thin book → skip |
| `has_open_trade` | Prevents re-entry while a trade is already open |

---

## The 5 s Blind Spot (Historical Context)

Prior to commit `7a8b351`, `SNAPSHOT_INTERVAL` defaulted to **5 seconds**.

A 5 s gap meant the backtest could not see `best_bid` dips below `0.01` that
lasted less than 5 s. Those dips represent markets resolving as LOSS. Because
the live WebSocket fires on every order-book change, the live bot caught these
events and closed trades as LOSS. The backtest did not — it only saw the next
snapshot, often after the price had recovered.

Effect: the backtest over-counted wins, producing an apparent win-rate higher
than what the live bot achieved. Over a 3-month session this accumulated to
approximately **50 extra LOSS events** invisible to the backtest.

Fix: `SNAPSHOT_INTERVAL = 1` (default since that commit). At 1 s, short-lived
price dips are captured, and backtest results align closely with live performance.

---

## Useful SQL Queries

### Row count and date range

```sql
SELECT count(*) as rows,
       datetime(min(ts_ms)/1000, 'unixepoch') as first,
       datetime(max(ts_ms)/1000, 'unixepoch') as last
FROM snapshots;
```

### Average best_bid per direction per day

```sql
SELECT date(ts_ms/1000, 'unixepoch') as day,
       direction,
       round(avg(best_bid), 4) as avg_bid,
       count(*) as rows
FROM snapshots
GROUP BY day, direction
ORDER BY day DESC;
```

### All snapshots for an open trade

```sql
SELECT datetime(s.ts_ms/1000, 'unixepoch') as ts,
       s.best_bid, s.best_ask, s.obi, s.secs_remaining
FROM snapshots s
JOIN trades t ON s.market_id = t.market_id
WHERE t.id = 42          -- replace with trade id
  AND s.ts_ms BETWEEN t.entry_ts_ms AND coalesce(t.resolution_ts_ms, t.entry_ts_ms + 3600000)
ORDER BY s.ts_ms;
```

### Proportion of time with an open trade

```sql
SELECT round(100.0 * sum(has_open_trade) / count(*), 2) as pct_in_trade
FROM snapshots;
```

### Bid distribution (histogram buckets)

```sql
SELECT round(best_bid, 1) as bucket, count(*) as n
FROM snapshots
GROUP BY bucket
ORDER BY bucket DESC;
```

---

## Related Files

| File | Purpose |
|---|---|
| `tradinebotte-polymarket/live_bot.py` | `save_snapshot()`, `SNAPSHOT_INTERVAL`, `--snapshot-interval` flag |
| `bot/api_polymarket.py` | `parse_book_message()` — OBI + volume computation |
| `scripts/collect_db.sh` | Download / rotate the remote `live.db` |
| `scripts/start_collector.sh` | Deploy the data-collection bot |
| `scripts/schedule_collect.sh` | Install weekly cron to rotate + download |
| `data/` | Local archive of downloaded `live_YYYY_WNN.db` files |
