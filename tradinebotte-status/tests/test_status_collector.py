"""Unit tests for status_collector — store_heartbeat and open_db."""

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from status_collector import open_db, store_heartbeat


class TestOpenDb(unittest.TestCase):

    def test_creates_heartbeats_table(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = open_db(db_path)
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            db.close()
            self.assertIn("heartbeats", names)
        finally:
            os.unlink(db_path)

    def test_idempotent_second_open(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = open_db(db_path)
            db.close()
            db2 = open_db(db_path)
            count = db2.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
            db2.close()
            self.assertEqual(count, 0)
        finally:
            os.unlink(db_path)

    def test_creates_indexes(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            db = open_db(db_path)
            idx_names = {
                r[0] for r in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            db.close()
            self.assertIn("idx_heartbeats_account_bot", idx_names)
            self.assertIn("idx_heartbeats_ts", idx_names)
        finally:
            os.unlink(db_path)


class TestStoreHeartbeat(unittest.TestCase):

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = open_db(self.db_path)

    def tearDown(self):
        self.db.close()
        os.unlink(self.db_path)

    def _fetch_one(self) -> dict:
        row = self.db.execute(
            "SELECT ts, account, bot_name, version, status, bounds_ok, payload"
            " FROM heartbeats ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self.assertIsNotNone(row)
        keys = ("ts", "account", "bot_name", "version", "status", "bounds_ok", "payload")
        return dict(zip(keys, row))

    def test_full_payload_stored(self):
        payload = {
            "ts": 1718000000,
            "account": "acct-a",
            "bot_name": "account_bot",
            "version": "e983770",
            "status": "running",
            "bounds_ok": True,
        }
        store_heartbeat(self.db, payload)
        row = self._fetch_one()
        self.assertEqual(row["ts"], 1718000000)
        self.assertEqual(row["account"], "acct-a")
        self.assertEqual(row["bot_name"], "account_bot")
        self.assertEqual(row["version"], "e983770")
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["bounds_ok"], 1)

    def test_missing_account_defaults_to_unknown(self):
        store_heartbeat(self.db, {"bot_name": "swing", "ts": 1})
        row = self._fetch_one()
        self.assertEqual(row["account"], "unknown")

    def test_missing_bot_name_defaults_to_unknown(self):
        store_heartbeat(self.db, {"account": "acct-x", "ts": 1})
        row = self._fetch_one()
        self.assertEqual(row["bot_name"], "unknown")

    def test_bounds_ok_true_stored_as_1(self):
        store_heartbeat(self.db, {"account": "a", "bot_name": "b", "bounds_ok": True, "ts": 1})
        row = self._fetch_one()
        self.assertEqual(row["bounds_ok"], 1)

    def test_bounds_ok_false_stored_as_0(self):
        store_heartbeat(self.db, {"account": "a", "bot_name": "b", "bounds_ok": False, "ts": 1})
        row = self._fetch_one()
        self.assertEqual(row["bounds_ok"], 0)

    def test_bounds_ok_none_stored_as_null(self):
        store_heartbeat(self.db, {"account": "a", "bot_name": "b", "ts": 1})
        row = self._fetch_one()
        self.assertIsNone(row["bounds_ok"])

    def test_missing_ts_uses_current_time(self):
        before = int(time.time()) - 2
        store_heartbeat(self.db, {"account": "a", "bot_name": "b"})
        after = int(time.time()) + 2
        row = self._fetch_one()
        self.assertGreaterEqual(row["ts"], before)
        self.assertLessEqual(row["ts"], after)

    def test_payload_column_is_valid_json(self):
        payload = {"account": "acct-b", "bot_name": "grid", "ts": 42, "extra": [1, 2]}
        store_heartbeat(self.db, payload)
        row = self._fetch_one()
        recovered = json.loads(row["payload"])
        self.assertEqual(recovered["extra"], [1, 2])

    def test_extra_fields_preserved_in_payload(self):
        payload = {"account": "a", "bot_name": "b", "ts": 1, "custom_metric": 3.14}
        store_heartbeat(self.db, payload)
        row = self._fetch_one()
        recovered = json.loads(row["payload"])
        self.assertAlmostEqual(recovered["custom_metric"], 3.14)

    def test_multiple_rows_distinct_accounts(self):
        store_heartbeat(self.db, {"account": "acct-1", "bot_name": "bot", "ts": 1})
        store_heartbeat(self.db, {"account": "acct-2", "bot_name": "bot", "ts": 2})
        count = self.db.execute("SELECT count(*) FROM heartbeats").fetchone()[0]
        self.assertEqual(count, 2)


class TestDefaultStatusAddr(unittest.TestCase):

    def test_returns_tcp_loopback_5562(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../tradinetools"))
        from tradinetools.zmq import default_status_addr, PORT_STATUS
        addr = default_status_addr()
        self.assertEqual(addr, f"tcp://127.0.0.1:{PORT_STATUS}")
        self.assertIn("127.0.0.1", addr)
        self.assertNotIn("ipc://", addr)


if __name__ == "__main__":
    unittest.main()
