"""Unit tests for tradinetools.db — shared deployment/status state DB.

All tests run against a throwaway on-disk DB in a temp dir (never /data1).
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tradinetools.db import (
    open_db, upsert_inventory, record_deploy, expected_vs_actual,
)


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


class TestExpectedVsActual(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = open_db(os.path.join(self.dir, "t.db"))
        upsert_inventory(self.db, [
            {"account": "acct2", "bot_name": "live_bot", "kind": "bot"},
            {"account": "acct9", "bot_name": "ghost_bot", "kind": "bot"},
        ])

    def tearDown(self):
        self.db.close()

    def test_silent_bot_has_null_last_ts(self):
        rows = {(r["account"], r["bot_name"]): r for r in expected_vs_actual(self.db)}
        # never sent a heartbeat → visible but last_ts None (today it would be invisible)
        self.assertIsNone(rows[("acct9", "ghost_bot")]["last_ts"])

    def test_reporting_bot_has_last_ts(self):
        self.db.execute(
            "INSERT INTO heartbeats (ts, account, bot_name, version, status)"
            " VALUES (?, 'acct2', 'live_bot', 'v1', 'ok')", (1234567890,))
        self.db.commit()
        rows = {(r["account"], r["bot_name"]): r for r in expected_vs_actual(self.db)}
        self.assertEqual(rows[("acct2", "live_bot")]["last_ts"], 1234567890)

    def test_disabled_row_excluded(self):
        upsert_inventory(self.db, [
            {"account": "acct2", "bot_name": "live_bot", "kind": "bot", "enabled": False},
        ])
        keys = {(r["account"], r["bot_name"]) for r in expected_vs_actual(self.db)}
        self.assertNotIn(("acct2", "live_bot"), keys)


if __name__ == "__main__":
    unittest.main()
