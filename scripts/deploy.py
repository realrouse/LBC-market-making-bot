#!/usr/bin/env python3
"""deploy.py — inventory-driven deploy orchestrator (Phase 1).

Replaces the hardcoded per-account `run_step` list in
`tradinebotte-cex/scripts/deploy_all.sh`: the account→deploy-script mapping is now
*derived* from `inventory.toml` (the single source of truth) instead of being a third
duplicate of the topology. Behaviour is otherwise identical to deploy_all.sh:

  * Sequential, one account at a time (never parallel — same host).
  * Account-1 stays a bespoke, order-critical block (feeds restarted exactly once,
    BEFORE account_bot; rsync-only unless --restart-infra) — kept as a special case in
    Phase 1 (see docs/audit-and-inventory-deploy-plan.md open question #3).
  * Accounts 2–6 run each inventory row's `deploy_script`, in file order (which already
    matches the current deploy order), deduped, with flags forwarded.

Phase 1 deliberately still calls the existing per-account wrapper scripts; Phase 2 moves
their env presets into inventory and deletes them.

Usage:
  python3 scripts/deploy.py                    # full deploy (rsync-only account-1)
  python3 scripts/deploy.py --restart-infra    # also restart account-1 feeds/services
  python3 scripts/deploy.py --skip-restart     # forwarded to each script (rsync only)
  python3 scripts/deploy.py --verify-only      # forwarded (status check, no changes)
  python3 scripts/deploy.py --list             # print the derived plan and exit (no exec)
  python3 scripts/deploy.py --dry-run          # print each step it WOULD run (no exec)

Exit: 0 = all steps OK, 1 = one or more failed, 2 = config error.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INVENTORY = os.path.join(REPO, "inventory.toml")

# Account-1 (idx 0) INFRA is order-critical (feeds before account_bot) — not naively
# derivable, so it stays a hardcoded block mirroring deploy_all.sh.
PM = "tradinebotte-polymarket/scripts"
STATUS_DIR = "tradinebotte-status/scripts"

# Scripts already run by the account-1 bespoke block (or deployed independently). Rows
# whose deploy target is one of these are skipped by the derivation — but a *trading* bot
# added to account-1 (a different deployer) IS derived, so scaling a family onto every
# account, incl. account-1, works without silently dropping it.
_BESPOKE_SCRIPTS = {"update_claude1.sh", "setup_data_plane.sh", "deploy_status_service.sh"}

# Every trading bot deploys natively (deployer=scripts/deploy_actions.py, --idx from account_idx);
# the last bash trading deployers (update_standalone / update_swing / deploy_grid_mexc) were retired
# 2026-07-18. acct-1 infra stays on its bespoke scripts, handled by the account-1 block below (never
# via _row_env). So there is no account-index injection left to do.


def _row_env(row: dict) -> dict[str, str]:
    """The explicit deploy_env for a row (used by both the plan builder and check_inventory's
    collision guard so they agree). No auto-injection: no bash trading deployer remains."""
    return {str(k): str(v) for k, v in (row.get("deploy_env") or {}).items()}

_C = {"y": "\033[1;33m", "g": "\033[0;32m", "r": "\033[0;31m", "b": "\033[1m", "n": "\033[0m"}


def _c(key: str, s: str) -> str:
    return f"{_C[key]}{s}{_C['n']}" if sys.stdout.isatty() else s


@dataclass
class Step:
    label: str
    script: str                       # repo-relative path
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)   # deploy_env preset (Phase 2)
    interpreter: str = "bash"         # "bash" (deploy scripts) | "python" (native deploy_actions.py)
    display_only: str | None = None   # if set, a summary-only row (e.g. "RSYNC"), not run


# ── Native (single-tree) dispatch ────────────────────────────────────────────
# A row deploys through the native declarative engine (scripts/deploy_actions.py) instead of
# a bash deployer iff its `deployer` IS deploy_actions.py AND its bot_type resolves to a native
# family target. This is EXPLICIT (keyed on the deployer path, not derived from bot_type) so the
# un-migrated primaries (poly/swing/mexc-grid), which have native family targets too, stay on
# their bash deployers until they are individually migrated.
_NATIVE_DEPLOYER = "deploy_actions.py"
_DEPLOY_ACTIONS = None


def _native_family(row: dict) -> str | None:
    """The native deploy family for a row that opts into native dispatch, else None.
    Opt-in = deployer basename is deploy_actions.py; family = deploy_actions.native_target(bot_type)
    when it resolves to a ('family', fam) target (infra is single-instance, never a family row)."""
    dep = row.get("deployer") or ""
    if os.path.basename(dep) != _NATIVE_DEPLOYER:
        return None
    global _DEPLOY_ACTIONS
    if _DEPLOY_ACTIONS is None:
        # lazy so deploy.py stays importable without deploy_actions on the path (mirrors
        # check_inventory's lazy import); deploy_actions is in this same scripts/ dir.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import deploy_actions as _da  # noqa: PLC0415
        _DEPLOY_ACTIONS = _da
    tgt = _DEPLOY_ACTIONS.native_target(row.get("bot_type", "") or "")
    return tgt[1] if tgt and tgt[0] == "family" else None


def _row_step(row: dict) -> Step | None:
    """The derived deploy Step for one inventory row (accounts 2..N), or None if the row has no
    deployer/deploy_script or is run by the account-1 bespoke block. The SINGLE source of the
    per-row step so build_plan and check_inventory's collision guard cannot drift."""
    script = row.get("deployer") or row.get("deploy_script")
    if not script:
        return None
    if os.path.basename(script) in _BESPOKE_SCRIPTS:
        return None                       # already run by the account-1 bespoke block
    acct = f"account-{row['account_idx'] + 1}"
    bot = row.get("bot_name", "?")
    btype = row.get("bot_type", "")
    label = f"{acct} — {bot}" + (f" ({btype})" if btype else "")
    fam = _native_family(row)
    if fam:
        # native single-tree deploy: python3 deploy_actions.py <fam> --idx N --strategy S
        # [--single-tree]. account_idx supplies --idx (no per-account env var); the strategy is
        # the row's `strategy` field. The argv (family + idx + strategy) is what makes two native
        # rows on ONE account distinct (e.g. idx-2 accumulation vs grid_binance) — see _step_key.
        args = [fam, "--idx", str(row["account_idx"]),
                "--strategy", str(row.get("strategy", ""))]
        if row.get("single_tree"):
            args.append("--single-tree")
        return Step(label, script, args=args, env={}, interpreter="python")
    return Step(label, script, env=_row_env(row))


def _step_key(step: Step) -> tuple:
    """Dedup identity of a step. Includes `args` (not just script+env) so two native rows sharing
    deploy_actions.py + an account index but differing on family/strategy are NOT collapsed —
    the case idx-2 accumulation + binance-grid, which (script, env) or (script, env, idx) would
    silently merge into one step, dropping a bot. Additive for bash rows (args=[])."""
    return (step.interpreter, step.script,
            tuple(sorted(step.env.items())), tuple(step.args))


def load_rows(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        return tomllib.load(fh).get("bot", [])


def build_plan(rows: list[dict], *, restart_infra: bool) -> list[Step]:
    """Ordered deploy plan. Account-1 = bespoke block; accounts 2..N derived from inventory."""
    plan: list[Step] = []

    # ── Account-1 (idx 0): bespoke, order-critical (mirrors deploy_all.sh) ──────────
    if restart_infra:
        # feed restarted exactly ONCE and BEFORE account_bot, else the 15M consumer
        # orphans on a dead feed (the recurring stale-feed gotcha).
        plan.append(Step("account-1 — indicators (rsync + restart)",
                         f"{PM}/update_claude1.sh", ["--restart-indicators"]))
        plan.append(Step("account-1 — data plane (feeds: 15M + feed5m + cex_feed)",
                         f"{STATUS_DIR}/setup_data_plane.sh"))
        plan.append(Step("account-1 — account_bot (after feeds stable)",
                         f"{PM}/update_claude1.sh", ["--restart-account"]))
    else:
        plan.append(Step("account-1 — rsync (indicators + feed + account_bot)",
                         f"{PM}/update_claude1.sh", ["--skip-restart"]))

    # ── Accounts 2..N: derived from inventory, file order ───────────────────────────
    # A row is deployed by `deployer` + `deploy_env` (Phase 2: generic engine + preset), by the
    # native single-tree engine (deployer=deploy_actions.py → a python step), or, for
    # not-yet-migrated rows, by a standalone `deploy_script`. Dedup on the full step identity
    # (_step_key: interpreter+script+env+args) — several rows share one generic engine
    # (update_standalone.sh) with different presets, and two native rows can share deploy_actions.py
    # with the same account index but different family/strategy, so deduping on script alone (or
    # script+env) would wrongly collapse them.
    seen: set[tuple] = set()
    for row in rows:
        step = _row_step(row)
        if step is None:
            continue
        key = _step_key(step)
        if key in seen:
            continue
        seen.add(key)
        plan.append(step)
    return plan


def run_heartbeat_snapshot(label: str) -> None:
    hb = os.path.join(REPO, STATUS_DIR, "heartbeat_status.sh")
    print(_c("y", f"\n─── {label} ───"))
    if not os.path.isfile(hb):
        print(_c("y", "  ! heartbeat_status.sh not found — skipping"))
        return
    subprocess.run(["bash", hb], cwd=REPO, check=False)


def run_inventory_sync() -> bool:
    """Post-deploy: reconcile the shared-DB `inventory` table with inventory.toml (the committed
    source of truth) so fields like is_live can never silently drift from it — they did, once:
    is_live sat at 0 in the DB for the real-money bot for months because sync_inventory was only
    ever run by hand. Idempotent; runs on the operator, which can write the shared DB. Best-effort:
    a failure is reported but does NOT fail the deploy, whose real work already succeeded."""
    # sync_inventory.py lives in tradinebotte-status/ (NOT the scripts/ subdir STATUS_DIR points at).
    script = os.path.join(REPO, "tradinebotte-status", "sync_inventory.py")
    print(_c("y", "\n─── RECONCILE INVENTORY → shared DB (sync_inventory) ───"))
    if not os.path.isfile(script):
        print(_c("y", "  ! sync_inventory.py not found — skipping"))
        return True
    rc = subprocess.run([sys.executable, script], cwd=REPO, check=False).returncode
    if rc == 0:
        print(_c("g", "  ✓ inventory reconciled with inventory.toml"))
        return True
    print(_c("r", "  ✗ inventory sync FAILED — deploy is still OK; re-run sync_inventory.py by hand"))
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Inventory-driven deploy orchestrator.")
    ap.add_argument("--restart-infra", action="store_true",
                    help="also restart account-1 feeds/services (disrupts live_bots ~30s)")
    ap.add_argument("--only", metavar="TOKEN", default=None,
                    help="only steps whose label contains TOKEN (e.g. account-2, live_bot)")
    ap.add_argument("--exclude", metavar="TOKEN", default=None,
                    help="drop steps whose label contains TOKEN (e.g. accumulation); "
                         "applied after --only")
    ap.add_argument("--list", action="store_true", help="print the derived plan and exit")
    ap.add_argument("--dry-run", action="store_true",
                    help="print each step that would run, without executing")
    # Everything else (e.g. --skip-restart, --verify-only) is forwarded to each script.
    args, forward = ap.parse_known_args()

    if not os.path.isfile(INVENTORY):
        print(f"inventory not found: {INVENTORY}", file=sys.stderr)
        return 2
    rows = load_rows(INVENTORY)
    if not rows:
        print("inventory has no [[bot]] rows", file=sys.stderr)
        return 2

    plan = build_plan(rows, restart_infra=args.restart_infra)
    if args.only:
        plan = [s for s in plan if args.only.lower() in s.label.lower()]
        if not plan:
            print(f"--only {args.only!r} matched no steps", file=sys.stderr)
            return 2
    if args.exclude:
        plan = [s for s in plan if args.exclude.lower() not in s.label.lower()]
        if not plan:
            print(f"--exclude {args.exclude!r} dropped every step", file=sys.stderr)
            return 2

    if args.list:
        print(_c("b", f"Derived deploy plan ({len(plan)} steps) — from inventory.toml:"))
        for i, s in enumerate(plan, 1):
            envp = " ".join(f"{k}={v}" for k, v in s.env.items())
            extra = " ".join(s.args + forward)
            tag = "py " if s.interpreter == "python" else ""
            line = f"  {i:2}. {s.label:52} {tag}{s.script}"
            if envp:
                line += f"  {{{envp}}}"
            if extra:
                line += f"  [{extra}]"
            print(line)
        return 0

    if not args.dry_run:
        run_heartbeat_snapshot("HEARTBEAT — PRE-DEPLOY SNAPSHOT")

    results: list[tuple[str, str]] = []
    failures = 0
    for s in plan:
        # native steps run under the SAME interpreter driving deploy.py (sys.executable — the
        # project .venv the shim picked, ≥3.11), bash steps under bash. Forwarded flags
        # (--verify-only/--skip-restart) are accepted by both the bash deployers and
        # deploy_actions.py (which grew --skip-restart for exactly this uniform forwarding).
        launcher = [sys.executable] if s.interpreter == "python" else ["bash"]
        cmd = [*launcher, os.path.join(REPO, s.script), *s.args, *forward]
        run_env = {**os.environ, **s.env}
        print(_c("y", f"\n▶▶▶ {s.label} ▶▶▶"))
        if args.dry_run:
            envp = " ".join(f"{k}={v}" for k, v in s.env.items())
            print("  would run:", (f"env {envp} " if envp else "") + " ".join(cmd))
            results.append((s.label, "DRY"))
            continue
        rc = subprocess.run(cmd, cwd=REPO, env=run_env, check=False).returncode
        results.append((s.label, "OK" if rc == 0 else "FAILED"))
        if rc != 0:
            failures += 1

    # Reconcile inventory from toml after a real deploy (skip verify-only — it changes nothing).
    # Runs regardless of step failures: it syncs DESIRED state, which is correct even if a step
    # failed. Non-fatal to the deploy's own exit status.
    if not args.dry_run and "--verify-only" not in forward:
        run_inventory_sync()

    if not args.dry_run:
        run_heartbeat_snapshot("HEARTBEAT — POST-DEPLOY SNAPSHOT")

    print(_c("b", _c("y", f"\n═══ DEPLOY — SUMMARY ({len(plan)} steps) ═══")))
    for label, res in results:
        mark = {"OK": _c("g", "  ✓ "), "FAILED": _c("r", "  ✗ "),
                "DRY": _c("y", "  · ")}.get(res, "  ? ")
        print(f"{mark}{label}")

    if failures == 0:
        print(_c("g", _c("b", "\n  ALL STEPS OK" if not args.dry_run else "\n  DRY-RUN COMPLETE")))
        return 0
    print(_c("r", _c("b", f"\n  {failures} STEP(S) FAILED — check output above")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
