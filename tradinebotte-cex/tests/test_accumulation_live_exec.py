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

from strategy_engines.accumulation import (  # noqa: E402
    AccumulationStrategy, PendingRebuy, diff_sell_ladder, plan_sell_ladder,
    reconcile_order, reconcile_pending_buy)


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
        get_account=AsyncMock(return_value=None),      # None = UNKNOWN → drift check no-ops
        cancel_order=AsyncMock(return_value=True))
    conn = sqlite3.connect(":memory:")
    eng.ensure_schema(conn)
    state = types.SimpleNamespace(conn=conn, session=None, strategy=eng, last_book_ts=0.0)
    return eng, state


_LADDER = [{"band_pct": 5.0, "fraction": 0.30}, {"band_pct": 10.0, "fraction": 0.30}]


def _ladder_engine(**over):
    """A live engine holding coins, with the ratchet ladder configured."""
    cfg = {"sell_ladder": _LADDER, "min_holdings_pct": 0.40, "sell_ceiling_price": 0.02,
           "rebuy_discount_min_pct": 3.0, "rebuy_discount_max_pct": 10.0,
           "rebuy_spread_mult": 3.0, "sell_rearm_tol_pct": 0.5}
    cfg.update(over)
    eng, state = _live_engine(**cfg)
    eng.acc.holdings_btc = 4000.0
    eng.acc.peak_holdings_btc = 4000.0
    eng.acc.avg_entry = 0.0025
    eng.acc.initial_done = True
    eng.acc.free_usdt = 90.0
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


class TestPlanSellLadder(unittest.TestCase):
    """plan_sell_ladder — PURE policy. Resting sells can no more be shadow-tested than
    fills can, so these tests are the gate on real coins leaving the account."""

    _P = {"sell_ladder": _LADDER, "min_holdings_pct": 0.40, "sell_ceiling_price": 0.02}

    def _plan(self, **over):
        kw = dict(holdings=4000.0, avg_entry=0.0025, peak_holdings=4000.0,
                  best_ask=0.0024, price=0.0024, params=self._P, gate_open=True)
        kw.update(over)
        return plan_sell_ladder(**kw)

    def test_bands_anchor_to_avg_entry_not_market(self):
        plan = self._plan()
        self.assertEqual([p["band_pct"] for p in plan], [5.0, 10.0])
        self.assertAlmostEqual(plan[0]["price"], 0.0025 * 1.05)
        self.assertAlmostEqual(plan[1]["price"], 0.0025 * 1.10)
        self.assertAlmostEqual(plan[0]["qty"], 1200.0)      # 30% of holdings
        self.assertAlmostEqual(plan[1]["qty"], 1200.0)

    def test_never_crosses_clamped_to_best_ask(self):
        # market already above the +5% band → resting at the band would sell into the bid
        plan = self._plan(best_ask=0.0030, price=0.0030)
        self.assertAlmostEqual(plan[0]["price"], 0.0030)    # clamped up to the ask
        self.assertAlmostEqual(plan[1]["price"], 0.0030)
        for p in plan:
            self.assertGreaterEqual(p["price"], 0.0030)     # never below the ask → maker

    def test_floor_is_never_breached_even_if_every_band_fills(self):
        plan = self._plan()
        self.assertLessEqual(sum(p["qty"] for p in plan), 4000.0 * 0.60 + 1e-9)

    def test_floor_caps_the_ladder_when_holdings_already_low(self):
        # already sold down near the floor: only the remainder may be laddered
        plan = self._plan(holdings=1800.0, peak_holdings=4000.0)   # floor = 1600
        self.assertAlmostEqual(sum(p["qty"] for p in plan), 200.0)

    def test_at_floor_plans_nothing(self):
        self.assertEqual(self._plan(holdings=1600.0, peak_holdings=4000.0), [])

    def test_ceiling_stops_new_sells(self):
        self.assertEqual(self._plan(price=0.02, best_ask=0.02), [])
        self.assertEqual(self._plan(price=0.05, best_ask=0.05), [])

    def test_ceiling_is_judged_on_MARKET_price_not_only_the_band_price(self):
        """Isolates the ceiling's price check from its per-band check — the two otherwise
        mask each other and the price check tests vacuously. Here the market has run far
        above the ceiling while avg_entry (and so the band) is far below it: without the
        market-price check a band sneaks through and rests absurdly under the market."""
        plan = plan_sell_ladder(holdings=4000.0, avg_entry=0.0005, peak_holdings=4000.0,
                                best_ask=0.001, price=0.03, params=self._P, gate_open=True)
        self.assertEqual(plan, [])          # 0.03 >= ceiling 0.02 → trim nothing, let it run

    def test_ceiling_drops_only_the_bands_above_it(self):
        p = {**self._P, "sell_ceiling_price": 0.00260}          # +5% = 0.002625 > ceiling
        plan = plan_sell_ladder(holdings=4000.0, avg_entry=0.0025, peak_holdings=4000.0,
                                best_ask=0.0024, price=0.0024, params=p, gate_open=True)
        self.assertEqual(plan, [])

    def test_gate_closed_plans_nothing(self):
        self.assertEqual(self._plan(gate_open=False), [])

    def test_no_holdings_or_no_basis_plans_nothing(self):
        self.assertEqual(self._plan(holdings=0.0), [])
        self.assertEqual(self._plan(avg_entry=0.0), [])


class TestDiffSellLadder(unittest.TestCase):

    def _t(self, band, price):
        return {"order_id": f"o{band}", "band_pct": band, "price": price}

    def test_places_missing_bands(self):
        desired = [{"band_pct": 5.0, "price": 0.0026, "qty": 1200.0}]
        cancel, place = diff_sell_ladder(desired, [], tol_pct=0.005)
        self.assertEqual(cancel, [])
        self.assertEqual(len(place), 1)

    def test_hysteresis_keeps_a_close_enough_order(self):
        desired = [{"band_pct": 5.0, "price": 0.002601, "qty": 1200.0}]
        cancel, place = diff_sell_ladder(desired, [self._t(5.0, 0.0026)], tol_pct=0.005)
        self.assertEqual((cancel, place), ([], []))      # 0.04% drift → leave it resting

    def test_reprices_when_drift_exceeds_tolerance(self):
        desired = [{"band_pct": 5.0, "price": 0.0028, "qty": 1200.0}]
        cancel, place = diff_sell_ladder(desired, [self._t(5.0, 0.0026)], tol_pct=0.005)
        self.assertEqual(len(cancel), 1)
        self.assertEqual(len(place), 1)

    def test_cancels_bands_no_longer_wanted(self):
        cancel, place = diff_sell_ladder([], [self._t(5.0, 0.0026), self._t(10.0, 0.00275)],
                                         tol_pct=0.005)
        self.assertEqual(len(cancel), 2)
        self.assertEqual(place, [])


class TestLadderWiring(unittest.IsolatedAsyncioTestCase):

    def _tick(self, mid):
        return types.SimpleNamespace(mid=mid, obi_ema=0.0, spread_bps=10.0, ts_ms=1_000_000)

    async def test_places_post_only_sells_and_does_not_debit_holdings(self):
        eng, state = _ladder_engine()
        eng._api.post_order = AsyncMock(side_effect=["s1", "s2"])
        await eng.on_book_update(state, self._tick(0.0024))
        self.assertEqual(eng._api.post_order.await_count, 2)
        self.assertEqual(eng._api.post_order.await_args.kwargs["order_type"], "LIMIT_MAKER")
        self.assertEqual(eng._api.post_order.await_args.kwargs["side"], "SELL")
        self.assertEqual(len(eng.acc.open_sells), 2)
        self.assertAlmostEqual(eng.acc.holdings_btc, 4000.0)   # nothing sold until it fills

    async def test_sell_fill_credits_coins_out_usdt_in_and_mints_rebuy(self):
        eng, state = _ladder_engine()
        eng._api.post_order = AsyncMock(side_effect=["s1", "s2"])
        await eng.on_book_update(state, self._tick(0.0024))
        # s1 fills completely at the +5% band; s2 is still resting
        eng._api.get_open_orders = AsyncMock(return_value=[
            {"order_id": "s2", "side": "SELL", "price": 0.00275, "qty": 1200.0,
             "status": "NEW", "executed_qty": 0.0, "cummulative_quote_qty": 0.0}])
        eng._api.get_order = AsyncMock(return_value={
            "status": "FILLED", "executed_qty": 1200.0, "cummulative_quote_qty": 3.15})
        await eng._reconcile_live_sells(state, 2_000_000)
        self.assertAlmostEqual(eng.acc.holdings_btc, 2800.0)   # 4000 - 1200
        self.assertAlmostEqual(eng.acc.free_usdt, 93.15)       # proceeds in, 0 fee
        self.assertEqual(len(eng.acc.pending_rebuys), 1)
        rb = eng.acc.pending_rebuys[0]
        self.assertAlmostEqual(rb.qty_btc, 1200.0)
        self.assertLess(rb.rebuy_price, 3.15 / 1200.0)         # rebuy strictly below the fill

    async def test_partial_sell_fill_mints_obligation_only_for_what_sold(self):
        eng, state = _ladder_engine()
        eng._api.post_order = AsyncMock(side_effect=["s1", "s2"])
        await eng.on_book_update(state, self._tick(0.0024))
        eng._api.get_open_orders = AsyncMock(return_value=[
            {"order_id": "s1", "side": "SELL", "price": 0.002625, "qty": 1200.0,
             "status": "PARTIALLY_FILLED", "executed_qty": 400.0,
             "cummulative_quote_qty": 1.05},
            {"order_id": "s2", "side": "SELL", "price": 0.00275, "qty": 1200.0,
             "status": "NEW", "executed_qty": 0.0, "cummulative_quote_qty": 0.0}])
        await eng._reconcile_live_sells(state, 2_000_000)
        self.assertAlmostEqual(eng.acc.holdings_btc, 3600.0)   # only the 400 that sold
        self.assertAlmostEqual(eng.acc.pending_rebuys[0].qty_btc, 400.0)
        self.assertIn("s1", eng.acc.open_sells)                # remainder still resting

    async def test_ratchet_round_trip_returns_more_coins_than_it_sold(self):
        """The whole point: sell high, rebuy lower, end with MORE coins."""
        eng, state = _ladder_engine()
        eng._api.post_order = AsyncMock(side_effect=["s1", "s2"])
        await eng.on_book_update(state, self._tick(0.0024))
        sold_qty, proceeds = 1200.0, 1200.0 * 0.002625
        eng._api.get_open_orders = AsyncMock(return_value=[
            {"order_id": "s2", "side": "SELL", "price": 0.00275, "qty": 1200.0,
             "status": "NEW", "executed_qty": 0.0, "cummulative_quote_qty": 0.0}])
        eng._api.get_order = AsyncMock(return_value={
            "status": "FILLED", "executed_qty": sold_qty,
            "cummulative_quote_qty": proceeds})
        await eng._reconcile_live_sells(state, 2_000_000)
        rb = eng.acc.pending_rebuys[0]
        rebought = proceeds / rb.rebuy_price          # spend the proceeds at the rebuy price
        self.assertGreater(rebought, sold_qty)
        self.assertGreater(rebought / sold_qty - 1.0, 0.02)    # ≳3% more coins per cycle

    async def test_post_only_autocancel_is_not_tracked_and_not_an_error(self):
        # MEXC returns an orderId then auto-cancels a crossing LIMIT_MAKER: it never rested.
        eng, state = _ladder_engine()
        eng._api.post_order = AsyncMock(return_value="s1")
        eng._api.get_order = AsyncMock(return_value={
            "status": "CANCELED", "executed_qty": 0.0, "cummulative_quote_qty": 0.0})
        placed = await eng._place_live_sell(state, {"band_pct": 5.0, "price": 0.0026,
                                                    "qty": 1200.0}, 1)
        self.assertFalse(placed)
        self.assertEqual(eng.acc.open_sells, {})               # not tracked as resting
        self.assertEqual(eng._fail_streak, 0)                  # market moved ≠ rejection

    async def test_oversell_is_refused_even_if_the_plan_asks(self):
        eng, state = _ladder_engine()
        eng.acc.open_sells = {"x": {"order_id": "x", "band_pct": 5.0, "price": 0.0026,
                                    "orig_qty": 2400.0, "executed_qty_seen": 0.0,
                                    "quote_spent_seen": 0.0, "placed_ts": 0.0}}
        ok = await eng._place_live_sell(state, {"band_pct": 10.0, "price": 0.00275,
                                                "qty": 1200.0}, 1)
        self.assertFalse(ok)             # 2400 resting + 1200 > 2400 sellable (floor 1600)
        eng._api.post_order.assert_not_awaited()

    async def test_shadow_places_no_sells(self):
        eng, state = _ladder_engine(shadow=True)
        await eng.on_book_update(state, self._tick(0.0024))
        eng._api.post_order.assert_not_awaited()
        self.assertEqual(eng.acc.open_sells, {})

    async def test_halted_engine_places_nothing(self):
        eng, state = _ladder_engine()
        eng.acc.halted = True
        await eng.on_book_update(state, self._tick(0.0024))
        eng._api.post_order.assert_not_awaited()


class TestDriftGuard(unittest.IsolatedAsyncioTestCase):
    """The guard the advisor flagged: a resting sell moves coins free -> locked, so a
    free-only comparison 'drifts' by exactly the resting qty and false-halts."""

    def _acct(self, free, locked):
        return {"can_trade": True, "permissions": ["SPOT"],
                "balances": {"LBC": {"free": free, "locked": locked},
                             "USDT": {"free": 200.0, "locked": 0.0}}}

    async def test_resting_sell_does_not_false_halt(self):
        eng, state = _ladder_engine()
        # 4000 held, 1200 locked in a resting sell → free is only 2800
        eng._api.get_account = AsyncMock(return_value=self._acct(2800.0, 1200.0))
        await eng._check_drift(state)
        self.assertFalse(eng.acc.halted)          # free+locked == internal → fine

    async def test_over_claiming_halts(self):
        eng, state = _ladder_engine()
        eng._api.get_account = AsyncMock(return_value=self._acct(1000.0, 0.0))
        await eng._check_drift(state)
        self.assertTrue(eng.acc.halted)           # books claim 4000, exchange has 1000

    async def test_extra_coins_in_the_account_do_not_halt(self):
        # a deposit is harmless: only OVER-claiming can oversell
        eng, state = _ladder_engine()
        eng._api.get_account = AsyncMock(return_value=self._acct(28_000_000.0, 0.0))
        await eng._check_drift(state)
        self.assertFalse(eng.acc.halted)

    async def test_unknown_account_does_not_halt(self):
        eng, state = _ladder_engine()
        eng._api.get_account = AsyncMock(return_value=None)
        await eng._check_drift(state)
        self.assertFalse(eng.acc.halted)          # None = UNKNOWN, never "zero balance"


class TestLadderAdoption(unittest.IsolatedAsyncioTestCase):

    def _rec(self, oid, band, price, qty=1200.0):
        return {"order_id": oid, "band_pct": band, "price": price, "orig_qty": qty,
                "executed_qty_seen": 0.0, "quote_spent_seen": 0.0, "placed_ts": 0.0}

    async def test_restart_adopts_resting_sells_instead_of_cancelling_them(self):
        eng, state = _ladder_engine()
        eng._adopted = False
        eng.acc.open_sells = {"s1": self._rec("s1", 5.0, 0.002625)}
        eng._api.get_open_orders = AsyncMock(return_value=[
            {"order_id": "s1", "side": "SELL", "price": 0.002625, "qty": 1200.0,
             "status": "NEW", "executed_qty": 0.0, "cummulative_quote_qty": 0.0}])
        await eng._adopt_open_orders(state)
        eng._api.cancel_order.assert_not_awaited()             # the ladder survives restart
        self.assertIn("s1", eng.acc.open_sells)

    async def test_restart_credits_a_sell_that_filled_while_down(self):
        eng, state = _ladder_engine()
        eng._adopted = False
        eng.acc.open_sells = {"s1": self._rec("s1", 5.0, 0.002625)}
        eng._api.get_open_orders = AsyncMock(return_value=[])   # gone from the book
        eng._api.get_order = AsyncMock(return_value={
            "status": "FILLED", "executed_qty": 1200.0, "cummulative_quote_qty": 3.15})
        await eng._adopt_open_orders(state)
        self.assertAlmostEqual(eng.acc.holdings_btc, 2800.0)    # credited on adoption
        self.assertEqual(len(eng.acc.pending_rebuys), 1)

    async def test_untracked_order_is_still_cancelled_as_an_orphan(self):
        eng, state = _ladder_engine()
        eng._adopted = False
        eng._api.get_open_orders = AsyncMock(return_value=[
            {"order_id": "ghost", "side": "SELL", "price": 0.9, "qty": 5.0,
             "status": "NEW", "executed_qty": 0.0, "cummulative_quote_qty": 0.0}])
        await eng._adopt_open_orders(state)
        eng._api.cancel_order.assert_awaited_once()
        self.assertEqual(eng._api.cancel_order.await_args.args[2], "ghost")

    async def test_ladder_survives_a_save_restore_round_trip(self):
        eng, state = _ladder_engine()
        eng.acc.open_sells = {"s1": self._rec("s1", 5.0, 0.002625)}
        eng._save_sells(state.conn)
        eng._save_state(state.conn)
        eng2, _ = _ladder_engine()
        eng2.acc.open_sells = {}
        eng2._restore_state(state.conn)
        self.assertIn("s1", eng2.acc.open_sells)
        self.assertAlmostEqual(eng2.acc.open_sells["s1"]["price"], 0.002625)


class TestRebuyObligationLifecycle(unittest.IsolatedAsyncioTestCase):

    async def test_rebuy_obligation_is_not_discharged_on_placement_only_on_fill(self):
        """Mirror of the initial_done bug: in live, _buy returning True means PLACED. If a
        rebuy bid is cancelled unfilled, the obligation must survive — otherwise we sold
        coins and quietly abandoned buying them back, which breaks the ratchet."""
        eng, state = _ladder_engine()
        eng.acc.pending_rebuys.append(
            PendingRebuy(band_pct=5.0, sell_price=0.0026, qty_btc=1200.0,
                         rebuy_price=0.0026, ts_ms=1))
        await eng._check_rebuys(state, 0.0025, 2)              # price below rebuy → bid
        eng._api.post_order.assert_awaited_once()
        self.assertEqual(len(eng.acc.pending_rebuys), 1)       # NOT discharged yet
        # the bid is cancelled unfilled → obligation still stands
        eng._api.get_order = AsyncMock(return_value={
            "status": "CANCELED", "executed_qty": 0.0, "cummulative_quote_qty": 0.0})
        await eng._reconcile_live_buy(state, 0.0025, 3)
        self.assertEqual(len(eng.acc.pending_rebuys), 1)

    async def test_rebuy_obligation_discharged_when_its_bid_fills(self):
        eng, state = _ladder_engine()
        eng.acc.pending_rebuys.append(
            PendingRebuy(band_pct=5.0, sell_price=0.0026, qty_btc=1200.0,
                         rebuy_price=0.0026, ts_ms=1))
        await eng._check_rebuys(state, 0.0025, 2)
        eng._api.get_order = AsyncMock(return_value={
            "status": "FILLED", "executed_qty": 1300.0, "cummulative_quote_qty": 3.10})
        await eng._reconcile_live_buy(state, 0.0025, 3)
        self.assertEqual(eng.acc.pending_rebuys, [])           # discharged on the FILL
        self.assertAlmostEqual(eng.acc.holdings_btc, 5300.0)   # 4000 + 1300 rebought


if __name__ == "__main__":
    unittest.main()
