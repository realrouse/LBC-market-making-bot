"""tradinetools — shared utilities for the tradinebotte ecosystem."""

import asyncio
import json
import logging
import os
import time
from typing import Any, Callable

__version__ = "0.1.0"

_hb_logger = logging.getLogger("tradinetools.heartbeat")


def read_version_stamp(install_dir: str | None = None) -> str:
    """Return the deployed git commit hash for this bot instance.

    Priority:
      1. TRADINEBOTTE_VERSION env var (set by CI or manual override)
      2. version.stamp file in install_dir (written by deploy scripts at restart time)
      3. "unknown" fallback

    The version.stamp file contains a short git hash written by the deploy script
    immediately before restarting the bot process, so the running bot always knows
    which exact commit it is executing.
    """
    env_ver = os.environ.get("TRADINEBOTTE_VERSION", "").strip()
    if env_ver:
        return env_ver

    search_dirs = []
    if install_dir:
        search_dirs.append(install_dir)
    search_dirs.append(os.path.expanduser("~/tradinebotte"))
    search_dirs.append(os.getcwd())

    for d in search_dirs:
        stamp = os.path.join(d, "version.stamp")
        try:
            with open(stamp, encoding="utf-8") as f:
                ver = f.read().strip()
            if ver:
                return ver
        except OSError:
            continue

    return "unknown"


def build_heartbeat(
    bot_name: str,
    install_dir: str | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Build a heartbeat payload dict.

    The `account` field resolves from TRADINEBOTTE_ACCOUNT env var first, then USER.
    The `extra` dict is merged last so bot-specific fields (bounds_ok, etc.) override
    defaults if there is a name collision.
    """
    account = os.environ.get("TRADINEBOTTE_ACCOUNT") or os.environ.get("USER", "unknown")
    payload: dict[str, Any] = {
        "ts": int(time.time()),
        "bot_name": bot_name,
        "account": account,
        "version": read_version_stamp(install_dir),
        "status": "running",
    }
    payload.update(extra)
    return payload


async def heartbeat_loop(
    bot_name: str,
    install_dir: str | None,
    get_extra: Callable[[], dict[str, Any]],
    *,
    interval: int = 3600,
    warmup_interval: int = 60,
    warmup_count: int = 3,
) -> None:
    """Send a JSON heartbeat to the status collector at regular intervals.

    Fires immediately on startup.  The first `warmup_count` sleep cycles use
    `warmup_interval` seconds so the collector picks up the bot quickly after
    deployment; subsequent cycles use `interval` seconds.

    `TRADINEBOTTE_HB_INTERVAL` env var overrides `interval` at runtime.

    Swallows all exceptions except CancelledError so a collector outage never
    crashes a bot.  Callers must cancel this task to stop it.
    """
    try:
        import zmq  # noqa: PLC0415
        import zmq.asyncio  # noqa: PLC0415
        from tradinetools.zmq import default_status_addr, make_push  # noqa: PLC0415
    except ImportError:
        _hb_logger.warning("pyzmq not installed — heartbeat disabled")
        return

    env_interval = os.environ.get("TRADINEBOTTE_HB_INTERVAL")
    if env_interval:
        try:
            interval = int(env_interval)
        except ValueError:
            _hb_logger.warning("TRADINEBOTTE_HB_INTERVAL=%r is not an integer — ignored", env_interval)

    addr = os.environ.get("TRADINEBOTTE_STATUS_ADDR") or default_status_addr()
    ctx = zmq.asyncio.Context()
    sock = make_push(ctx, addr)
    sock.setsockopt(zmq.LINGER, 0)
    cycle = 0
    try:
        while True:
            try:
                payload = build_heartbeat(bot_name, install_dir, get_extra())
                await sock.send(json.dumps(payload).encode())
            except Exception as exc:
                _hb_logger.warning("heartbeat send failed: %s", exc)
            cycle += 1
            sleep_s = warmup_interval if cycle < warmup_count else interval
            await asyncio.sleep(sleep_s)
    finally:
        sock.close(linger=0)
        ctx.term()
