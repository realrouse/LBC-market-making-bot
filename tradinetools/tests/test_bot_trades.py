"""Unit tests for the durable bot_trades log (db.store_trade) — the real-money status page's
per-trade source. The push channel can drop OR redeliver and a bot may re-push on restart, so
ingestion MUST be idempotent on the natural key."""

import os
import sys
import tempfile
import unittest

# Repo dir (parent of the `tradinetools` package) only — never the package's own dir, whose
# local zmq.py would shadow the real pyzmq for sibling tests (test_zmq.py).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tradinetools.db import open_db, store_trade  # noqa: E402


def _trade(**over):
    base = dict(account="acct-a", bot_name="mexc-accumulation-lbcusdt-955a99",
                ts_ms=1784289808196, side="buy", reason="live-fill", price=0.002386,
                qty=886.95, quote=2.1162627, fee=0.0, order_id="C02__ABC", maker=True,
                avg_entry_after=0.00241, holdings_after=4213.24, free_after=90.2)
    base.update(over)
    return base


class TestBotTrades(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.db = open_db(os.path.join(self.d, "t.db"))

    def test_schema_present(self):
        tabs = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        self.assertIn("bot_trades", tabs)

    def test_insert_and_idempotent(self):
        self.assertTrue(store_trade(self.db, _trade()))          # first: inserted
        self.assertFalse(store_trade(self.db, _trade()))         # exact dup: ignored
        n = self.db.execute("SELECT count(*) FROM bot_trades").fetchone()[0]
        self.assertEqual(n, 1)

    def test_distinct_trades_coexist(self):
        store_trade(self.db, _trade())
        store_trade(self.db, _trade(ts_ms=1784289809000, side="sell", price=0.0026, qty=800.0))
        n = self.db.execute("SELECT count(*) FROM bot_trades").fetchone()[0]
        self.assertEqual(n, 2)

    def test_maker_and_types_coerced(self):
        store_trade(self.db, _trade(maker=True))
        row = self.db.execute("SELECT maker, price, qty FROM bot_trades").fetchone()
        self.assertEqual(row[0], 1)                              # bool → 0/1
        self.assertAlmostEqual(row[1], 0.002386)
        self.assertAlmostEqual(row[2], 886.95)

    def test_optional_fields_default_null(self):
        t = _trade()
        for k in ("quote", "fee", "order_id", "maker", "avg_entry_after",
                  "holdings_after", "free_after"):
            t.pop(k)
        self.assertTrue(store_trade(self.db, t))
        row = self.db.execute("SELECT order_id, fee, maker FROM bot_trades").fetchone()
        self.assertEqual(row, (None, None, None))


if __name__ == "__main__":
    unittest.main()
