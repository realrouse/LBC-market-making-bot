# Test coverage matrix — strategy family × shared side-effect

Purpose: make test-coverage **parity across strategy families** visible and enforceable.
The 2026-06-16 silent-recording bug shipped because a non-polymarket data path
(`cex_feed_consumer_loop`) dropped a side-effect (snapshot persistence) that was only
ever tested on the polymarket path. No family is "main"; each shared side-effect must be
tested for every family that has it.

Legend: ✓ direct test · ~ partial / indirect · ✗ gap · — n/a (family lacks this side-effect)

| Family (entrypoint) | Data persist + `last_write_ts` | Trade / state ledger | PnL export (heartbeat) | Structural guard |
|---|---|---|---|---|
| Polymarket threshold (`live_bot`) | ✓ `snapshots` — TestPersistSnapshot, TestHandleBookUpdate | ✓ `trades` — test_bot | ✓ cumulative_pnl — test_bot | ✓ meta-test |
| Polymarket grid (`live_bot`) | ✓ `snapshots` (shared path) | ✓ `trades` | ✓ | ✓ meta-test |
| Polymarket multibot (`account_bot`) | ~ `snapshots` via shared `handle_book_update` (covered by live_bot tests; no account_bot-specific test) | ✓ `trades` | ✓ | ✓ meta-test |
| CEX grid Binance/MEXC (`live_bot` + cex_feed) | ✓ `snapshots` — TestCexFeedSnapshots | ✓ `grid_levels` — test_strategy_engines, test_grid_summary | ✓ total_pnl — test_regression | ✓ meta-test |
| CEX swing (`live_bot` + cex_feed) | ✓ `snapshots` — TestCexFeedSnapshots | ✓ `swing_orders` — test_strategy_engines | ~ total_pnl | ✓ meta-test |
| Accumulation mexc/std/deepdip (`accumulation_bot`) | ✓ `accum_snapshots` — TestAccumSnapshotWrite | ✗ **`accum_trades` — no test** | ✓ total_realized→pnl_total — test_regression | ✗ no structural guard (separate consumer loop) |
| Orderbook (`orderbook_bot`) | ✗ `ob_snapshots` | ✗ `ob_trades` | ~ render-only | ✗ — bot DISABLED 2026-06-14 |

## What the structural guard enforces

`test_bot.py::TestDataPathCoverage` statically inspects `live_bot.py`: every function that
drives a strategy (`on_book_update`) must also reach a snapshot-persistence call
(`_persist_snapshot` / `save_snapshot` / `save_cex_snapshot`). A new consumer loop that
forgets persistence fails at test time — the exact 2026-06-16 shape. The runtime
`⚠data` status badge (`last_write_ts` freshness) is the second line of defence.

The guard currently covers `live_bot` consumers only. `accumulation_bot` uses a different
consumer (`_zmq_loop` → `_handle_indicator` → `_record_accum_snapshot`); its persistence is
unit-tested (TestAccumSnapshotWrite) but not yet structurally guarded.

## Open gaps (next, in priority order)

1. **`accum_trades` write** — the accumulation trade ledger has no test (`accumulation_bot`
   records buys/sells to `accum_trades`; only the snapshot write is now covered).
2. **`account_bot`-specific persistence test** — currently relies on shared live_bot tests;
   a dedicated test would lock the multibot path.
3. **Structural guard for `accumulation_bot`** — extend the on-consumer-must-persist check
   to its `_handle_indicator` path (or a shared registry of data consumers).
4. **Orderbook family** — untested, but the bot is disabled; (re)add coverage only if it is
   recalibrated and re-enabled.
