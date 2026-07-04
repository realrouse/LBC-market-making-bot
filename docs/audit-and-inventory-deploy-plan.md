# Audit + Inventory-Driven Deploy Plan

> Working document — draft to iterate on. Two parts: (1) the codebase audit, (2) the plan
> to replace the per-account deploy scripts with a dynamic, inventory-driven deploy system.

---

## Part 0 — About the `.venv` "polluting scans"

`.venv/` is a **local virtualenv living inside the repo working tree** (`tradinebotte/.venv/`,
543 MB, ~4760 third-party `.py` files: numpy, matplotlib, setuptools…). It is **gitignored**
(0 tracked files), so it is *not* in git and *not* a deploy concern.

The "pollution" is purely a **scan-hygiene** issue: plain filesystem tools (`find .`, `grep -r`,
`wc -l`) recurse into `.venv/` unless told not to, so they count thousands of vendored files.
That is why an early count read "13079 test funcs" / "213972 LOC" — most of it was numpy's and
matplotlib's own test suites, not this project.

- **Wrong:** `find . -name '*.py' | xargs wc -l` → traverses `.venv`.
- **Right:** `git ls-files '*.py'` (respects `.gitignore`, so `.venv` is skipped), or add
  `-not -path '*/.venv/*' -not -path '*/__pycache__/*'` to `find`.

No action needed on `.venv` itself; just exclude it (and `__pycache__`) from any repo-wide
metric. It does not affect the code, tests, or deploy.

**Resolution (done):** the venv is correctly placed (standardized `.venv`, gitignored,
rsync-excluded) and already skipped by all real tooling — pytest ignores dot-dirs, CI runs
`unittest discover -s <dir>` (scoped) and `pylint $(git ls-files '*.py')` (gitignore-aware).
Added `pyproject.toml` (tool-config only: `pytest`/`ruff`/`coverage`) making the non-source
exclusion explicit and centralized — verified `pytest` from root collects **0** `.venv`
items and all 6 CI test dirs still pass. Convention: repo scans use `git ls-files` / `rg`,
not raw `find`/`grep -r`.

**Adjacent finding (not fixed):** `.github/workflows/mypy.yml` type-checks
`bot/live_bot.py` and `bot/api_polymarket.py`, but `bot/` has **0 tracked files**
(`live_bot.py`/`api_polymarket.py` moved to `tradinebotte-polymarket/`). The mypy CI job
checks nothing meaningful — repoint it at the moved files (may surface real type errors) or
retire it. Same class of stale-config wart as the deploy findings.

---

## Part 1 — Audit findings

Confidence is labelled **[verified]** (diff/grep concluded) or **[heuristic]** (signal only).

### Dead code — minimal
- **[verified]** No dead modules (dotted-path imports confirmed used), no orphaned `.sh`
  scripts, no dead module-level functions in `analysis/`.
- **[heuristic]** `setup_bot_logger` (`tradinebotte-polymarket/bot_utils.py`) *appears unused*
  (only its definition is referenced). Confirm it is not an intended public API before removing.
- **Scope note:** the sweep covered **module-level functions** across the bot/tool/analysis
  modules. **Class methods were not swept** and dynamic dispatch is not detected statically.

### Duplication — the real theme
- **[CORRECTED] `api_*` "copy-paste" was OVER-claimed.** An earlier `awk` diff extracted empty
  strings and reported `compute_fee`/`parse_book_update`/`make_subscribe_msg` as identical
  mexc-vs-binance. A structural (AST) comparison across all 4 CEX adapters shows they are
  **4-distinct** — legitimate per-exchange polymorphism (different fee schedules / WS
  formats), NOT duplication. Only trivial one-liners overlapped.
- **[verified — DONE] Polymarket concept leaked into CEX → removed.** The prediction-market
  accessors (`get_up_token_id`, `get_down_token_id`, `get_market_id`, `get_market_question`,
  `get_market_{start,end}_ts_ms`) were dead stubs in all 4 CEX adapters — called only by
  `pm_data.py`/`feed.py`, both of which use `api_polymarket`, and required by no CEX strategy
  (`connectors.validate`). Removed from the CEX adapters (kept on `api_polymarket`); tests +
  the connector docstring updated; a regression test now asserts CEX adapters never re-grow
  them. Directly serves the *"no longer polymarket-first"* directive.
- **[verified — dismissed] `_hb_payload` is NOT duplication.** `tradinetools.build_heartbeat`
  already centralises the envelope (ts/account/bot_name/version/status/mode); each bot's
  `_hb_payload` is the intended `get_extra` callback returning only that bot's metrics.
- **[heuristic] `analysis/`** re-defines `sma` (4×) and `win_rate` (4×) across backtests
  (bodies not diffed). One-off scripts, so tolerable; an `analysis/common.py` would help.

### Scripts & deploy — flagship finding (detailed in Part 2)
- **[verified]** ~8 near-identical **thin wrapper** scripts (`deploy_accumulation_claude2/3/4.sh`,
  `deploy_grid_claude3.sh`, `deploy_scalping_claude4.sh`, `update_claude2/3/4/5.sh`) differ only
  in `(TEST_USERS index, strategy JSON, data-source env)`. They just `exec` a shared generic
  deployer with env presets.
- **[verified]** The account→strategy mapping is **triplicated**: filenames, wrapper contents,
  and the hardcoded `run_step` list in `deploy_all.sh` — while `inventory.toml` is the declared
  single source of truth (and already carries a `deploy_script` field per bot).
- **[verified — low severity]** The pre-commit hook (`.git-hooks/pre-commit`, active via
  `core.hooksPath`) scans **staged content**, not **filenames** → account-named files
  (`deploy_grid_claude3.sh`) slip through. Cosmetic: content is index-based
  (`ACCUM_USER_IDX=1`). `inventory.toml` containing the per-account names is blessed
  elsewhere as the topology source.

### Tests — good volume, uneven distribution
- **[verified]** ~1418 test functions, but concentrated on `status` (9 files) and `tradinetools`
  (11). The highest-risk trading code is least tested by LOC/test-file ratio:
  `indicators` (1 file / 3457 LOC), `polymarket` (3 / 7854), `cex` (5 / 8354).

### Improvement themes
1. **Deploy** (Part 2 — highest ROI): iterate `inventory.toml`, delete the wrappers. ✅ DONE
   (Phases 1–4 + status/labels convergence).
2. **Adapters**: ~~move identical helpers into `api_common.py`~~ (moot — they're per-exchange,
   not duplicated); ✅ **dropped the dead Polymarket accessors from the CEX adapters**. A formal
   `ExchangeAdapter` Protocol is still optional (`connectors.validate` already gates by method).
3. **Tests**: add coverage to `indicators` (1 file / 3457 LOC) and the CEX/Polymarket bot cores.
   ← next.
4. **Hook**: add a staged-filename check for `claude[1-6]` (after Part 2 removes those files).

---

## Part 2 — Inventory-Driven Dynamic Deploy: implementation plan

### Current state (verified)
- `inventory.toml` = single source of truth: ~15 `[[bot]]` rows, each with `account_idx`,
  `bot_name`, `kind`, `service_unit`, `install_dir`, `is_live`, **`deploy_script`**.
- `deploy_all.sh` **ignores** those `deploy_script` fields and instead hardcodes a `run_step`
  list (lines ~108-120) mapping account→wrapper.
- Each wrapper is a 6-15 line env preset, e.g.:
  ```sh
  # deploy_accumulation_claude2.sh
  ACCUM_USER_IDX=1 BOT_STRATEGY=strategies/accumulation/btc_accumulation_mexc.json \
    exec bash "$(dirname "$0")/deploy_accumulation.sh" "$@"
  # update_claude2.sh
  TEST_STANDALONE_USER_IDX=1 TRADINEBOTTE_DATA_SOURCE=feed TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5557 \
    exec bash "$(dirname "$0")/update_standalone.sh" "$@"
  ```
- The **generic deployers already exist and are already parametrised by env vars**:
  `update_standalone.sh` (17 KB), `deploy_accumulation.sh` (10 KB), `deploy_grid_mexc.sh`,
  `update_swing.sh`, and the special `update_claude1.sh` (acct-1 infra, 310 lines).
- `tomllib` loaders already exist: `sync_inventory.load_inventory()`,
  `check_inventory.load_rows()`.

**Insight:** a wrapper = `{ generic_deployer, env-preset dict }`. That env preset is exactly the
data that should live in `inventory.toml`. Nothing about the actual deploy logic needs to change
— only the *dispatch* and where the *presets* live.

### Target architecture
```
inventory.toml  (adds: deployer + deploy_env per row)
      │
      ▼
deploy.py  (orchestrator: parse → order → dedupe → sequential dispatch)
      │  shells out, one bot/account at a time, with row's deploy_env in the environment
      ▼
generic deployers (unchanged): update_standalone.sh · deploy_accumulation.sh ·
                               deploy_grid_mexc.sh · update_swing.sh · update_claude1.sh
```
Delete all thin wrappers. `bot_status.sh` and `generate_status.py` later read topology from the
same inventory (the promised "Phase 5"), killing the last duplication.

### Schema change (Phase 0)
Add two fields per `[[bot]]` (keep `deploy_script` during migration for a fallback):
```toml
deployer   = "tradinebotte-cex/scripts/deploy_accumulation.sh"   # the generic engine
deploy_env = { ACCUM_USER_IDX = "1", BOT_STRATEGY = "strategies/accumulation/btc_accumulation_mexc.json" }
# optional: deploy_phase = "infra" | "bot"   (default "bot"); deploy_order = <int> within account
```
`account_idx` already exists → the dispatcher can also inject `TEST_STANDALONE_USER_IDX` /
`ACCUM_USER_IDX` from it, but keeping them explicit in `deploy_env` first is safer (verify they
match, then de-duplicate).

### Phased rollout (each phase independently shippable + testable on the ephemeral account)

**Phase 1 — dispatcher, wrappers still present (lowest risk). ✅ DONE.**
- New `scripts/deploy.py` (Python, reuses the tomllib loader).
- Runs each row's existing **`deploy_script`**, **deduped**, in a deterministic order.
- `deploy_all.sh` becomes a thin shim → `scripts/deploy.py "$@"`.
- Net effect: the hardcoded `run_step` list is **derived from inventory** — the triplicated
  mapping collapses to one source. Validated with a fleet-wide `--verify-only` (all 10 ✓).

**Phase 2 — move env presets into inventory, collapse wrappers. ✅ DONE.**
- The 6 pure thin exec-preset wrappers (`update_claude2/3/4.sh` →
  `update_standalone.sh`; `deploy_accumulation_claude2/3/4.sh` → `deploy_accumulation.sh`)
  moved their presets into inventory `deployer` + `deploy_env` and were **deleted**.
  deploy.py runs `env <deploy_env> bash <deployer>`; dedup keys on `(script, env)` so the
  three rows sharing `update_standalone.sh` (idx 1/2/3) stay distinct.
- `check_inventory.py` updated: the obsolete inventory↔deploy_all.sh drift check (a shim
  now) is replaced by a check that every accounts-2..N bot is reachable in deploy.py's
  derived plan; it also validates `deployer` paths + `deploy_env` shape.
- Added `deploy.py --only <TOKEN>` (single account/bot); docs (going-live, UPDATE) and the
  engine header comments repointed off the deleted wrappers.
- Validated: full test suite green + a second fleet-wide `--verify-only` (all 10 ✓, engines
  resolved to the correct accounts via the presets).
**Phase 2b — de-account-name the remaining full deployer. ✅ DONE.**
- `deploy_grid_claude3.sh` → **`deploy_grid_binance.sh`** (generic engine); the acct-3
  binding (`TEST_GRID_BINANCE_USER_IDX=2`, strategy) moved to inventory `deploy_env`, with
  the old hardcoded values kept only as unset-fallbacks in the script.
- Removed the orphan `update_claude5.sh` (a thin preset for acct-5, which now runs swing).
- After P2b, the only account-named deploy script left is `update_claude1.sh` (the
  bespoke account-1 infra block) and `deploy_scalping_claude4.sh` (disabled orderbook, kept
  for reference).

**Phase 3 — converge the other topology consumers (the promised "Phase 5").**
- `bot_status.sh` LABELS and `generate_status.py` `_ACCOUNT_LABELS` / `_LIVE_BOTS` read from
  inventory (via a tiny shared loader or the synced shared DB). Removes the last copies.

**Phase 4 — enforce + validate.**
- Extend `.git-hooks/pre-commit` with a staged-**filename** check for `claude[1-6]`.
- Extend `check_inventory.py` to validate `deployer` exists + is executable, `deploy_env` keys
  are known, and that **no orphan wrapper** remains referenced.

### Scale-out robustness — a bot of every family on every account (verified + hardened)
Stress-tested the inventory-driven pieces against a synthetic full matrix (4 trading
families × 6 accounts, incl. account-1). Findings + fixes:
- ✅ `account_labels` / `_LIVE_BOTS` scale cleanly (labels become e.g. `acct-2
  [poly+accum+grid+swing]`; live set tracks `is_live`).
- ✅ All 5 generic deployers are already index-parametrized (`TEST_STANDALONE_USER_IDX`,
  `ACCUM_USER_IDX`, `TEST_GRID_BINANCE_USER_IDX`, `TEST_SWING_USER_IDX`,
  `TEST_GRID_MEXC_USER_IDX`), so one engine serves all accounts.
- **[FIXED] account-1 trading bots were silently dropped** — `deploy.py` skipped *all*
  idx-0 rows. Now it skips only rows run by the **bespoke infra scripts**
  (`update_claude1.sh` / `setup_data_plane.sh` / `deploy_status_service.sh`), so a trading
  bot added to account-1 IS derived. `check_inventory` aligned (was silent about it).
- **[FIXED] deploy collision guard** — two bots sharing the same `deployer` + `deploy_env`
  dedup to one step, silently dropping one. `check_inventory` now flags it (the fix is a
  distinct account index per row's `deploy_env`).
- Regression tests: `TestScaleOut` (test_deploy) + `test_check_inventory.py`.
- **✅ DONE — auto-inject the account index.** `deploy.py` derives each deployer's
  account-index env var from `account_idx` (map `_DEPLOYER_IDX_VAR`, applied by `_row_env()`,
  which `check_inventory` reuses so its collision guard sees the post-injection env). The
  explicit indices were dropped from `inventory.toml`, and `update_swing.sh` / `deploy_grid_mexc.sh`
  migrated to `deployer` form — so `deploy_env` now carries only strategy/feed config, never
  the index. Adding a family to every account is now index-free and collision-proof by
  construction (full-matrix test: 24 bots → 24 distinct steps with zero `deploy_env` index).
  Re-validated: a fleet-wide `--verify-only` resolved every engine to the correct account
  (incl. the migrated swing→acct-5, grid_mexc→acct-6). An explicit `deploy_env` index still wins.
- Minor: `bot_status.sh` still loops `for IDX in 0..5` (hardcoded 6 accounts) — fine for the
  6-account (claude) fleet, revisit if account count changes.

### Constraints the dispatcher MUST preserve (from ops memory)
- **Sequential, one account at a time** — never parallel SSH/rsync to the same host. Wait for
  each account to finish before the next.
- **acct-1 is special:** default = rsync-only; `--restart-infra` gates feed/service restarts
  (which disrupt all live_bots ~30 s). Feed order matters: feeds must be up+flowing *before*
  `account_bot` restarts (see the "stale 15M WS after feed restart" gotcha). Keep acct-1's
  bespoke path (`update_claude1.sh` / `setup_data_plane.sh`) rather than genericising it now.
- **`pgrep -f` self-match gotcha:** any process-matching in the deployers must filter via
  `/proc/$P/exe` so the orchestrator's own `ssh/bash`/`python` process is never killed.
- **`service_unit` `{account}` substitution** for the per-user `account_bot` unit.
- Config sourcing: **save `TEST_STANDALONE_USER_IDX` before `source "$CONF"`** (a past bug sent
  all Polymarket deploys to the ephemeral test account).
- Test flow: dry-run + the ephemeral test account (full wipe when OK) before touching the real
  accounts; run `prepare_release.sh` before any merge to `main`.

### Dispatcher UX (proposed flags)
```
deploy.py [--only <account_idx|bot_name>] [--dry-run] [--restart-infra]
          [--exclude-infra] [--list]  [-- <args forwarded to each deployer>]
```
- `--dry-run` prints the ordered plan (row → deployer → env) without executing.
- `--only` targets one account or one bot for iteration.
- `--list` prints the resolved deploy order from inventory.

### Open questions for us to refine
1. Orchestrator language — **Python** (`deploy.py`, reuses the tomllib loader, clean ordering/
   dedupe logic; shells out to the proven bash engines) vs. staying in bash. Recommendation:
   Python orchestrator, bash engines unchanged.
2. Do `account_idx` → env-var (`ACCUM_USER_IDX` / `TEST_STANDALONE_USER_IDX`) mappings get
   derived by the dispatcher, or stay explicit in `deploy_env`? (Start explicit, de-dup later.)
3. How to encode acct-1's intra-account ordering + `--restart-infra` semantics in inventory
   (a `deploy_phase`/`deploy_order` field?) vs. keeping acct-1 as a hardcoded special case.
4. Should `orderbook_bot` (disabled) and other paused strategies carry an `enabled=false` row so
   inventory stays the complete desired-state, with the dispatcher skipping them?
