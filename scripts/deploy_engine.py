#!/usr/bin/env python3
"""deploy_engine.py — Phase A of the deploy-engine refactor (see docs/deploy-engine-design.md).

Adds a BOUNDED-PARALLEL scheduler on top of the existing inventory-driven plan. The plan and
the deploy logic are unchanged: each step still shells to its proven bash deployer (deploy.py's
build_plan). What is new is *how* steps run:

  * `--jobs N` (default 2)  : at most N steps run concurrently (N simultaneous SSH sessions).
  * serialize_key (= account): two steps for the SAME account NEVER overlap — they share
    ~/tradinebotte, one venv and kill-stale logic, so concurrent rsync/restart would race.
    Steps for DIFFERENT accounts run in parallel, up to N. Within an account, steps keep plan
    order — which preserves account-1's order-critical block (feeds before account_bot).

This is the lowest-risk step of the refactor: same artifacts, same services, same bash engines —
only the scheduling changes, cutting a fleet redeploy from sequential (~account-count × per-bot)
toward ~(per-bot × longest-account-chain).

Usage:
  python3 scripts/deploy_engine.py [--jobs N] [--restart-infra] [--only TOK] [--exclude TOK]
                                   [--list] [--dry-run] [-- <args forwarded to each deployer>]
Exit: 0 = all OK, 1 = one or more failed, 2 = config error.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deploy as _d  # reuse the proven plan derivation (build_plan / load_rows / Step / helpers)
import deploy_actions as _da  # Phase E: native_target bridge (bot_type → deploy_family/deploy_infra)

# The env var each family carries its strategy in (inventory deploy_env). First present wins;
# polymarket carries none (merge config). Absent for swing today → surfaced as a gap by --native.
_STRATEGY_ENV = ("BOT_STRATEGY", "TEST_GRID_BINANCE_STRATEGY", "TEST_GRID_MEXC_STRATEGY", "TEST_SWING_STRATEGY")


@dataclass
class NativeStep:
    """One native deploy: call deploy_actions.deploy_family/deploy_infra in-process (Phase E).
    `label` is 'account-N — <bot>' so the scheduler's per-account serialize_key works exactly as
    for the bash plan."""
    label: str
    kind: str            # "family" | "infra"
    target: str          # deploy_family/deploy_infra target key
    idx: int             # account index in TEST_USERS
    strategy: str = ""
    install_dir: str = "~/tradinebotte"


def build_native_exec_plan(rows: list[dict]) -> tuple[list[NativeStep], list[str]]:
    """Executable native plan: one NativeStep per row whose bot_type has a native target, in file
    order (preserves acct-1 feeds-before-account_bot). Returns (steps, skipped) — skipped names any
    row with no native target (still bash-only; check_inventory.check_native_coverage flags these)."""
    steps: list[NativeStep] = []
    skipped: list[str] = []
    for r in rows:
        kt = _da.native_target(r.get("bot_type", ""))
        if kt is None:
            skipped.append(f"{r.get('bot_name')} ({r.get('bot_type')})")
            continue
        kind, target = kt
        idx = int(r.get("account_idx", 0))
        env = r.get("deploy_env") or {}
        strat = next((env[k] for k in _STRATEGY_ENV if k in env), "")
        steps.append(NativeStep(
            label=f"account-{idx + 1} — {r.get('bot_name')} ({r.get('bot_type')})",
            kind=kind, target=target, idx=idx, strategy=strat,
            # install_dir here is the CODE dir — always ~/tradinebotte. deploy_family/deploy_infra
            # derive the per-bot DATA dir from the family's data_suffix (accum → ~/tradinebotte-accum).
            # We deliberately do NOT read the inventory `install_dir`: for accumulation rows it holds
            # the *data* dir (~/tradinebotte-accum), so using it made act_deps cd into a dir with no
            # venv → deterministic pip-failed + tt:FAIL for every accumulation deploy.
            install_dir="~/tradinebotte"))
    return steps, skipped


def _native_call_str(s: NativeStep) -> str:
    fn = "deploy_infra" if s.kind == "infra" else "deploy_family"
    return f"{fn}({s.target}, idx={s.idx}" + (f", strategy={s.strategy}" if s.strategy else "") + ")"


def _serialize_key(step: _d.Step) -> str:
    """The mutual-exclusion domain for a step: its account (label prefix 'account-N').
    Steps sharing a key run strictly sequentially, in plan order."""
    return step.label.split("—", 1)[0].strip() or step.label


def _schedule(plan: list, *, jobs: int, run_one) -> tuple[list[tuple[str, str]], int]:
    """Bounded-parallel + per-account-serialized scheduler, shared by the bash and native paths.
    One ordered queue per serialize_key (account); a key has ≤1 in-flight step; up to `jobs`
    distinct keys run at once; same-key steps stay in plan order (→ acct-1 feed ordering).
    run_one(step) → (label, rc, dt). Results are collected in THIS thread (no lock needed).
    Returns (results, failures)."""
    queues: dict[str, list] = {}
    for s in plan:
        queues.setdefault(_serialize_key(s), []).append(s)
    results: list[tuple[str, str]] = []
    failures = 0
    active: dict = {}                       # Future -> serialize_key
    busy_keys: set[str] = set()
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        while queues or active:
            for key in list(queues):
                if len(active) >= jobs:
                    break
                if key in busy_keys or not queues[key]:
                    continue
                step = queues[key].pop(0)
                if not queues[key]:
                    del queues[key]
                busy_keys.add(key)
                active[ex.submit(run_one, step)] = key
                print(_d._c("y", f"▶ [{key}] {step.label}"))
            if not active:
                break
            done, _ = wait(list(active), return_when=FIRST_COMPLETED)
            for fut in done:
                key = active.pop(fut)
                busy_keys.discard(key)
                label, rc, dt = fut.result()
                mark = _d._c("g", "✓") if rc == 0 else _d._c("r", "✗")
                print(f"{mark} [{key}] {label}  ({dt:.0f}s)")
                results.append((label, "OK" if rc == 0 else "FAILED"))
                if rc != 0:
                    failures += 1
    return results, failures


def _print_summary(results: list[tuple[str, str]], failures: int, *, jobs: int, mode: str) -> int:
    print(_d._c("b", _d._c("y", f"\n═══ DEPLOY ({mode}, jobs={jobs}) — SUMMARY ({len(results)} steps) ═══")))
    for label, res in results:
        m = _d._c("g", "  ✓ ") if res == "OK" else _d._c("r", "  ✗ ")
        print(f"{m}{label}")
    if failures == 0:
        print(_d._c("g", _d._c("b", "\n  ALL STEPS OK")))
        return 0
    print(_d._c("r", _d._c("b", f"\n  {failures} STEP(S) FAILED")))
    return 1


def run_parallel(plan: list[_d.Step], forward: list[str], *, jobs: int, dry_run: bool) -> int:
    """(bash path) Schedule the plan; each step shells to its proven bash deployer."""
    if dry_run:
        queues: dict[str, list[_d.Step]] = {}
        for s in plan:
            queues.setdefault(_serialize_key(s), []).append(s)
        print(_d._c("b", f"Parallel plan (jobs={jobs}) — domains run concurrently, "
                         f"steps within a domain sequentially:"))
        for key, q in queues.items():
            print(_d._c("y", f"  [{key}]  (serial chain of {len(q)})"))
            for s in q:
                envp = " ".join(f"{k}={v}" for k, v in s.env.items())
                print(f"      · {s.label}" + (f"  {{{envp}}}" if envp else ""))
        return 0

    def _run(step: _d.Step) -> tuple[str, int, float]:
        cmd = ["bash", os.path.join(_d.REPO, step.script), *step.args, *forward]
        t0 = time.monotonic()
        rc = subprocess.run(cmd, cwd=_d.REPO, env={**os.environ, **step.env}, check=False).returncode
        return step.label, rc, time.monotonic() - t0

    results, failures = _schedule(plan, jobs=jobs, run_one=_run)
    return _print_summary(results, failures, jobs=jobs, mode="parallel")


def run_native(plan: list[NativeStep], *, jobs: int, dry_run: bool) -> int:
    """(native path — Phase E cutover) Schedule the plan; each step calls deploy_actions'
    deploy_family/deploy_infra IN-PROCESS (no bash). Same scheduler + per-account serialization.
    test_ports is auto-applied only for the test-account idx (deploy_actions' fail-closed guard)."""
    if dry_run:
        queues: dict[str, list[NativeStep]] = {}
        for s in plan:
            queues.setdefault(_serialize_key(s), []).append(s)
        print(_d._c("b", f"Native plan (jobs={jobs}) — deploy_actions in-process, per-account serialized:"))
        for key, q in queues.items():
            print(_d._c("y", f"  [{key}]  (serial chain of {len(q)})"))
            for s in q:
                print(f"      · {s.label} → {_native_call_str(s)}")
        return 0

    conf = _da.load_conf()
    sidx = conf.get("standalone_idx")

    def _run(step: NativeStep) -> tuple[str, int, float]:
        host = _da.Host(conf["users"][step.idx], conf["passwords"][step.idx], conf["server"], conf["port"])
        test_ports = sidx is not None and step.idx == sidx     # only the ephemeral test account
        t0 = time.monotonic()
        # deploy_family/deploy_infra assert on any failed action → catch so ONE step's failure
        # (e.g. a transient rsync blip) is recorded as FAILED, NOT propagated to crash the whole
        # engine mid-fleet (which would leave the summary + remaining steps unrun).
        try:
            if step.kind == "infra":
                rc = _da.deploy_infra(host, step.target, install_dir=step.install_dir, test_ports=test_ports)
            else:
                rc = _da.deploy_family(host, step.target, install_dir=step.install_dir,
                                       strategy=step.strategy, test_ports=test_ports)
        except Exception as e:                                 # noqa: BLE001 — deploy actions assert
            print(_d._c("r", f"  ✗ {step.label}: {type(e).__name__}: {e}"))
            rc = 1
        return step.label, rc, time.monotonic() - t0

    results, failures = _schedule(plan, jobs=jobs, run_one=_run)
    return _print_summary(results, failures, jobs=jobs, mode="native")


def main() -> int:
    ap = argparse.ArgumentParser(description="Bounded-parallel inventory-driven deploy engine (Phase A).")
    ap.add_argument("--jobs", type=int, default=2,
                    help="max concurrent steps / SSH sessions (default 2). Same-account steps "
                         "never overlap regardless of this cap.")
    ap.add_argument("--restart-infra", action="store_true",
                    help="also restart account-1 feeds/services (disrupts live_bots)")
    ap.add_argument("--only", metavar="TOKEN", default=None, help="keep steps whose label contains TOKEN")
    ap.add_argument("--exclude", metavar="TOKEN", default=None, help="drop steps whose label contains TOKEN")
    ap.add_argument("--list", action="store_true", help="print the parallel plan and exit")
    ap.add_argument("--native", action="store_true",
                    help="Phase-E cutover: deploy via deploy_actions (deploy_family/deploy_infra) "
                         "in-process instead of the bash engines. Combine with --list/--dry-run to "
                         "preview, and --only/--exclude to scope the blast radius.")
    ap.add_argument("--dry-run", action="store_true", help="print the schedule without executing")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="skip the pre/post heartbeat snapshots (faster; e.g. --verify-only runs)")
    args, forward = ap.parse_known_args()

    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2
    if not os.path.isfile(_d.INVENTORY):
        print(f"inventory not found: {_d.INVENTORY}", file=sys.stderr)
        return 2
    rows = _d.load_rows(_d.INVENTORY)
    if not rows:
        print("inventory has no [[bot]] rows", file=sys.stderr)
        return 2

    # ── Native path (Phase E cutover): deploy_actions in-process ────────────────────
    if args.native:
        nplan, skipped = build_native_exec_plan(rows)
        if skipped:
            print(_d._c("y", f"note: {len(skipped)} row(s) have no native target (bash-only, skipped): "
                             + ", ".join(skipped)))
        # Mirror the bash default: account-1 (idx 0) infra is NOT restarted unless --restart-infra
        # (deploy_infra always restarts — it has no rsync-only mode — so we EXCLUDE it here). Without
        # this gate, a bare `--native` would restart every feed on acct-1 → fleet-wide stale-WS blast
        # radius ([[feedback_deploy_feed_restart_stale_ws]]). Trading bots on acct-1 (none today) stay.
        if not args.restart_infra:
            gated = [s for s in nplan if s.idx == 0 and s.kind == "infra"]
            if gated:
                print(_d._c("y", f"note: {len(gated)} account-1 infra step(s) gated off "
                                 f"(pass --restart-infra to include; feed restart has fleet blast radius)"))
            nplan = [s for s in nplan if not (s.idx == 0 and s.kind == "infra")]
        if args.only:
            nplan = [s for s in nplan if args.only.lower() in s.label.lower()]
        if args.exclude:
            nplan = [s for s in nplan if args.exclude.lower() not in s.label.lower()]
        if not nplan:
            print("no native steps to run after filtering", file=sys.stderr)
            return 2
        if args.list or args.dry_run:
            return run_native(nplan, jobs=args.jobs, dry_run=True)
        if not args.no_snapshot:
            _d.run_heartbeat_snapshot("HEARTBEAT — PRE-DEPLOY SNAPSHOT")
        rc = run_native(nplan, jobs=args.jobs, dry_run=False)
        if not args.no_snapshot:
            _d.run_heartbeat_snapshot("HEARTBEAT — POST-DEPLOY SNAPSHOT")
        return rc

    # ── Bash path (proven engines) ──────────────────────────────────────────────────
    plan = _d.build_plan(rows, restart_infra=args.restart_infra)
    if args.only:
        plan = [s for s in plan if args.only.lower() in s.label.lower()]
    if args.exclude:
        plan = [s for s in plan if args.exclude.lower() not in s.label.lower()]
    if not plan:
        print("no steps to run after filtering", file=sys.stderr)
        return 2

    if args.list:
        return run_parallel(plan, forward, jobs=args.jobs, dry_run=True)

    if not args.dry_run and not args.no_snapshot:
        _d.run_heartbeat_snapshot("HEARTBEAT — PRE-DEPLOY SNAPSHOT")
    rc = run_parallel(plan, forward, jobs=args.jobs, dry_run=args.dry_run)
    if not args.dry_run and not args.no_snapshot:
        _d.run_heartbeat_snapshot("HEARTBEAT — POST-DEPLOY SNAPSHOT")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
