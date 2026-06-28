"""
Neutral persistence helpers (Plan D step 3b, path B).

Strategy-agnostic SQLite helpers shared by every bot. They take a raw connection
(or, later, a duck-typed state) and reference no exchange concept, so they belong in
the neutral core rather than the Polymarket entrypoint. Importing this module pulls in
no `api_*` — the "core imports no plugin" invariant holds.
"""

import sqlite3
import time


def read_capital_base(conn: sqlite3.Connection) -> "float | None":
    """Persisted effective capital base, or None if never written (fresh DB)."""
    try:
        row = conn.execute(
            "SELECT value FROM bot_meta WHERE key='capital_base'").fetchone()
    except sqlite3.OperationalError:
        return None
    return float(row[0]) if row else None


def write_capital_base(conn: sqlite3.Connection, value: float) -> None:
    """Persist the effective capital base so it survives restarts (until the next
    reset overrides it)."""
    conn.execute(
        "INSERT INTO bot_meta(key, value, updated_at) VALUES('capital_base', ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (float(value), time.time()),
    )
    conn.commit()
