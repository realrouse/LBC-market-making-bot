"""Unit tests for the tradinetools control plane (control_loop dispatch + guard).

The dispatch and encode helpers are socket-free and fully unit-testable. The most
important test is the fail-closed refusal of destructive commands on a live bot —
that branch is never exercised in production (every bot is sim today), so it gets
its first real exercise here.
"""

import asyncio
import importlib.util
import os
import unittest

# Load the inner package __init__ by file path (mirrors test_heartbeat.py).
_INIT = os.path.join(os.path.dirname(__file__), "..", "tradinetools", "__init__.py")
_spec = importlib.util.spec_from_file_location("_tbnt_control", _INIT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

Command      = _mod.Command
_ctl_dispatch = _mod._ctl_dispatch
_ctl_encode   = _mod._ctl_encode
_ctl_is_sim   = _mod._ctl_is_sim


def _run(coro):
    return asyncio.run(coro)


class TestIsSim(unittest.TestCase):
    """Fail-closed: only (is_live False AND mode 'sim') counts as simulation."""

    def test_sim_true_only_when_unambiguous(self):
        self.assertTrue(_ctl_is_sim("sim", False))

    def test_live_flag_blocks(self):
        self.assertFalse(_ctl_is_sim("sim", True))

    def test_mode_none_blocks(self):
        self.assertFalse(_ctl_is_sim(None, False))

    def test_mode_other_blocks(self):
        self.assertFalse(_ctl_is_sim("live", False))


class TestDispatchBuiltins(unittest.TestCase):

    def test_ping(self):
        r = _run(_ctl_dispatch({}, "sim", False, "grid_bot", '{"cmd":"ping"}'))
        self.assertTrue(r["ok"])
        self.assertEqual(r["msg"], "pong")

    def test_status_reports_mode_and_commands(self):
        cmds = {"reset": Command(lambda a: {}, destructive=True, help="x")}
        r = _run(_ctl_dispatch(cmds, "sim", False, "grid_bot", '{"cmd":"status"}'))
        self.assertEqual(r["data"]["mode"], "sim")
        self.assertFalse(r["data"]["is_live"])
        self.assertIn("reset", r["data"]["commands"])
        self.assertIn("ping", r["data"]["commands"])

    def test_help_lists_destructive_flag(self):
        cmds = {"reset": Command(lambda a: {}, destructive=True, help="reset bot")}
        r = _run(_ctl_dispatch(cmds, "sim", False, "grid_bot", '{"cmd":"help"}'))
        self.assertTrue(r["data"]["commands"]["reset"]["destructive"])
        self.assertEqual(r["data"]["commands"]["reset"]["help"], "reset bot")

    def test_unknown_command(self):
        r = _run(_ctl_dispatch({}, "sim", False, "grid_bot", '{"cmd":"nope"}'))
        self.assertFalse(r["ok"])

    def test_invalid_json(self):
        r = _run(_ctl_dispatch({}, "sim", False, "grid_bot", "not json"))
        self.assertFalse(r["ok"])

    def test_non_object_json(self):
        r = _run(_ctl_dispatch({}, "sim", False, "grid_bot", "[1,2,3]"))
        self.assertFalse(r["ok"])


class TestDispatchHandlers(unittest.IsolatedAsyncioTestCase):

    async def test_non_destructive_runs(self):
        calls = []
        cmds = {"info": Command(lambda a: {"echo": a.get("x")})}
        r = await _ctl_dispatch(cmds, "sim", False, "g", '{"cmd":"info","args":{"x":7}}')
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"]["echo"], 7)

    async def test_destructive_runs_on_sim(self):
        flag = {"hit": False}
        def h(_a):
            flag["hit"] = True
            return {"done": True}
        cmds = {"reset": Command(h, destructive=True)}
        r = await _ctl_dispatch(cmds, "sim", False, "g", '{"cmd":"reset"}')
        self.assertTrue(r["ok"])
        self.assertTrue(flag["hit"])

    async def test_destructive_refused_on_live_and_handler_not_called(self):
        """THE critical test: a live bot must refuse reset and never run the handler."""
        flag = {"hit": False}
        def h(_a):
            flag["hit"] = True
            return {}
        cmds = {"reset": Command(h, destructive=True)}
        r = await _ctl_dispatch(cmds, "live", True, "g", '{"cmd":"reset"}')
        self.assertFalse(r["ok"])
        self.assertIn("refused", r["msg"])
        self.assertFalse(flag["hit"], "destructive handler must NOT run on a live bot")

    async def test_destructive_refused_when_mode_unknown(self):
        flag = {"hit": False}
        cmds = {"reset": Command(lambda a: flag.__setitem__("hit", True), destructive=True)}
        r = await _ctl_dispatch(cmds, None, False, "g", '{"cmd":"reset"}')
        self.assertFalse(r["ok"])
        self.assertFalse(flag["hit"])

    async def test_handler_exception_does_not_crash(self):
        def boom(_a):
            raise RuntimeError("kaboom")
        cmds = {"x": Command(boom)}
        r = await _ctl_dispatch(cmds, "sim", False, "g", '{"cmd":"x"}')
        self.assertFalse(r["ok"])
        self.assertIn("command failed", r["msg"])

    async def test_async_handler_awaited(self):
        async def ah(_a):
            return {"async": True}
        cmds = {"x": Command(ah)}
        r = await _ctl_dispatch(cmds, "sim", False, "g", '{"cmd":"x"}')
        self.assertTrue(r["data"]["async"])


class TestEncode(unittest.TestCase):

    def test_serializable(self):
        self.assertEqual(_ctl_encode({"ok": True}), b'{"ok": true}')

    def test_non_serializable_falls_back(self):
        out = _ctl_encode({"ok": True, "data": {"x": object()}})
        self.assertIn(b'"ok": false', out)
        self.assertIn(b"not serializable", out)


class TestControlLoopOverSocket(unittest.IsolatedAsyncioTestCase):
    """End-to-end over a real IPC REP socket: REQ→reply, and REP lockstep holds
    across multiple requests (a wedged REP would fail the second call)."""

    async def _req(self, addr, payload, timeout=3.0):
        import zmq, zmq.asyncio, json
        ctx = zmq.asyncio.Context()
        s = ctx.socket(zmq.REQ)
        s.setsockopt(zmq.LINGER, 0)
        s.setsockopt(zmq.RCVTIMEO, int(timeout * 1000))
        s.connect(addr)
        try:
            await s.send(json.dumps(payload).encode())
            return json.loads(await s.recv())
        finally:
            s.close(linger=0)
            ctx.term()

    async def test_roundtrip_and_lockstep(self):
        import os, asyncio
        addr = f"ipc:///tmp/tbnt-test-ctl-{os.getpid()}.sock"
        hit = {"n": 0}
        cmds = {
            "info":  Command(lambda a: {"echo": a.get("v")}),
            "reset": Command(lambda a: hit.__setitem__("n", hit["n"] + 1),
                             destructive=True),
        }
        task = asyncio.create_task(
            _mod.control_loop("test_bot", cmds, mode="live", is_live=True, addr=addr))
        await asyncio.sleep(0.2)   # let it bind
        try:
            r1 = await self._req(addr, {"cmd": "ping"})
            self.assertEqual(r1["msg"], "pong")
            # destructive on a live bot must be refused, handler not run
            r2 = await self._req(addr, {"cmd": "reset"})
            self.assertFalse(r2["ok"])
            self.assertEqual(hit["n"], 0)
            # lockstep intact: a third request still works
            r3 = await self._req(addr, {"cmd": "info", "args": {"v": 42}})
            self.assertEqual(r3["data"]["echo"], 42)
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    unittest.main()
