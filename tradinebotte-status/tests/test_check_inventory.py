"""Unit tests for check_inventory.check_deploy_pipeline — the inventory→deploy guard.

Locks the invariants that keep the inventory-driven deploy correct as it scales (a family
added on every account): every bot is reachable in the derived plan, no two bots collapse
to the same deploy step, and account-1 infra rows (run by the bespoke block) are exempt.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import check_inventory as ci  # noqa: E402

_GRID = "tradinebotte-cex/scripts/deploy_grid_binance.sh"


class TestDeployPipeline(unittest.TestCase):

    def test_collision_when_two_bots_share_deployer_and_env(self):
        rows = [
            {"account_idx": 1, "bot_name": "grid_bot", "kind": "bot",
             "deployer": _GRID, "deploy_env": {"TEST_GRID_BINANCE_USER_IDX": "1"}},
            {"account_idx": 2, "bot_name": "grid_bot", "kind": "bot",
             "deployer": _GRID, "deploy_env": {"TEST_GRID_BINANCE_USER_IDX": "1"}},  # same idx!
        ]
        probs = ci.check_deploy_pipeline(rows)
        self.assertTrue(any("collision" in p for p in probs), probs)

    def test_distinct_indices_do_not_collide(self):
        rows = [
            {"account_idx": 1, "bot_name": "grid_bot", "kind": "bot",
             "deployer": _GRID, "deploy_env": {"TEST_GRID_BINANCE_USER_IDX": "1"}},
            {"account_idx": 2, "bot_name": "grid_bot", "kind": "bot",
             "deployer": _GRID, "deploy_env": {"TEST_GRID_BINANCE_USER_IDX": "2"}},
        ]
        self.assertEqual([p for p in ci.check_deploy_pipeline(rows) if "collision" in p], [])

    def test_account1_trading_bot_is_validated_reachable(self):
        rows = [
            {"account_idx": 0, "bot_name": "account_bot", "kind": "bot",
             "deploy_script": "tradinebotte-polymarket/scripts/update_claude1.sh"},
            {"account_idx": 0, "bot_name": "grid_bot", "kind": "bot",
             "deployer": _GRID, "deploy_env": {"TEST_GRID_BINANCE_USER_IDX": "0"}},
        ]
        # The trading bot must NOT be flagged undeployable/absent (it's in the derived plan).
        self.assertEqual(ci.check_deploy_pipeline(rows), [])

    def test_bespoke_infra_row_not_flagged(self):
        rows = [{"account_idx": 0, "bot_name": "feed5m", "kind": "service",
                 "deploy_script": "tradinebotte-status/scripts/setup_data_plane.sh"}]
        self.assertEqual(ci.check_deploy_pipeline(rows), [])

    def test_undeployable_bot_flagged(self):
        rows = [{"account_idx": 2, "bot_name": "mystery_bot", "kind": "bot"}]  # no deployer
        self.assertTrue(any("undeployable" in p for p in ci.check_deploy_pipeline(rows)))


class TestNativeCoverage(unittest.TestCase):
    """The Phase-E native-engine guard: every bot_type maps to a native target, and the
    depends_on graph is acyclic. The DAG half is inert against the current inventory (no row
    sets depends_on), so it is exercised here with synthetic graphs — otherwise the recursive
    cycle detector would ship untested."""

    def test_unknown_bot_type_flagged(self):
        rows = [{"account_idx": 1, "bot_name": "x", "bot_type": "cex-orderbook"}]  # no native target
        self.assertTrue(any("no native deploy target" in p for p in ci.check_native_coverage(rows)))

    def test_known_bot_type_ok(self):
        rows = [{"account_idx": 1, "bot_name": "x", "bot_type": "cex-grid-binance-sim"}]
        self.assertEqual(ci.check_native_coverage(rows), [])

    def test_depends_on_cycle_detected(self):
        rows = [
            {"account_idx": 0, "bot_name": "A", "bot_type": "infra-feed-15m", "depends_on": ["B"]},
            {"account_idx": 0, "bot_name": "B", "bot_type": "infra-cex-feed", "depends_on": ["A"]},
        ]
        self.assertTrue(any("cycle" in p for p in ci.check_native_coverage(rows)))

    def test_depends_on_acyclic_ok(self):
        rows = [
            {"account_idx": 0, "bot_name": "feed", "bot_type": "infra-feed-15m"},
            {"account_idx": 0, "bot_name": "acct", "bot_type": "polymarket-multibot", "depends_on": ["feed"]},
        ]
        self.assertEqual(ci.check_native_coverage(rows), [])

    def test_depends_on_unknown_name_flagged(self):
        rows = [{"account_idx": 0, "bot_name": "acct", "bot_type": "polymarket-multibot",
                 "depends_on": ["ghost"]}]
        self.assertTrue(any("not a known bot_name" in p for p in ci.check_native_coverage(rows)))


if __name__ == "__main__":
    unittest.main()
