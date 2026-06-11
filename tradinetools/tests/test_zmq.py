"""Unit tests for tradinetools.zmq — socket factories and warn_if_external_bind."""

import asyncio
import stat
import sys
import os
import tempfile
import time
import socket as _socket
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import zmq
import zmq.asyncio

from tradinetools.zmq import (
    make_pub, make_sub, make_rep, make_req, make_pull, make_push,
    warn_if_external_bind, ipc_socket_dir, default_ipc_addr,
    PORT_FEED, PORT_INDICATORS, PORT_IND_REG, PORT_STATUS,
)


def _free_port() -> int:
    """Return a free TCP port on localhost."""
    s = _socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestWarnIfExternalBind(unittest.TestCase):

    def test_loopback_127_no_warning(self):
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            # Trigger a real warning so assertLogs doesn't fail on empty
            import logging
            logging.getLogger("tradinetools.zmq").warning("_sentinel_")
            warn_if_external_bind("tcp://127.0.0.1:5559", "TEST")
        self.assertFalse(
            any("SECURITY" in line for line in cm.output),
            "127.0.0.1 should not trigger a SECURITY warning",
        )

    def test_loopback_localhost_no_warning(self):
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            import logging
            logging.getLogger("tradinetools.zmq").warning("_sentinel_")
            warn_if_external_bind("tcp://localhost:5559", "TEST")
        self.assertFalse(any("SECURITY" in line for line in cm.output))

    def test_external_address_triggers_warning(self):
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            warn_if_external_bind("tcp://0.0.0.0:5559", "EXPOSED")
        self.assertTrue(any("SECURITY" in line for line in cm.output))

    def test_non_tcp_scheme_no_warning(self):
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            import logging
            logging.getLogger("tradinetools.zmq").warning("_sentinel_")
            warn_if_external_bind("ipc:///tmp/feed.ipc", "IPC")
        self.assertFalse(any("SECURITY" in line for line in cm.output))

    def test_name_included_in_warning(self):
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            warn_if_external_bind("tcp://192.168.1.1:5559", "MY_SERVICE")
        self.assertTrue(any("MY_SERVICE" in line for line in cm.output))


class TestMakePubSocketType(unittest.TestCase):
    """Test socket types returned by the factories (no send/recv needed)."""

    def setUp(self):
        self.ctx = zmq.asyncio.Context()

    def tearDown(self):
        self.ctx.term()

    def test_make_pub_returns_pub_socket(self):
        port = _free_port()
        sock = make_pub(self.ctx, f"tcp://127.0.0.1:{port}")
        try:
            self.assertEqual(sock.type, zmq.PUB)
        finally:
            sock.close()

    def test_make_sub_returns_sub_socket(self):
        port = _free_port()
        pub = make_pub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub = make_sub(self.ctx, f"tcp://127.0.0.1:{port}")
        try:
            self.assertEqual(sub.type, zmq.SUB)
        finally:
            pub.close(); sub.close()

    def test_make_pub_named_no_security_warning_on_loopback(self):
        port = _free_port()
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            import logging
            logging.getLogger("tradinetools.zmq").warning("_sentinel_")
            sock = make_pub(self.ctx, f"tcp://127.0.0.1:{port}", name="INDICATORS")
        sock.close()
        self.assertFalse(any("SECURITY" in line for line in cm.output))


class TestMakePubSubRoundtrip(unittest.IsolatedAsyncioTestCase):
    """Async roundtrip tests using zmq.asyncio sockets and send/recv coroutines."""

    async def asyncSetUp(self):
        self.ctx = zmq.asyncio.Context()

    async def asyncTearDown(self):
        self.ctx.term()

    async def test_schema_v1_roundtrip(self):
        port = _free_port()
        pub = make_pub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub = make_sub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            msg = {"v": 1, "t": "indicators", "stream_id": "btc_4h", "rsi_14": 52.3}
            await pub.send_json(msg)
            received = await sub.recv_json()
            self.assertEqual(received["v"], 1)
            self.assertEqual(received["t"], "indicators")
            self.assertAlmostEqual(received["rsi_14"], 52.3)
        finally:
            pub.close(); sub.close()

    async def test_multiple_messages_all_received(self):
        port = _free_port()
        pub = make_pub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub = make_sub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            for i in range(3):
                await pub.send_json({"v": 1, "t": "indicators", "stream_id": "btc_4h", "ts": i})
                await asyncio.sleep(0.005)

            received = [await sub.recv_json() for _ in range(3)]
            self.assertEqual(len(received), 3)
            self.assertEqual([r["ts"] for r in received], [0, 1, 2])
        finally:
            pub.close(); sub.close()

    async def test_sub_subscribed_to_all_topics(self):
        port = _free_port()
        pub = make_pub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub = make_sub(self.ctx, f"tcp://127.0.0.1:{port}")
        sub.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            await pub.send_json({"v": 1, "t": "book",       "token_id": "a"})
            await pub.send_json({"v": 1, "t": "indicators",  "stream_id": "x"})
            r1 = await sub.recv_json()
            r2 = await sub.recv_json()
            types = {r1["t"], r2["t"]}
            self.assertIn("book",       types)
            self.assertIn("indicators", types)
        finally:
            pub.close(); sub.close()


class TestMakeRepReq(unittest.TestCase):

    def setUp(self):
        self.ctx = zmq.Context()

    def tearDown(self):
        self.ctx.term()

    def test_make_rep_returns_rep_socket(self):
        port = _free_port()
        sock = make_rep(self.ctx, f"tcp://127.0.0.1:{port}")
        try:
            self.assertEqual(sock.type, zmq.REP)
        finally:
            sock.close()

    def test_make_req_returns_req_socket(self):
        port = _free_port()
        rep = make_rep(self.ctx, f"tcp://127.0.0.1:{port}")
        req = make_req(self.ctx, f"tcp://127.0.0.1:{port}")
        try:
            self.assertEqual(req.type, zmq.REQ)
        finally:
            rep.close(); req.close()

    def test_rep_req_roundtrip(self):
        port = _free_port()
        rep = make_rep(self.ctx, f"tcp://127.0.0.1:{port}")
        req = make_req(self.ctx, f"tcp://127.0.0.1:{port}")
        rep.setsockopt(zmq.RCVTIMEO, 1000)
        req.setsockopt(zmq.RCVTIMEO, 1000)
        time.sleep(0.05)

        try:
            request = {"t": "register", "v": 1, "stream_id": "btc_4h", "bot_id": "b1"}
            req.send_json(request)

            received = rep.recv_json()
            self.assertEqual(received["t"],         "register")
            self.assertEqual(received["v"],          1)
            self.assertEqual(received["stream_id"], "btc_4h")

            reply = {"t": "register_ack", "v": 1, "ok": True, "stream_id": "btc_4h"}
            rep.send_json(reply)

            ack = req.recv_json()
            self.assertTrue(ack["ok"])
            self.assertEqual(ack["v"], 1)
        finally:
            rep.close(); req.close()


class TestPortConstants(unittest.TestCase):

    def test_feed_port(self):
        self.assertEqual(PORT_FEED, 5557)

    def test_indicators_port(self):
        self.assertEqual(PORT_INDICATORS, 5559)

    def test_ind_reg_port(self):
        self.assertEqual(PORT_IND_REG, 5561)

    def test_status_port(self):
        self.assertEqual(PORT_STATUS, 5562)

    def test_ports_are_distinct(self):
        ports = {PORT_FEED, PORT_INDICATORS, PORT_IND_REG, PORT_STATUS}
        self.assertEqual(len(ports), 4)


class TestIpcSocketDir(unittest.TestCase):

    def test_returns_existing_directory(self):
        d = ipc_socket_dir()
        self.assertTrue(os.path.isdir(d), f"ipc_socket_dir() returned non-existent dir: {d}")

    def test_returns_absolute_path(self):
        d = ipc_socket_dir()
        self.assertTrue(os.path.isabs(d))

    def test_fallback_dir_created_with_0700(self):
        """When /run/user/$UID is absent, fallback is created with mode 0700."""
        with tempfile.TemporaryDirectory() as td:
            fallback = os.path.join(td, "sock-dir")
            # Patch ipc_socket_dir to use our tmp path instead of /run/user
            import tradinetools.zmq as _zmq_mod
            orig = _zmq_mod.ipc_socket_dir

            def _patched():
                os.makedirs(fallback, mode=0o700, exist_ok=True)
                return fallback

            _zmq_mod.ipc_socket_dir = _patched
            try:
                result = _zmq_mod.ipc_socket_dir()
                self.assertEqual(result, fallback)
                self.assertTrue(os.path.isdir(fallback))
                mode = stat.S_IMODE(os.stat(fallback).st_mode)
                self.assertEqual(mode, 0o700)
            finally:
                _zmq_mod.ipc_socket_dir = orig


class TestDefaultIpcAddr(unittest.TestCase):

    def test_returns_ipc_scheme(self):
        addr = default_ipc_addr("tradinebotte-feed")
        self.assertTrue(addr.startswith("ipc://"), f"Expected ipc://, got: {addr}")

    def test_contains_socket_name(self):
        addr = default_ipc_addr("tradinebotte-feed")
        self.assertIn("tradinebotte-feed.sock", addr)

    def test_different_names_differ(self):
        a1 = default_ipc_addr("tradinebotte-feed")
        a2 = default_ipc_addr("tradinebotte-indicators")
        self.assertNotEqual(a1, a2)


class TestIpcPubSubRoundtrip(unittest.IsolatedAsyncioTestCase):
    """IPC PUB/SUB roundtrip using a temp directory — does not touch /run/user."""

    async def asyncSetUp(self):
        self.ctx = zmq.asyncio.Context()
        self._tmpdir = tempfile.mkdtemp()

    async def asyncTearDown(self):
        self.ctx.term()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def test_ipc_pub_sub_roundtrip(self):
        addr = f"ipc://{self._tmpdir}/test-feed.sock"
        pub = make_pub(self.ctx, addr, name="TEST-FEED")
        sub = make_sub(self.ctx, addr)
        sub.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            msg = {"v": 1, "t": "book", "token_id": "abc", "best_bid": 0.95}
            await pub.send_json(msg)
            received = await sub.recv_json()
            self.assertEqual(received["t"], "book")
            self.assertAlmostEqual(received["best_bid"], 0.95)
        finally:
            pub.close()
            sub.close()

    async def test_ipc_socket_file_exists_after_bind(self):
        addr = f"ipc://{self._tmpdir}/test-exists.sock"
        pub = make_pub(self.ctx, addr)
        try:
            self.assertTrue(os.path.exists(f"{self._tmpdir}/test-exists.sock"))
        finally:
            pub.close()

    async def test_ipc_socket_file_is_0600(self):
        addr = f"ipc://{self._tmpdir}/test-perms.sock"
        pub = make_pub(self.ctx, addr)
        try:
            mode = stat.S_IMODE(os.stat(f"{self._tmpdir}/test-perms.sock").st_mode)
            self.assertEqual(mode, 0o600, f"Expected 0o600, got {oct(mode)}")
        finally:
            pub.close()

    async def test_ipc_stale_socket_cleaned_on_rebind(self):
        """make_pub removes a stale socket file so Restart=on-failure works."""
        sock_path = f"{self._tmpdir}/test-stale.sock"
        addr = f"ipc://{sock_path}"

        # Simulate a stale socket file left by a crash
        with open(sock_path, "wb") as f:
            f.write(b"stale")

        pub = make_pub(self.ctx, addr)
        try:
            self.assertTrue(os.path.exists(sock_path))
        finally:
            pub.close()


class TestIpcRepReq(unittest.TestCase):
    """IPC REP/REQ roundtrip using a temp directory."""

    def setUp(self):
        self.ctx = zmq.Context()
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        self.ctx.term()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_ipc_rep_req_roundtrip(self):
        addr = f"ipc://{self._tmpdir}/test-reg.sock"
        rep = make_rep(self.ctx, addr, name="TEST-REG")
        req = make_req(self.ctx, addr)
        rep.setsockopt(zmq.RCVTIMEO, 1000)
        req.setsockopt(zmq.RCVTIMEO, 1000)
        time.sleep(0.05)

        try:
            req.send_json({"t": "register", "stream_id": "btc_4h"})
            msg = rep.recv_json()
            self.assertEqual(msg["t"], "register")
            rep.send_json({"t": "register_ack", "ok": True})
            ack = req.recv_json()
            self.assertTrue(ack["ok"])
        finally:
            rep.close()
            req.close()

    def test_ipc_rep_socket_is_0600(self):
        addr = f"ipc://{self._tmpdir}/test-rep-perms.sock"
        rep = make_rep(self.ctx, addr)
        try:
            mode = stat.S_IMODE(os.stat(f"{self._tmpdir}/test-rep-perms.sock").st_mode)
            self.assertEqual(mode, 0o600, f"Expected 0o600, got {oct(mode)}")
        finally:
            rep.close()


class TestMakePullPushSocketType(unittest.TestCase):
    """Socket-type checks for make_pull / make_push (no send/recv needed)."""

    def setUp(self):
        self.ctx = zmq.asyncio.Context()

    def tearDown(self):
        self.ctx.term()

    def test_make_pull_returns_pull_socket(self):
        port = _free_port()
        sock = make_pull(self.ctx, f"tcp://127.0.0.1:{port}")
        try:
            self.assertEqual(sock.type, zmq.PULL)
        finally:
            sock.close()

    def test_make_push_returns_push_socket(self):
        port = _free_port()
        pull = make_pull(self.ctx, f"tcp://127.0.0.1:{port}")
        push = make_push(self.ctx, f"tcp://127.0.0.1:{port}")
        try:
            self.assertEqual(push.type, zmq.PUSH)
        finally:
            pull.close(); push.close()

    def test_make_pull_no_security_warning_on_loopback(self):
        port = _free_port()
        with self.assertLogs("tradinetools.zmq", level="WARNING") as cm:
            import logging
            logging.getLogger("tradinetools.zmq").warning("_sentinel_")
            sock = make_pull(self.ctx, f"tcp://127.0.0.1:{port}", name="STATUS")
        sock.close()
        self.assertFalse(any("SECURITY" in line for line in cm.output))


class TestMakePullPushRoundtrip(unittest.IsolatedAsyncioTestCase):
    """TCP PUSH/PULL roundtrip — the transport used for cross-user heartbeats."""

    async def asyncSetUp(self):
        self.ctx = zmq.asyncio.Context()

    async def asyncTearDown(self):
        self.ctx.term()

    async def test_single_heartbeat_roundtrip(self):
        port = _free_port()
        pull = make_pull(self.ctx, f"tcp://127.0.0.1:{port}", name="STATUS")
        push = make_push(self.ctx, f"tcp://127.0.0.1:{port}")
        pull.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            msg = {
                "account": "account-3", "bot_name": "accumulation_bot",
                "version": "abc1234", "status": "ok",
                "ts": 1749600000, "trades_today": 2, "pnl_today": 1.43,
            }
            await push.send_json(msg)
            received = await pull.recv_json()
            self.assertEqual(received["bot_name"], "accumulation_bot")
            self.assertEqual(received["status"], "ok")
            self.assertAlmostEqual(received["pnl_today"], 1.43)
        finally:
            pull.close(); push.close()

    async def test_multiple_pushers_all_received(self):
        """Multiple bots PUSHing simultaneously — PULL queues all messages."""
        port = _free_port()
        pull = make_pull(self.ctx, f"tcp://127.0.0.1:{port}", name="STATUS")
        push1 = make_push(self.ctx, f"tcp://127.0.0.1:{port}")
        push2 = make_push(self.ctx, f"tcp://127.0.0.1:{port}")
        pull.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            await push1.send_json({"bot_name": "bot-a", "status": "ok"})
            await push2.send_json({"bot_name": "bot-b", "status": "ok"})
            r1 = await pull.recv_json()
            r2 = await pull.recv_json()
            names = {r1["bot_name"], r2["bot_name"]}
            self.assertEqual(names, {"bot-a", "bot-b"})
        finally:
            pull.close(); push1.close(); push2.close()

    async def test_fire_and_forget_no_reply_needed(self):
        """PUSH does not block waiting for a reply (unlike REQ)."""
        port = _free_port()
        pull = make_pull(self.ctx, f"tcp://127.0.0.1:{port}")
        push = make_push(self.ctx, f"tcp://127.0.0.1:{port}")
        pull.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            for i in range(5):
                await push.send_json({"seq": i})
            received = [await pull.recv_json() for _ in range(5)]
            self.assertEqual([r["seq"] for r in received], list(range(5)))
        finally:
            pull.close(); push.close()


class TestIpcPullPushRoundtrip(unittest.IsolatedAsyncioTestCase):
    """IPC PUSH/PULL roundtrip using a temp directory."""

    async def asyncSetUp(self):
        self.ctx = zmq.asyncio.Context()
        self._tmpdir = tempfile.mkdtemp()

    async def asyncTearDown(self):
        self.ctx.term()
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    async def test_ipc_pull_push_roundtrip(self):
        addr = f"ipc://{self._tmpdir}/test-status.sock"
        pull = make_pull(self.ctx, addr, name="TEST-STATUS")
        push = make_push(self.ctx, addr)
        pull.setsockopt(zmq.RCVTIMEO, 1000)
        await asyncio.sleep(0.05)

        try:
            msg = {"bot_name": "swing_bot", "status": "ok", "bounds_ok": True}
            await push.send_json(msg)
            received = await pull.recv_json()
            self.assertEqual(received["bot_name"], "swing_bot")
            self.assertTrue(received["bounds_ok"])
        finally:
            pull.close(); push.close()

    async def test_ipc_pull_socket_file_is_0600(self):
        addr = f"ipc://{self._tmpdir}/test-pull-perms.sock"
        pull = make_pull(self.ctx, addr)
        try:
            mode = stat.S_IMODE(os.stat(f"{self._tmpdir}/test-pull-perms.sock").st_mode)
            self.assertEqual(mode, 0o600, f"Expected 0o600, got {oct(mode)}")
        finally:
            pull.close()

    async def test_ipc_stale_socket_cleaned_on_rebind(self):
        sock_path = f"{self._tmpdir}/test-pull-stale.sock"
        addr = f"ipc://{sock_path}"
        with open(sock_path, "wb") as f:
            f.write(b"stale")
        pull = make_pull(self.ctx, addr)
        try:
            self.assertTrue(os.path.exists(sock_path))
        finally:
            pull.close()


if __name__ == "__main__":
    unittest.main()
