# Deploy directory & service convergence — design + phased plan

> Status: **proposal, awaiting approval.** Written 2026-07-13 after the native cutover exposed the
> divergence. No prod change yet. Companion to [`deploy-engine-design.md`](deploy-engine-design.md).

## 1. Problem — the divergence the native cutover surfaced

The `install_dir` inventory field is **overloaded** and the per-bot data layout is **ad hoc**:

| bot | code dir | data dir (TRADINEBOTTE_DIR) | service | inventory `install_dir` |
|---|---|---|---|---|
| poly / threshold | `~/tradinebotte` | `~/tradinebotte` | `tradinebotte-live.service` | `~/tradinebotte` (code) |
| swing | `~/tradinebotte` | `~/tradinebotte` | `tradinebotte-live.service` | `~/tradinebotte` (code) |
| mexc grid | `~/tradinebotte` | `~/tradinebotte` | `tradinebotte-live.service` | `~/tradinebotte` (code) |
| accumulation | `~/tradinebotte` | `~/tradinebotte-accum` | `tradinebotte-accumulation.service` | **`~/tradinebotte-accum` (DATA!)** |
| **binance grid** | `~/tradinebotte` | `~/tradinebotte-grid` | **`tradinebotte-grid.service`** | **`~/tradinebotte-grid` (DATA!)** |
| feed5m | `~/tradinebotte` | `~/feed5m` | `tradinebotte-feed5m.service` | — |

Concrete failures this caused during the native cutover (all traced to the overload):
- **The native engine read `install_dir` as the CODE dir** and passed `~/tradinebotte-accum` → `act_deps`
  `cd`'d into a dir with no venv → **deterministic `pip-failed` + `tt:FAIL` for every accumulation deploy**
  (fixed by hardcoding the code dir to `~/tradinebotte` in `build_native_exec_plan`).
- That hardcode then **armed a foot-gun** on the binance grid: a native `grid` deploy of account-3 would
  write `~/tradinebotte/config.json` + restart `tradinebotte-live.service` — files that belong to
  account-3's **poly** bot → **it would clobber the poly** (disarmed for now via `_NATIVE_BASH_ONLY`).

**Root cause = a field named `install_dir` sometimes holds a data dir, and each bot's data dir/service is
chosen ad hoc** rather than derived. Per the standing directive *"eliminate the source, don't maintain the
dirty thing"*, patching the deployers around this is wrong; converge the layout.

## 2. The constraint the naïve fix misses

"Converge the binance grid onto `tradinebotte-live.service` like the mexc grids" **does not work**:
**account-3 runs three live_bots** — poly (`live.service`), accumulation (`accumulation.service`), binance
grid (`grid.service`). Moving the grid onto `live.service` **collides with the poly** (same unit + same
`config.json`). So `tradinebotte-grid.service` is not gratuitous mess — it exists *because* multiple bots
cohabit an account and each needs a distinct service + data dir. The convergence is therefore **not** "one
service" but **"a systematic, derived service + data dir per bot role,"** of which `grid.service` becomes a
regular case rather than a special one.

## 3. CHOSEN: single-tree, per-instance-suffixed files (option 2), test-account-first (decided 2026-07-13)

All bots on an account run from **`~/tradinebotte`** (code AND data — one tree). Per-bot files are suffixed
by an **instance key** so they don't collide:

- `live_bot` today derives every path from `TRADINEBOTTE_DIR` + a FIXED name (`config.json` / `live.db` /
  `live.log`), so two bots in one dir would collide on all three. Fix: a new env var **`TRADINEBOTTE_INSTANCE`**
  (set in each bot's systemd unit = its **role**: `threshold`/`accumulation`/`grid`/`swing`; one role per
  account today, so unique). When set, paths become `config_<instance>.json`, `live_<instance>.db`,
  `<instance>.log`, all under `~/tradinebotte`. When UNSET → today's `config.json`/`live.db`/`live.log`
  (**backward-compatible**: a running bot with no `TRADINEBOTTE_INSTANCE` is unchanged, so P1 ships as a no-op).
- Services stay distinct systemd units (systemd needs unique unit names) but ALL set
  `TRADINEBOTTE_DIR=%h/tradinebotte` + `TRADINEBOTTE_INSTANCE=<role>`. `tradinebotte-grid.service` /
  `-accumulation.service` remain as *unit names*, but their data no longer lives in a separate dir.
- `bot_id` stays the fleet join key (already role-keyed → `bot_id_<role>` files coexist in one tree fine);
  heartbeat/control IPC sockets are already `bot_id`-keyed → no collision. The instance suffix is the ROLE
  (short, stable, known at deploy time) rather than the full generated `bot_id` — same uniqueness, cleaner
  filenames; trivially swappable to `bot_id` if ever two same-role bots must coexist on one account.

Cost acknowledged (why this was the "bigger" option): it changes `live_bot`'s path contract + needs a data
migration (move `~/tradinebotte-accum/*` → `~/tradinebotte/*_accumulation.*`). Mitigated by backward-compat
(P1 is inert until a unit opts in) + test-account validation before any prod account.

### Collision surface (enumerated 2026-07-13 — the complete list, not bot-by-bot)
Every fixed-name per-bot file under the data dir, and how single-tree handles it:
- **config.json / live.db / live.log** → per-instance suffixed by `instance_paths()` (**P1, done**).
- **`bot_id_<role>`** (`resolve_bot_id` writes `<data_dir>/bot_id_<role>`) → role-keyed, so the three coexist
  in one tree with NO collision. **BUT it is read-or-GENERATE**: if P4's data move doesn't carry the existing
  `~/tradinebotte-accum/bot_id_accumulation` into `~/tradinebotte/`, the bot generates a NEW bot_id → its
  heartbeat/statuspage row/deploy history **silently orphan** (inventory still lists the old id). This is
  invisible on a fresh test-account (no pre-existing id to diverge from). **P4 MUST `mv` bot_id_<role> too;**
  **P2's test-account check MUST pre-seed a `bot_id_<role>` and assert resolve REUSES it, not regenerates.**
- **`.reset_request_<bot_name>.json`** (`reset_marker_path`) → already **bot_name**-keyed → distinct ✓.
- **`.deps_hash`** → the venv is shared per account, so a shared stamp is correct ✓.
- **version.stamp** (fixed) / **webstatus** (`~/public_html/tradinebot_status.html`, fixed) / snapshots →
  shared/last-writer; cosmetic, do NOT gate P2 — suffix as a follow-up in the same P2 commit.
- account_bot's `account.log` is infra (one instance on acct-1), out of the per-account multi-bot scope.

Net: the load-bearing surface is **config/db/log (P1 ✓) + bot_id migration (P4) + pre-seed proof (P2)**. No
further bot-code change beyond P1 is required for correctness.

### P2 acceptance = enumeration, not "heartbeats present"
After deploying poly+accum+grid into ONE test-account `~/tradinebotte` (with `--single-tree --test-ports`):
- exactly one `config_<role>.json`, `live_<role>.db`, `<role>.log` **per bot**, and **ZERO** plain
  `config.json`/`live.db`/`live.log` (a stray fixed-name file = a bot still on the legacy path = the collision);
- three distinct `bot_id_<role>` files whose values match inventory; pre-seed one before deploy and assert reuse;
- each DB advancing independently (own mtime/rows) — not one bot writing another's file.
"All active + heartbeats present" is NOT sufficient (it passes even if two bots share a DB).

### Phases (single-tree, test-account-first)
- **P1 — `live_bot` path derivation** from `TRADINEBOTTE_INSTANCE` (+ backward-compat default) + unit test.
  Inert in prod (no unit sets it yet). *(this is the first implementation step)*
- **P2 — deploy specs** set `TRADINEBOTTE_INSTANCE=<role>` in each unit + write `config_<role>.json`.
  Validate on **test-account**: 2+ bots (poly + accum + grid) coexist in one `~/tradinebotte`, each its own
  config/db/log, no collision, correct heartbeats.
- **P3 — binance grid → uniform** single-tree instance (`grid`), delete `_NATIVE_BASH_ONLY`; validate test-account.
- **P4 — prod cutover, account-by-account**: stop unit → move `~/tradinebotte-<old>/{config,db}` →
  `~/tradinebotte/{config_<role>.json,live_<role>.db}` → set env → start → verify heartbeat/DB continuity.
  Revert = old bash deployer + old dir. Serialize (host flakiness).
- **P5 — residue+retire**: remove `~/tradinebotte-accum` / `~/tradinebotte-grid`, purge orphan units,
  delete `deploy_grid_binance.sh`; `check_inventory` asserts single-tree (no bot uses a separate data dir).

## 3b. (superseded) per-role-dir alternative — kept for reference

Inventory splits the overloaded field into two, one of which is **derived, never hand-written**:
- **`code_dir`** — invariant `~/tradinebotte` for every bot (the rsync target; drop the field entirely and
  make it a constant unless a real second code location ever appears).
- **`data_dir`** — **derived from role**: `~/tradinebotte-<role>` (`threshold` / `accumulation` / `grid` /
  `swing` / …). Uniform, never ad hoc. `config.json` + the bot's DB + log live here (unchanged contract:
  the bot still reads `TRADINEBOTTE_DIR/config.json`, so **no `live_bot` code change**).
- **`service`** — `tradinebotte-<role>.service`, derived. `grid.service` stops being a binance special case.

Why this over the single-tree alternative (option 2 = everything in `~/tradinebotte`, files suffixed by
`bot_id`): single-tree is *more* unified but forces `live_bot` to load `config_<bot_id>.json` and a per-bot
DB path — a change to the running bots' **config contract** + a file migration, i.e. bot-code blast radius.
Per-role derived dirs keep the `TRADINEBOTTE_DIR/config.json` contract intact and are the smaller, safer
convergence. (`bot_id` role-keyed sockets/heartbeats already prevent the IPC collisions single-tree worried
about — but that doesn't cover the fixed-name `config.json`.)

Net after convergence: `native_target`/`FAMILIES` derive `data_suffix` + `service` from role uniformly;
`_NATIVE_BASH_ONLY` (the binance-grid escape hatch) is deleted; `deploy_grid_binance.sh` +
`tradinebotte-grid.service` + `~/tradinebotte-grid` are removed.

## 4. Phased plan (prod-safe, test-account-first, revert-able)

- **P1 — schema.** Add `code_dir` (const) + role-derived `data_dir` to the inventory model + `check_inventory`
  (forbid a data dir in a code field; assert `data_dir == ~/tradinebotte-<role>`). No prod change.
- **P2 — native spec derivation.** `FAMILIES`/`deploy_infra` derive `data_suffix` + `service` from role; the
  native engine stops depending on the overloaded `install_dir`. Validate on **test-account** (test-ports) that
  every family lands the same config/service as today.
- **P3 — binance grid → uniform.** Give it a real native path (a `grid` spec that reads connector + service
  from role, or a `grid_binance` variant that is still `tradinebotte-grid.service` + `~/tradinebotte-grid`
  *derived, not special*). Validate on test-account. Delete `_NATIVE_BASH_ONLY`.
- **P4 — prod cutover, account-by-account.** For each bot whose data dir/service name changes: stop unit →
  move `~/tradinebotte-<old>` → `~/tradinebotte-<role>` (preserve DB/config) → install derived unit → start
  → verify heartbeat under the same `bot_id`. Revert = the old bash deployer. One account at a time (host
  flakiness observed 2026-07-13 → serialize + verify each).
- **P5 — residue + retire.** Remove stale `~/tradinebotte-grid` (and any renamed old dirs), purge orphan
  systemd units, delete `deploy_grid_binance.sh`. `check_inventory` asserts no bot references a non-derived
  dir/service.

## 5. Open questions for approval

1. **Fork:** per-role derived dirs (§3, recommended) vs single-tree bot_id-suffixed (bot-code change)?
2. **Do the "primary" bots move too?** Today poly/swing/mexc-grid sit in `~/tradinebotte` (empty suffix).
   Full uniformity moves them to `~/tradinebotte-<role>` — cleaner but migrates *every* bot's data, not just
   the two divergent ones. Alternative: keep an empty suffix legal for one primary-per-account (less
   migration, mild residual asymmetry). Recommend full uniformity if we're paying for the migration anyway.

## 6. Reconciliation addendum (2026-07-13) — wire the pipeline + inventory to single-tree

> Status: **IMPLEMENTED + validated 2026-07-13** (working tree on `dev`, uncommitted pending the
> dev→main release). Written after the P4 prod cutover + a ~2h soak; reviewed with the advisor,
> user go-ahead given. Closes the "interim footgun". Acceptance results in §6.5.

### 6.1 Why now — the interim footgun
The P4 cutover moved 5 bots (accumulation idx1/2/3/7 + binance-grid idx2) onto single-tree **purely via a
systemd `.d/single-tree.conf` drop-in** that only `deploy_actions.py --single-tree` writes. But the standard
pipeline is unaware: `inventory.toml` still says `install_dir = ~/tradinebotte-accum|-grid` with **bash**
deployers, and `deploy.py` still runs them. So the user's **"bot upgrades" trigger (`deploy_all.sh`)** against
these 5 writes `~/tradinebotte-accum/config.json`, which the drop-in-overridden unit **ignores** (it reads
`config_<role>.json` in `~/tradinebotte`) → **silent config no-op**. Non-destructive (the drop-in protects the
data dir) but a real foot-gun. Soak re-verified healthy 2026-07-13 22:2x: all 5 fresh heartbeats (<120s),
bot_ids reused, holdings continuous (grid pnl 140.54 exact, LBC accum 26894.8).

### 6.2 Scope — the 5 migrated bots only (Option B, mixed layout)
Reconcile **only** the bots already on single-tree. The un-migrated primaries (poly `update_standalone.sh`,
swing `update_swing.sh`, mexc-grid `deploy_grid_mexc.sh`) + acct-1 infra **stay on their bash paths** — their
legacy layout is still correct for them. Full-native for every family is explicitly **out of this release**: it
would drag in poly's merge-config landmine (§ "TWO P4 migration landmines" in [[project_convergence_p2_pickup]])
for no gain here.

### 6.3 Design
1. **`deploy.py` native dispatch.** A row dispatches natively **iff `deployer == scripts/deploy_actions.py`**
   (explicit, greppable — *not* derived from bot_type, which would sweep in the primaries). Family comes from
   `deploy_actions.native_target(bot_type)` (must resolve to `("family", fam)`). The step becomes a **python**
   command: `python3 scripts/deploy_actions.py <fam> --idx <account_idx> --strategy <strategy>` `[--single-tree]`
   (`--single-tree` added when the row has `single_tree = true`). Bash rows are unchanged. `Step` grows an
   `interpreter` (`"bash"` default | `"python"`); the executor picks `bash`/`python3` accordingly.
2. **Dedup collision fix — THE sharp edge.** Pinning case: **idx-2/account-3 runs BOTH the accum row AND the
   binance-grid row.** After repatch both have `deployer = deploy_actions.py` and `account_idx = 2`, so keying
   the dedup on `(script, env)` — or even `(script, env, idx)` — **collapses them into one step and silently
   drops one.** They only become distinct on the **full argv** (`family` = `accumulation` vs `grid_binance`,
   plus `--strategy`). Fix: the dedup key = `(script, env, tuple(args))`. Bash rows keep `args=[]`, so this is
   purely additive (no regression on the poly/swing dedup). **Extract ONE `_plan_key(row)` (or dedup off the
   `build_plan` steps `check_inventory` already imports)** so `build_plan` and
   `check_inventory.check_deploy_pipeline` cannot drift — a divergent second copy makes the collision guard
   useless.
3. **`--skip-restart` on `deploy_actions.py`.** `deploy.py` forwards `--verify-only`/`--skip-restart` via
   `*forward`; `deploy_actions.py` uses strict `parse_args()` and today has no `--skip-restart` → it would
   **hard-error the native step**. Add `--skip-restart` (uniform flags with the bash deployers). `--verify-only`
   already works (it requires `--strategy`, which native rows always supply).
4. **Inventory repatch (5 rows).** `install_dir` → `~/tradinebotte`; add `single_tree = true`; `deployer` →
   `scripts/deploy_actions.py`; replace the strategy env var (`BOT_STRATEGY` / `TEST_GRID_BINANCE_STRATEGY`) with
   a uniform **`strategy = "..."`** field; **`service_unit` unchanged** (accumulation/grid units stay — only the
   DIR converges); fix the now-wrong "own data dir" comments.
5. **Delete the dead bash deployers.** `deploy_grid_binance.sh` **and** `deploy_accumulation.sh` — after repatch
   NO row uses either (both families are fully native). [[feedback_eliminate_dont_maintain]] covers both;
   grid_binance was only singled out earlier for its extra wiring. Scrub the stale `cleanup_server.sh` comment
   mentions. `check_no_legacy_refs.sh` is orthogonal (it guards the old `heartbeat.db` read-path only) — deleting
   these scripts does not touch it.
6. **Coupled tests.** `tests/test_deploy.py` (`_EXPECTED_2_6` → native steps; `TestScaleOut._FAM` +
   `test_account1_trading_bot_is_derived` swap off `deploy_grid_binance.sh`);
   `tradinebotte-status/tests/test_check_inventory.py` (`_GRID` synthetic constant swap);
   `tradinebotte-polymarket/tests/test_bot.py` (`_BOT_DEPLOY_SCRIPTS`: drop `deploy_grid_binance.sh`, **add
   `scripts/deploy_actions.py`** — the native path must ship `botcore/`, which it does via `_BASE_SYNC`).

### 6.4 Sequencing (mechanism-first, mirrors the P1-inert pattern)
- **A.** Land `deploy.py` native dispatch + the `(script,env,args)` dedup key + `deploy_actions --skip-restart`,
  with the shared `_plan_key`. **Inert** — no inventory row triggers it yet — plus unit tests. Independently
  testable.
- **B.** Repatch the 5 inventory rows → native dispatch goes live for exactly them.
- **C.** Delete the dead scripts + update the coupled tests + scrub comments.

### 6.5 Acceptance (enumerated — the prior phases' standard, not "tests pass")
1. **`deploy_all.sh --list` on the real inventory** = the cheap dedup acceptance: **5 distinct native steps**,
   with **idx2 showing accum AND grid as separate steps**, none collapsed.
2. Unit tests asserting the **derived native argv** for each of the 5 rows.
3. **test-account e2e THROUGH `deploy.py`** (not the direct `deploy_actions.py` CLI — the *dispatch* path is what's
   new; the CLI path is already proven by P2–P4).
4. **First prod touch = `deploy_all.sh --verify-only`**, never a blind full run. (A plain `--single-tree` re-run
   on the already-migrated tree is idempotent-safe — DB/bot_id already moved, write-mode config regenerates — but
   prove it via `--verify-only` first.)
5. `check_inventory.py` offline green; full suite green.

### 6.5b Acceptance results (2026-07-13)
1. ✅ `deploy_all.sh --list`: **5 native `py` steps**, idx-2/account-3 shows accum (#5) AND
   grid_binance (#6) as separate steps — the dedup-collision fix proven on the real inventory.
2. ✅ New unit tests: `TestNativeDispatch` (7, incl. the idx-2 accum+grid pinning case) +
   `TestRealInventory.test_derived_plan_matches_historical_order` / `test_migrated_families_deploy_natively`.
3. ✅ **test-account e2e THROUGH `deploy.py`**: dispatched `native accumulation deploy → the test account
   [test-ports +10]`, verified `active errors=0`; enumerated layout = config_accumulation.json +
   live_accumulation.db + bot_id_accumulation + accumulation.log present, **ZERO** plain
   config.json/live.db/live.log, and **`/proc/<pid>/fd` holds ONLY live_accumulation.db**
   (categorical isolation). `--verify-only` forwarded cleanly through the native step (no argparse
   error). Test account wiped + its 8 heartbeats/2 deploys purged from the shared DB.
4. ✅ `check_inventory.py` offline green (17 rows); deploy/deploy_actions/check_inventory test
   suites green. Two PRE-EXISTING, orthogonal full-suite failures (NOT introduced here): stale
   venv `tradinetools` copy (env, refreshed locally) and `cex_consumer.indicators_consumer_loop`
   snapshot AST check (a separate real finding).
5. Prod not yet touched — first prod touch stays `deploy_all.sh --verify-only`, user-triggered.

### 6.6 NOT in this change — retire the old dirs (still blocked)
`rm -rf ~/tradinebotte-accum|-grid` is **DESTRUCTIVE and the sole revert path.** Soak is only ~2h (cutover was
20:10–20:32 today); it needs a **day+** soak **and explicit user OK**. Deferred, unchanged from
[[project_convergence_p2_pickup]].
