"""tradinetools — shared utilities for the tradinebotte ecosystem."""

import asyncio
import inspect
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable

__version__ = "0.1.0"

_hb_logger = logging.getLogger("tradinetools.heartbeat")
_ctl_logger = logging.getLogger("tradinetools.control")


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
    *,
    mode: str | None = None,
) -> dict[str, Any]:
    """Build a heartbeat payload dict.

    The `account` field resolves from TRADINEBOTTE_ACCOUNT env var first, then USER.
    `mode` is the bot's self-reported real-vs-sim mode ("live" | "sim"), decided at
    startup from a deterministic credential/config signal — never the runtime order-id
    heuristic (a freshly-restarted sim bot with no open orders would mislabel as live).
    When None the field is omitted (e.g. infra services with no sim/live concept).
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
    if mode is not None:
        payload["mode"] = mode
    payload.update(extra)
    return payload


async def heartbeat_loop(
    bot_name: str,
    install_dir: str | None,
    get_extra: Callable[[], dict[str, Any]],
    *,
    mode: str | None = None,
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
                payload = build_heartbeat(bot_name, install_dir, get_extra(), mode=mode)
                await sock.send(json.dumps(payload).encode())
            except Exception as exc:
                _hb_logger.warning("heartbeat send failed: %s", exc)
            cycle += 1
            sleep_s = warmup_interval if cycle < warmup_count else interval
            await asyncio.sleep(sleep_s)
    finally:
        sock.close(linger=0)
        ctx.term()


# ─── CONTROL PLANE ────────────────────────────────────────────────────────────
# An extensible request/reply control channel mounted beside heartbeat_loop in
# every bot/service. Transport is an IPC REP socket (per-UID, chmod 0600) reached
# by the operator via SSH-as-the-bot-user — no network surface. The first command
# is `reset` (sim-only); more will follow. Destructive commands are refused
# fail-closed inside the process, which is the source of truth on its own mode.

_CTL_BUILTINS = ("ping", "status", "help")


@dataclass
class Command:
    """A control-plane command.

    handler:     callable(args: dict) -> dict | None | awaitable[...]. The return
                 value (or {} for None) becomes the reply's "data" field.
    destructive: when True the command is refused unless the bot is unambiguously
                 in simulation (is_live is False AND mode == "sim") — fail-closed.
    help:        one-line description surfaced by the built-in `help` command.
    """
    handler: Callable[[dict[str, Any]], Any]
    destructive: bool = False
    help: str = ""


def _ctl_is_sim(mode: str | None, is_live: bool) -> bool:
    """Fail-closed: True only when the bot is unambiguously in simulation.

    Any ambiguity (is_live truthy, mode missing or not exactly "sim") returns
    False, so a destructive command is refused unless we are certain it is safe.
    """
    return is_live is False and mode == "sim"


def _ctl_encode(reply: dict[str, Any]) -> bytes:
    """Serialize a reply, falling back to a static error if it is not JSON-able.

    Guarantees the REP socket always has exactly one send per recv — a handler
    returning a non-serializable object must not wedge the channel.
    """
    try:
        return json.dumps(reply).encode()
    except Exception:  # pylint: disable=broad-exception-caught
        return b'{"ok": false, "msg": "internal: reply not serializable"}'


async def _ctl_dispatch(
    commands: dict[str, Command],
    mode: str | None,
    is_live: bool,
    bot_name: str,
    raw: bytes | str,
) -> dict[str, Any]:
    """Parse one request and return a reply dict. Never raises."""
    try:
        req = json.loads(raw)
        if not isinstance(req, dict):
            raise ValueError("not an object")
    except Exception:  # pylint: disable=broad-exception-caught
        return {"ok": False, "msg": "invalid request (expected a JSON object)"}

    cmd = req.get("cmd")
    args = req.get("args")
    if not isinstance(args, dict):
        args = {}

    if cmd == "ping":
        return {"ok": True, "msg": "pong", "data": {"bot_name": bot_name}}
    if cmd == "status":
        return {"ok": True, "data": {
            "bot_name": bot_name,
            "mode": mode,
            "is_live": bool(is_live),
            "commands": sorted([*_CTL_BUILTINS, *commands]),
        }}
    if cmd == "help":
        return {"ok": True, "data": {
            "builtins": list(_CTL_BUILTINS),
            "commands": {n: {"help": c.help, "destructive": c.destructive}
                         for n, c in commands.items()},
        }}

    c = commands.get(cmd)
    if c is None:
        return {"ok": False, "msg": f"unknown command: {cmd!r}"}
    if c.destructive and not _ctl_is_sim(mode, is_live):
        return {"ok": False, "msg": (
            f"refused: {cmd!r} is destructive and allowed only on simulation bots "
            f"(mode={mode!r}, is_live={bool(is_live)})")}
    try:
        result = c.handler(args)
        if inspect.isawaitable(result):
            result = await result
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _ctl_logger.warning("control command %r failed: %s", cmd, exc)
        return {"ok": False, "msg": f"command failed: {exc}"}
    return {"ok": True, "data": result if result is not None else {}}


async def control_loop(
    bot_name: str,
    commands: dict[str, Command] | None = None,
    *,
    mode: str | None = None,
    is_live: bool = False,
    addr: str | None = None,
) -> None:
    """Serve the control channel until cancelled.

    Binds an IPC REP socket at TRADINEBOTTE_CTL_ADDR or
    ipc://<runtime>/tradinebotte-ctl-<bot_name>.sock (owner-only). Swallows all
    errors except CancelledError so a control-channel fault never takes down a
    trading bot; a bind failure logs and returns (no control channel, bot lives).
    """
    commands = commands or {}
    try:
        import zmq  # noqa: PLC0415
        import zmq.asyncio  # noqa: PLC0415
        from tradinetools.zmq import default_ipc_addr, make_rep_async  # noqa: PLC0415
    except ImportError:
        _ctl_logger.warning("pyzmq not installed — control channel disabled")
        return

    addr = addr or os.environ.get("TRADINEBOTTE_CTL_ADDR") \
        or default_ipc_addr(f"tradinebotte-ctl-{bot_name}")
    ctx = zmq.asyncio.Context()
    try:
        sock = make_rep_async(ctx, addr)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        _ctl_logger.warning("control bind failed at %s: %s — channel disabled", addr, exc)
        ctx.term()
        return
    sock.setsockopt(zmq.LINGER, 0)
    _ctl_logger.info("control channel on %s — %d command(s): %s",
                     addr, len(commands), ", ".join(sorted(commands)) or "(builtins only)")
    try:
        while True:
            try:
                raw = await sock.recv()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _ctl_logger.warning("control recv error: %s", exc)
                await asyncio.sleep(1)   # avoid a busy loop on a wedged socket
                continue
            reply = await _ctl_dispatch(commands, mode, is_live, bot_name, raw)
            # REP lockstep: exactly one send per recv, always serializable.
            try:
                await sock.send(_ctl_encode(reply))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pylint: disable=broad-exception-caught
                _ctl_logger.warning("control send error: %s", exc)
    except asyncio.CancelledError:
        raise
    finally:
        sock.close(linger=0)
        ctx.term()
