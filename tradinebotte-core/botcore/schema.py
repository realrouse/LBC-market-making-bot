"""
Neutral base schema (Plan D step 5).

The exchange-agnostic infrastructure tables, owned by the core rather than the
Polymarket entrypoint:
  - schema_version: the migration-version tracker (the plugin owns the MIGRATIONS content;
    the core just owns the table the runner reads/writes).
  - bot_meta: a generic key/value table — `botcore.persistence.read/write_capital_base`
    already own its access, so its DDL belongs beside that code.

Apply this BEFORE the plugin's own tables + migrations, so `schema_version` exists for
the migration runner. All statements are `IF NOT EXISTS`, so applying it to a fresh DB and
to an existing (already-migrated) DB both converge to the same schema.
"""

import sqlite3

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bot_meta (
    key TEXT PRIMARY KEY, value REAL NOT NULL, updated_at REAL NOT NULL);
"""


def apply_base_schema(conn: sqlite3.Connection) -> None:
    """Create the neutral infrastructure tables (idempotent)."""
    conn.executescript(BASE_SCHEMA)
