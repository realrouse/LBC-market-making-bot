"""Tests for AccumulationStrategy — the accumulation family as a hosted strategy engine.

Golden values in test_full_strategy_path were verified byte-identical to the former standalone
accumulation_bot.py (initial buy → OBI-dip scale-in → profit-band sell+rebuy → rebuy fill) via a
standalone-vs-engine parity harness at the time of the refactor; they now guard the engine against
drift after the standalone was retired. The gate tests exercise the on_indicator seam (the macro
gate stack that klines-only backtests can't see); the snapshot test ports the accum_snapshots
freshness-clock coverage from the old test_accum_snapshot.
"""

import asyncio
import os
import sqlite3
import sys
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tradinebotte-core"))

from strategy_engines import load          # noqa: E402
from strategy_engines.accumulation import AccumulationStrategy  # noqa: E402
from botcore.strategy import Strategy       # noqa: E402
from botcore import connectors as botconn   # noqa: E402

_CFG = {
    "symbol": "BTCUSDT", "capital_usdt": 1000.0, "initial_stake_usdt": 500.0,
    "scale_in_usdt": 100.0, "obi_entry_thresh": 0.5, "obi_confirm_n": 2,
    "min_scale_interval_s": 1, "scale_in_cooldown_min_s": 1, "snapshot_every_n": 2,
    "profit_bands_pct": [5.0], "sell_fraction": 0.15, "min_holdings_pct": 0.5,
    "earn_enabled": False,
}
_T0 = 1_700_000_000_000
_PERMISSIVE_GATES = {
    "btc_vwap_context": {"dip_score": 0.5, "dip_zone": "below"},
    "btc_macro_obi":    {"macro_obi": 0.0, "macro_obi_direction": "neutral"},
    "fear_greed":       {"fear_greed": 50, "fear_greed_label": "Neutral"},
    "btc_liquidations": {"liq_long_usd": 0.0, "liq_short_usd": 0.0},
    "btc_ls_ratio":     {"long_short_ratio": 1.0},
    "btc_4h":           {"rsi_14": 50.0},
}


def _seq(gates: dict):
    """The verified scenario: init buy, then (gates set), then two OBI-dip ticks → scale-in,
    a spike → profit-band sell, a dip → rebuy fill."""
    seq = [("s", {"mid": 70000.0, "obi_ema": -0.9, "spread_bps": 2.0}, _T0)]
    for sid, body in gates.items():
        seq.append((sid, body, _T0))
    seq += [
        ("s", {"mid": 69000.0, "obi_ema": -0.9, "spread_bps": 2.0}, _T0 + 2000),
        ("s", {"mid": 68000.0, "obi_ema": -0.9, "spread_bps": 2.0}, _T0 + 4000),
        ("s", {"mid": 74000.0, "obi_ema": 0.0,  "spread_bps": 2.0}, _T0 + 6000),
        ("s", {"mid": 68000.0, "obi_ema": 0.0,  "spread_bps": 2.0}, _T0 + 8000),
    ]
    return seq


def _drive(cfg: dict, seq: list):
    eng = load("accumulation", types.SimpleNamespace(connector="binance", strategy_cfg=dict(cfg)))
    conn = sqlite3.connect(":memory:")
    state = types.SimpleNamespace(conn=conn, session=None, strategy=eng, last_book_ts=0.0)

    async def go():
        await eng.restore_from_db(state)
        for kind, body, ts_ms in seq:
            if kind == "s":
                ts = types.SimpleNamespace(mid=body["mid"], obi_ema=body["obi_ema"],
                                           spread_bps=body["spread_bps"], ts_ms=ts_ms)
                await eng.on_book_update(state, ts)
            else:
                await eng.on_indicator(state, {"stream_id": kind, **body})
    asyncio.run(go())
    trades = conn.execute(
        "SELECT side, reason, round(price,4), round(qty_btc,8), round(usdt_value,4), "
        "round(avg_entry_after,4), round(holdings_after,8), round(free_usdt_after,4) "
        "FROM accum_trades ORDER BY id").fetchall()
    return eng, conn, trades


class TestProtocol(unittest.TestCase):

    def test_registered_and_conforms(self):
        eng = load("accumulation", types.SimpleNamespace(connector="binance", strategy_cfg={}))
        self.assertIsInstance(eng, AccumulationStrategy)
        self.assertEqual(eng.STRATEGY_TYPE, "accumulation")
        self.assertIsInstance(eng, Strategy)                 # runtime_checkable protocol

    def test_paper_needs_no_connector_methods(self):
        # validate() must pass for any connector — accumulation posts no exchange orders.
        dummy = types.SimpleNamespace(__name__="dummy_connector")
        botconn.validate(dummy, "accumulation")              # no raise
        self.assertEqual(botconn._STRATEGY_REQUIREMENTS["accumulation"], [])


class TestFullStrategyPath(unittest.TestCase):

    def test_golden_trades_match_former_standalone(self):
        eng, _conn, trades = _drive(_CFG, _seq(_PERMISSIVE_GATES))
        self.assertEqual(trades, [
            ("buy",  "initial",         70000.0, 0.00714286, 500.0,     70000.0,     0.00714286, 499.9),
            ("buy",  "obi_dip(+2.9%)",  68000.0, 0.00357143, 242.8571,  69333.3333,  0.01071429, 256.9943),
            ("sell", "profit+5.0%",     74000.0, 0.00160714, 118.9286,  69333.3333,  0.00910714, 375.8991),
            ("buy",  "rebuy+5.0%",      68000.0, 0.00160714, 109.2857,  69133.3333,  0.01071429, 266.5915),
        ])
        self.assertAlmostEqual(eng.acc.total_realized, 7.476214, places=6)
        hb = eng.heartbeat_payload()
        self.assertEqual(hb["pnl_total"], round(eng.acc.total_realized, 2))
        self.assertEqual(hb["last_write_ts"], 1_700_000_006.0)   # snapshot cadence advanced it
        self.assertTrue(hb["bounds_ok"])


class TestGates(unittest.TestCase):

    def test_extreme_greed_blocks_scale_in(self):
        gates = dict(_PERMISSIVE_GATES)
        gates["fear_greed"] = {"fear_greed": 90, "fear_greed_label": "Extreme Greed"}
        _eng, _conn, trades = _drive(_CFG, _seq(gates))
        reasons = [t[1] for t in trades]
        self.assertNotIn("obi_dip(+2.9%)", "".join(reasons))   # scale-in gated out
        self.assertEqual(reasons[0], "initial")

    def test_short_squeeze_blocks_scale_in(self):
        gates = dict(_PERMISSIVE_GATES)
        gates["btc_liquidations"] = {"liq_long_usd": 0.0, "liq_short_usd": 50_000_000}
        _eng, _conn, trades = _drive(_CFG, _seq(gates))
        self.assertFalse(any(t[1].startswith("obi_dip") for t in trades))


class TestSnapshot(unittest.TestCase):

    def _eng(self):
        return load("accumulation", types.SimpleNamespace(
            connector="binance", strategy_cfg={"earn_enabled": False}))

    def test_writes_row_and_advances_freshness(self):
        eng = self._eng()
        conn = sqlite3.connect(":memory:")
        eng.ensure_schema(conn)
        eng.acc.holdings_btc = 0.01
        eng.acc.avg_entry = 60000.0
        eng.acc.free_usdt = 500.0
        eng.acc.obi_ema = -0.2
        eng._record_snapshot(conn, 61000.0, 1_782_550_000_000)
        row = conn.execute("SELECT ts_ms, price, holdings_btc, free_usdt, obi_ema, invested_usdt "
                           "FROM accum_snapshots").fetchone()
        self.assertEqual(row, (1_782_550_000_000, 61000.0, 0.01, 500.0, -0.2, 600.0))
        self.assertAlmostEqual(eng.acc.last_write_ts, 1_782_550_000.0)

    def test_zero_holdings_invested_is_zero(self):
        eng = self._eng()
        conn = sqlite3.connect(":memory:")
        eng.ensure_schema(conn)
        eng._record_snapshot(conn, 60000.0, 1_782_550_002_000)
        invested = conn.execute("SELECT invested_usdt FROM accum_snapshots").fetchone()[0]
        self.assertEqual(invested, 0.0)


if __name__ == "__main__":
    unittest.main()
