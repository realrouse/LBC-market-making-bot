#!/usr/bin/env python3
"""feed_watchdog.py — restart a Polymarket feed that is alive but no longer publishing.

Solution C of the feed-robustness plan. The feed (feed.py) can sit "active" yet silent:
get_markets() masks API/session errors as [] → the bot loops in "No markets — waiting 30s"
without ever connecting its WebSocket or pinging (0 books, 0 pings observed 2026-06-27).
Its consumers (the standalone Polymarket bots) then starve invisibly. This watchdog is failure-mode-agnostic:
it reads the feed's own heartbeat (already published: ws_connected + last_book_ts) from the
shared state DB and restarts the feed service if it has been alive-but-not-publishing.

Runs as a systemd --user timer on the feed account (~every 60s). The DECISION is a pure
function (unit-tested); main() reads the DB and acts.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import time
from typing import Any, Optional

logger = logging.getLogger("feed_watchdog")

DEFAULT_DB = os.environ.get("TRADINEBOTTE_DB", "/data1/tradinebotte-shared/database/tradinebotte.db")
# A feed that is heartbeating but hasn't published a book in this long is stuck. Generous
# vs the 15M cadence (±16min windows almost always overlap a market, so books arrive within
# a few minutes) — 300s avoids tripping on a genuinely quiet market.
STALE_BOOK_S = int(os.environ.get("FEED_STALE_BOOK_S", 300))
# If the latest heartbeat itself is older than this, the feed (or the collector) is down —
# NOT our trigger: a dead process is systemd Restart=on-failure's job, and a dead collector
# would be a false signal. We only act on "alive but silent".
HB_DEAD_S = int(os.environ.get("FEED_HB_DEAD_S", 300))
SAMPLES = 3  # heartbeats to fetch; ws_connected must be False across the latest 2 to count


def decide(rows: list[tuple[int, dict[str, Any]]], now: int,
           *, stale_book_s: int = STALE_BOOK_S, hb_dead_s: int = HB_DEAD_S) -> str:
    """Decide whether to restart the feed. PURE — unit-tested.

    `rows`: (ts, payload) for the feed's heartbeats, MOST RECENT FIRST.
    Returns one of: "restart" | "ok" | "no-data" | "stale-heartbeat".
    """
    if not rows:
        return "no-data"
    latest_ts, latest = rows[0]
    if now - latest_ts > hb_dead_s:
        # Feed not heartbeating (process down, or collector down) — not an alive-but-silent
        # case; leave it to systemd / collector monitoring.
        return "stale-heartbeat"
    last_book_ts = latest.get("last_book_ts") or 0
    if now - last_book_ts > stale_book_s:
        # Alive (heartbeating) but no book published for too long — covers the stuck
        # "No markets" loop (last_book_ts never advances) AND connected-but-silent.
        return "restart"
    # Faster path: a sustained disconnect (ws_connected False across the last 2 samples)
    # before the book-staleness threshold is even reached.
    last2 = rows[:2]
    if len(last2) == 2 and all(r[1].get("ws_connected") is False for r in last2):
        return "restart"
    return "ok"


def _fetch(db_path: str, bot_name: str, account: str) -> list[tuple[int, dict[str, Any]]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    try:
        cur = conn.execute(
            "SELECT ts, payload FROM heartbeats WHERE bot_name=? AND account=? "
            "ORDER BY ts DESC LIMIT ?", (bot_name, account, SAMPLES))
        out: list[tuple[int, dict[str, Any]]] = []
        for ts, payload in cur.fetchall():
            try:
                p = json.loads(payload) if payload else {}
            except (json.JSONDecodeError, TypeError):
                p = {}
            out.append((int(ts), p if isinstance(p, dict) else {}))
        return out
    finally:
        conn.close()


def _restart(service: str, dry_run: bool) -> None:
    if dry_run:
        logger.warning("[dry-run] would restart %s", service)
        return
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    subprocess.run(["systemctl", "--user", "reset-failed", service], env=env, check=False)
    r = subprocess.run(["systemctl", "--user", "restart", service], env=env, check=False)
    if r.returncode == 0:
        logger.warning("RESTARTED %s — feed was alive but not publishing", service)
    else:
        logger.error("restart of %s failed (rc=%d)", service, r.returncode)


def main() -> int:
    ap = argparse.ArgumentParser(description="Restart a feed that is alive but not publishing.")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--bot-name", default=os.environ.get("TRADINEBOTTE_FEED_NAME", "feed"))
    ap.add_argument("--service", default="tradinebotte-feed.service")
    ap.add_argument("--account", default=os.environ.get("TRADINEBOTTE_ACCOUNT") or os.environ.get("USER", ""))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [feed_watchdog] %(message)s")

    if not args.account:
        logger.error("no account (set TRADINEBOTTE_ACCOUNT or USER)"); return 2
    try:
        rows = _fetch(args.db, args.bot_name, args.account)
    except sqlite3.Error as e:
        logger.error("DB read failed: %s", e); return 2

    verdict = decide(rows, int(time.time()))
    logger.info("feed=%s account=%s verdict=%s (%d heartbeats)", args.bot_name, args.account, verdict, len(rows))
    if verdict == "restart":
        _restart(args.service, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
