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

# Account-1 (idx 0) is infra with order-critical restart semantics — not naively
# derivable, so it stays a hardcoded block mirroring deploy_all.sh.
PM = "tradinebotte-polymarket/scripts"
STATUS_DIR = "tradinebotte-status/scripts"

_C = {"y": "\033[1;33m", "g": "\033[0;32m", "r": "\033[0;31m", "b": "\033[1m", "n": "\033[0m"}


def _c(key: str, s: str) -> str:
    return f"{_C[key]}{s}{_C['n']}" if sys.stdout.isatty() else s


@dataclass
class Step:
    label: str
    script: str                       # repo-relative path
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)   # deploy_env preset (Phase 2)
    display_only: str | None = None   # if set, a summary-only row (e.g. "RSYNC"), not run


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
    # A row is deployed by `deployer` + `deploy_env` (Phase 2: generic engine + preset)
    # or, for not-yet-migrated rows, by a standalone `deploy_script`. Dedup on the FULL
    # (script, env) pair — several rows now share one generic engine (update_standalone.sh)
    # with different presets (TEST_STANDALONE_USER_IDX), so deduping on script alone would
    # wrongly collapse them.
    seen: set[tuple] = set()
    for row in rows:
        if row.get("account_idx", 0) == 0:
            continue                      # account-1 handled above
        script = row.get("deployer") or row.get("deploy_script")
        env = {str(k): str(v) for k, v in (row.get("deploy_env") or {}).items()}
        if not script:
            continue
        key = (script, tuple(sorted(env.items())))
        if key in seen:
            continue
        seen.add(key)
        acct = f"account-{row['account_idx'] + 1}"
        bot = row.get("bot_name", "?")
        btype = row.get("bot_type", "")
        label = f"{acct} — {bot}" + (f" ({btype})" if btype else "")
        plan.append(Step(label, script, env=env))
    return plan


def run_heartbeat_snapshot(label: str) -> None:
    hb = os.path.join(REPO, STATUS_DIR, "heartbeat_status.sh")
    print(_c("y", f"\n─── {label} ───"))
    if not os.path.isfile(hb):
        print(_c("y", "  ! heartbeat_status.sh not found — skipping"))
        return
    subprocess.run(["bash", hb], cwd=REPO, check=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="Inventory-driven deploy orchestrator.")
    ap.add_argument("--restart-infra", action="store_true",
                    help="also restart account-1 feeds/services (disrupts live_bots ~30s)")
    ap.add_argument("--only", metavar="TOKEN", default=None,
                    help="only steps whose label contains TOKEN (e.g. account-2, live_bot)")
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

    if args.list:
        print(_c("b", f"Derived deploy plan ({len(plan)} steps) — from inventory.toml:"))
        for i, s in enumerate(plan, 1):
            envp = " ".join(f"{k}={v}" for k, v in s.env.items())
            extra = " ".join(s.args + forward)
            line = f"  {i:2}. {s.label:52} {s.script}"
            if envp:
                line += f"  {{{envp}}}"
            if extra:
                line += f"  [{extra}]"
            print(line)
        return 0

    run_heartbeat_snapshot("HEARTBEAT — PRE-DEPLOY SNAPSHOT") if not args.dry_run else None

    results: list[tuple[str, str]] = []
    failures = 0
    for s in plan:
        cmd = ["bash", os.path.join(REPO, s.script), *s.args, *forward]
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
