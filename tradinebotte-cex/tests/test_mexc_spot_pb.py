"""MEXC spot protobuf depth decode — validates api_mexc.parse_book_update on a real
captured `spot@public.limit.depth.v3.api.pb` binary frame (no network).

The fixture _mexc_spot_depth_frame.hex is a live 360-byte protobuf depth snapshot
captured from wss://wbs-api.mexc.com/ws. If the vendored proto / _pb2 or the decode
path regresses, these assertions fail offline before any deploy.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api_mexc  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(__file__), "_mexc_spot_depth_frame.hex")

try:
    import google.protobuf  # noqa: F401  pylint: disable=unused-import
    import mexc_spot_depth_pb2  # noqa: F401  pylint: disable=unused-import
    _HAVE_PB = True
except ImportError:
    _HAVE_PB = False


@unittest.skipUnless(_HAVE_PB, "protobuf + mexc_spot_depth_pb2 not installed")
class TestMexcSpotProtobufDecode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(_FIXTURE, encoding="utf-8") as f:
            cls.frame = bytes.fromhex(f.read().strip())

    def test_binary_frame_decodes_to_sane_snapshot(self):
        snap = api_mexc.parse_book_update(self.frame)
        self.assertIsNotNone(snap, "protobuf frame should decode to a book snapshot")
        self.assertIn("best_bid", snap)
        self.assertIn("best_ask", snap)
        bid, ask = snap["best_bid"], snap["best_ask"]
        self.assertGreater(bid, 0.0)
        self.assertGreater(ask, 0.0)
        self.assertLessEqual(bid, ask, "best_bid must not exceed best_ask")
        # Sanity: BTC in a plausible range, tight spread (top-of-book snapshot).
        self.assertTrue(1_000 < bid < 1_000_000)
        self.assertLess((ask - bid) / bid, 0.01, "spread should be < 1%")
        self.assertIn("obi", snap)
        self.assertGreaterEqual(snap["obi"], -1.0)
        self.assertLessEqual(snap["obi"], 1.0)

    def test_text_ack_frame_returns_none(self):
        # The subscribe ACK arrives as a JSON text frame and carries no depth.
        ack = '{"id":0,"code":0,"msg":"spot@public.limit.depth.v3.api.pb@BTCUSDT@5"}'
        self.assertIsNone(api_mexc.parse_book_update(ack))

    def test_garbage_bytes_return_none(self):
        self.assertIsNone(api_mexc.parse_book_update(b"\x00\x01not-protobuf\xff"))

    def test_ws_binary_flag_set(self):
        self.assertTrue(getattr(api_mexc, "WS_BINARY", False))


if __name__ == "__main__":
    unittest.main()
