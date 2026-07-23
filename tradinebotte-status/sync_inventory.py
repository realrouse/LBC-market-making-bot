#!/usr/bin/env python3
"""sync_inventory.py — upsert the git-tracked inventory.toml into the shared DB.

Reads the desired-state topology from inventory.toml (the single source of truth),
resolves each account_idx to the real account name via TEST_USERS in the (untracked)
conf, and upserts into the `inventory` table.  Idempotent: re-running changes nothing
but updated_ts.  Run after editing inventory.toml, and as a deploy step so the DB
always matches the committed topology.

Account names are deliberately NOT in inventory.toml (infra identifiers stay out of
git); they are resolved here so the DB `inventory.account` matches `heartbeats.account`.

Usage:
    python3 tradinebotte-status/sync_inventory.py [--inventory PATH] [--db PATH]
                                                  [--conf PATH] [--dry-run]

Defaults:
    --inventory  <repo>/inventory.toml
    --db         $TRADINEBOTTE_DB or /data1/tradinebotte-shared/database/tradinebotte.db
    --conf       $TEST_MULTIBOT_CONF or ~/.tradinebotte-test.conf
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

# Make `tradinetools` importable whether run from the repo or a deployed install dir.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_HERE, "tradinetools"),                       # deployed layout
              os.path.join(os.path.dirname(_HERE), "tradinetools")):     # repo layout
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break
from tradinetools.db import open_db, upsert_inventory  # noqa: E402

DEFAULT_DB = os.environ.get(
    "TRADINEBOTTE_DB", "/data1/tradinebotte-shared/database/tradinebotte.db"
)
DEFAULT_INVENTORY = os.path.join(os.path.dirname(_HERE), "inventory.toml")
DEFAULT_CONF = os.environ.get(
    "TEST_MULTIBOT_CONF", os.path.expanduser("~/.tradinebotte-test.conf")
)


def conf_users(conf: str) -> list[str]:
    """Read the TEST_USERS bash array by sourcing the conf in a subshell."""
    out = subprocess.run(
        ["bash", "-c", f'source "{conf}"; printf "%s\\n" "${{TEST_USERS[@]}}"'],
        capture_output=True, text=True,
    )
    return [x for x in out.stdout.splitlines() if x]


def load_inventory(path: str) -> list[dict]:
    """Parse inventory.toml → list of bot/service rows (the [[bot]] array)."""
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    rows = data.get("bot", [])
    if not rows:
        raise SystemExit(f"{path}: no [[bot]] entries found")
    return rows


def resolve(rows: list[dict], users: list[str]) -> list[dict]:
    """Resolve account_idx → real account name and substitute {account} in service_unit."""
    resolved = []
    for r in rows:
        idx = r.get("account_idx")
        if idx is None or not isinstance(idx, int) or idx >= len(users):
            raise SystemExit(
                f"inventory row {r.get('bot_name')!r}: account_idx={idx} "
                f"out of range (TEST_USERS has {len(users)} entries)"
            )
        account = users[idx]
        out = dict(r)
        out["account"] = account
        if out.get("service_unit"):
            out["service_unit"] = out["service_unit"].replace("{account}", account)
        resolved.append(out)
    return resolved


def main() -> None:
    p = argparse.ArgumentParser(description="Sync inventory.toml into the shared DB")
    p.add_argument("--inventory", default=DEFAULT_INVENTORY)
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("--conf", default=DEFAULT_CONF)
    p.add_argument("--dry-run", action="store_true",
                   help="parse + resolve + report, write nothing")
    args = p.parse_args()

    users = conf_users(args.conf)
    if not users:
        raise SystemExit(f"no TEST_USERS resolved from {args.conf}")
    rows = resolve(load_inventory(args.inventory), users)
    print(f"parsed + resolved {len(rows)} inventory rows from {args.inventory}")
    if args.dry_run:
        for r in rows:
            print(f"  idx={r['account_idx']} {r['account']:<10} {r['bot_name']:<18} "
                  f"{r.get('kind','bot'):<8} {r.get('bot_type','')}")
        return

    db = open_db(args.db)
    n = upsert_inventory(db, rows)
    db.close()
    print(f"upserted {n} rows into {args.db}")


if __name__ == "__main__":
    main()
