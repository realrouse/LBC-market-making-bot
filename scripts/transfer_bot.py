#!/usr/bin/env python3
"""transfer_bot.py — move a native single-tree trading bot from one account to another.

The cross-account twin of `deploy_actions.py --migrate` (which moves a bot's layout WITHIN one
account). Here we carry a bot's STATE (live_<role>.db + bot_id_<role>) ACROSS accounts, redeploy it
natively on the target, and retire it on the source — so its identity (generated bot_id, the status
page join key) and its trade history follow it, and nothing is left running on the source.

Use case: empty a mixed account down to infra only (e.g. move every trading bot off account-1 so it
runs feeds/collector/indicators exclusively).

    python3 scripts/transfer_bot.py --bot <bot_name> --to <target_idx> [--dry-run] [--force]

Source account_idx is read from inventory.toml by bot_name. NATIVE-deployer bots only
(deployer = scripts/deploy_actions.py). Refuses is_live bots unless --force. After a successful
transfer it prints the one-line inventory.toml edit to commit (account_idx <src> -> <to>).
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11 (tomllib is stdlib only in 3.11+)
    import tomli as tomllib

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
import deploy_actions as da  # noqa: E402  (Host, FAMILIES, native_target, deploy_family, act_*)

INVENTORY = os.path.join(REPO, "inventory.toml")
INSTALL_DIR = "~/tradinebotte"
SHARED_DB = os.environ.get("TRADINEBOTTE_DB", "/data1/tradinebotte-shared/database/tradinebotte.db")


def _reconcile_shared_db(old_acct: str, new_acct: str, bot_name: str, dry: bool) -> None:
    """Keep the shared state DB consistent after the move (both tables key on (account, bot_name)):
      - heartbeats: DELETE the source's rows — they are stale liveness pings that would otherwise
        show the bot DEAD on its OLD account and inflate the statuspage issue count.
      - bot_trades + deploys: re-account source→target so the bot's history stays under one account.
    Runs under `sg claudes` (the shared dir is group-writable by claudes, per the collector)."""
    sql = (f"DELETE FROM heartbeats WHERE account='{old_acct}' AND bot_name='{bot_name}';"
           f"UPDATE bot_trades SET account='{new_acct}' WHERE account='{old_acct}' AND bot_name='{bot_name}';"
           f"UPDATE deploys SET account='{new_acct}' WHERE account='{old_acct}' AND bot_name='{bot_name}';")
    if dry:
        print(f"  [DRY-RUN] shared-DB reconcile: {sql}")
        return
    cmd = ["sg", "claudes", "-c", f"umask 002; sqlite3 '{SHARED_DB}' \"{sql}\""]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    print(f"  ▸ shared-DB reconciled (purged stale heartbeats on {old_acct}, re-accounted trades/deploys)"
          + (f"  [warn: {r.stderr.strip()}]" if r.returncode else ""))


def _rp(p: str) -> str:
    """Quote a remote path for the shell while letting a leading ~ expand."""
    return p if p.startswith("~") else f"'{p}'"


def _find_bot(bot_name: str) -> dict:
    with open(INVENTORY, "rb") as f:
        rows = tomllib.load(f)["bot"]
    for r in rows:
        if r.get("bot_name") == bot_name:
            return r
    sys.exit(f"bot {bot_name!r} not found in inventory.toml")


def _state_files(role: str) -> list[str]:
    # config_<role>.json is rewritten by the deploy (self-contained), so it is NOT carried.
    return [f"live_{role}.db", f"live_{role}.db-wal", f"live_{role}.db-shm", f"bot_id_{role}"]


def _copy_state(src: da.Host, tgt: da.Host, role: str, tmp: str) -> None:
    """Pull each state file source→deployer tmp, then push tmp→target. Same host, two OS users, so
    the deployer is the only party that can read both homes. bot_id is INCLUDED (unlike Host.rsync)."""
    os.makedirs(tmp, exist_ok=True)
    for f in _state_files(role):
        local = os.path.join(tmp, f)
        # pull (source → deployer); a missing -wal/-shm is fine (rsync just copies nothing)
        _rsync(f"{src.user}@{src.server}:{INSTALL_DIR}/{f}", local, src)
        if not os.path.exists(local):
            continue
        # push (deployer → target)
        _rsync(local, f"{tgt.user}@{tgt.server}:{INSTALL_DIR}/{f}", tgt)


def _rsync(a: str, b: str, host: da.Host) -> None:
    cmd = ["/usr/bin/sshpass", "-e", "rsync", "-az",
           "-e", f"ssh -p {host.port} {' '.join(da.Host.SSH_OPTS)}", a, b]
    env = {**os.environ, "SSHPASS": host.password}
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)
    # rc 23/24 = "some files vanished / partial" — benign for optional -wal/-shm; only hard-fail on 1-12
    if r.returncode not in (0, 23, 24):
        sys.exit(f"rsync failed ({a} -> {b}): rc={r.returncode}\n{r.stderr}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Move a native single-tree bot between accounts.")
    ap.add_argument("--bot", required=True, help="bot_name (inventory join key)")
    ap.add_argument("--to", type=int, required=True, metavar="IDX", help="target account index")
    ap.add_argument("--from", type=int, dest="src", default=None, metavar="IDX",
                    help="source account index (default: from inventory; pass it if inventory is stale, "
                         "e.g. to move a bot back before the inventory edit is committed)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--force", action="store_true", help="allow transferring an is_live (real-money) bot")
    a = ap.parse_args()

    row = _find_bot(a.bot)
    src_idx = a.src if a.src is not None else row["account_idx"]
    if src_idx == a.to:
        sys.exit("source and target are the same account")
    if os.path.basename(row.get("deployer", "")) != "deploy_actions.py":
        sys.exit(f"{a.bot} is not a native-deployer bot "
                f"(deployer={row.get('deployer')!r}); transfer supports native only")
    if row.get("is_live") and not a.force:
        sys.exit(f"{a.bot} is is_live=true (real money). Re-run with --force to transfer it.")

    tgt = da.native_target(row.get("bot_type", ""))
    if not tgt or tgt[0] != "family":
        sys.exit(f"{a.bot} bot_type={row.get('bot_type')!r} is not a native FAMILY (infra/multibot not supported)")
    family = tgt[1]
    spec = da.FAMILIES[family]
    role, unit = spec["role"], spec["unit"]
    strategy = row.get("strategy", "")

    conf = da.load_conf()
    if a.to >= len(conf["users"]) or src_idx >= len(conf["users"]):
        sys.exit("account index out of range")
    src = da.Host(conf["users"][src_idx], conf["passwords"][src_idx], conf["server"], conf["port"])
    tgt_h = da.Host(conf["users"][a.to], conf["passwords"][a.to], conf["server"], conf["port"])

    print(f"▶ transfer {a.bot}  ({family}/{role})  acct-{src_idx + 1} → acct-{a.to + 1}")
    print(f"    unit={unit}  strategy={strategy}  state={_state_files(role)}")

    # collision guard: the target must not already run this unit
    r = tgt_h.ssh(f"systemctl --user is-active {unit} 2>/dev/null || echo inactive")
    if "active" == r.stdout.strip():
        sys.exit(f"target acct-{a.to + 1} already runs {unit} — refusing to clobber it")

    if a.dry_run:
        print("  [DRY-RUN] would: stop on source → copy state → deploy native on target → verify → "
              "clean source → reconcile shared DB → print inventory edit")
        _reconcile_shared_db(conf["users"][src_idx], conf["users"][a.to], a.bot, dry=True)
        return 0

    tmp = f"/tmp/transfer_{a.bot}"
    # 1. stop on source (a live SQLite/-wal copy can tear if the writer is running)
    print("  ▸ stop on source"); src.ssh(f"systemctl --user stop {unit}")
    # 2. carry state across
    print("  ▸ copy state (live_<role>.db + bot_id)"); _copy_state(src, tgt_h, role, tmp)
    # 3. deploy natively on the target (rewrites config, restarts on the carried DB + bot_id)
    print("  ▸ deploy on target")
    rc = da.deploy_family(tgt_h, family, install_dir=INSTALL_DIR, strategy=strategy, single_tree=True)
    if rc != 0:
        sys.exit(f"target deploy failed (rc={rc}) — source bot is STOPPED; re-start it or re-run")
    # 4. verify identity carried
    new_id = da.act_read_bot_id(tgt_h, INSTALL_DIR, role)
    print(f"  ▸ target bot_id = {new_id}  (must equal the source's generated id: {a.bot})")
    # 5. retire on source: clear the single-tree drop-in, remove the bot's state, disable the unit
    print("  ▸ retire on source (disable unit, clear drop-in, remove state)")
    da.act_single_tree_dropin(src, unit, "")   # clears the drop-in (idempotent)
    files = " ".join(f"{INSTALL_DIR}/{f}" for f in [*_state_files(role), f"config_{role}.json"])
    src.ssh(f"systemctl --user disable {unit} 2>/dev/null; rm -f {files}; echo cleaned")
    # 6. reconcile the shared state DB so the statuspage shows the bot only on its new account
    _reconcile_shared_db(conf["users"][src_idx], conf["users"][a.to], a.bot, dry=False)

    print(f"\n✅ transferred. NOW edit inventory.toml: {a.bot} account_idx {src_idx} → {a.to}, then run "
          f"`sync_inventory.py` + `check_inventory.py`, update tests/test_deploy.py (plan order), commit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
