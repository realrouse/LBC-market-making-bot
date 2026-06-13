#!/usr/bin/env python3
"""record_deploy.py — append one row to the deploy journal in the shared DB.

Called by every deploy_*.sh path right where it writes version.stamp, so the journal
captures when / how / which version every bot was deployed.  Writes directly to the
local shared DB (deploy scripts run as neofutur on apollo — no SSH needed).

The go/no-go rule for the whole feature: NO deploy path may run without calling this.

Usage:
    python3 tradinebotte-status/record_deploy.py \
        --account "$GR_USER" --bot grid_bot --git-hash "$GIT_HASH" \
        --script "$(basename "$0")" --mode full --result OK

Defaults:
    --db        $TRADINEBOTTE_DB or /data1/tradinebotte-shared/database/tradinebotte.db
    --deployer  $USER
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in (os.path.join(_HERE, "tradinetools"),
              os.path.join(os.path.dirname(_HERE), "tradinetools")):
    if os.path.isdir(_cand):
        sys.path.insert(0, _cand)
        break
from tradinetools.db import open_db, record_deploy  # noqa: E402

DEFAULT_DB = os.environ.get(
    "TRADINEBOTTE_DB", "/data1/tradinebotte-shared/database/tradinebotte.db"
)


def main() -> None:
    p = argparse.ArgumentParser(description="Append a row to the deploy journal")
    p.add_argument("--account", required=True)
    p.add_argument("--bot", required=True, dest="bot_name")
    p.add_argument("--git-hash", default=None)
    p.add_argument("--script", default=None)
    p.add_argument("--mode", default=None, choices=[None, "rsync", "restart", "full"])
    p.add_argument("--result", default=None, choices=[None, "OK", "FAILED", "RSYNC"])
    p.add_argument("--deployer", default=None)
    p.add_argument("--db", default=DEFAULT_DB)
    args = p.parse_args()

    deployer = args.deployer or os.environ.get("USER") or getpass.getuser()
    db = open_db(args.db)
    record_deploy(
        db,
        account=args.account,
        bot_name=args.bot_name,
        git_hash=args.git_hash,
        script=args.script,
        mode=args.mode,
        result=args.result,
        deployer=deployer,
    )
    db.close()
    print(f"recorded deploy: {args.account}/{args.bot_name} "
          f"{args.git_hash or '?'} {args.mode or ''} {args.result or ''}")


if __name__ == "__main__":
    main()
