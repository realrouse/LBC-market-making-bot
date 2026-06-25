"""Unit tests for tradinetools.health_server — the opt-in HTTP /health endpoint."""

import asyncio
import importlib.util
import json
import os
import socket
import unittest

# `tradinetools` is a namespace package at the repo level; health_server lives in the
# inner package __init__, so load it by file path to sidestep the namespace-vs-package
# import ambiguity in the test runner (same pattern as test_heartbeat.py).
_INIT = os.path.join(os.path.dirname(__file__), "..", "tradinetools", "__init__.py")
_spec = importlib.util.spec_from_file_location("_tbnt_health", _INIT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
health_server = _mod.health_server


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestHealthDisabled(unittest.TestCase):
    """With no port configured the coroutine must return at once, binding nothing."""

    def test_returns_immediately_when_env_unset(self):
        os.environ.pop("TRADINEBOTTE_HEALTH_PORT", None)

        async def run():
            # If it tried to serve, this would block until timeout.
            await asyncio.wait_for(
                health_server("bot_x", None, lambda: {}, mode="sim"), timeout=2)

        asyncio.run(run())

    def test_returns_immediately_on_invalid_port(self):
        os.environ["TRADINEBOTTE_HEALTH_PORT"] = "not-an-int"
        try:
            async def run():
                await asyncio.wait_for(
                    health_server("bot_x", None, lambda: {}, mode="sim"), timeout=2)
            asyncio.run(run())
        finally:
            os.environ.pop("TRADINEBOTTE_HEALTH_PORT", None)


class TestHealthServing(unittest.TestCase):
    """When enabled it serves the reused payload and shuts down cleanly on cancel."""

    def test_serves_payload_and_stops_on_cancel(self):
        port = _free_port()
        os.environ["TRADINEBOTTE_ACCOUNT"] = "testacct"

        def extra():
            return {"capital": 1234.56, "open_trades": 2, "daily_pnl": -3.4}

        async def run():
            import aiohttp  # client; runs in the same loop without blocking it
            task = asyncio.create_task(
                health_server("bot_x", None, extra, mode="sim", port=port))
            await asyncio.sleep(0.5)  # let the site bind
            async with aiohttp.ClientSession() as sess:
                async with sess.get(f"http://127.0.0.1:{port}/health") as resp:
                    self.assertEqual(resp.status, 200)
                    body = json.loads(await resp.text())

            # payload is the heartbeat shape + uptime_s, with the reused stats merged in.
            self.assertEqual(body["status"], "running")
            self.assertEqual(body["bot_name"], "bot_x")
            self.assertEqual(body["account"], "testacct")
            self.assertEqual(body["mode"], "sim")
            self.assertEqual(body["capital"], 1234.56)
            self.assertEqual(body["open_trades"], 2)
            self.assertIn("uptime_s", body)

            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # after cancel the port must be released (connection refused)
            await asyncio.sleep(0.2)
            with self.assertRaises(aiohttp.ClientError):
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(f"http://127.0.0.1:{port}/health",
                                        timeout=aiohttp.ClientTimeout(total=2)):
                        pass

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
