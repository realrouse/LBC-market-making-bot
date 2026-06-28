"""
Neutral persistence helpers (Plan D step 3b, path B).

Strategy-agnostic SQLite helpers shared by every bot. They take a raw connection
(or, later, a duck-typed state) and reference no exchange concept, so they belong in
the neutral core rather than the Polymarket entrypoint. Importing this module pulls in
no `api_*` — the "core imports no plugin" invariant holds.
"""

import sqlite3
import time
from typing import Any, Callable

# Batched snapshot commits: one flush every SNAPSHOT_COMMIT_SECS seconds.
SNAPSHOT_COMMIT_SECS = 30


def _persist_snapshot(state: Any, row_writer: Callable[[], None]) -> None:
    """The single shared snapshot-persistence step — invoked by EVERY data path.

    Honours the enable-snapshots guard, writes one row (its shape delegated to
    `row_writer`: save_snapshot for the polymarket/WS path, save_cex_snapshot for the
    CEX consumer), advances the data-freshness clock (state.last_write_ts → status
    ⚠data), and batch-commits every SNAPSHOT_COMMIT_SECS. Callers own only their cadence
    gate (per-token for WS, per-loop for CEX).

    `state` is duck-typed (not the concrete BotState): this step only needs
    `state.config.enable_snapshots`, `state.last_write_ts`, `state.last_snapshot_commit_ts`
    and `state.conn` — all neutral — so the core depends on no plugin's state class.

    Persistence used to be a side-effect buried inside handle_book_update; the
    2026-06-16 CEX path bypassed that function and silently stopped recording. This
    extraction does NOT by itself prevent that — a path that bypasses _persist_snapshot
    too would record silently again. What it does: give every data path one named,
    enable-guarded, tested step to call instead of a hidden side-effect. The actual
    safety net for a silent recording-stop is the status ⚠data monitor (it flags a
    stale last_write_ts regardless of which path dropped the write)."""
    if not state.config.enable_snapshots:
        return
    row_writer()
    now = time.time()
    state.last_write_ts = now
    if now - state.last_snapshot_commit_ts >= SNAPSHOT_COMMIT_SECS:
        state.conn.commit()
        state.last_snapshot_commit_ts = now


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
