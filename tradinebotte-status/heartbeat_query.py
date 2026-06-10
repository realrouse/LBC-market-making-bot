#!/usr/bin/env python3
"""heartbeat_query.py — Query heartbeat.db and report bot liveness.

Prints a colour-coded table of the most recent heartbeat per (account, bot_name).
Exits 0 if every visible row is ALIVE and every --require name is satisfied.
Exits 1 otherwise (any STALE/DEAD row, or a required bot_name has no ALIVE row).

NOTE: Only bots that have sent at least one heartbeat appear in the table.
      A bot that was never deployed with Phase-4 code produces no rows and is
      completely invisible.  Green output means all *known* bots are alive;
      it does NOT guarantee all expected bots are running.  Use --require to
      cross-check specific bot names against the table.

Usage:
    python3 heartbeat_query.py [options]

Options:
    --db PATH        heartbeat.db location  (default ~/tradinebotte/heartbeat.db)
    --stale-after N  Seconds before ALIVE → STALE  (default 7200  = 2 h)
    --dead-after  N  Seconds before STALE  → DEAD   (default 14400 = 4 h)
    --require NAME   Bot name that must have ≥ 1 ALIVE row; repeatable.
                     A missing or non-ALIVE required name counts as an issue.
    --no-color       Suppress ANSI colour codes
"""

import argparse
import os
import sqlite3
import sys
import time
from typing import NamedTuple

# ─── Colour helpers ──────────────────────────────────────────────────────────

_RED    = "\033[0;31m"
_GREEN  = "\033[0;32m"
_YELLOW = "\033[1;33m"
_BOLD   = "\033[1m"
_NC     = "\033[0m"


def _c(code: str, text: str, use_color: bool) -> str:
    return f"{code}{text}{_NC}" if use_color else text


# ─── Core logic (testable without I/O) ───────────────────────────────────────

class BotRow(NamedTuple):
    account:    str
    bot_name:   str
    last_ts:    int
    age_s:      int
    flag:       str   # ALIVE | STALE | DEAD
    bot_status: str
    bounds_ok:  str   # ok | FAIL | -
    version:    str


def classify(age_s: int, stale_after: int, dead_after: int) -> str:
    """Return ALIVE, STALE, or DEAD based on age and thresholds."""
    if age_s <= stale_after:
        return "ALIVE"
    if age_s <= dead_after:
        return "STALE"
    return "DEAD"


def query_heartbeats(
    db_path: str,
    stale_after: int,
    dead_after: int,
    now: int | None = None,
) -> list[BotRow]:
    """Return one BotRow per (account, bot_name), most recent heartbeat only."""
    if now is None:
        now = int(time.time())
    db = sqlite3.connect(db_path)
    try:
        rows = db.execute(
            """
            SELECT account, bot_name, max(ts) AS last_ts,
                   status, bounds_ok, version
            FROM heartbeats
            GROUP BY account, bot_name
            ORDER BY account, bot_name
            """
        ).fetchall()
    finally:
        db.close()

    result = []
    for account, bot_name, last_ts, bot_status, bounds_ok_val, version in rows:
        age_s     = now - int(last_ts)
        flag      = classify(age_s, stale_after, dead_after)
        bounds    = "-" if bounds_ok_val is None else ("ok" if bounds_ok_val else "FAIL")
        ver       = (version or "unknown")[:10]
        result.append(BotRow(
            account    = str(account),
            bot_name   = str(bot_name),
            last_ts    = int(last_ts),
            age_s      = age_s,
            flag       = flag,
            bot_status = str(bot_status or "-"),
            bounds_ok  = bounds,
            version    = ver,
        ))
    return result


# ─── Presentation ─────────────────────────────────────────────────────────────

def print_table(
    rows: list[BotRow],
    required: set[str],
    color: bool,
) -> int:
    """Print the formatted table; return the number of issues detected."""
    issues = 0

    header = f"  {'STATUS':<6}  {'ACCOUNT':<20}  {'BOT':<20}  {'AGE':>7}  {'STATUS':<9}  {'BOUNDS':<10}  VERSION"
    sep    = "  " + "─" * (len(header) - 2)
    print(header)
    print(sep)

    alive_bots: set[str] = set()
    for r in rows:
        mins = r.age_s // 60
        line = (
            f"  {r.flag:<6}  {r.account:<20}  {r.bot_name:<20}"
            f"  {mins:>5}min  {r.bot_status:<9}  {r.bounds_ok:<10}  {r.version}"
        )
        if r.flag == "ALIVE":
            print(_c(_GREEN, f"✓ {line}", color))
            alive_bots.add(r.bot_name)
        elif r.flag == "STALE":
            print(_c(_YELLOW, f"! {line}", color))
            issues += 1
        else:  # DEAD
            print(_c(_RED, f"✗ {line}", color))
            issues += 1

    missing = required - alive_bots
    if missing:
        print()
        for name in sorted(missing):
            print(_c(_RED, f"  ✗ MISSING  required bot '{name}' has no ALIVE row", color))
            issues += 1

    return issues


# ─── Entry point ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Report latest heartbeat status for all known bots."
    )
    parser.add_argument(
        "--db",
        default=os.environ.get(
            "TRADINEBOTTE_STATUS_DIR",
            os.path.expanduser("~/tradinebotte"),
        ) + "/heartbeat.db",
        help="Path to heartbeat.db (default %(default)s)",
    )
    parser.add_argument(
        "--stale-after",
        type=int,
        default=int(os.environ.get("HEARTBEAT_STALE_S", 7200)),
        metavar="N",
        help="Seconds before ALIVE → STALE (default %(default)s)",
    )
    parser.add_argument(
        "--dead-after",
        type=int,
        default=int(os.environ.get("HEARTBEAT_DEAD_S", 14400)),
        metavar="N",
        help="Seconds before STALE → DEAD (default %(default)s)",
    )
    parser.add_argument(
        "--require",
        action="append",
        default=[],
        metavar="BOT_NAME",
        help="Bot name that must have ≥ 1 ALIVE row (repeatable)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Suppress ANSI colour codes",
    )
    parser.add_argument(
        "--color",
        action="store_true",
        help="Force ANSI colour codes even when stdout is not a TTY (e.g. over SSH)",
    )
    args = parser.parse_args()

    color = (not args.no_color) and (args.color or sys.stdout.isatty())

    if not os.path.exists(args.db):
        print(f"heartbeat.db not found: {args.db}", file=sys.stderr)
        print("  Is the status collector running?  Deploy it with:", file=sys.stderr)
        print("    bash tradinebotte-status/scripts/deploy_status_service.sh", file=sys.stderr)
        sys.exit(1)

    rows = query_heartbeats(args.db, args.stale_after, args.dead_after)
    if not rows:
        print("heartbeat.db exists but has no rows — collector running but no bot has reported yet.")
        sys.exit(1)

    issues = print_table(rows, set(args.require), color)

    print()
    if issues == 0:
        print(_c(_GREEN + _BOLD, "  All known bots ALIVE", color))
    else:
        print(_c(_YELLOW + _BOLD, f"  {issues} issue(s) — check output above", color))

    sys.exit(0 if issues == 0 else 1)


if __name__ == "__main__":
    main()
