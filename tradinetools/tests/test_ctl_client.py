"""Tests for the control-plane REQ client (tradinetools.ctl_client)."""

import asyncio
import os
import threading
import unittest

from tradinetools import Command, control_loop, ctl_client


class TestCtlClientErrors(unittest.TestCase):

    def test_dead_socket_returns_client_error(self):
        addr = f"ipc:///tmp/tbnt-ctlc-dead-{os.getpid()}.sock"
        rc = ctl_client.main(
            ["--bot", "x", "--cmd", "ping", "--addr", addr, "--timeout", "0.3"])
        self.assertEqual(rc, 2)   # could not reach the bot


class TestCtlClientRoundtrip(unittest.TestCase):
    """Sync REQ client against an async REP control_loop in a background loop —
    the exact interop used in production (botctl → ctl_client → bot)."""

    def setUp(self):
        self.addr = f"ipc:///tmp/tbnt-ctlc-{os.getpid()}.sock"
        self.ready = threading.Event()
        self.stop = threading.Event()
        self.hit = {"n": 0}

        def serve():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            cmds = {"reset": Command(
                lambda a: self.hit.__setitem__("n", self.hit["n"] + 1),
                destructive=True)}

            async def run():
                # live bot → reset must be refused
                task = asyncio.create_task(
                    control_loop("t", cmds, mode="live", is_live=True, addr=self.addr))
                await asyncio.sleep(0.2)
                self.ready.set()
                while not self.stop.is_set():
                    await asyncio.sleep(0.05)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            loop.run_until_complete(run())
            loop.close()

        self.th = threading.Thread(target=serve, daemon=True)
        self.th.start()
        self.assertTrue(self.ready.wait(3), "control_loop did not start")

    def tearDown(self):
        self.stop.set()
        self.th.join(3)

    def test_ping_roundtrip(self):
        r = ctl_client.send("t", "ping", addr=self.addr, timeout=2)
        self.assertEqual(r["msg"], "pong")

    def test_reset_refused_on_live_handler_not_called(self):
        r = ctl_client.send("t", "reset", {"capital": 100}, addr=self.addr, timeout=2)
        self.assertFalse(r["ok"])
        self.assertEqual(self.hit["n"], 0)

    def test_main_exit_code_reflects_ok(self):
        # ping ok → 0
        self.assertEqual(
            ctl_client.main(["--bot", "t", "--cmd", "ping", "--addr", self.addr,
                             "--timeout", "2"]), 0)
        # reset refused → 1
        self.assertEqual(
            ctl_client.main(["--bot", "t", "--cmd", "reset", "--capital", "100",
                             "--addr", self.addr, "--timeout", "2"]), 1)


if __name__ == "__main__":
    unittest.main()
