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

_PORT_BASE = int(os.environ.get("TRADINEBOTTE_PORT_BASE", "5557"))
_FEED_ADDR = os.environ.get("TRADINEBOTTE_FEED_ADDR", f"tcp://127.0.0.1:{_PORT_BASE}")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# live_bot no longer has module-level side effects — safe to import anywhere.
import live_bot as bot

logger = logging.getLogger("account")

# Reconnect timeout: if no message from the feed within this many seconds,
# warn and keep waiting (feed may reconnect to the exchange automatically).
FEED_TIMEOUT = 60  # seconds

# Shared /tmp directory for the feed — common to all Linux users so the file
# lock coordinates a single feed.py instance across users. Created with
# sticky-bit + world-writable (1777) so every user can create lock/log files
# inside it while the sticky bit prevents cross-user deletion.
_FEED_TMP_DIR   = "/tmp/tradinebotte-feed"
# Lock filename includes a hash of the feed address so multiple feed addresses
# can coexist on the same machine without colliding.
_FEED_LOCK_PATH = f"{_FEED_TMP_DIR}/feed-{abs(hash(_FEED_ADDR)) % 100000}.lock"
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

    os.makedirs(_FEED_TMP_DIR, exist_ok=True)
    try:
        os.chmod(_FEED_TMP_DIR, 0o1777)
    except PermissionError:
        pass  # another user created it first; trust it is already world-writable
    _fd = os.open(_FEED_LOCK_PATH, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    lock_file = os.fdopen(_fd, "w", encoding="utf-8")  # pylint: disable=consider-using-with
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
        log_path = f"{_FEED_TMP_DIR}/feed-{abs(hash(_FEED_ADDR)) % 100000}.log"
        _env_keep = {"PATH", "HOME", "LANG", "VIRTUAL_ENV", "PYTHONPATH", "LC_ALL", "LC_CTYPE"}
        env = {k: v for k, v in os.environ.items() if k in _env_keep}
        env["TRADINEBOTTE_FEED_ADDR"] = _FEED_ADDR
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
    """Main message loop: receive ZMQ messages from feed.py and dispatch to live_bot handlers."""
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

            bot.purge_expired_markets(state)
    finally:
        sock.close(linger=0)
        ctx.term()


async def main() -> None:
    config = bot.make_config()

    logger.info("=" * 65)
    logger.info("  ACCOUNT BOT — dir=%s", config.install_dir)
    logger.info("  Feed : %s", _FEED_ADDR)
    if VERBOSE:
        logger.info("  Mode VERBOSE actif — logs DEBUG complets")
    logger.info("=" * 65)

    if VERBOSE:
        logger.debug("[INIT] TRADINEBOTTE_DIR=%s", config.install_dir)
        logger.debug("[INIT] SIGNAL_THRESHOLD=%.2f", config.signal_threshold)
        logger.debug("[INIT] STAKE=%.2f WIN=%.2f LOSS=%.2f",
                     config.stake, config.win_threshold, config.loss_threshold)

    _ensure_feed()

    if VERBOSE:
        logger.debug("[INIT] init_db...")
    conn  = bot.init_db(config)
    state = bot.BotState(conn, config)
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
