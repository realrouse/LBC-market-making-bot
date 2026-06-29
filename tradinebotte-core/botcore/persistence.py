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


def cumulative_pnl(state: Any) -> "tuple[float, int]":
    """Realized cumulative PnL and completed-trade count for the active strategy.

    Sourced from the persisted, restart-restored state (grid_state for grid,
    swing_state for swing, the threshold trades table otherwise), so the cumulative
    survives restarts — only the operator reset command zeroes it. Exported on the
    heartbeat so the status page shows the real cumulative, not just the daily PnL.

    `state`/`strategy` are duck-typed: the strategy's cumulative is read via getattr
    (`grid`/`sw`), falling back to the state's own totals — no plugin type imported.
    """
    strat = getattr(state, "strategy", None)
    grid = getattr(strat, "grid", None)
    if grid is not None:
        return float(grid.total_profit_usd), int(grid.total_cycles)
    sw = getattr(strat, "sw", None)
    if sw is not None:
        return float(sw.total_pnl), int(sw.total_trades)
    return float(state.total_pnl), int(state.total_trades)


def equity(state: Any) -> float:
    """Current equity = persisted capital base + realized cumulative PnL.

    Reported uniformly on the heartbeat. For the threshold strategy this equals the
    live state.capital; for grid/swing it folds in their strategy-table cumulative
    (which state.capital does not track), so the figure resumes across restarts.
    """
    return state.config.capital_start + cumulative_pnl(state)[0]


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
