"""
Shared SQLite state DB for the tradinebotte deployment + status system.

ONE file on the always-on server (apollo), three tables:

    heartbeats  — runtime liveness pushed BY the bots (collector is the sole writer).
                  DDL is intentionally identical to status_collector.py's historical
                  inline schema so the Phase-2 migration can copy rows column-for-column.
    inventory   — desired state ("what SHOULD run where"): the single source of truth,
                  synced from inventory.toml (git-tracked).  Replaces the topology that
                  is today duplicated across deploy_all.sh / bot_status.sh / generate_status.py.
    deploys     — append-only deploy journal: when / how / which version, written by every
                  deploy_*.sh path (closes the gap left by the overwrite-only version.stamp).

All writers run on the same host but as different OS users (the collector account for
heartbeats, the deployer for the journal), all in the `claudes` group.  open_db() forces
umask 002 so the DB file and its WAL/-shm sidecars are created group-writable (0660);
the shared directory carries setgid so the `claudes` group is inherited.

This module is the SINGLE definition of the schema — status_collector.py imports open_db()
from here instead of keeping its own _DB_SCHEMA copy (Phase 2).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from typing import Any, Iterable

# ─── Schema (single source of truth) ─────────────────────────────────────────

# heartbeats: MUST stay byte-compatible with status_collector.py's original inline
# _DB_SCHEMA — test_db.py asserts structural identity to keep the migration copy safe.
SCHEMA_HEARTBEATS = """
CREATE TABLE IF NOT EXISTS heartbeats (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    account   TEXT    NOT NULL,
    bot_name  TEXT    NOT NULL,
    version   TEXT,
    status    TEXT,
    bounds_ok INTEGER,
    payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_account_bot ON heartbeats(account, bot_name);
CREATE INDEX IF NOT EXISTS idx_heartbeats_ts          ON heartbeats(ts);
"""

# inventory: desired state.  Primary key (account, bot_name) is also the join key
# against heartbeats for the "expected vs actual" status view.
#   kind        'bot'      — a trading bot; liveness tracked via heartbeats
#               'service'  — infra unit; liveness primarily via systemctl.
#               NB: indicators + feed DO emit heartbeats (verified against live data);
#               only status_collector never reports.  So a 'service' row may or may not
#               have heartbeats — the status page should not blindly flag services "silent".
#   is_live     1 real money / 0 simulation / NULL n-a (infra)
SCHEMA_INVENTORY = """
CREATE TABLE IF NOT EXISTS inventory (
    account       TEXT NOT NULL,
    bot_name      TEXT NOT NULL,
    display_name  TEXT,
    kind          TEXT NOT NULL DEFAULT 'bot',
    bot_type      TEXT,
    service_unit  TEXT,
    install_dir   TEXT,
    port          INTEGER,
    is_live       INTEGER,
    deploy_script TEXT,
    depends_on    TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    updated_ts    INTEGER NOT NULL,
    PRIMARY KEY (account, bot_name)
);
"""

# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS never alters an
# existing table, so migrate explicitly + idempotently (ignore "duplicate column").
_INVENTORY_ADD_COLUMNS = (
    ("display_name", "TEXT"),   # readable label for the status page (bot_name is the unique id)
    ("depends_on",   "TEXT"),   # JSON list of bot_names this bot needs up (deploy order + monitoring root-cause)
)

# deploys: append-only journal.  One row per (bot, deploy step).
#   mode    'rsync' | 'restart' | 'full'   (how the step ran)
#   result  'OK' | 'FAILED' | 'RSYNC'      (mirrors deploy_all.sh summary states)
SCHEMA_DEPLOYS = """
CREATE TABLE IF NOT EXISTS deploys (
    id        INTEGER PRIMARY KEY,
    ts        INTEGER NOT NULL,
    account   TEXT    NOT NULL,
    bot_name  TEXT    NOT NULL,
    git_hash  TEXT,
    script    TEXT,
    mode      TEXT,
    result    TEXT,
    deployer  TEXT
);
CREATE INDEX IF NOT EXISTS idx_deploys_account_bot ON deploys(account, bot_name);
CREATE INDEX IF NOT EXISTS idx_deploys_ts          ON deploys(ts);
"""

SCHEMA = SCHEMA_HEARTBEATS + SCHEMA_INVENTORY + SCHEMA_DEPLOYS

INVENTORY_COLUMNS = (
    "account", "bot_name", "display_name", "kind", "bot_type", "service_unit",
    "install_dir", "port", "is_live", "deploy_script", "depends_on", "enabled", "updated_ts",
)


# ─── Connection ──────────────────────────────────────────────────────────────

def open_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the shared state DB, applying the full schema.

    Cross-user writes within the `claudes` group need the DB file group-writable (0660).
    umask alone cannot achieve this: SQLite hard-codes the main DB file to 0644
    (SQLITE_DEFAULT_FILE_PERMISSIONS) and umask only *removes* bits.  So we explicitly
    chmod the file to 0660 after opening — SQLite then copies that mode onto the -wal/-shm
    sidecars it creates.  The chmod is best-effort: only the file owner (its creator) can
    do it; other group members simply skip it, the owner having already set it.
    umask 002 is still set so any other artefacts land group-writable.  WAL journaling is
    enabled for same-host multi-process concurrency.
    """
    prev_umask = os.umask(0o002)
    try:
        db = sqlite3.connect(db_path, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        # Idempotent column migrations for tables that predate a column (CREATE IF NOT
        # EXISTS won't add it). "duplicate column name" = already migrated → ignore.
        for _col, _type in _INVENTORY_ADD_COLUMNS:
            try:
                db.execute(f"ALTER TABLE inventory ADD COLUMN {_col} {_type}")
            except sqlite3.OperationalError as _e:
                if "duplicate column" not in str(_e).lower():
                    raise
        db.commit()
        try:
            os.chmod(db_path, 0o660)
        except (PermissionError, OSError):
            pass  # not the owner — file mode already set by whoever created it
        return db
    finally:
        os.umask(prev_umask)


# ─── Inventory ───────────────────────────────────────────────────────────────

def upsert_inventory(db: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Upsert desired-state rows (keyed on account+bot_name). Returns rows written.

    Idempotent: re-running with the same inventory.toml leaves the table unchanged
    except for updated_ts.  Missing optional fields default to NULL.
    """
    now = int(time.time())
    n = 0
    for r in rows:
        db.execute(
            "INSERT INTO inventory"
            " (account, bot_name, display_name, kind, bot_type, service_unit, install_dir,"
            "  port, is_live, deploy_script, depends_on, enabled, updated_ts)"
            " VALUES (:account, :bot_name, :display_name, :kind, :bot_type, :service_unit,"
            "         :install_dir, :port, :is_live, :deploy_script, :depends_on, :enabled,"
            "         :updated_ts)"
            " ON CONFLICT(account, bot_name) DO UPDATE SET"
            "   display_name=excluded.display_name, kind=excluded.kind,"
            "   bot_type=excluded.bot_type, service_unit=excluded.service_unit,"
            "   install_dir=excluded.install_dir, port=excluded.port,"
            "   is_live=excluded.is_live, deploy_script=excluded.deploy_script,"
            "   depends_on=excluded.depends_on, enabled=excluded.enabled,"
            "   updated_ts=excluded.updated_ts",
            {
                "account":       r["account"],
                "bot_name":      r["bot_name"],
                "display_name":  r.get("display_name"),
                "depends_on":    (json.dumps(r["depends_on"])
                                  if isinstance(r.get("depends_on"), (list, dict))
                                  else r.get("depends_on")),
                "kind":          r.get("kind", "bot"),
                "bot_type":      r.get("bot_type"),
                "service_unit":  r.get("service_unit"),
                "install_dir":   r.get("install_dir"),
                "port":          r.get("port"),
                "is_live":       _as_int_or_none(r.get("is_live")),
                "deploy_script": r.get("deploy_script"),
                "enabled":       1 if r.get("enabled", True) else 0,
                "updated_ts":    now,
            },
        )
        n += 1
    db.commit()
    return n


# ─── Deploy journal ──────────────────────────────────────────────────────────

def record_deploy(
    db: sqlite3.Connection,
    *,
    account: str,
    bot_name: str,
    git_hash: str | None = None,
    script: str | None = None,
    mode: str | None = None,
    result: str | None = None,
    deployer: str | None = None,
    ts: int | None = None,
) -> None:
    """Append one row to the deploy journal. Never updates — history is immutable."""
    db.execute(
        "INSERT INTO deploys (ts, account, bot_name, git_hash, script, mode, result, deployer)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (int(ts or time.time()), account, bot_name, git_hash, script, mode, result, deployer),
    )
    db.commit()


# ─── Expected vs actual (status view) ────────────────────────────────────────

def expected_vs_actual(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """Left-join inventory → latest heartbeat, so EXPECTED-but-silent bots are visible.

    Returns one row per enabled inventory entry.  last_ts is NULL when no heartbeat was
    ever received (today such a bot is simply invisible).  `kind='service'` rows are
    included but callers should judge their liveness via systemctl, not last_ts.
    """
    rows = db.execute(
        """
        SELECT i.account, i.bot_name, i.kind, i.bot_type, i.service_unit,
               i.is_live, h.last_ts, h.version, h.status
        FROM inventory AS i
        LEFT JOIN (
            SELECT account, bot_name, max(ts) AS last_ts, version, status
            FROM heartbeats GROUP BY account, bot_name
        ) AS h ON h.account = i.account AND h.bot_name = i.bot_name
        WHERE i.enabled = 1
        ORDER BY i.account, i.bot_name
        """
    ).fetchall()
    cols = ("account", "bot_name", "kind", "bot_type", "service_unit",
            "is_live", "last_ts", "version", "status")
    return [dict(zip(cols, r)) for r in rows]


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _as_int_or_none(v: Any) -> int | None:
    if v is None:
        return None
    return 1 if v else 0
