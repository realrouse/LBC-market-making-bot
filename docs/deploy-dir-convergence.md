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
