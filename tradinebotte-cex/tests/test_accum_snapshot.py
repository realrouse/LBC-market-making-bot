"""Unit tests for accumulation_bot._record_accum_snapshot.

Phase 3 — test parity across families. Before this, NO test touched the accum_snapshots
write (the accumulation family's data-path side effect, incl. the last_write_ts freshness
clock that feeds the status ⚠data monitor). This closes that gap with a direct test of the
extracted, named persistence step — same treatment live_bot's _persist_snapshot gets.
"""

import os
import sqlite3  # noqa: F401  (kept explicit: the helper writes via the live schema)
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import accumulation_bot as accum  # noqa: E402


def _state():
    s = accum.AccumState(p={})
    s.holdings_btc = 0.01
    s.avg_entry = 60000.0
    s.free_usdt = 500.0
    s.last_price = 61000.0
    s.obi_ema = -0.2
    return s


class TestAccumSnapshotWrite(unittest.TestCase):

    def test_writes_row_and_advances_freshness(self):
        db = accum.init_db(Path(":memory:"))
        state = _state()
        self.assertEqual(state.last_write_ts, 0.0)          # nothing written yet

        accum._record_accum_snapshot(state, db, 61000.0, 1782550000000)

        rows = db.execute(
            "SELECT ts_ms, price, holdings_btc, free_usdt, obi_ema "
            "FROM accum_snapshots").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (1782550000000, 61000.0, 0.01, 500.0, -0.2))
        self.assertGreater(state.last_write_ts, 0)          # ⚠data freshness clock advanced
        self.assertAlmostEqual(state.last_write_ts, 1782550000.0)

    def test_invested_usdt_derived_from_holdings(self):
        db = accum.init_db(Path(":memory:"))
        state = _state()
        state.holdings_btc = 0.02
        state.avg_entry = 50000.0                           # invested = 0.02 * 50000
        accum._record_accum_snapshot(state, db, 52000.0, 1782550001000)
        invested = db.execute("SELECT invested_usdt FROM accum_snapshots").fetchone()[0]
        self.assertAlmostEqual(invested, 1000.0)

    def test_zero_holdings_invested_is_zero(self):
        db = accum.init_db(Path(":memory:"))
        state = _state()
        state.holdings_btc = 0.0
        state.avg_entry = 0.0
        accum._record_accum_snapshot(state, db, 60000.0, 1782550002000)
        invested = db.execute("SELECT invested_usdt FROM accum_snapshots").fetchone()[0]
        self.assertEqual(invested, 0.0)


if __name__ == "__main__":
    unittest.main()
