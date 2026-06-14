"""Unit tests for the control-plane reset helpers (marker + boot-time wipe)."""

import importlib.util
import json
import os
import tempfile
import unittest

_INIT = os.path.join(os.path.dirname(__file__), "..", "tradinetools", "__init__.py")
_spec = importlib.util.spec_from_file_location("_tbnt_reset", _INIT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

write_reset_marker   = _mod.write_reset_marker
consume_reset_marker = _mod.consume_reset_marker
reset_marker_path    = _mod.reset_marker_path


def _make_db(path, content=b"DBDATA"):
    with open(path, "wb") as f:
        f.write(content)


class TestResetMarker(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name
        self.db = os.path.join(self.dir, "live.db")
        self.addCleanup(self._tmp.cleanup)

    def test_write_marker_records_capital(self):
        write_reset_marker(self.dir, 250.0)
        with open(reset_marker_path(self.dir), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["capital"], 250.0)
        self.assertIn("ts", data)

    def test_no_marker_returns_none_and_keeps_db(self):
        _make_db(self.db)
        self.assertIsNone(consume_reset_marker(self.dir, self.db))
        self.assertTrue(os.path.exists(self.db))

    def test_consume_wipes_db_backs_up_and_returns_capital(self):
        _make_db(self.db)
        write_reset_marker(self.dir, 100.0)
        cap = consume_reset_marker(self.dir, self.db)
        self.assertEqual(cap, 100.0)
        self.assertFalse(os.path.exists(self.db), "db should be wiped")
        self.assertFalse(os.path.exists(reset_marker_path(self.dir)), "marker removed")
        backups = os.listdir(os.path.join(self.dir, "backups"))
        self.assertTrue(any(b.endswith(".reset.bak") for b in backups), "backup written")

    def test_consume_removes_wal_and_shm_sidecars(self):
        _make_db(self.db)
        _make_db(self.db + "-wal")
        _make_db(self.db + "-shm")
        write_reset_marker(self.dir, 100.0)
        consume_reset_marker(self.dir, self.db)
        self.assertFalse(os.path.exists(self.db + "-wal"))
        self.assertFalse(os.path.exists(self.db + "-shm"))

    def test_backup_failure_preserves_db_and_aborts(self):
        _make_db(self.db, b"PRECIOUS")
        write_reset_marker(self.dir, 100.0)
        # backup_dir points at an existing *file* → makedirs fails → wipe aborted
        bad = os.path.join(self.dir, "not_a_dir")
        _make_db(bad)
        cap = consume_reset_marker(self.dir, self.db, backup_dir=bad)
        self.assertIsNone(cap)
        self.assertTrue(os.path.exists(self.db), "db preserved when backup fails")
        with open(self.db, "rb") as f:
            self.assertEqual(f.read(), b"PRECIOUS")
        self.assertFalse(os.path.exists(reset_marker_path(self.dir)), "marker removed")

    def test_idempotent_second_consume_is_noop(self):
        _make_db(self.db)
        write_reset_marker(self.dir, 100.0)
        consume_reset_marker(self.dir, self.db)
        # a crash-restart loop must not re-wipe / re-trigger
        self.assertIsNone(consume_reset_marker(self.dir, self.db))

    def test_marker_without_capital_still_wipes(self):
        _make_db(self.db)
        # hand-write a marker lacking the capital field
        with open(reset_marker_path(self.dir), "w", encoding="utf-8") as f:
            json.dump({"ts": 1}, f)
        cap = consume_reset_marker(self.dir, self.db)
        self.assertIsNone(cap)
        self.assertFalse(os.path.exists(self.db))


if __name__ == "__main__":
    unittest.main()
