"""Control-plane REQ client.

Runs AS the bot's OS user on the bot host — the IPC control socket is per-UID, so
the client must share the bot's uid. The botctl operator CLI invokes this over an
SSH-as-the-bot-user session:

    python -m tradinetools.ctl_client --bot grid_bot --cmd ping
    python -m tradinetools.ctl_client --bot grid_bot --cmd reset --capital 2000

Prints the reply as one JSON line. Exit code: 0 if reply ok, 1 if the bot refused
or errored, 2 if the client could not reach the bot.
"""

import argparse
import json
import sys
from typing import Any


def send(
    bot: str,
    cmd: str,
    args: dict[str, Any] | None = None,
    *,
    addr: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Send one command to a bot's control socket and return the parsed reply."""
    import zmq  # noqa: PLC0415
    from tradinetools.zmq import default_ipc_addr  # noqa: PLC0415

    target = addr or default_ipc_addr(f"tradinebotte-ctl-{bot}")
    ctx = zmq.Context()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, int(timeout * 1000))
    sock.connect(target)
    try:
        sock.send(json.dumps({"cmd": cmd, "args": args or {}}).encode())
        return json.loads(sock.recv())
    finally:
        sock.close(linger=0)
        ctx.term()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tradinetools.ctl_client")
    p.add_argument("--bot", required=True, help="bot_name (matches its heartbeat)")
    p.add_argument("--cmd", required=True, help="ping | status | help | reset | ...")
    p.add_argument("--capital", type=float, default=None,
                   help="starting capital for `reset`")
    p.add_argument("--addr", default=None, help="override IPC address (testing)")
    p.add_argument("--timeout", type=float, default=5.0)
    a = p.parse_args(argv)

    args: dict[str, Any] = {}
    if a.capital is not None:
        args["capital"] = a.capital

    try:
        reply = send(a.bot, a.cmd, args, addr=a.addr, timeout=a.timeout)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        print(json.dumps({"ok": False, "msg": f"client error: {exc}"}))
        return 2
    print(json.dumps(reply))
    return 0 if reply.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
