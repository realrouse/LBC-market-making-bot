"""Unit tests for tradinetools.db — shared deployment/status state DB.

All tests run against a throwaway on-disk DB in a temp dir (never /data1).
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tradinetools.db import open_db, upsert_inventory, record_deploy


def _columns(db, table):
    return [(r[1], r[2]) for r in db.execute(f"PRAGMA table_info({table})")]


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = open_db(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.db.close()

    def test_all_tables_created(self):
        names = {r[0] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({"heartbeats", "inventory", "deploys"} & names,
                         {"heartbeats", "inventory", "deploys"})

    def test_wal_enabled(self):
        mode = self.db.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_db_file_group_writable(self):
        """SQLite hard-codes 0644; open_db must chmod to 0660 for cross-user writes."""
        path = os.path.join(self.dir, "t.db")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o660)

    def test_heartbeats_columns_frozen(self):
        """Guard the migration row copy: heartbeats columns must equal the historical set.

        status_collector.py now imports open_db() from this module (its inline schema was
        removed in Phase 2), so parity with the collector is by construction.  What still
        needs guarding is that nobody changes the heartbeats column set in a way that breaks
        copying rows out of the old standalone heartbeat.db — so we freeze the exact
        historical columns (name, type) here.
        """
        expected = [
            ("id", "INTEGER"), ("ts", "INTEGER"), ("account", "TEXT"),
            ("bot_name", "TEXT"), ("version", "TEXT"), ("status", "TEXT"),
            ("bounds_ok", "INTEGER"), ("payload", "TEXT"),
        ]
        ours = [(n, t.upper()) for n, t in _columns(self.db, "heartbeats")]
        self.assertEqual(ours, expected)


class TestInventory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = open_db(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.db.close()

    def _rows(self):
        return [
            {"account": "acct3", "bot_name": "grid_bot", "kind": "bot",
             "bot_type": "cex-grid-binance-sim", "service_unit": "tradinebotte-grid.service",
             "install_dir": "~/tradinebotte-grid", "is_live": False,
             "deploy_script": "x.sh"},
            {"account": "acct1", "bot_name": "feed", "kind": "service",
             "bot_type": "infra-feed", "port": 5557},
        ]

    def test_upsert_inserts(self):
        n = upsert_inventory(self.db, self._rows())
        self.assertEqual(n, 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 2)

    def test_upsert_idempotent(self):
        upsert_inventory(self.db, self._rows())
        upsert_inventory(self.db, self._rows())
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory").fetchone()[0], 2)

    def test_is_live_coerced_to_int(self):
        upsert_inventory(self.db, self._rows())
        val = self.db.execute(
            "SELECT is_live FROM inventory WHERE bot_name='grid_bot'").fetchone()[0]
        self.assertEqual(val, 0)
        # infra service left is_live unset → NULL
        val2 = self.db.execute(
            "SELECT is_live FROM inventory WHERE bot_name='feed'").fetchone()[0]
        self.assertIsNone(val2)

    def test_upsert_updates_changed_field(self):
        upsert_inventory(self.db, self._rows())
        changed = self._rows()
        changed[0]["bot_type"] = "cex-grid-binance-LIVE"
        upsert_inventory(self.db, changed)
        val = self.db.execute(
            "SELECT bot_type FROM inventory WHERE bot_name='grid_bot'").fetchone()[0]
        self.assertEqual(val, "cex-grid-binance-LIVE")

    def test_strategy_type_roundtrips(self):
        rows = self._rows()
        rows[0]["strategy_type"] = "grid"        # trading bot carries a strategy_type
        upsert_inventory(self.db, rows)           # the service row leaves it unset → NULL
        self.assertEqual(self.db.execute(
            "SELECT strategy_type FROM inventory WHERE bot_name='grid_bot'").fetchone()[0], "grid")
        self.assertIsNone(self.db.execute(
            "SELECT strategy_type FROM inventory WHERE bot_name='feed'").fetchone()[0])


class TestMigration(unittest.TestCase):
    def test_open_db_adds_strategy_type_to_legacy_inventory(self):
        """A shared DB whose inventory table predates strategy_type must gain the column on
        open_db (idempotent ALTER) — generate_status reads via raw SELECT, so if the column
        were missing the strategy_type query would blank the inventory."""
        import sqlite3
        path = os.path.join(tempfile.mkdtemp(), "legacy.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE inventory (account TEXT NOT NULL, bot_name TEXT NOT NULL, "
                    "kind TEXT, bot_type TEXT, enabled INTEGER DEFAULT 1, updated_ts INTEGER, "
                    "PRIMARY KEY(account, bot_name))")
        con.commit()
        con.close()
        db = open_db(path)
        cols = {r[1] for r in db.execute("PRAGMA table_info(inventory)")}
        self.assertIn("strategy_type", cols)
        db.close()
        open_db(path).close()   # second open: duplicate-column ALTER is ignored, no raise


class TestDeploys(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = open_db(os.path.join(self.dir, "t.db"))

    def tearDown(self):
        self.db.close()

    def test_record_appends(self):
        record_deploy(self.db, account="acct3", bot_name="grid_bot",
                      git_hash="abc1234", script="deploy_grid_acct3.sh",
                      mode="full", result="OK", deployer="neofutur")
        record_deploy(self.db, account="acct3", bot_name="grid_bot",
                      git_hash="def5678", script="deploy_grid_acct3.sh",
                      mode="full", result="OK", deployer="neofutur")
        rows = self.db.execute(
            "SELECT git_hash FROM deploys ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["abc1234", "def5678"])

    def test_record_sets_ts(self):
        before = int(time.time())
        record_deploy(self.db, account="acct2", bot_name="live_bot")
        ts = self.db.execute("SELECT ts FROM deploys").fetchone()[0]
        self.assertGreaterEqual(ts, before)


if __name__ == "__main__":
    unittest.main()
