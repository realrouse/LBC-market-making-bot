#!/usr/bin/env python3
"""
account_bot.py — Per-account trading bot, subscribes to feed.py via ZeroMQ

This process subscribes to a running feed.py PUB socket and runs the full
tradinebotte trading logic for one account in isolation.  Multiple instances
can run in parallel — each pointed at its own TRADINEBOTTE_DIR — without
sharing a WebSocket connection or interfering with each other.

The feed is started automatically if not already running: the first
account_bot to start acquires a file lock and launches feed.py as a
subprocess; subsequent account_bots wait for the lock to be released and
then connect to the already-running feed.  No manual feed management needed.

Full architecture documentation: docs/multi.md

Usage:
  TRADINEBOTTE_DIR=~/account-a python3 bot/account_bot.py
  TRADINEBOTTE_DIR=~/account-b python3 bot/account_bot.py
  TRADINEBOTTE_FEED_ADDR=tcp://127.0.0.1:5558 TRADINEBOTTE_DIR=~/acc python3 bot/account_bot.py

The TRADINEBOTTE_DIR env var must be set before import so live_bot picks up
the correct DB / log / config paths at module level.  Each account needs its
own config.json with its own private key.

Message types consumed from feed.py:
  {"t": "market", "market_id": ..., "question": ..., "up_token_id": ...,
   "dn_token_id": ..., "start_ms": ..., "end_ms": ...}
  {"t": "book",  "token_id": ..., "best_bid": ..., "best_ask": ...,
   "spread": ..., "bid_vol": ..., "ask_vol": ..., "obi": ...}
  {"t": "ping",  "ts": ...}
"""
# pylint: disable=duplicate-code  # market-expiry purge loop mirrors feed.py by design

import argparse, asyncio, fcntl, logging, os, subprocess, sys, time
import zmq, zmq.asyncio

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)

# Set by _parse_args() before live_bot import — used throughout for debug logs.
VERBOSE = False


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="tradinebotte account bot")
    p.add_argument("--verbose", action="store_true",
                   help="Enable DEBUG logging — very detailed, for diagnostics only")
    return p.parse_args()

# ─── CONFIGURE INSTALL DIR BEFORE IMPORTING live_bot ─────────────────────────
# live_bot reads TRADINEBOTTE_DIR at module level (INSTALL_DIR, DB_PATH, etc.)
# so the env var must be set before the import statement.
_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR", "tcp://127.0.0.1:5557")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import live_bot after path setup — this triggers all module-level config loads.
import live_bot as bot

logger = logging.getLogger("account")

# Reconnect timeout: if no message from the feed within this many seconds,
# warn and keep waiting (feed may reconnect to the exchange automatically).
FEED_TIMEOUT = 60  # seconds

# File lock path — one per feed address (hash suffix avoids collisions when
# multiple feed addresses are used on the same machine).
_FEED_LOCK_PATH = f"/tmp/tradinebotte-feed-{abs(hash(_FEED_ADDR)) % 100000}.lock"
_FEED_PROBE_MS  = 5_000   # ms to wait when probing for a live feed
_FEED_READY_S   = 30      # max seconds to wait for feed to become ready


# ─── Feed auto-start helpers ──────────────────────────────────────────────────

def _probe_feed_sync(addr: str, timeout_ms: int) -> bool:
    """Return True if at least one message arrives from feed within timeout_ms."""
    if VERBOSE:
        logger.debug("[PROBE] sonde %s (timeout %dms)...", addr, timeout_ms)
    ctx = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    sock.connect(addr)
    try:
        msg = sock.recv()
        if VERBOSE:
            logger.debug("[PROBE] reponse recue (%d octets) — feed actif", len(msg))
        return True
    except zmq.Again:
        if VERBOSE:
            logger.debug("[PROBE] timeout — pas de feed")
        return False
    finally:
        sock.close(linger=0)
        ctx.term()


def _ensure_feed() -> None:
    """
    Start feed.py if no feed is reachable on _FEED_ADDR.

    Uses an exclusive file lock so only one account_bot ever starts the feed,
    even when several are launched at exactly the same time.

    Race flow (all 3 bots start simultaneously):
      1. All probe → no feed found
      2. All race for LOCK_EX | LOCK_NB on the lock file
      3. Winner starts feed.py and waits until it's ready, then releases lock
      4. Losers get BlockingIOError, wait for shared (LOCK_SH) → proceed
    """
    if _probe_feed_sync(_FEED_ADDR, _FEED_PROBE_MS):
        logger.info("Feed actif sur %s", _FEED_ADDR)
        return

    lock_file = open(_FEED_LOCK_PATH, "w", encoding="utf-8")  # pylint: disable=consider-using-with
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another account_bot won the race and is starting the feed — wait.
        logger.info("Feed en cours de démarrage par un autre processus — attente...")
        fcntl.flock(lock_file, fcntl.LOCK_SH)
        lock_file.close()
        return

    # ── We hold the exclusive lock: start feed.py ──────────────────────────
    try:
        feed_py  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "feed.py")
        log_path = f"/tmp/tradinebotte-feed-{abs(hash(_FEED_ADDR)) % 100000}.log"
        env      = {**os.environ, "TRADINEBOTTE_FEED_ADDR": _FEED_ADDR}
        cmd      = [sys.executable, feed_py]
        if VERBOSE:
            cmd.append("--verbose")

        if VERBOSE:
            logger.debug("[ENSURE_FEED] démarrage feed.py : %s", " ".join(cmd))
            logger.debug("[ENSURE_FEED] log feed : %s", log_path)
        with open(log_path, "ab") as log_f:
            proc = subprocess.Popen(cmd, env=env, stdout=log_f, stderr=log_f)  # pylint: disable=consider-using-with
        logger.info("Feed démarré (PID %d) — log: %s", proc.pid, log_path)

        # Wait until feed publishes its first message.
        for i in range(_FEED_READY_S):
            time.sleep(1)
            if VERBOSE:
                logger.debug("[ENSURE_FEED] attente feed prêt... %ds/%ds", i + 1, _FEED_READY_S)
            if _probe_feed_sync(_FEED_ADDR, timeout_ms=2_000):
                logger.info("Feed prêt (%ds)", i + 1)
                return
        logger.warning("Feed démarré mais aucun message reçu après %ds", _FEED_READY_S)
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


# ─── Market / book handlers ───────────────────────────────────────────────────

def _register_from_market_msg(state: bot.BotState, msg: dict) -> None:
    """Build a TokenState pair from a feed "market" message."""
    mid = msg.get("market_id", "")
    up  = msg.get("up_token_id", "")
    dn  = msg.get("dn_token_id", "")
    q   = msg.get("question", "")[:80]
    sm  = int(msg.get("start_ms", 0))
    em  = int(msg.get("end_ms", 0))
    if not mid or not up or not dn:
        if VERBOSE:
            logger.debug("[MARKET] msg incomplet ignoré : %s", msg)
        return
    if em and time.time() * 1000 > em:
        if VERBOSE:
            logger.debug("[MARKET] marché expiré ignoré : %s", mid)
        return
    is_new = mid not in state.market_tokens
    for tid, direction in ((up, "UP"), (dn, "DOWN")):
        if tid not in state.tokens:
            state.tokens[tid] = bot.TokenState(tid, mid, direction, q, sm, em)
    state.market_tokens[mid] = {"UP": up, "DOWN": dn}
    if VERBOSE and is_new:
        logger.debug("[MARKET] enregistré : %s %s", mid[:16], q[:50])


async def _run(state: bot.BotState) -> None:
    ctx  = zmq.asyncio.Context()
    sock = ctx.socket(zmq.SUB)
    sock.connect(_FEED_ADDR)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    logger.info("Connecte au feed : %s", _FEED_ADDR)

    last_msg_ts = time.time()
    msgs_total  = 0

    try:
        while True:
            try:
                raw = await asyncio.wait_for(sock.recv_json(), timeout=FEED_TIMEOUT)
                last_msg_ts = time.time()
                msgs_total += 1
            except asyncio.TimeoutError:
                idle = time.time() - last_msg_ts
                logger.warning("Aucun message du feed depuis %.0fs — feed actif ?", idle)
                continue
            except Exception as e:
                logger.error("Erreur reception feed : %s", e)
                await asyncio.sleep(5)
                continue

            t = raw.get("t")

            if VERBOSE:
                if t == "book":
                    logger.debug("[FEED] book token=%.12s bid=%.4f ask=%.4f obi=%.3f",
                                 raw.get("token_id", ""), raw.get("best_bid", 0),
                                 raw.get("best_ask", 0), raw.get("obi", 0))
                elif t == "ping":
                    logger.debug("[FEED] ping #%d (total msgs: %d)", raw.get("ts", 0), msgs_total)
                elif t == "market":
                    logger.debug("[FEED] market %s", raw.get("market_id", "")[:16])

            if t == "market":
                _register_from_market_msg(state, raw)

            elif t == "book":
                token_id = raw.get("token_id", "")
                if token_id not in state.tokens:
                    if VERBOSE:
                        logger.debug("[BOOK] token inconnu ignoré : %.20s", token_id)
                    continue
                parsed = {k: v for k, v in raw.items() if k != "t"}
                if VERBOSE:
                    logger.debug("[BOOK] traitement signal — bid=%.4f seuil=%.2f",
                                 raw.get("best_bid", 0), bot.SIGNAL_THRESHOLD)
                await bot.handle_book_update(state, parsed)

            elif t == "ping":
                pass  # keepalive — no action needed

            # Purge expired markets periodically.
            expired = [
                tid for tid, ts in list(state.tokens.items())
                if ts.market_ended and ts.market_id not in state.open_trades
            ]
            for tid in expired:
                ts_obj = state.tokens.pop(tid, None)
                if ts_obj:
                    state.market_tokens.pop(ts_obj.market_id, None)
                    state.signalled.discard(ts_obj.market_id)
    finally:
        sock.close(linger=0)
        ctx.term()


async def main() -> None:
    logger.info("=" * 65)
    logger.info("  ACCOUNT BOT — dir=%s", bot.INSTALL_DIR)
    logger.info("  Feed : %s", _FEED_ADDR)
    if VERBOSE:
        logger.info("  Mode VERBOSE actif — logs DEBUG complets")
    logger.info("=" * 65)

    if VERBOSE:
        logger.debug("[INIT] TRADINEBOTTE_DIR=%s", os.environ.get("TRADINEBOTTE_DIR", "(non défini)"))
        logger.debug("[INIT] SIGNAL_THRESHOLD=%.2f", bot.SIGNAL_THRESHOLD)
        logger.debug("[INIT] STAKE=%.2f WIN=%.2f LOSS=%.2f",
                     bot.STAKE, bot.WIN_THRESHOLD, bot.LOSS_THRESHOLD)

    _ensure_feed()

    if VERBOSE:
        logger.debug("[INIT] init_db...")
    conn  = bot.init_db()
    state = bot.BotState(conn)
    bot.restore_state_from_db(state)
    if VERBOSE:
        logger.debug("[INIT] capital restauré=%.2f trades_ouverts=%d",
                     state.capital, len(state.open_trades))

    import aiohttp
    async with aiohttp.ClientSession() as session:
        state.session = session
        await _run(state)


if __name__ == "__main__":
    args = _parse_args()
    if args.verbose:
        VERBOSE = True
        logging.getLogger().setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    asyncio.run(main())
