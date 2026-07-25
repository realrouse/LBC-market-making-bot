#!/usr/bin/env python3
"""purge_account_state.py — delete a TEST account's rows (heartbeats + deploys + bot_trades)
from the shared state DB.

The ephemeral standalone test account (resolved from TEST_STANDALONE_USER_IDX) runs real
bots during an integration test; those bots PUSH heartbeats to the PROD status collector
(tcp://127.0.0.1:5562) — the heartbeat socket is PUSH→PULL, so there is no drop-safe way
to redirect them without risking a blocked send. The rows therefore land in the shared
prod DB and, once the test account is wiped, linger forever as DEAD heartbeats that inflate
every consumer's issue count / fleet PnL (the status page, bot_status.sh, …). This purges
them so a temporary test leaves no residue.

SAFETY RAIL (load-bearing): refuses to purge any account present in the `inventory` table.
TEST_STANDALONE_USER_IDX has been silently clobbered before (a `source conf` overwriting a
caller's value — see the deploy history), and if it ever resolves to a PROD index the
caller would pass a live account here; the inventory guard makes that a hard refusal rather
than a wiped-out live bot's history. Pass the account NAME at runtime (never hardcode it).

Usage:
    python3 purge_account_state.py --account <name> [--db PATH] [--dry-run]
Exit: 0 purged (or nothing to purge) · 2 refused (account is in inventory) · 1 error.
"""

import argparse
import os
import sqlite3
import sys

DEFAULT_DB = "/data1/tradinebotte-shared/database/tradinebotte.db"


def inventory_accounts(db: sqlite3.Connection) -> set:
    """The set of accounts that appear in the desired-state inventory (= prod accounts).
    Empty set if the table is missing/unreadable — the caller treats that as a FAIL-CLOSED
    refusal (see main): an empty inventory can't distinguish 'bare test DB' from 'prod DB
    whose inventory just isn't synced', and purging in the latter would delete a live bot's
    history via a clobbered idx — exactly what the guard exists to prevent."""
    try:
        return {r[0] for r in db.execute("SELECT DISTINCT account FROM inventory")}
    except sqlite3.OperationalError:
        return set()


def _count_trades(db: sqlite3.Connection, account: str) -> int:
    """bot_trades rows for `account`, or 0 if the table is absent (older/bare test DBs)."""
    try:
        return db.execute("SELECT count(*) FROM bot_trades WHERE account=?", (account,)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def purge_account(db: sqlite3.Connection, account: str) -> tuple[int, int, int]:
    """Delete every heartbeat + deploy + bot_trades row for `account`. Returns
    (heartbeats, deploys, trades) deleted. Caller MUST have checked the inventory guard first.
    bot_trades is included because a test that fires a trade (e.g. the accumulation engine's
    push_trade) leaks per-fill rows there too, not just heartbeats — the table is guarded so a
    DB predating it doesn't crash the purge."""
    hb = db.execute("DELETE FROM heartbeats WHERE account=?", (account,)).rowcount
    dep = db.execute("DELETE FROM deploys WHERE account=?", (account,)).rowcount
    try:
        trades = db.execute("DELETE FROM bot_trades WHERE account=?", (account,)).rowcount
    except sqlite3.OperationalError:
        trades = 0
    db.commit()
    return hb, dep, trades


def main() -> int:
    ap = argparse.ArgumentParser(description="Purge a TEST account's rows from the shared state DB.")
    ap.add_argument("--account", required=True, help="OS account name to purge (resolve at runtime)")
    ap.add_argument("--db", default=os.environ.get("TRADINEBOTTE_DB", DEFAULT_DB))
    ap.add_argument("--dry-run", action="store_true", help="report what would be deleted, delete nothing")
    a = ap.parse_args()

    if not os.path.exists(a.db):
        print(f"shared DB not found: {a.db} — nothing to purge", file=sys.stderr)
        return 0  # no DB = no residue; not an error for a teardown hook

    db = sqlite3.connect(a.db)
    try:
        inv = inventory_accounts(db)
        if not inv:
            # Fail closed: without a populated inventory we can't prove the target is a
            # test account, so refuse rather than risk purging a prod bot's history.
            print("REFUSED: inventory is empty/unreadable — cannot verify the account is "
                  "non-production; not purging.", file=sys.stderr)
            return 2
        if a.account in inv:
            print(f"REFUSED: '{a.account}' is an inventory (production) account — not purging.",
                  file=sys.stderr)
            return 2
        if a.dry_run:
            hb = db.execute("SELECT count(*) FROM heartbeats WHERE account=?", (a.account,)).fetchone()[0]
            dep = db.execute("SELECT count(*) FROM deploys WHERE account=?", (a.account,)).fetchone()[0]
            tr = _count_trades(db, a.account)
            print(f"[dry-run] would purge account '{a.account}': {hb} heartbeat(s), {dep} deploy(s), {tr} trade(s)")
            return 0
        hb, dep, tr = purge_account(db, a.account)
        print(f"purged account '{a.account}': {hb} heartbeat(s), {dep} deploy(s), {tr} trade(s)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
