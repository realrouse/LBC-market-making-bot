#!/usr/bin/env python3
"""check_inventory.py — validate inventory.toml against the repo and (optionally) live hosts.

This is both the correctness gate for the single source of truth AND the inverse of the
anti-drift check: it fails if the inventory describes something that does not exist
(a missing deploy_script, or a service_unit absent from the live host).

Offline checks (default, no network):
    - required fields present; account_idx is an int; kind in {bot, service}; is_live bool
    - (account_idx, bot_name) pairs are unique
    - deploy_script paths exist in the repo
    - deploy-pipeline drift: inventory deploy_scripts match the scripts deploy_all.sh
      invokes (bidirectional, minus _PIPELINE_EXCEPTIONS deployed independently)

Live checks (--live, sequential SSH per account — never parallel, same-server rule):
    - account_idx resolves to a real account via TEST_USERS in the conf
    - each row's service_unit ({account} substituted) appears in
      `systemctl --user list-units 'tradinebotte-*'`
    - heartbeat-key drift: every (account, bot_name) actually present in the shared DB
      is in the inventory (a running bot absent from the source of truth → FAIL); an
      inventory 'bot' with no heartbeat is a warning (never-deployed OR key mismatch).

Exit code 0 = all good; 1 = at least one problem (printed).

Usage:
    python3 tradinebotte-status/check_inventory.py [--inventory PATH]
    python3 tradinebotte-status/check_inventory.py --live [--conf ~/.tradinebotte-test.conf]
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tomllib

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
DEFAULT_INVENTORY = os.path.join(_REPO, "inventory.toml")
DEPLOY_ALL = os.path.join(_REPO, "tradinebotte-cex", "scripts", "deploy_all.sh")
# Scripts excluded from the inventory<->deploy_all set-compare (both directions):
#   deploy_status_service.sh — in inventory, deployed independently (not via deploy_all)
#   heartbeat_status.sh      — invoked by deploy_all as a status snapshot, not a bot deploy
#   setup_data_plane.sh      — installs the shared feeds (feed5m/cex_feed) independently
_PIPELINE_EXCEPTIONS = {"deploy_status_service.sh", "heartbeat_status.sh",
                        "setup_data_plane.sh"}

_REQUIRED = ("account_idx", "bot_name", "kind")
_KINDS = {"bot", "service"}

# Shared state DB on the collector account — read for the heartbeat-key drift check.
# Override via env if relocated.
COLLECTOR_DB = os.environ.get(
    "TRADINEBOTTE_DB", "/data1/tradinebotte-shared/database/tradinebotte.db"
)


def load_rows(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        return tomllib.load(fh).get("bot", [])


def check_offline(rows: list[dict]) -> list[str]:
    problems: list[str] = []
    seen: set[tuple[int, str]] = set()
    for i, r in enumerate(rows):
        tag = f"row {i} (idx={r.get('account_idx')}/{r.get('bot_name','?')})"
        for f in _REQUIRED:
            if r.get(f) is None:
                problems.append(f"{tag}: missing required field '{f}'")
        if not isinstance(r.get("account_idx"), int) or r.get("account_idx", -1) < 0:
            problems.append(f"{tag}: account_idx must be a non-negative int")
        if r.get("kind") not in _KINDS:
            problems.append(f"{tag}: kind '{r.get('kind')}' not in {_KINDS}")
        if "is_live" in r and not isinstance(r["is_live"], bool):
            problems.append(f"{tag}: is_live must be true/false, got {r['is_live']!r}")
        key = (r.get("account_idx"), r.get("bot_name", ""))
        if key in seen:
            problems.append(f"{tag}: duplicate (account_idx, bot_name) {key}")
        seen.add(key)
        ds = r.get("deploy_script")
        if ds and not os.path.isfile(os.path.join(_REPO, ds)):
            problems.append(f"{tag}: deploy_script not found in repo: {ds}")
    return problems


# ─── Deploy-pipeline drift (offline, repo-only) ──────────────────────────────

def check_deploy_pipeline(rows: list[dict]) -> list[str]:
    """Bidirectional set-compare: inventory deploy_scripts vs scripts deploy_all.sh invokes.

    Catches "added a bot to inventory but forgot the pipeline" and the reverse, without
    parsing deploy_all's orchestration.  Scripts in _PIPELINE_EXCEPTIONS are deployed
    independently and are not expected in deploy_all.sh.
    """
    problems: list[str] = []
    if not os.path.isfile(DEPLOY_ALL):
        return [f"deploy_all.sh not found at {DEPLOY_ALL}"]
    with open(DEPLOY_ALL, encoding="utf-8") as fh:
        src = fh.read()

    inv_scripts = {os.path.basename(r["deploy_script"])
                   for r in rows if r.get("deploy_script")}
    # deploy_all invokes scripts as "$PM/foo.sh" / "$CEX/foo.sh" / "$STATUS/foo.sh"
    invoked = set(re.findall(r'\$(?:PM|CEX|STATUS)/([a-z0-9_]+\.sh)', src))

    for s in sorted(inv_scripts - _PIPELINE_EXCEPTIONS - invoked):
        problems.append(f"inventory deploy_script not invoked by deploy_all.sh: {s}")
    for s in sorted(invoked - _PIPELINE_EXCEPTIONS - inv_scripts):
        problems.append(f"deploy_all.sh invokes a script absent from inventory: {s}")
    return problems


# ─── Live (sequential SSH) ───────────────────────────────────────────────────

def _conf_array(conf: str, var: str) -> list[str]:
    """Read a bash array from the conf by sourcing it in a subshell."""
    out = subprocess.run(
        ["bash", "-c", f'source "{conf}"; printf "%s\\n" "${{{var}[@]}}"'],
        capture_output=True, text=True,
    )
    return [x for x in out.stdout.splitlines() if x]


def _conf_scalar(conf: str, var: str) -> str:
    out = subprocess.run(
        ["bash", "-c", f'source "{conf}"; printf "%s" "${{{var}}}"'],
        capture_output=True, text=True,
    )
    return out.stdout.strip()


def _resolve(rows: list[dict], users: list[str]) -> tuple[list[dict], list[str]]:
    """Resolve account_idx → account name and substitute {account} in service_unit."""
    resolved, problems = [], []
    for r in rows:
        idx = r.get("account_idx")
        if not isinstance(idx, int) or idx >= len(users):
            problems.append(f"row {r.get('bot_name')!r}: account_idx={idx} out of range")
            continue
        out = dict(r)
        out["account"] = users[idx]
        if out.get("service_unit"):
            out["service_unit"] = out["service_unit"].replace("{account}", users[idx])
        resolved.append(out)
    return resolved, problems


def check_live(rows: list[dict], conf: str) -> list[str]:
    if not os.path.isfile(conf):
        return [f"--live: conf not found: {conf}"]
    server = _conf_scalar(conf, "TEST_SERVER")
    port = _conf_scalar(conf, "TEST_PORT") or "22"
    users = _conf_array(conf, "TEST_USERS")
    passwords = _conf_array(conf, "TEST_PASSWORDS")
    user_idx = {u: i for i, u in enumerate(users)}

    rows, problems = _resolve(rows, users)

    # group expected units per account, then one sequential SSH each
    by_account: dict[str, set[str]] = {}
    for r in rows:
        if r.get("service_unit"):
            by_account.setdefault(r["account"], set()).add(r["service_unit"])

    for account in sorted(by_account):
        if account not in user_idx:
            problems.append(f"{account}: not in TEST_USERS — cannot live-check")
            continue
        idx = user_idx[account]
        units = _remote_units(server, port, users[idx], passwords[idx])
        if units is None:
            problems.append(f"{account}: SSH/systemctl unreachable")
            continue
        for unit in sorted(by_account[account]):
            if unit not in units:
                problems.append(f"{account}: expected unit not active/loaded: {unit}")

    problems += check_heartbeat_keys(rows, server, port, users, passwords)
    problems += check_mode_mismatch(rows, server, port, users, passwords)
    return problems


def check_mode_mismatch(rows, server, port, users, passwords) -> list[str]:
    """Compare inventory.is_live (declared intent) with the mode each bot self-reports.

    is_live is intent; the heartbeat `mode` field is observed truth.  A bot reporting a
    mode that contradicts its declared is_live is flagged.  Bots that don't yet report a
    mode (mode-reporting not deployed) are silently skipped — not an error.
    """
    problems: list[str] = []
    if not users:
        return problems
    modes = _remote_heartbeat_modes(server, port, users[0], passwords[0], COLLECTOR_DB)
    if modes is None:
        return problems
    for r in rows:
        if r.get("kind", "bot") != "bot" or r.get("is_live") is None:
            continue
        expected = "live" if r["is_live"] else "sim"
        reported = modes.get((r["account"], r["bot_name"]))
        if reported and reported != expected:
            problems.append(
                f"is_live mismatch: {r['account']}/{r['bot_name']} "
                f"inventory declares {expected}, bot reports {reported}"
            )
    return problems


def _remote_heartbeat_modes(server, port, user, password, db_path) -> dict | None:
    """{(account, bot_name): mode} from the latest heartbeat payload (mode may be absent)."""
    py = (
        "import sqlite3,json; "
        f"d=sqlite3.connect('{db_path}'); "
        "rows=d.execute('SELECT account,bot_name,payload,max(ts) FROM heartbeats "
        "GROUP BY account,bot_name').fetchall(); "
        "[print(a+chr(9)+b+chr(9)+str((json.loads(p) if p else {}).get('mode') or '')) "
        "for a,b,p,_ in rows]"
    )
    env = dict(os.environ, SSHPASS=password)
    out = subprocess.run(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=10", "-o", "PreferredAuthentications=password",
         "-p", port, f"{user}@{server}", f"python3 -c \"{py}\""],
        capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        return None
    modes = {}
    for ln in out.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) == 3:
            modes[(parts[0], parts[1])] = parts[2] or None
    return modes


def check_heartbeat_keys(rows, server, port, users, passwords) -> list[str]:
    """Inverse-drift check: inventory (account, bot_name) vs live heartbeat keys.

    Queried once on the collector account (TEST_USERS[0]).  A heartbeat key missing from
    the inventory is real drift (FAIL); an inventory 'bot' with no heartbeat is a warning.
    """
    problems: list[str] = []
    if not users:
        return ["heartbeat check: no TEST_USERS"]
    live = _remote_heartbeat_keys(server, port, users[0], passwords[0], COLLECTOR_DB)
    if live is None:
        print("  (heartbeat check skipped — collector DB unreachable)")
        return problems
    inv = {(r["account"], r["bot_name"]) for r in rows}
    inv_bots = {(r["account"], r["bot_name"]) for r in rows if r.get("kind", "bot") == "bot"}

    for key in sorted(live - inv):
        problems.append(f"heartbeat key not in inventory (drift): {key[0]}/{key[1]}")
    for key in sorted(inv_bots - live):
        print(f"  warning: inventory bot has no heartbeat (never-deployed or key mismatch): "
              f"{key[0]}/{key[1]}")
    return problems


def _remote_units(server: str, port: str, user: str, password: str) -> set[str] | None:
    cmd = (
        "export XDG_RUNTIME_DIR=/run/user/$(id -u); "
        "systemctl --user list-units 'tradinebotte-*' --no-legend --plain --all "
        "2>/dev/null | awk '{print $1}'"
    )
    env = dict(os.environ, SSHPASS=password)
    out = subprocess.run(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=10", "-o", "PreferredAuthentications=password",
         "-p", port, f"{user}@{server}", cmd],
        capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        return None
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def _remote_heartbeat_keys(server, port, user, password, db_path) -> set | None:
    py = (
        "import sqlite3,sys; "
        f"d=sqlite3.connect('{db_path}'); "
        "[print(a+'\\t'+b) for a,b in "
        "d.execute('SELECT DISTINCT account,bot_name FROM heartbeats')]"
    )
    env = dict(os.environ, SSHPASS=password)
    out = subprocess.run(
        ["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=10", "-o", "PreferredAuthentications=password",
         "-p", port, f"{user}@{server}", f"python3 -c \"{py}\""],
        capture_output=True, text=True, env=env,
    )
    if out.returncode != 0:
        return None
    keys = set()
    for ln in out.stdout.splitlines():
        if "\t" in ln:
            a, b = ln.split("\t", 1)
            keys.add((a, b))
    return keys


def main() -> None:
    p = argparse.ArgumentParser(description="Validate inventory.toml")
    p.add_argument("--inventory", default=DEFAULT_INVENTORY)
    p.add_argument("--live", action="store_true", help="also check live systemctl per account")
    p.add_argument("--conf", default=os.path.expanduser("~/.tradinebotte-test.conf"))
    args = p.parse_args()

    rows = load_rows(args.inventory)
    if not rows:
        print(f"FAIL: no [[bot]] entries in {args.inventory}")
        sys.exit(1)

    problems = check_offline(rows) + check_deploy_pipeline(rows)
    if args.live:
        problems += check_live(rows, args.conf)

    if problems:
        print(f"FAIL: {len(problems)} problem(s) in {args.inventory}:")
        for pb in problems:
            print(f"  - {pb}")
        sys.exit(1)
    mode = "offline+live" if args.live else "offline"
    print(f"OK: {len(rows)} inventory rows valid ({mode})")


if __name__ == "__main__":
    main()
