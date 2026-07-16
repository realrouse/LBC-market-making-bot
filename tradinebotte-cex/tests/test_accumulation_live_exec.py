# pylint: disable=protected-access
"""reconcile_pending_buy — the PURE money-critical fill state machine for live maker buys.

Shadow mode can't exercise fills (a resting maker bid may not fill for days), so this logic
is validated here, deterministically, with mocked get_order() sequences — the real gate before
going live, per the execution design. Covers: multi-partial crediting without double-count or
cost-basis drift, full fill, cancel/expire, staleness (price + age), and the API-error no-op.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy_engines.accumulation import reconcile_pending_buy  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
