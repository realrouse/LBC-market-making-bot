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

### Step 2 — `api_polymarket` behind the connector interface (invert the hard dep) — **DONE (not yet deployed)**
- **What was done:** `live_bot.py` no longer does `import api_polymarket as api`. The
  module-level `api` is now bound via `connectors.load(CONNECTOR)` (default `"polymarket"`),
  and `_load_connector(name)` loads *every* connector through the registry — the polymarket
  no-op special-case is gone. All ~19 `api.*` calls already went through the rebindable `api`
  global (most of the mechanical work landed with the CEX session), so this step was the
  surgical removal of the one privileged hard import + de-special-casing the loader.
- **Validated:** poly suite 439 (was 438 + 1 new guard) / cex 126 / top-level `tests/` 409 —
  all green, behavior-preserving. New guard `TestConnectorFactory
  .test_live_bot_loads_default_via_registry_not_hard_import` fails if a privileged
  `import api_polymarket` reappears. Standalone import confirmed `live_bot.api == api_polymarket`.
  `connectors/__init__.py` ships to polymarket accounts (install.sh L119-120,
  update_standalone.sh L143-148), so importing the registry at module top is safe in every
  deploy mode.
- **NOT done (deferred, by design):** the Polymarket market-discovery path — `register_market`
  (calls `get_market_id` / `get_up_token_id` / `get_down_token_id` /
  `get_market_{question,start_ts_ms,end_ts_ms}`), `_market_refresh_loop`, `_run_ws`
  (`WS_URL` / `WS_BATCH_SIZE` / `make_subscribe_msg`) — still assumes a Polymarket-shaped
  connector. It is the `else` fall-through of the data-source dispatch (`ws_loop`, live_bot.py
  ~L2052), behavior-preserving today only because polymarket is the default and CEX always
  uses `cex_feed_consumer_loop`. **This is Step 4's job** (Step 4 literally lists
  "register_market, market-refresh, token bookkeeping"). The only poly-metadata use on a
  *shared* path is the `GAMMA_TAG_15M` log line in `main()`, and it is correctly gated by
  `if config.connector == "polymarket"`.
- **⚠ Still to deploy:** canary a Polymarket account (trades/PnL/snapshots identical) +
  re-check `account_bot` (2nd `handle_book_update` caller) before calling this fully landed.
- **Step-3 hook:** the import-time `api = connectors.load(CONNECTOR)` binding stays at module
  scope *on purpose* — tests patch `live_bot.api.*`, so `live_bot.api` must exist at import.
  When live_bot moves into the neutral core (Step 3), that import-time binding is the last
  core→plugin coupling: it will need the test-patch pattern restructured (e.g. strategy/
  connector injected into `BotState`, tests patching that) so core imports no plugin.

### Step 3 — neutral core package + move the `Strategy` protocol there — **IN PROGRESS**
- **Import-graph VERIFIED (the "cycle" is a myth):** `strategy_engines` does NOT import
  `live_bot` — only docstrings mention it. Dependency is one-directional
  `live_bot → {connectors, strategy_engines.base, botcore}`. Also: NOTHING imported,
  subclassed, or isinstance-checked the `Strategy` protocol before step 3 — all five
  strategies were duck-typed, the protocol was pure documentation.
- **Step 3a DONE (commits `e98bac4` + the 3a-2 commit), NOT yet deployed:**
  - **3a-1** (`e98bac4`): created `tradinebotte-core/botcore/` (importable as `botcore`)
    holding the `Strategy` protocol; `strategy_engines/base.py` re-exports it (self-bootstraps
    the sibling core dir onto sys.path → zero test-path churn). Wired `botcore/` into all 7
    bot-shipping deploy scripts (install, update_standalone, setup_data_plane, deploy_grid_×2,
    update_swing, test_multibot) + syntax-check list. Guard `TestCoreShipsWithTheBot`. Also
    fixed a step-2 regression: `cleanup_server.sh` deleted `connectors/` which live_bot now
    needs at import. Pure scaffolding, no live_bot edit.
  - **3a-2**: `live_bot` imports `from botcore import Strategy` at module top (added a
    `_core_dir` sys.path insert mirroring `_cex_dir`); `ThresholdStrategy(Strategy)` subclasses
    it — first real conformer, step-1 duck-typing removed. Guard
    `test_threshold_explicitly_conforms_to_core_strategy_protocol`. This is the dependency
    inversion the advisor flagged: importing live_bot now imports botcore unconditionally
    (botcore must ship everywhere — done in 3a-1).
  - Gate green each commit: poly 441 / cex 126 / tests 409 / indicators 139 / status 75 /
    tradinetools 168.
- **⚠ Validate before 3b:** a CLEAN INSTALL on the ephemeral fresh-install test account
  (`TEST_STANDALONE_USER_IDX`, via `test_standalone_deploy.sh` / `test_multibot_deploy.sh`) —
  grepping scripts proves intent, a fresh install proves botcore lands on a flat-deploy account
  where live_bot now imports it at startup. All sim now → cheap.
- **Step 3b-1 DONE (not yet committed at time of writing → see git log), connector-load
  interface → core:** moved the connector registry from the CEX package into `botcore.connectors`;
  `connectors/__init__.py` is now a self-bootstrapping shim re-exporting it (cex_feed, the
  strategy engines, and tests keep `from connectors import load`); live_bot imports
  `from botcore.connectors import load, validate`. Robustness: `install.sh` now copies `botcore/`
  as a whole dir + globs the syntax check (no more hand-listed file set — the flat-sim test had
  its own hardcoded list that could drift to a false green). Invariant test: `import botcore`
  loads no `api_*`. Blast radius verified: every `connectors` importer (live_bot, cex_feed,
  strategy_engines/*) already ships botcore from 3a-1; accumulation/orderbook/scalping don't
  import it. NB this is a peripheral win — live_bot does NOT shrink, and its lazy
  `from strategy_engines import load` (~L1980) is still a real core→cex plugin dep for later.
- **Step 3b-2a DONE (committed → see git log): strategy injection.** `BotState.__init__` no
  longer instantiates `ThresholdStrategy()`; it takes a `strategy=None` param and stores it, so
  BotState names no concrete strategy. The 2 polymarket entrypoints (live_bot main, account_bot)
  inject `ThresholdStrategy()`; `make_state` (both test files) mirror it; main() still overrides
  for grid/swing (guarded by `strategy_type != "threshold"`, verified). **Landmine resolved:**
  `None`-means-threshold is NOT a live convention — handle_book_update (L1017) requires a real
  instance; the L3354 test's `strategy=None` is only a `cumulative_pnl` getattr-fallthrough
  shortcut (a real ThresholdStrategy yields the identical result). Churn was tiny (advisor was
  right): of 22 direct `BotState(conn)` test sites only 2 touched `.strategy`; the rest pass
  untouched with `strategy=None`. Used **explicit constructor injection**, NOT a ClassVar/global
  factory (that would be a service-locator with an import-order landmine when BotState moves to
  core). Gate: tests 411 / poly 441 / cex 126 / indicators 139 / status 75 / tools 168. Clean-install
  NOT load-bearing here (no file/install.sh/import-shape change) → not run.
- **DESIGN PASS (2026-06-28) — corrects this plan's core/plugin partition. READ THIS.**
  An earlier steer (and the doc's own Step-3 list) treated `BotState`, `handle_book_update`,
  the consumer loops and a "connector-injection (3b-2b)" step as the neutral core to extract.
  **That is wrong** — verified by the `api.*` usage map + `handle_book_update` caller analysis:
  - **`handle_book_update` is polymarket, not core** — it is token-keyed
    (`state.tokens.get(parsed["token_id"])`), called only by polymarket paths (account_bot,
    `feed_consumer_loop`, `_run_ws`); CEX **bypasses** it (`cex_feed_consumer_loop`).
  - **The consumer loops are per-plugin**, not one shared loop: `feed_consumer_loop` = polymarket,
    `cex_feed_consumer_loop` = CEX.
  - **The module global `api` is exclusively polymarket-bound** — all 21 `api.*` sites are in
    `enter_live_trade` / `register_market` / `_run_ws` / `_market_refresh_loop` / `compute_stake`
    / `main`(GAMMA, gated by `connector=="polymarket"`). No core-bound function uses `api`.
  - ⇒ **"3b-2b connector injection" is NOT a core prerequisite** — `api` already belongs to the
    polymarket plugin by usage; it travels to the plugin in Step 4. The tests patching
    `live_bot.api.*` are a Step-4 mechanical concern (follow `api` to its new home), not a blocker.
  - **Headline:** `live_bot.py` is *mostly the polymarket plugin with a thin neutral core embedded*,
    not a neutral core with polymarket bolted on. What is genuinely neutral is thin and mostly
    already done (Strategy protocol 3a, connector registry 3b-1).

- **BotState surface partition (the discriminating artifact):**
  - *Neutral fields:* `conn`, `config`, `capital`, `total_trades/wins/losses/total_pnl`,
    `daily_pnl/_daily_pnl_day/weekly_pnl/_weekly_pnl_week`, `last_snapshot_commit_ts/last_book_ts/
    last_write_ts`, `api_fail_streak/api_cooldown_until`, `strategy` (injected 3b-2a), `session`.
  - *Polymarket fields:* `tokens` (→`TokenState`), `market_tokens`, `open_trades`,
    `traded_direction`, `signalled`, `rejection_stats`.
  - *Neutral state-taking fns (touch only neutral fields):* `_persist_snapshot` (config.enable_snapshots,
    last_write_ts, last_snapshot_commit_ts, conn — verified), `equity`/`cumulative_pnl`,
    `read/write_capital_base` (conn-only), the heartbeat payload.
  - *Polymarket state-taking fns:* `handle_book_update`, `check_signal`, `check_resolution`,
    `enter_live_trade`, `register_market`/`_run_ws`/`_market_refresh_loop`, `compute_stake`.

- **⏸ STRATEGIC FORK — the user's call (do NOT pick unilaterally):** further extraction is blocked
  on BotState's fate. The neutral state-taking fns all take `BotState`, which is polymarket-coupled.
  Two paths:
  - **(A) Split BotState** into a neutral base (neutral fields + the neutral fns) in `botcore` +
    a `PolymarketState(base)` extension (polymarket fields/fns) in the plugin. Classic clean split;
    enables symmetric plugins; larger refactor (every `state.<polymarket field>` access must be on
    the subclass; tests touch BotState heavily).
  - **(B) Thin core, pivot to Step 4.** Accept `live_bot.py` is mostly the polymarket plugin. Move
    only the genuinely-neutral *leaf* helpers (`_persist_snapshot`, capital-base helpers, heartbeat
    wiring) to `botcore` opportunistically, then jump to Step 4: package the polymarket bulk into
    `tradinebotte-polymarket/` as a plugin peer of `tradinebotte-cex/`, leaving `botcore` as the
    deliberately-thin neutral core. Less BotState surgery; reaches the Plan C shape faster.
  - Recommendation to discuss: **(B)** — the partition shows the neutral core is inherently thin, so
    forcing a full neutral BotState (A) is high-effort for a base that little neutral code consumes.
  - **DECISION (2026-06-28): user chose (B) — thin core, pivot to Step 4.**

- **PATH B EXECUTION PLAN.** Phase 1 = extract the neutral leaf helpers to `botcore` (each a
  separate commit + full gate; all behavior-preserving). Re-export from `live_bot` so existing
  `bot.<fn>` callers (live_bot, account_bot, tests) keep working with **zero churn** — the same
  shim pattern used for the Strategy protocol and the connector registry. `botcore` stays pure
  (these helpers take `conn` or a duck-typed `state`/`strategy`; no polymarket type imported — the
  `import botcore` loads-no-`api_*` invariant still holds). Caller/feasibility verified 2026-06-28:
  - **3b-3a DONE (committed → see git log):** `botcore/persistence.py` ← `read_capital_base` /
    `write_capital_base` (`conn`-only); live_bot re-exports them (`from botcore.persistence import
    …`) so `bot.<fn>` callers are unchanged. Verified re-export identity + core-purity invariant
    (now imports `botcore.persistence`) + full gate green. No install.sh edit needed (whole-dir
    botcore copy from 3b-1 ships it; flat test globs it).
  - **3b-3b DONE (committed → see git log):** `_persist_snapshot` (+ `SNAPSHOT_COMMIT_SECS`) moved
    into `botcore/persistence.py`, param duck-typed (`Any`) not `BotState`; live_bot re-exports both.
    `TestDataPathCoverage` needed NO change — its AST scan keys off the call sites, which stay in
    live_bot; the count guard sees the re-export + calls. Dropped the now-unused `Callable` import.
  - **3b-3c DONE (committed → see git log):** `cumulative_pnl` / `equity` moved (duck-typed on
    `state`/`strategy`); live_bot re-exports both; account_bot's `bot.cumulative_pnl` unchanged.
  - **PHASE 1 COMPLETE.** `botcore` = strategy protocol + connector registry + persistence helpers
    (capital-base, `_persist_snapshot`, `cumulative_pnl`/`equity`) = the deliberately-thin neutral
    core. Heartbeat/control wiring is ALREADY neutral (in `tradinetools`); `_hb_payload` is a
    `main()` closure (entrypoint glue) — left in place. Core-purity invariant (imports
    `botcore.persistence`, loads no `api_*`) + full gate green throughout.
- **Phase 2 = Step 4 (its OWN design pass + user go — do not fold into Phase 1).** Physically split
  `live_bot.py`'s polymarket bulk (BotState, handle_book_update, ThresholdStrategy, enter_live_trade,
  register_market/_run_ws/_market_refresh_loop, the `api` global, TokenState, check_signal/resolution,
  GAMMA) into the `tradinebotte-polymarket/` plugin, leaving `live_bot.py` a thin entrypoint wiring
  core + plugin. This is where the `api` global + the `live_bot.api.*` test patches relocate. HIGH
  risk — touches ExecStart paths, every deploy script, test sys.path; the clean-install becomes
  load-bearing again.
  - **Coverage gap to close later (low risk):** the standalone clean-install only runs live_bot,
    which imports `botcore.connectors` directly — it does NOT exercise the `connectors/` shim's
    flat self-bootstrap (only cex_feed + the strategy engines hit that, on grid/swing accounts).
    A `test_multibot_deploy` run closes it.
- **Validate:** import graph clean (no core→plugin import), full suite, clean-install.

### Step 4 — DESIGN PASS (2026-06-28): a value/risk decision, mostly declinable
**Headline: interface symmetry is ALREADY done.** Polymarket and CEX go through one Strategy
protocol, one connector registry, one strategy-agnostic dispatch, and the shared neutral core
(Phase 1). What Step 4 buys is only **layout/packaging** symmetry (moving polymarket code out of
the entrypoint *file*) — cosmetic relative to [[feedback_not_polymarket_first]], whose archi/test/
observability/diagnostics bias is largely already addressed. Layout purity for its own sake is the
lowest-value, highest-cost rung.

**Structural map — `live_bot.py` (2025 lines) is a 3-way tangle:**
- *Neutral entrypoint/orchestration:* logging, `BotConfig`/`make_config`, `init_db`/`SCHEMA`,
  `main()` lifecycle (heartbeat/control/health via tradinetools), `_load_connector`, the dispatch
  seam (`main` L1998-2006: `feed_consumer_loop` / `cex_feed_consumer_loop` / `ws_loop`).
- *Polymarket plugin (bulk):* `TokenState`, `ThresholdStrategy`, `check_signal`/`enter_live_trade`/
  `check_resolution`/`close_trade`/`compute_stake`, `register_market`/`purge_expired_markets`/
  `_register_market_from_feed`, `ws_loop`/`_market_refresh_loop`/`_run_ws`, `feed_consumer_loop`,
  `handle_book_update`, `save_snapshot`, the `api` global, GAMMA, trading-hours filter, `RejectionStats`.
- *CEX glue in the polymarket file:* `cex_feed_consumer_loop`, `save_cex_snapshot`.

**Why a FULL split (4b) is expensive/risky out of proportion to the value:**
1. **ExecStart pins `live_bot.py` as the runnable entrypoint** — it can't become a thin shell without
   touching every deploy script + systemd unit + account_bot's import (the deploy surface that has
   bitten this project: feed-restart, pgrep, shipping gaps).
2. **The gate is weakest exactly where 4b is riskiest.** Trading-behavior coverage lives in the unit
   tests that `patch("live_bot.api.*")` (~10 sites) — and 4b *rewrites those very patch points* as
   `api` relocates. Moving the trading code AND rewriting its safety net in one step = "behavior-
   preserving" genuinely hard; the clean-install only proves "starts + WS connects", not "still
   trades correctly". 4b would need a behavior-level integration check that doesn't exist yet.

**THREE OPTIONS (the user's call — recommendation: (i) or (ii)):**
- **(i) Declare Plan D structurally DONE.** Interfaces + core + dispatch are symmetric; the remainder
  is layout. Stop here. (Step 5 snapshots-schema remains optional/separate.)
- **(ii) CEX-glue tidy (4a) then stop.** Extract `cex_feed_consumer_loop` + `save_cex_snapshot` out
  of the polymarket file into the CEX package — the most glaring layout wart (CEX data-path code
  living in the polymarket entrypoint). Self-contained: touches NO `api` global; deps are
  `botcore._persist_snapshot` + the strategy + `save_cex_snapshot`. Gotcha: lazy-import it inside the
  `_use_cexfeed` dispatch branch (mirroring the lazy `strategy_engines` import) so polymarket-only
  accounts need not ship it. Honest: removes a wart, NOT the asymmetry — live_bot.py stays a
  polymarket-resident universal entrypoint.
- **(iii) Full split (4b).** Neutral entrypoint + polymarket plugin package. Large, deploy-wide, risky
  per above. **Justified only by a concrete roadmap driver** — a 3rd strategy family, or hard package
  boundaries for an open-source release. If no concrete driver, decline it.

- **Coverage gap (close if any Step-4 work happens):** the standalone clean-install only runs live_bot
  (imports `botcore.connectors` directly) — it does NOT exercise the `connectors/` shim's flat
  self-bootstrap (cex_feed + strategy engines, on grid/swing accounts). A `test_multibot_deploy` run
  closes it; (ii) would touch cex_feed so re-validate there.

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
