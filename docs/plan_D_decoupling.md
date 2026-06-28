# Plan D — decoupling the universal entrypoint from Polymarket (continuation)

Handoff doc for the next session. Plan D is the incremental **strangler** path toward
**Plan C**: a strategy-agnostic core with Polymarket and CEX as **peer plugins**. Each step
is behavior-preserving + tested + deployed + validated — the recipe that worked for Step 1,
B, C and E.

## Goal (Plan C end-state)
A neutral **core** (bot lifecycle, data-consumer loops, snapshot/trade persistence,
heartbeat, control plane, `BotState`) that imports **no** polymarket or cex specifics.
Polymarket (ThresholdStrategy + its API/market-discovery) and CEX (grid/swing + connectors)
are symmetric plugins behind **one Strategy interface** + **one connector interface**.

## Where we are (DONE)
- **Step 1** (`72550a8`, deployed fleet): extracted `ThresholdStrategy`. The Polymarket
  threshold path is now a `Strategy` peer; `handle_book_update` dispatches unconditionally
  via `state.strategy`. ThresholdStrategy is **duck-typed** (no Protocol import) to avoid
  hardening the import coupling — Step 3 undoes that once the protocol moves to core.
- Safety nets to validate every step (built this session): `⚠data` freshness monitor,
  `feed_watchdog` (C), the structural meta-test `TestDataPathCoverage`, and the shared
  `_persist_snapshot` step.

## Remaining steps

### Step 2 — `api_polymarket` behind the connector interface (invert the hard dep)
- Today `live_bot.py` does `import api_polymarket as api` and calls ~12 `api.*` functions:
  `get_markets`, `parse_book_update`, `post_order`, `make_subscribe_msg`, `compute_fee`,
  and Polymarket-specific market/token metadata (`get_market_id`, `get_up_token_id`,
  `get_down_token_id`, `get_market_{question,start_ts_ms,end_ts_ms}`).
- CEX connectors already load via `connectors.load(name)` (interface: `parse_book_update`,
  `get_markets`, `post_order`). The Polymarket path is the odd one out (hard import).
- **Task:** make `api_polymarket` a loadable connector (`load("polymarket")`); have
  `live_bot` obtain its connector via the config's `connector` name instead of the hard
  import. The Polymarket-specific metadata helpers either extend the connector protocol or
  live on the ThresholdStrategy/connector as extras only the threshold path uses.
- **Risk:** broad but mechanical (`api.` is used throughout). Behavior-preserving.
- **Validate:** full suite + canary polymarket account (trades/PnL/snapshots identical) +
  account_bot (2nd `handle_book_update` caller).

### Step 3 — neutral core package + move the `Strategy` protocol there
- Today the `Strategy` protocol is in `tradinebotte-cex/strategy_engines/base.py`, and
  `live_bot` (polymarket pkg) imports `strategy_engines` (cex pkg). **VERIFY the real import
  graph first** — a grep found only *docstring* references to live_bot in
  `strategy_engines/__init__.py`, so the "cycle" may be one-directional; confirm before
  designing.
- **Task:** create a neutral package (`tradinebotte-core/` or `tradinebotte-engine/`) and
  move into it the strategy-agnostic machinery: the `Strategy` protocol, `BotState`,
  `handle_book_update`, `_persist_snapshot`, the consumer loops, heartbeat/control wiring,
  and the connector-load interface. Core imports neither polymarket nor cex.
- Once the protocol is neutral, ThresholdStrategy + CEX engines **explicitly** conform
  (import it) — removing the Step-1 duck-typing workaround.
- **Risk: HIGH** — moves the core of the 2040-line `live_bot.py` out of the polymarket
  package; touches every deploy script, systemd `ExecStart`, and test sys.path. Sub-step it;
  keep ExecStart paths working at each commit.
- **Validate:** import graph clean (no core→plugin import), full suite, canary.

### Step 4 — move Polymarket into a plugin package (reach Plan C)
- Move ThresholdStrategy + the Polymarket connector + Polymarket market discovery
  (`register_market`, market-refresh, token bookkeeping) into `tradinebotte-polymarket/` as a
  **plugin** — peer of `tradinebotte-cex/`. Core then has nothing Polymarket-specific.
- End-state = Plan C: core neutral; polymarket and cex symmetric behind the interfaces.

### Step 5 — generalize the `snapshots` schema (optional / lowest priority)
- Today `snapshots` is Polymarket-shaped (`market_id/token_id/direction/secs_remaining`);
  CEX writes placeholders via `save_cex_snapshot` (`direction='UP'`, `secs_remaining=9999`).
- **Decide:** (a) keep the shared table with the documented placeholder convention (cheapest;
  `btc_variation.py` + backtests already read it) **or** (b) per-family tables (`ob_snapshots`,
  `accum_snapshots` already exist) with backtests reading per family. Lean (a) unless the
  placeholders cause real friction. Deferrable.

## Principles (every step)
- **Behavior-preserving**: the Polymarket trading logic must behave byte-identically.
  Bar = all poly tests pass UNCHANGED + a canary trades identically.
- **One step = one commit = one deploy = one validation.** Canary a Polymarket account for
  poly-touching steps; always re-check `account_bot` (the 2nd `handle_book_update` caller).
- Lean on the safety nets: `⚠data` monitor, `feed_watchdog`, `TestDataPathCoverage`.
- Mind the deploy gotchas (see memories): feed-restart ordering (E), reconnect lag (~1
  market window), no parallel SSH, `pgrep` self-match (`/proc/$PID/exe`), `sg claudes` for the
  shared DB, deploy sequentially.

## Verify at session start (don't assume — code may have moved)
- The real import graph live_bot ↔ strategy_engines ↔ connectors (the "cycle").
- Whether `connectors.load()` can carry the Polymarket metadata surface or needs extension.
- Deployed commit + that `dev` is still the integration branch (Plan D so far is unmerged on
  `dev`: Step 1 `72550a8`; robustness work C/B1/B2/E `149c132`/`c5cafec`/`24a304f`/`e2e4dae`).
- Re-read `[[feedback_not_polymarket_first]]` — Plan D is the structural half of that directive.
