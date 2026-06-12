#!/usr/bin/env python3
"""status_collector.py — centralised heartbeat receiver for tradinebotte bots.

Binds a ZMQ PULL socket on TRADINEBOTTE_STATUS_ADDR (default tcp://127.0.0.1:5562).
Each bot pushes a JSON heartbeat once per hour.  Payloads are validated, indexed
columns extracted, and the full payload stored in heartbeat.db.

Usage:
    python3 status_collector.py [--dir DIR] [--status-addr ADDR]

Environment:
    TRADINEBOTTE_STATUS_ADDR   ZMQ bind address (default tcp://127.0.0.1:5562)
    TRADINEBOTTE_STATUS_DIR    Install directory for heartbeat.db
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from typing import Any

import zmq
import zmq.asyncio

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tradinetools"))
from tradinetools.zmq import default_status_addr, make_pull

logger = logging.getLogger("status_collector")

_DB_SCHEMA = """
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


def open_db(db_path: str) -> sqlite3.Connection:
    """Open (or create) the heartbeat DB and apply the schema. Returns the open connection."""
    db = sqlite3.connect(db_path, check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(_DB_SCHEMA)
    db.commit()
    return db


def store_heartbeat(db: sqlite3.Connection, payload: dict[str, Any]) -> None:
    """Extract indexed columns and write one heartbeat row.

    Missing fields default gracefully; bounds_ok is coerced to 0/1/None.
    """
    ts = int(payload.get("ts") or time.time())
    account = str(payload.get("account") or "unknown")
    bot_name = str(payload.get("bot_name") or "unknown")
    version = payload.get("version")
    status = payload.get("status")
    raw_bounds = payload.get("bounds_ok")
    if raw_bounds is None:
        bounds_ok = None
    else:
        bounds_ok = 1 if raw_bounds else 0
    db.execute(
        "INSERT INTO heartbeats (ts, account, bot_name, version, status, bounds_ok, payload)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ts, account, bot_name, version, status, bounds_ok, json.dumps(payload)),
    )
    db.commit()


def _prune_old_heartbeats(db: sqlite3.Connection) -> int:
    """Delete heartbeat rows older than 1 year; return count removed."""
    cutoff = int(time.time()) - 365 * 86400
    cur = db.execute("DELETE FROM heartbeats WHERE ts < ?", (cutoff,))
    db.commit()
    return cur.rowcount


async def _prune_loop(db: sqlite3.Connection, stop: asyncio.Event) -> None:
    """Run _prune_old_heartbeats at startup and every 24 h."""
    while not stop.is_set():
        deleted = _prune_old_heartbeats(db)
        if deleted:
            logger.info("pruned %d heartbeat row(s) older than 1 year", deleted)
        await asyncio.sleep(86400)


async def _recv_loop(
    sock: zmq.asyncio.Socket,
    db: sqlite3.Connection,
    stop: asyncio.Event,
) -> None:
    _db_fail_streak = 0
    _DB_FAIL_SUPPRESS = 5   # suppress repeated DB errors after this many consecutive failures

    while not stop.is_set():
        try:
            raw = await asyncio.wait_for(sock.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            continue
        except zmq.ZMQError as exc:
            if stop.is_set():
                break
            logger.warning("ZMQ recv error: %s", exc)
            continue
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("Malformed heartbeat (not JSON): %s", exc)
            continue
        if not isinstance(payload, dict):
            logger.warning("Malformed heartbeat (expected dict, got %s): %r", type(payload).__name__, payload)
            continue
        try:
            store_heartbeat(db, payload)
            _db_fail_streak = 0
            logger.info(
                "heartbeat stored: account=%s bot=%s version=%s status=%s",
                payload.get("account"),
                payload.get("bot_name"),
                payload.get("version"),
                payload.get("status"),
            )
        except sqlite3.Error as exc:
            _db_fail_streak += 1
            if _db_fail_streak <= _DB_FAIL_SUPPRESS:
                logger.error("DB write failed: %s", exc)
            if _db_fail_streak == _DB_FAIL_SUPPRESS:
                logger.error("DB write failing repeatedly — suppressing further errors until recovery")


async def run(status_addr: str, db_path: str) -> None:
    """Bind the PULL socket, open the DB, and run the collector until SIGTERM/SIGINT."""
    ctx = zmq.asyncio.Context()
    db = open_db(db_path)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    sock = make_pull(ctx, status_addr, name="STATUS_ADDR")
    logger.info("status_collector listening on %s — db=%s", status_addr, db_path)
    prune_task = asyncio.create_task(_prune_loop(db, stop), name="prune")
    try:
        await _recv_loop(sock, db, stop)
    finally:
        prune_task.cancel()
        await asyncio.gather(prune_task, return_exceptions=True)
        sock.close(linger=0)
        ctx.term()
        db.close()
    logger.info("status_collector stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    default_dir = os.environ.get(
        "TRADINEBOTTE_STATUS_DIR", os.path.expanduser("~/tradinebotte")
    )
    default_addr = os.environ.get("TRADINEBOTTE_STATUS_ADDR", default_status_addr())

    parser = argparse.ArgumentParser(description="tradinebotte heartbeat collector")
    parser.add_argument(
        "--dir",
        default=default_dir,
        help="Install directory — heartbeat.db is written here",
    )
    parser.add_argument(
        "--status-addr",
        default=default_addr,
        help="ZMQ PULL bind address (default %(default)s)",
    )
    args = parser.parse_args()
    os.makedirs(args.dir, exist_ok=True)
    asyncio.run(run(args.status_addr, os.path.join(args.dir, "heartbeat.db")))


if __name__ == "__main__":
    main()
