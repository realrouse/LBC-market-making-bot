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


class TestDeployerEnv(unittest.TestCase):
    """Phase 2: a row deploys via `deployer` + `deploy_env` (generic engine + preset).
    Several rows share one engine with different presets, so dedup must key on (script, env)."""

    def _rows(self):
        return [
            {"account_idx": 1, "bot_name": "live_bot", "deployer": "eng.sh",
             "deploy_env": {"IDX": "1", "SRC": "feed"}},
            {"account_idx": 2, "bot_name": "live_bot", "deployer": "eng.sh",
             "deploy_env": {"IDX": "2", "SRC": "feed"}},           # same engine, diff preset
            {"account_idx": 3, "bot_name": "live_bot", "deployer": "eng.sh",
             "deploy_env": {"IDX": "1", "SRC": "feed"}},           # dup of acct-2's row → dropped
        ]

    def test_env_carried_onto_step(self):
        plan = deploy.build_plan(self._rows(), restart_infra=False)
        self.assertEqual(plan[1].script, "eng.sh")
        self.assertEqual(plan[1].env, {"IDX": "1", "SRC": "feed"})

    def test_dedup_keys_on_script_and_env(self):
        plan = deploy.build_plan(self._rows(), restart_infra=False)
        engines = [(s.script, s.env["IDX"]) for s in plan[1:]]
        # IDX 1 and 2 kept (different presets); the second IDX=1 deduped.
        self.assertEqual(engines, [("eng.sh", "1"), ("eng.sh", "2")])

    def test_deployer_preferred_over_deploy_script(self):
        rows = [{"account_idx": 1, "bot_name": "x", "deployer": "new.sh",
                 "deploy_script": "old.sh", "deploy_env": {"A": "1"}}]
        plan = deploy.build_plan(rows, restart_infra=False)
        self.assertEqual(plan[1].script, "new.sh")


class TestRealInventory(unittest.TestCase):
    """Regression guard: the shipped inventory.toml must reproduce the historical deploy
    order (the sequence deploy_all.sh used to hardcode)."""

    # Post-Phase-2: generic engines + presets. update_standalone / deploy_accumulation
    # each appear 3× (one per account), distinguished by deploy_env.
    _EXPECTED_2_6 = [
        "tradinebotte-polymarket/scripts/update_standalone.sh",   # acct-2 live_bot
        "tradinebotte-cex/scripts/deploy_accumulation.sh",        # acct-2 accum
        "tradinebotte-polymarket/scripts/update_standalone.sh",   # acct-3 live_bot
        "tradinebotte-cex/scripts/deploy_accumulation.sh",        # acct-3 accum
        "tradinebotte-cex/scripts/deploy_grid_binance.sh",        # acct-3 grid (generic engine)
        "tradinebotte-polymarket/scripts/update_standalone.sh",   # acct-4 live_bot
        "tradinebotte-cex/scripts/deploy_accumulation.sh",        # acct-4 accum
        "tradinebotte-cex/scripts/update_swing.sh",               # acct-5 swing (not migrated)
        "tradinebotte-cex/scripts/deploy_grid_mexc.sh",           # acct-6 grid (not migrated)
    ]

    def test_load_rows_real_inventory(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        self.assertGreater(len(rows), 10)
        for r in rows:
            self.assertIn("account_idx", r)
            if r["account_idx"] >= 1:              # every acct-2..N bot must be deployable
                self.assertTrue(r.get("deployer") or r.get("deploy_script"))

    def test_derived_plan_matches_historical_order(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        derived = [s.script for s in plan[1:]]          # drop the account-1 step
        self.assertEqual(derived, self._EXPECTED_2_6)

    def test_standalone_presets_have_distinct_indices(self):
        # The 3 update_standalone rows must carry TEST_STANDALONE_USER_IDX 1/2/3 (dedup
        # must NOT collapse them despite sharing the engine).
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        idxs = sorted(s.env["TEST_STANDALONE_USER_IDX"] for s in plan
                      if s.script.endswith("update_standalone.sh"))
        self.assertEqual(idxs, ["1", "2", "3"])

    def test_every_derived_script_exists(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        for s in plan:
            self.assertTrue(os.path.isfile(os.path.join(deploy.REPO, s.script)),
                            f"deploy script missing: {s.script}")


class TestAutoInjectIndex(unittest.TestCase):
    """deploy.py injects each deployer's account-index env var from account_idx, so inventory
    rows need not repeat the index — and two accounts can't collide on one engine."""

    def test_index_injected_from_account_idx(self):
        rows = [{"account_idx": 3, "bot_name": "swing_bot",
                 "deployer": "tradinebotte-cex/scripts/update_swing.sh"}]   # no deploy_env
        step = deploy.build_plan(rows, restart_infra=False)[1]
        self.assertEqual(step.env["TEST_SWING_USER_IDX"], "3")

    def test_explicit_deploy_env_index_wins(self):
        rows = [{"account_idx": 3, "bot_name": "swing_bot",
                 "deployer": "tradinebotte-cex/scripts/update_swing.sh",
                 "deploy_env": {"TEST_SWING_USER_IDX": "9"}}]                # override
        step = deploy.build_plan(rows, restart_infra=False)[1]
        self.assertEqual(step.env["TEST_SWING_USER_IDX"], "9")

    def test_unknown_deployer_gets_no_injection(self):
        rows = [{"account_idx": 3, "bot_name": "x", "deployer": "custom.sh"}]
        self.assertEqual(deploy.build_plan(rows, restart_infra=False)[1].env, {})

    def test_same_engine_different_accounts_do_not_collide(self):
        rows = [{"account_idx": i, "bot_name": "swing_bot",
                 "deployer": "tradinebotte-cex/scripts/update_swing.sh"} for i in (1, 2, 3)]
        plan = deploy.build_plan(rows, restart_infra=False)
        self.assertEqual(len(plan) - 1, 3)          # none deduped away


class TestScaleOut(unittest.TestCase):
    """Forward-looking: adding a bot of every family on every account (incl. account-1) must
    not silently drop any bot. account-1 trading bots are derived (only the bespoke INFRA
    scripts are skipped), and every (account, family) yields a distinct step."""

    _FAM = {
        "live_bot":         ("tradinebotte-polymarket/scripts/update_standalone.sh", "TEST_STANDALONE_USER_IDX"),
        "grid_bot":         ("tradinebotte-cex/scripts/deploy_grid_binance.sh",      "TEST_GRID_BINANCE_USER_IDX"),
        "swing_bot":        ("tradinebotte-cex/scripts/update_swing.sh",             "TEST_SWING_USER_IDX"),
        "accumulation_bot": ("tradinebotte-cex/scripts/deploy_accumulation.sh",      "ACCUM_USER_IDX"),
    }

    def _matrix(self, n_accounts=6):
        rows = [{"account_idx": 0, "bot_name": "account_bot", "kind": "bot",
                 "deploy_script": "tradinebotte-polymarket/scripts/update_claude1.sh"}]
        for idx in range(n_accounts):
            for fam, (dep, iv) in self._FAM.items():
                rows.append({"account_idx": idx, "bot_name": fam, "kind": "bot",
                             "deployer": dep, "deploy_env": {iv: str(idx)}})
        return rows

    def test_full_matrix_every_bot_gets_a_distinct_step(self):
        rows = self._matrix()
        plan = deploy.build_plan(rows, restart_infra=False)
        trading = [s for s in plan[1:]]                  # drop the account-1 bespoke step
        self.assertEqual(len(trading), 6 * len(self._FAM))   # 24, none dropped
        keys = {(s.script, tuple(sorted(s.env.items()))) for s in trading}
        self.assertEqual(len(keys), len(trading))        # no collision

    def test_account1_trading_bot_is_derived(self):
        # A trading bot on account-1 (a non-bespoke deployer) must appear in the plan.
        rows = [
            {"account_idx": 0, "bot_name": "account_bot", "kind": "bot",
             "deploy_script": "tradinebotte-polymarket/scripts/update_claude1.sh"},
            {"account_idx": 0, "bot_name": "grid_bot", "kind": "bot",
             "deployer": "tradinebotte-cex/scripts/deploy_grid_binance.sh",
             "deploy_env": {"TEST_GRID_BINANCE_USER_IDX": "0"}},
        ]
        plan = deploy.build_plan(rows, restart_infra=False)
        self.assertTrue(any(s.script.endswith("deploy_grid_binance.sh") for s in plan),
                        "account-1 trading bot was dropped from the derived plan")

    def test_bespoke_infra_scripts_are_skipped(self):
        # Rows deployed by the account-1 bespoke block must NOT be re-derived.
        rows = [{"account_idx": 0, "bot_name": b, "kind": k,
                 "deploy_script": f"tradinebotte-{'status' if 'data' in s else 'polymarket'}/scripts/{s}"}
                for b, k, s in [("indicators", "service", "update_claude1.sh"),
                                ("feed5m", "service", "setup_data_plane.sh")]]
        plan = deploy.build_plan(rows, restart_infra=False)
        derived = [s for s in plan[1:]]                  # after the account-1 step
        self.assertEqual(derived, [], "bespoke infra scripts should not be derived")


if __name__ == "__main__":
    unittest.main()
