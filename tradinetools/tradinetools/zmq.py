"""ZMQ socket factory helpers for tradinebotte services."""

import logging

import zmq
import zmq.asyncio

logger = logging.getLogger(__name__)

# Default port assignments
PORT_FEED       = 5557   # feed.py PUB (Polymarket order book)
PORT_FEED_ALT   = 5558   # feed.py PUB alternate
PORT_INDICATORS = 5559   # indicators.py PUB
PORT_IND_REG    = 5561   # indicators.py REP (registration)


def warn_if_external_bind(addr: str, name: str) -> None:
    """Warn when a ZMQ socket is bound to a non-loopback address."""
    if addr.startswith("tcp://") and "127.0.0.1" not in addr and "localhost" not in addr:
        logger.warning(
            "SECURITY: %s (%s) bound to non-loopback — "
            "ensure ZMQ CURVE auth is active before exposing to the network.",
            name, addr,
        )


def make_pub(ctx: zmq.asyncio.Context, addr: str) -> zmq.asyncio.Socket:
    """Bind a PUB socket to addr and return it."""
    warn_if_external_bind(addr, "PUB")
    sock = ctx.socket(zmq.PUB)
    sock.bind(addr)
    return sock


def make_sub(ctx: zmq.asyncio.Context, addr: str) -> zmq.asyncio.Socket:
    """Connect a SUB socket to addr, subscribe to all topics, return it."""
    sock = ctx.socket(zmq.SUB)
    sock.connect(addr)
    sock.setsockopt(zmq.SUBSCRIBE, b"")
    return sock


def make_rep(ctx: zmq.Context, addr: str) -> zmq.Socket:
    """Bind a synchronous REP socket to addr and return it."""
    warn_if_external_bind(addr, "REP")
    sock = ctx.socket(zmq.REP)
    sock.bind(addr)
    return sock


def make_req(ctx: zmq.Context, addr: str) -> zmq.Socket:
    """Connect a synchronous REQ socket to addr and return it."""
    sock = ctx.socket(zmq.REQ)
    sock.connect(addr)
    return sock
