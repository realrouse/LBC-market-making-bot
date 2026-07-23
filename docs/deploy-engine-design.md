# Deploy Engine — Design & Phased Plan

> Status: **proposal / draft to iterate on.**
>
> ⚠ **`scripts/deploy_engine.py` (Phase A, the bounded-parallel scheduler) was RETIRED 2026-07-23** —
> the native single-tree deploy eliminated the slow bash steps it parallelised, leaving it orphaned.
> This doc is kept for the broader design history; pending a P5 rewrite.
> Companion to [`audit-and-inventory-deploy-plan.md`](audit-and-inventory-deploy-plan.md),
> which delivered the inventory-driven *dispatcher* (Phases 1/2/2b **done**). This document
> proposes the next step that plan deferred: replacing the per-family **bash engines** with a
> single **generic, parallel, inventory-driven deploy engine**.

---

## 1. Context — where we are, and why go further

The dispatcher work is done: `scripts/deploy.py` derives the deploy plan from `inventory.toml`
(no more triplicated topology), auto-injects each deployer's account index from `account_idx`,
and dedupes. But its target architecture deliberately **keeps the bash engines** and runs them
**sequentially, one account at a time**:

```
inventory.toml ─▶ deploy.py (derive plan) ─▶ 7 bash engines (~2000 lines) ─▶ ssh
                                              deploy_grid_mexc.sh · deploy_grid_binance.sh
                                              deploy_accumulation.sh · update_swing.sh
                                              update_standalone.sh · update_claude1.sh · setup_data_plane.sh
```

Those 7 engines are ~300 lines each and **re-implement the same pipeline**: parse conf → rsync
(with divergent exclude-lists) → write `config.json` (6 of 7) → venv/pip → refresh `tradinetools`
→ kill-stale (`pgrep -f`, self-match-prone) → install/restart systemd unit → `record_deploy`
(hardcoded bot name) → verify. This is the **last large duplication** in the tree, and it is the
source of the bash-fragility bugs hit repeatedly in practice:

- deploying to the **wrong account** (an index guessed by hand; `test-account` is idx 6, `the real-money account` idx 7);
- `TEST_STANDALONE_USER_IDX` **overwritten by `source "$CONF"`** (once sent every Polymarket deploy to the test account);
- `pgrep -f` **self-matching** the deployer's own shell;
- `record_deploy` journaling under **legacy names** (drifts on every deploy after the bot_id rename).

And the sequential model is **slow**: a full fleet redeploy is ~18 min (pip + full rsync + verify
**per bot**), even when nothing changed.

**Goal:** an independent, dynamic, fast engine — a single Python tool driven entirely by the
inventory, with **bounded parallelism** (default **2** concurrent SSH connections) and the speed
wins the fast-path already proved (~18 min → ~1–2 min).

## 2. Goals & non-goals

**Goals**
- One **generic engine** — no per-family logic; a bot is fully described by declarative inventory fields.
- **Bounded parallelism**, `jobs` default **2**, safe on a single shared host.
- **Faster**: skip pip when deps unchanged, targeted sync, targeted restart, parallel across accounts.
- **bot_id-aware** `record_deploy` (fixes the open journal-drift follow-up).
- Preserve every ops-safety constraint the dispatcher must keep (§7).

**Non-goals**
- Change *what* is deployed (same artifacts, services, config semantics).
- A CI/remote-cloud system. This drives SSH to one always-on host (the server), same as today.
- Big-bang rewrite — migration is family-by-family, engines coexisting (§8).

## 3. Target architecture

```
inventory.toml  (declarative: sync set · config · service_env · role · depends_on · serialize_key)
      │
      ▼
deploy engine (Python)
   ├─ Planner    : inventory → task DAG (deps) + serialize domains
   ├─ Scheduler  : bounded worker pool (jobs, default 2) honouring deps + serialize_key
   └─ Actions    : idempotent steps, one library shared by every bot
         connect · sync · deps · tradinetools · config · service_install · restart
                  · read_bot_id · record_deploy · verify
      │  (one ssh/rsync session per worker)
      ▼
   the server (acct-1..7)
```

- **A bot's deploy = a pipeline of actions** parametrised by its inventory row. No family branches.
- **Actions are idempotent** and independently testable (e.g. `deps` no-ops when the requirements
  hash is unchanged; `sync` is rsync content-diff; `config` writes only if changed).
- The engine is **independent**: it owns connection, ret/idempotency, ordering, logging, summary —
  the bash pipelines collapse into ~10 small typed actions.

## 4. Concurrency model — the core new capability

```
jobs = 2 (default)             # max simultaneous SSH connections; --jobs N or inventory `jobs`
serialize_key (per bot)        # default = account; bots sharing a key NEVER overlap
depends_on (per bot)           # DAG edges; a bot starts only after its deps succeed
```

- **Worker pool** of `jobs` runs ready tasks concurrently.
- **Serialize domains**: two bots with the same `serialize_key` (default **account**) run
  **strictly sequentially** — they share `~/tradinebotte`, one venv, and kill-stale logic, so
  concurrent rsync/restart would race. Bots on **different** accounts run in parallel up to `jobs`.
- **Dependencies**: `depends_on` edges make a consumer wait for its data-plane (grids → `cex_feed`,
  accumulations → `indicators`, `account_bot` → feeds). This **replaces the hardcoded acct-1
  order-critical block** with a declarative DAG.
- **Reconciling the "never parallel, same host" memory rule:** that rule was a blunt safeguard.
  The engine keeps its *intent* — never two conflicting operations at once — precisely, via
  serialize domains + a low `jobs` default (2) to cap host IO/CPU, while still parallelising the
  genuinely independent accounts. It is *not* an invitation to `jobs=12`.
- **Failure policy**: a failed bot fails its dependents (marked *skipped*), is recorded, but
  independent branches continue; the run ends with a per-bot summary and a non-zero exit.

## 5. Inventory schema additions

Declarative fields that let the engine drop the bash. All optional with safe defaults; keep
`deployer`/`deploy_script` during migration as a fallback.

| Field | Type | Purpose |
|---|---|---|
| `role` | str | already implicit; names the `bot_id` (`{exchange}-{strategy}-{pair}`) and its `bot_id_<role>` file |
| `sync` (or `family`) | list / str | repo paths (or a named file-set) to push → replaces per-engine exclude-lists |
| `config` | table | declarative `config.json` (`strategy`, `data_source`, `feed_addr`, …) → replaces inline heredocs |
| `service_env` / `env_file` | table / path | systemd `Environment=` / `EnvironmentFile=` (e.g. `TRADINEBOTTE_DIR`, `TRADINEBOTTE_IPV4_ONLY`, the staged MEXC key) — secrets by path, never in git |
| `serialize_key` | str | scheduler mutual-exclusion key (default = account) |
| `depends_on` | list | bot_names / roles this bot needs up first |
| `data_dir` | str | data dir if distinct from the code/`install_dir` |
| `jobs` | int (file-level) | default parallelism cap (default 2) |

Example (declarative, no bash):
```toml
[[bot]]
account_idx  = 7
bot_name     = "mexc-grid-lbcusdt-a00f5f"
role         = "grid"
sync         = "cex-grid"                         # named file-set: live_bot.py, botcore, connectors, api_mexc, tradinetools
config       = { strategy = "strategies/grid/grid_LBC_USDT_mexc.json", data_source = "cex_feed", feed_addr = "tcp://127.0.0.1:5563" }
service_unit = "tradinebotte-live.service"
install_dir  = "~/tradinebotte"
depends_on   = ["infra-cexfeed-0e7b3a"]
is_live      = false
```

## 6. Speed — where the time goes and how it drops

| Cost today (per bot) | Fix |
|---|---|
| `pip install` even when unchanged | **skip** when `requirements.txt` hash matches a remote stamp |
| full rsync of the tree | **targeted sync** from the role file-set (rsync stays content-diff) |
| kill-stale + poll + verify | targeted restart via **systemd `MainPID`**; verify from the heartbeat, not a sleep-loop |
| strictly sequential | **parallel** across accounts (jobs) |

Combined with the proven fast-path (push changed files + restart), a fleet redeploy goes from
**~18 min → ~1–2 min**, and a single-bot iteration from ~90 s → a few seconds.

## 7. Safety constraints the engine MUST preserve

Carried over from ops memory + `audit-and-inventory-deploy-plan.md §"Constraints"`:
- **No process self-match:** target the service's **systemd `MainPID`** (or cgroup), never `pgrep -f`.
- **acct-1 order:** feeds must be up+flowing **before** `account_bot`; encoded as `depends_on`.
  `--restart-infra` still gates the disruptive infra restarts; default leaves infra alone.
- **`service_unit` `{account}` substitution** for the per-user `account_bot` unit.
- **No conf-source index bug:** the engine computes the target from `account_idx` in-process; it
  never sources the conf into a shell that also carries an index env var.
- **`tradinetools` before restart:** refresh site-packages before restarting any bot that imports a
  new symbol (the `resolve_bot_id` crash-loop class).
- **Test flow:** `--dry-run` + the ephemeral `test-account` (full wipe when OK) before real accounts;
  `prepare_release.sh` before any merge to `main`.
- **`record_deploy`** reads the remote `bot_id_<role>` and journals under the **bot_id**.
- **`--exclude=bot_id*`** on sync so a redeploy never clobbers a bot's identity.

## 8. Phased implementation plan

Each phase is independently shippable, dry-runnable, and validated on `test-account` first.

**Phase A — Scheduler + action-runner, engines still bash (lowest risk).**
New `deploy/` engine: task DAG from inventory, bounded pool (`--jobs`, default 2), `serialize_key`
(= account) and `depends_on`. Each bot's single "action" still shells to its **existing** bash
deployer. *Win:* parallelism + dependency ordering **immediately**, with zero change to deploy
logic. `deploy_all.sh`/`deploy.py` become shims over the engine. Validate: fleet `--verify-only`,
timings (expect ~Nx), a forced dependency (grid waits on cex_feed).

**Phase B — Native actions, grid pilot.**
Implement the action library (`sync`/`deps`/`config`/`service`/`restart`/`read_bot_id`/`record_deploy`/`verify`)
and drive the **grid** family natively from declarative fields (`sync`/`config`/`service_env`),
bypassing `deploy_grid_mexc.sh` / `deploy_grid_binance.sh`. Diff native-vs-bash result on `test-account`
(same files, same service, same heartbeat). Retire the two grid bash engines.

**Phase C — Migrate the remaining families.**
accumulation → swing → polymarket `update_standalone.sh`. Move each preset into inventory fields;
delete the bash engine once its family is green. After C, only the acct-1 bespoke scripts remain.

**Phase D — Infra (acct-1) declaratively (highest blast radius → last).** *(deployers built + validated; prod cutover deferred to E.)*
`deploy_actions.py` gained an `INFRA` spec dict + `deploy_infra()` for the 6 infra services
(`indicators`/`feed`/`feed5m`/`cexfeed`/`status`/`account`). Key design decision — **the infra
deploy never rewrites the unit** (`act_service_restart` is install-if-absent), mirroring bash
`update_claude1.sh` (`_restart_service` = rsync `.py` + restart, unit untouched); that is what
preserves hand-set remote unit env. Validated on `test-account`:
- **15M feed → GREEN** with the discriminating check: running unit carries
  `TRADINEBOTTE_MARKET_TAG_ID=102467`, installed from the **new baked template**
  `tradinebotte-feed15m.service`, and `feed.log` shows `BTC 15M markets (tag=102467)` actively
  resolving — over per-user IPC (`ipc:///run/user/N/…`, no host-wide collision).
- **cexfeed / indicators → pipeline validated, verify correctly RED.** Every mechanical step ran
  (sync → tradinetools refresh+import → unit install → restart → bot_id → verify); they can't go
  green because they bind **host-wide singleton loopback ports owned by prod acct-1**
  (cexfeed 5563; indicators' config forces TCP 5559/5561). This is a structural finding, not a
  defect: one infra instance per host — you can't spin a test copy on the shared server.
- **feed5m / status / account** → same host-wide-singleton or `sg claudes`+shared-DB coupling;
  acceptance is the **static equivalence diff** vs each bash deployer (as in Phase C), which holds:
  same code sync (superset), same unit templates, same restart semantics.

**Tag wart eliminated:** the 15M market tag was injected imperatively by
`setup_data_plane.sh` (`sed -i … 102467`) because the old `tradinebotte-feed.user.service` was
tagless. It is now **baked into `tradinebotte-feed15m.service`** (symmetric with feed5m's 102892).
The native `feed` deployer uses it; the live bash `sed` is left in place (prod path, untouched) and
becomes redundant → **drop it at the E cutover**. Also surfaced: `tradinebotte-feedwatchdog.service/.timer`
runs on acct-1 but is **absent from inventory** (tracking gap — add a row). Observability note: units
with `StandardError=null` (indicators) hide crashes from the journal, defeating the verify's log-ERROR
scan → the **systemd MainPID check is the load-bearing signal**, not the log grep.

**Phase E — Enforce, speed, cleanup.**
`pip`-skip (requirements hash), systemd-`MainPID` targeting, `record_deploy` under bot_id,
`check_inventory` validates the new fields (`sync`/`config`/`depends_on`/`serialize_key`) + the
DAG is acyclic; **delete all bash engines** (~2000 lines) and the account-index env-var indirection.
Also at cutover: **wire `deploy_engine` → native `deploy_family`/`deploy_infra`** (derive family/service
from `bot_type` + read `deploy_env`, both from inventory — so 5557/mexc/etc. stop being FAMILIES hardcodes);
**drop `setup_data_plane.sh`'s `sed … 102467`** (now redundant — the tag is baked into `tradinebotte-feed15m.service`);
**add the `feedwatchdog` row** to inventory (it runs on acct-1 but is untracked).

**test-account test-port profile (independent quicktest) — DONE (2026-07-13).** Phase D proved that infra
services can't go green on `test-account` because they bind **host-wide singleton loopback ports owned by prod
acct-1** (feed5m 5557, cexfeed 5563, indicators 5559/5561, status 5562) — the shared server has one instance
per port. Implemented in `deploy_actions.py`: a `--test-ports` flag applying a **uniform `+10` offset**
(`TEST_PORT_OFFSET`) to every TCP bind. Per-service **systemd drop-in** (`…/<unit>.d/test-ports.conf`, a
separate file → the base unit stays install-if-absent, never rewritten) sets the right env: feed5m
`TRADINEBOTTE_FEED_ADDR=…:5567`, cexfeed `TRADINEBOTTE_CEX_FEED_ADDR=…:5573`, status
`TRADINEBOTTE_STATUS_ADDR=…:5572`, indicators `TRADINEBOTTE_PORT_BASE=5567` (its built-in `_shift_addr`
shifts feed/out/reg **and** the config-JSON addrs by the same +10 — no config rewrite needed). Consumers get
their `config.json` `feed_addr` offset too (grid/swing 5563→5573, poly 5557→5567). The 15M feed + account_bot
need no offset (per-user IPC in `/run/user/%U/`, already isolated). **Fail-closed**: the flag is auto-implied
for the test-account idx (`TEST_STANDALONE_USER_IDX`, resolved at runtime — no account name in git) and
*refused* on any other idx (offsetting a prod bot onto wrong ports would break the fleet).
**Validated end-to-end on test-account**: pre-flight confirmed the base range bound + 5567–5573 free; cexfeed
went **RED (5563 collision) → GREEN binding 5573** (`ss` confirmed the process's own MainPID listens on 5573,
not merely the env present); a grid deployed with the same profile wrote `feed_addr=…:5573` and its log shows
`Data source: shared CEX feed tcp://127.0.0.1:5573 … (consumer mode)` — the whole producer→consumer stack runs
self-contained, zero prod collision. One offset constant applied uniformly (a *profile*, not per-service
hacks). test-account wiped after (ephemeral, per policy).

**Phase E native-bridge foundation — DONE (2026-07-13); prod cutover + bash deletion still deferred.**
The pieces that make native reachable from inventory, all offline/testable:
- `deploy_actions.native_target(bot_type)` → `(kind, target)` maps every one of the 17 inventory bot_types
  to `deploy_family`/`deploy_infra` (0 unmapped; ordered so `polymarket-multibot`→account_bot beats the
  generic polymarket rule, and `infra-feed-15m/5m` beat a generic feed).
- `deploy_engine.py --native` prints the **review-only** native plan derived from inventory (file order
  preserves acct-1 feeds-before-account_bot); it resolves each family's strategy from `deploy_env` and
  **flags gaps**. It surfaced two rows relying on a bash *default* strategy not in inventory (swing acct-5,
  mexc-futures grid acct-6) — now closed by writing the explicit `TEST_SWING_STRATEGY` /
  `TEST_GRID_MEXC_STRATEGY` (= each script's own default → no behaviour change), so the inventory is now
  self-describing for the native path (plan shows **0 gaps**).
- `check_inventory.check_native_coverage` enforces (1) every bot_type has a native target and (2) the
  `depends_on` graph is acyclic — both offline. Tests: `tests/test_deploy_actions.py` (12) green.

Still **deferred** (needs a user-triggered prod deploy + observation — feed/infra blast radius): wiring
`--native` to actually *execute* (call `deploy_family`/`deploy_infra` in-process instead of the bash script),
dropping `setup_data_plane.sh`'s tag `sed`, the `feedwatchdog` inventory row (it emits no heartbeat — needs a
"no-heartbeat" marker so it doesn't pollute the statuspage expected-set), and **deleting the ~2000 bash lines**.
Do NOT delete bash until `--native` execution is proven in prod.

## 9. Risks & mitigations

- **Parallel same-host contention** → bounded `jobs` (2) + `serialize_key`; never two ops in one serialize domain.
- **Big-bang risk** → family-by-family; the engine coexists with bash through Phase C; a bad phase reverts to the bash engine for that family.
- **Infra disruption** → Phase D last, `--restart-infra`-gated, data-plane verified post-restart.
- **Hidden per-family quirks in bash** (special exclude-lists, one-off migrations like the accum DB move) → surface them into explicit inventory fields during B/C; the native diff on `test-account` catches drift.
- **Testability** → `test-account` (idempotent, wiped) + `--dry-run` + `test_deploy` regression tests extended per phase.

## 10. Open questions

1. `serialize_key` default — **account** (safe; shared home/venv) vs. `install_dir` (finer, would let
   `~/tradinebotte` and `~/tradinebotte-grid` on the same account run in parallel — but they share the venv). Start with **account**.
2. `config` — fully declarative table vs. a template file per family? Declarative table is enough for today's configs.
3. `jobs` default 2 — global inventory field **and** a `--jobs` override (override wins). Never auto-scale.
4. Keep the bash engines as a per-family fallback for one release after each migration, or delete on green?
5. Should disabled bots (e.g. `orderbook_bot`) carry an `enabled = false` row so inventory stays complete desired-state, the engine skipping them?
