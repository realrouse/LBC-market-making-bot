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


if __name__ == "__main__":
    unittest.main()
