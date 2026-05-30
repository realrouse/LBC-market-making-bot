"""Unit tests for tradinetools.zmq — socket factories and warn_if_external_bind."""

import asyncio
import sys
import os
import time
import socket as _socket
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import zmq
import zmq.asyncio

from tradinetools.zmq import (
    make_pub, make_sub, make_rep, make_req,
    warn_if_external_bind,
    PORT_FEED, PORT_INDICATORS, PORT_IND_REG,
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

    def test_ports_are_distinct(self):
        ports = {PORT_FEED, PORT_INDICATORS, PORT_IND_REG}
        self.assertEqual(len(ports), 3)


if __name__ == "__main__":
    unittest.main()
