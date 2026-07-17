# pylint: disable=protected-access
"""reconcile_pending_buy — the PURE money-critical fill state machine for live maker buys.

Shadow mode can't exercise fills (a resting maker bid may not fill for days), so this logic
is validated here, deterministically, with mocked get_order() sequences — the real gate before
going live, per the execution design. Covers: multi-partial crediting without double-count or
cost-basis drift, full fill, cancel/expire, staleness (price + age), and the API-error no-op.
"""

import os
import sqlite3
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy_engines.accumulation import reconcile_pending_buy, AccumulationStrategy  # noqa: E402


def _pending(order_id="1", price=0.0025, orig_qty=4000.0,
             seen_qty=0.0, seen_quote=0.0, placed_ts=1000.0):
    return {"order_id": order_id, "price": price, "orig_qty": orig_qty,
            "executed_qty_seen": seen_qty, "quote_spent_seen": seen_quote,
            "placed_ts": placed_ts}


def _order(status, executed_qty=0.0, quote=0.0):
    return {"status": status, "orig_qty": 4000.0, "executed_qty": executed_qty,
            "cummulative_quote_qty": quote,
            "avg_price": (quote / executed_qty if executed_qty else None), "side": "BUY"}


_KW = dict(now_ts=1000.0, price=0.0025, stale_pct=0.02, max_age_s=3600.0)


class TestReconcilePendingBuy(unittest.TestCase):

    def test_new_fresh_holds(self):
        p = _pending()
        np, dq, dqu, act = reconcile_pending_buy(p, _order("NEW"), **_KW)
        self.assertEqual(act, "hold")
        self.assertEqual((dq, dqu), (0.0, 0.0))
        self.assertEqual(np, p)

    def test_api_error_is_noop_unchanged(self):
        p = _pending(seen_qty=1000.0, seen_quote=2.5)
        np, dq, dqu, act = reconcile_pending_buy(p, None, **_KW)
        self.assertEqual(act, "noop")
        self.assertEqual((dq, dqu), (0.0, 0.0))
        self.assertIs(np, p)                       # nothing assumed on a transient error

    def test_full_fill_from_new(self):
        np, dq, dqu, act = reconcile_pending_buy(
            _pending(), _order("FILLED", 4000.0, 10.0), **_KW)
        self.assertEqual(act, "filled")
        self.assertEqual((dq, dqu), (4000.0, 10.0))
        self.assertIsNone(np)                       # done

    def test_multi_partial_then_fill_no_double_count(self):
        p = _pending()
        seq = [("PARTIALLY_FILLED", 1000.0, 2.5),
               ("PARTIALLY_FILLED", 1500.0, 3.75),
               ("FILLED",           4000.0, 10.0)]
        tot_q = tot_c = 0.0
        for status, exq, quote in seq:
            p, dq, dqu, act = reconcile_pending_buy(p, _order(status, exq, quote), **_KW)
            tot_q += dq
            tot_c += dqu
        self.assertIsNone(p)                        # filled → cleared
        self.assertAlmostEqual(tot_q, 4000.0)       # every unit credited exactly once
        self.assertAlmostEqual(tot_c, 10.0)

    def test_partial_quote_credited_independently_no_costbasis_drift(self):
        # advisor's case: two partials at DIFFERENT prices. Crediting dqty × cumulative-avg
        # would misprice the 2nd tranche; crediting the QUOTE delta is exact.
        p = _pending()
        p, dq1, dqu1, _ = reconcile_pending_buy(p, _order("PARTIALLY_FILLED", 1000.0, 2.0), **_KW)
        self.assertAlmostEqual(dqu1, 2.0)           # 1000 @ 0.002
        p, dq2, dqu2, act = reconcile_pending_buy(p, _order("FILLED", 2000.0, 5.0), **_KW)
        self.assertEqual(act, "filled")
        self.assertAlmostEqual(dq2, 1000.0)
        self.assertAlmostEqual(dqu2, 3.0)           # the 2nd 1000 filled @ 0.003, NOT avg 0.0025

    def test_double_reconcile_same_result_credits_once(self):
        p = _pending()
        p, dq1, dqu1, _ = reconcile_pending_buy(p, _order("PARTIALLY_FILLED", 1000.0, 2.5), **_KW)
        # same order state polled again (no new fill) → zero delta, no double credit
        p2, dq2, dqu2, act = reconcile_pending_buy(p, _order("PARTIALLY_FILLED", 1000.0, 2.5), **_KW)
        self.assertEqual((dq2, dqu2), (0.0, 0.0))
        self.assertEqual(act, "partial")

    def test_canceled_clears_and_credits_any_final_fill(self):
        # a fill can land in the same poll that reports CANCELED (canceled remainder)
        p = _pending(seen_qty=1000.0, seen_quote=2.5)
        np, dq, dqu, act = reconcile_pending_buy(p, _order("CANCELED", 1500.0, 3.75), **_KW)
        self.assertEqual(act, "canceled")
        self.assertAlmostEqual(dq, 500.0)
        self.assertAlmostEqual(dqu, 1.25)
        self.assertIsNone(np)

    def test_stale_by_price_signals_cancel(self):
        # current price rose > stale_pct above our resting bid → cancel & re-bid
        np, dq, dqu, act = reconcile_pending_buy(
            _pending(price=0.0025), _order("NEW"),
            now_ts=1000.0, price=0.0025 * 1.03, stale_pct=0.02, max_age_s=3600.0)
        self.assertEqual(act, "cancel")
        self.assertEqual(np, _pending(price=0.0025))   # unchanged until the cancel confirms

    def test_stale_by_age_signals_cancel(self):
        np, dq, dqu, act = reconcile_pending_buy(
            _pending(placed_ts=0.0), _order("NEW"),
            now_ts=5000.0, price=0.0025, stale_pct=0.02, max_age_s=3600.0)
        self.assertEqual(act, "cancel")

    def test_not_stale_within_bounds_holds(self):
        np, dq, dqu, act = reconcile_pending_buy(
            _pending(price=0.0025, placed_ts=0.0), _order("NEW"),
            now_ts=100.0, price=0.0025 * 1.01, stale_pct=0.02, max_age_s=3600.0)
        self.assertEqual(act, "hold")


def _live_engine(**over):
    cfg = {"symbol": "LBCUSDT", "capital_usdt": 100.0, "initial_stake_usdt": 10.0,
           "maker_bid_offset_pct": 0.5, "earn_enabled": False}
    cfg.update(over)
    # build PAPER (no connector load in __init__), then flip to live + inject a fake connector
    eng = AccumulationStrategy(types.SimpleNamespace(connector="mexc", strategy_cfg=cfg))
    eng.live = True
    eng.shadow = bool(over.get("shadow", False))
    eng._adopted = True
    eng._api = types.SimpleNamespace(
        post_order=AsyncMock(return_value="oid1"),
        get_order=AsyncMock(return_value={"status": "NEW", "executed_qty": 0.0,
                                          "cummulative_quote_qty": 0.0}),
        get_open_orders=AsyncMock(return_value=[]),
        cancel_order=AsyncMock(return_value=True))
    conn = sqlite3.connect(":memory:")
    eng.ensure_schema(conn)
    state = types.SimpleNamespace(conn=conn, session=None, strategy=eng, last_book_ts=0.0)
    return eng, state


class TestLiveWiring(unittest.IsolatedAsyncioTestCase):
    """The async glue around the pure state machine: placement, fill crediting from REAL
    amounts, the one-bid budget guard, shadow (places nothing), and orphan adoption."""

    async def test_place_sets_pending_without_crediting(self):
        eng, state = _live_engine()
        ok = await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1)
        self.assertTrue(ok)
        eng._api.post_order.assert_awaited_once()
        self.assertIsNotNone(eng.acc.pending_buy)
        self.assertEqual(eng.acc.holdings_btc, 0.0)      # not credited until it fills
        self.assertEqual(eng.acc.free_usdt, 100.0)

    async def test_fill_credits_real_filled_amounts(self):
        eng, state = _live_engine()
        await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1)
        eng._api.get_order = AsyncMock(return_value={
            "status": "FILLED", "executed_qty": 3980.0, "cummulative_quote_qty": 10.0})
        await eng._reconcile_live_buy(state, 0.0025, 2)
        self.assertAlmostEqual(eng.acc.holdings_btc, 3980.0)
        self.assertAlmostEqual(eng.acc.free_usdt, 90.0)          # real quote spent, not the bid math
        self.assertAlmostEqual(eng.acc.avg_entry, 10.0 / 3980.0)
        self.assertIsNone(eng.acc.pending_buy)
        row = state.conn.execute("SELECT side, qty_btc, usdt_value FROM accum_trades").fetchone()
        self.assertEqual(row[0], "buy")
        self.assertAlmostEqual(row[1], 3980.0)

    async def test_one_bid_guard_blocks_second_placement(self):
        eng, state = _live_engine()
        self.assertTrue(await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1))
        self.assertFalse(await eng._place_live_buy(state, 0.0025, 5.0, "scale-in", 2))
        eng._api.post_order.assert_awaited_once()                # only one order ever placed

    async def test_no_placement_before_adoption(self):
        eng, state = _live_engine()
        eng._adopted = False
        self.assertFalse(await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1))
        eng._api.post_order.assert_not_awaited()

    async def test_shadow_places_nothing_but_papers(self):
        eng, state = _live_engine(shadow=True)
        ok = await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1)
        self.assertTrue(ok)
        eng._api.post_order.assert_not_awaited()                 # NOTHING placed
        self.assertIsNone(eng.acc.pending_buy)
        self.assertGreater(eng.acc.holdings_btc, 0.0)            # paper trajectory still runs

    async def test_adopt_cancels_orphan(self):
        eng, state = _live_engine()
        eng._adopted = False
        eng._api.get_open_orders = AsyncMock(return_value=[
            {"order_id": "orphan9", "side": "BUY", "qty": 100.0, "price": 0.002}])
        await eng._adopt_open_orders(state)
        eng._api.cancel_order.assert_awaited_once()
        self.assertEqual(eng._api.cancel_order.await_args.args[2], "orphan9")
        self.assertTrue(eng._adopted)

    async def test_canceled_initial_bid_is_rebid_not_consumed(self):
        """The live bug that idled the real bot with the full budget: `initial_done` was
        set on PLACEMENT. A resting bid canceled unfilled then left the engine believing
        the initial buy had happened while holding nothing, so only the dip-gated
        scale-in path could ever buy. The initial must survive an unfilled cancel."""
        eng, state = _live_engine()
        ts = types.SimpleNamespace(mid=0.0025, obi_ema=0.0, spread_bps=10.0,
                                   ts_ms=1_000_000)
        await eng.on_book_update(state, ts)
        self.assertIsNotNone(eng.acc.pending_buy)
        self.assertFalse(eng.acc.initial_done)          # placed != owned
        eng._api.post_order.reset_mock()

        # the bid is canceled unfilled (stale) — nothing was ever bought. Reconcile runs
        # before the initial branch, so the same tick clears it and re-bids.
        eng._api.get_order = AsyncMock(return_value={
            "status": "CANCELED", "executed_qty": 0.0, "cummulative_quote_qty": 0.0})
        await eng.on_book_update(state, ts)
        self.assertEqual(eng.acc.holdings_btc, 0.0)     # never owned anything
        self.assertFalse(eng.acc.initial_done)          # opportunity NOT consumed
        eng._api.post_order.assert_awaited_once()       # re-bid instead of idling
        self.assertIsNotNone(eng.acc.pending_buy)

    async def test_initial_done_only_after_real_fill(self):
        eng, state = _live_engine()
        ts = types.SimpleNamespace(mid=0.0025, obi_ema=0.0, spread_bps=10.0, ts_ms=1_000_000)
        await eng.on_book_update(state, ts)
        self.assertFalse(eng.acc.initial_done)
        eng._api.get_order = AsyncMock(return_value={
            "status": "FILLED", "executed_qty": 4000.0, "cummulative_quote_qty": 10.0})
        await eng.on_book_update(state, ts)
        self.assertTrue(eng.acc.initial_done)           # a real fill completes it
        self.assertAlmostEqual(eng.acc.holdings_btc, 4000.0)

    async def test_pending_bid_does_not_spam_orders_while_resting(self):
        eng, state = _live_engine()
        ts = types.SimpleNamespace(mid=0.0025, obi_ema=0.0, spread_bps=10.0, ts_ms=1_000_000)
        for _ in range(5):
            await eng.on_book_update(state, ts)
        eng._api.post_order.assert_awaited_once()       # one bid, not five

    async def test_placement_failure_backs_off_instead_of_hot_looping(self):
        """A rejected order leaves no pending bid, so the next tick retries at once. During
        the real Content-Type outage this hammered MEXC ~20x in 50s with an IP-whitelisted
        key. A persistent rejection must back off, not spin."""
        eng, state = _live_engine()
        eng._api.post_order = AsyncMock(return_value=None)      # exchange rejects everything
        ts = types.SimpleNamespace(mid=0.0025, obi_ema=0.0, spread_bps=10.0, ts_ms=1_000_000)
        for _ in range(6):
            await eng.on_book_update(state, ts)
        # first attempt fires, the rest are suppressed by the backoff window
        self.assertEqual(eng._api.post_order.await_count, 1)
        self.assertGreater(eng._retry_after, time.time())
        self.assertEqual(eng.acc.holdings_btc, 0.0)             # nothing credited on failure

    async def test_backoff_grows_and_clears_after_success(self):
        eng, state = _live_engine()
        eng._api.post_order = AsyncMock(return_value=None)
        await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1)
        first = eng._retry_after
        eng._retry_after = 0.0                                   # simulate the window elapsing
        await eng._place_live_buy(state, 0.0025, 10.0, "initial", 2)
        self.assertEqual(eng._fail_streak, 2)
        self.assertGreater(eng._retry_after - time.time(), first - time.time())  # grew
        # a successful placement clears the breaker
        eng._retry_after = 0.0
        eng._api.post_order = AsyncMock(return_value="oid9")
        self.assertTrue(await eng._place_live_buy(state, 0.0025, 10.0, "initial", 3))
        self.assertEqual(eng._fail_streak, 0)
        self.assertEqual(eng._retry_after, 0.0)

    async def test_stale_bid_is_canceled_on_reconcile(self):
        eng, state = _live_engine()
        await eng._place_live_buy(state, 0.0025, 10.0, "initial", 1)
        eng._api.get_order = AsyncMock(return_value={
            "status": "NEW", "executed_qty": 0.0, "cummulative_quote_qty": 0.0})
        await eng._reconcile_live_buy(state, 0.0025 * 1.05, 2)   # price ran 5% above the bid
        eng._api.cancel_order.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
