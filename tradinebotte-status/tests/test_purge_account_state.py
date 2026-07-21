"""Unit tests for purge_account_state — the shared-DB cleanup for the ephemeral test
account. The load-bearing case is the inventory guard: it must NEVER delete a prod
account's rows even if a clobbered TEST_STANDALONE_USER_IDX points the caller at one.
"""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import purge_account_state as p  # noqa: E402


def _db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE heartbeats (ts INT, account TEXT, bot_name TEXT, payload TEXT)")
    db.execute("CREATE TABLE deploys (ts INT, account TEXT, bot_name TEXT, result TEXT)")
    db.execute("CREATE TABLE bot_trades (account TEXT, bot_name TEXT, ts_ms INT)")
    db.execute("CREATE TABLE inventory (account TEXT, bot_name TEXT, enabled INT)")
    return db


class TestPurge(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        db = _db(self.path)
        # prod account 'acctP' is in inventory + has rows; test account 'acctT' is NOT.
        db.execute("INSERT INTO inventory VALUES ('acctP','bot-p',1)")
        for a in ("acctP", "acctT"):
            db.executemany("INSERT INTO heartbeats VALUES (?,?,?,?)",
                           [(1, a, f"{a}-bot", "{}")] * 3)
            db.execute("INSERT INTO deploys VALUES (1,?,?,?)", (a, f"{a}-bot", "OK"))
            db.executemany("INSERT INTO bot_trades VALUES (?,?,?)",
                           [(a, f"{a}-bot", 1)] * 2)
        db.commit()
        db.close()

    def tearDown(self):
        os.unlink(self.path)

    def _counts(self, account):
        db = sqlite3.connect(self.path)
        hb = db.execute("SELECT count(*) FROM heartbeats WHERE account=?", (account,)).fetchone()[0]
        dep = db.execute("SELECT count(*) FROM deploys WHERE account=?", (account,)).fetchone()[0]
        tr = db.execute("SELECT count(*) FROM bot_trades WHERE account=?", (account,)).fetchone()[0]
        db.close()
        return hb, dep, tr

    def test_purges_test_account(self):
        db = sqlite3.connect(self.path)
        self.assertNotIn("acctT", p.inventory_accounts(db))
        hb, dep, tr = p.purge_account(db, "acctT")
        db.close()
        self.assertEqual((hb, dep, tr), (3, 1, 2))
        self.assertEqual(self._counts("acctT"), (0, 0, 0))
        self.assertEqual(self._counts("acctP"), (3, 1, 2))  # prod untouched

    def test_inventory_guard_refuses_prod_account_via_main(self):
        # exercise the CLI guard directly: a prod account must be refused, not purged.
        sys.argv = ["purge", "--account", "acctP", "--db", self.path]
        self.assertEqual(p.main(), 2)              # REFUSED
        self.assertEqual(self._counts("acctP"), (3, 1, 2))  # nothing deleted

    def test_main_purges_test_account_and_is_idempotent(self):
        sys.argv = ["purge", "--account", "acctT", "--db", self.path]
        self.assertEqual(p.main(), 0)
        self.assertEqual(self._counts("acctT"), (0, 0, 0))
        self.assertEqual(p.main(), 0)              # re-run: no rows, still 0

    def test_missing_db_is_not_an_error(self):
        sys.argv = ["purge", "--account", "acctT", "--db", "/nonexistent/x.db"]
        self.assertEqual(p.main(), 0)

    def test_missing_inventory_table_fails_closed(self):
        # No inventory table → can't prove the target is a test account → REFUSE (not purge).
        bare = self.path + ".bare"
        db = sqlite3.connect(bare)
        db.execute("CREATE TABLE heartbeats (ts INT, account TEXT, bot_name TEXT, payload TEXT)")
        db.execute("CREATE TABLE deploys (ts INT, account TEXT, bot_name TEXT, result TEXT)")
        db.execute("INSERT INTO heartbeats VALUES (1,'acctT','b','{}')")
        db.commit(); db.close()
        sys.argv = ["purge", "--account", "acctT", "--db", bare]
        self.assertEqual(p.main(), 2)                       # fail-closed refusal
        # and it deleted nothing
        db = sqlite3.connect(bare)
        self.assertEqual(db.execute("SELECT count(*) FROM heartbeats").fetchone()[0], 1)
        db.close()
        os.unlink(bare)

    def test_empty_inventory_with_prod_heartbeats_fails_closed(self):
        # The clobbered-idx trap: inventory table exists but is EMPTY (unsynced), while prod
        # heartbeats are present. Must REFUSE — not purge a would-be prod account's history.
        unsynced = self.path + ".unsynced"
        db = _db(unsynced)  # creates empty inventory table
        db.execute("INSERT INTO heartbeats VALUES (1,'acctP','acctP-bot','{}')")
        db.commit(); db.close()
        sys.argv = ["purge", "--account", "acctP", "--db", unsynced]
        self.assertEqual(p.main(), 2)                       # refused
        db = sqlite3.connect(unsynced)
        self.assertEqual(db.execute("SELECT count(*) FROM heartbeats").fetchone()[0], 1)
        db.close()
        os.unlink(unsynced)


if __name__ == "__main__":
    unittest.main()
