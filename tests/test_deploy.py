"""Unit tests for scripts/deploy.py — the inventory-driven deploy orchestrator.

The orchestrator is production-critical (it drives every account's deploy), so these lock
the plan-derivation logic: account-1 stays a bespoke ordered block, accounts 2..N are
derived from inventory rows in file order and deduped, and the real inventory.toml still
produces exactly the historical deploy sequence (a regression guard against topology drift).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import deploy  # noqa: E402


def _rows():
    """Synthetic inventory: acct-1 infra (ignored in derivation) + acct-2/3 bots, with a
    duplicate deploy_script that must be deduped."""
    return [
        {"account_idx": 0, "bot_name": "account_bot", "deploy_script": "pm/update_claude1.sh"},
        {"account_idx": 1, "bot_name": "live_bot", "bot_type": "poly", "deploy_script": "a.sh"},
        {"account_idx": 1, "bot_name": "accumulation_bot", "bot_type": "cex", "deploy_script": "b.sh"},
        {"account_idx": 2, "bot_name": "grid_bot", "bot_type": "grid", "deploy_script": "c.sh"},
        {"account_idx": 2, "bot_name": "live_bot", "deploy_script": "a.sh"},  # dup → deduped
    ]


class TestBuildPlan(unittest.TestCase):

    def test_account1_rsync_only_by_default(self):
        plan = deploy.build_plan(_rows(), restart_infra=False)
        self.assertIn("update_claude1.sh", plan[0].script)
        self.assertEqual(plan[0].args, ["--skip-restart"])
        self.assertIn("account-1", plan[0].label)

    def test_account1_restart_infra_is_ordered_block(self):
        plan = deploy.build_plan(_rows(), restart_infra=True)
        # indicators (restart) → data plane (feeds) → account_bot, in that exact order.
        self.assertEqual(plan[0].args, ["--restart-indicators"])
        self.assertIn("setup_data_plane.sh", plan[1].script)
        self.assertEqual(plan[2].args, ["--restart-account"])

    def test_accounts_derived_in_file_order(self):
        plan = deploy.build_plan(_rows(), restart_infra=False)
        derived = [s.script for s in plan[1:]]          # after the account-1 step
        self.assertEqual(derived, ["a.sh", "b.sh", "c.sh"])  # dup a.sh at acct-3 removed

    def test_duplicate_deploy_scripts_deduped(self):
        plan = deploy.build_plan(_rows(), restart_infra=False)
        scripts = [s.script for s in plan]
        self.assertEqual(len(scripts), len(set(scripts)))

    def test_account1_rows_not_double_run_in_derivation(self):
        # The idx-0 row's deploy_script must not reappear among the derived steps.
        plan = deploy.build_plan(_rows(), restart_infra=False)
        self.assertNotIn("pm/update_claude1.sh", [s.script for s in plan[1:]])

    def test_label_includes_account_bot_and_type(self):
        plan = deploy.build_plan(_rows(), restart_infra=False)
        self.assertEqual(plan[1].label, "account-2 — live_bot (poly)")


class TestRealInventory(unittest.TestCase):
    """Regression guard: the shipped inventory.toml must reproduce the historical deploy
    order (the sequence deploy_all.sh used to hardcode)."""

    _EXPECTED_2_6 = [
        "tradinebotte-polymarket/scripts/update_claude2.sh",
        "tradinebotte-cex/scripts/deploy_accumulation_claude2.sh",
        "tradinebotte-polymarket/scripts/update_claude3.sh",
        "tradinebotte-cex/scripts/deploy_accumulation_claude3.sh",
        "tradinebotte-cex/scripts/deploy_grid_claude3.sh",
        "tradinebotte-polymarket/scripts/update_claude4.sh",
        "tradinebotte-cex/scripts/deploy_accumulation_claude4.sh",
        "tradinebotte-cex/scripts/update_swing.sh",
        "tradinebotte-cex/scripts/deploy_grid_mexc.sh",
    ]

    def test_load_rows_real_inventory(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        self.assertGreater(len(rows), 10)
        for r in rows:
            self.assertIn("account_idx", r)
            if r["account_idx"] >= 1:
                self.assertIn("deploy_script", r)

    def test_derived_plan_matches_historical_order(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        derived = [s.script for s in plan[1:]]          # drop the account-1 step
        self.assertEqual(derived, self._EXPECTED_2_6)

    def test_every_derived_script_exists(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        for s in plan:
            self.assertTrue(os.path.isfile(os.path.join(deploy.REPO, s.script)),
                            f"deploy script missing: {s.script}")


if __name__ == "__main__":
    unittest.main()
