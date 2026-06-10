"""ZMQ socket factory helpers for tradinebotte services."""
import logging
import os
import stat

import zmq
import zmq.asyncio

logger = logging.getLogger(__name__)

# Default port assignments
PORT_FEED       = 5557   # feed.py PUB (Polymarket order book)
PORT_FEED_ALT   = 5558   # feed.py PUB alternate
PORT_INDICATORS = 5559   # indicators.py PUB
PORT_IND_REG    = 5561   # indicators.py REP (registration)
PORT_STATUS     = 5562   # status_collector.py PULL (bot heartbeats)


def ipc_socket_dir() -> str:
    """Return the directory for IPC socket files, creating it when necessary.

    Uses /run/user/$UID when available (systemd-logind sets 0700 automatically).
    Falls back to /tmp/tradinebotte-$UID/ (created with 0700) when systemd-logind
    is absent or the session runtime directory has not yet been created.
    """
    uid = os.getuid()
    run_user = f"/run/user/{uid}"
    if os.path.isdir(run_user):
        return run_user
    fallback = f"/tmp/tradinebotte-{uid}"
    os.makedirs(fallback, mode=0o700, exist_ok=True)
    return fallback


def default_ipc_addr(name: str) -> str:
    """Return the default IPC address for a named socket.

    Example: default_ipc_addr("tradinebotte-feed")
             → "ipc:///run/user/1000/tradinebotte-feed.sock"
    """
    return f"ipc://{ipc_socket_dir()}/{name}.sock"


def _prepare_ipc_bind(addr: str) -> None:
    """Remove a stale IPC socket file before binding.

    ZMQ bind() fails with EADDRINUSE if the socket file still exists from a
    previous (crashed) run.  Unlinking it first makes Restart=on-failure work.
    """
    if not addr.startswith("ipc://"):
        return
    path = addr[len("ipc://"):]
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _chmod_ipc(addr: str) -> None:
    """Restrict an IPC socket file to owner-only (0600) after bind."""
    if not addr.startswith("ipc://"):
        return
    path = addr[len("ipc://"):]
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def warn_if_external_bind(addr: str, name: str) -> None:
    """Warn when a ZMQ socket is bound to a non-loopback address."""
    if addr.startswith("tcp://") and "127.0.0.1" not in addr and "localhost" not in addr:
        logger.warning(
            "SECURITY: %s (%s) bound to non-loopback — "
            "ensure ZMQ CURVE auth is active before exposing to the network.",
            name, addr,
        )


def make_pub(ctx: zmq.asyncio.Context, addr: str, name: str = "PUB") -> zmq.asyncio.Socket:
    """Bind a PUB socket to addr and return it."""
    warn_if_external_bind(addr, name)
    _prepare_ipc_bind(addr)
    sock = ctx.socket(zmq.PUB)
    sock.bind(addr)
    _chmod_ipc(addr)
    return sock


def make_sub(ctx: zmq.asyncio.Context, addr: str) -> zmq.asyncio.Socket:
    """Connect a SUB socket to addr, subscribe to all topics, return it."""
    sock = ctx.socket(zmq.SUB)
    sock.connect(addr)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    return sock


def make_rep(ctx: zmq.Context, addr: str, name: str = "REP") -> zmq.Socket:
    """Bind a synchronous REP socket to addr and return it."""
    warn_if_external_bind(addr, name)
    _prepare_ipc_bind(addr)
    sock = ctx.socket(zmq.REP)
    sock.bind(addr)
    _chmod_ipc(addr)
    return sock


def make_req(ctx: zmq.Context, addr: str) -> zmq.Socket:
    """Connect a synchronous REQ socket to addr and return it."""
    sock = ctx.socket(zmq.REQ)
    sock.connect(addr)
    return sock


def make_pull(ctx: zmq.asyncio.Context, addr: str, name: str = "PULL") -> zmq.asyncio.Socket:
    """Bind an async PULL socket to addr and return it."""
    warn_if_external_bind(addr, name)
    _prepare_ipc_bind(addr)
    sock = ctx.socket(zmq.PULL)
    sock.bind(addr)
    _chmod_ipc(addr)
    return sock


def make_push(ctx: zmq.asyncio.Context, addr: str) -> zmq.asyncio.Socket:
    """Connect an async PUSH socket to addr and return it."""
    sock = ctx.socket(zmq.PUSH)
    sock.connect(addr)
    return sock
