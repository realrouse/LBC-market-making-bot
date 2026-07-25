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

# inventory.toml is local/git-ignored (it describes this deployment's real fleet — see
# docs/going-live.md); a fresh clone/CI checkout has only inventory.toml.example. The tests
# gated below assert against the REAL file's exact content (or drive deploy.main() with no
# rows override, which reads it directly) — skip rather than fail where it isn't present.
_skip_without_real_inventory = unittest.skipUnless(
    os.path.isfile(deploy.INVENTORY), "inventory.toml is local-only (not present in this checkout)")


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
        # indicators (restart) → data plane (feeds), in that exact order (acct-1 is infra only now).
        self.assertEqual(plan[0].args, ["--restart-indicators"])
        self.assertIn("setup_data_plane.sh", plan[1].script)

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


@_skip_without_real_inventory
class TestRealInventory(unittest.TestCase):
    """Regression guard: the shipped inventory.toml must reproduce the historical deploy
    order (the sequence deploy_all.sh used to hardcode)."""

    # Post single-tree reconciliation: accumulation (×4) + binance-grid + swing + mexc-grid deploy
    # NATIVELY (deployer=deploy_actions.py → a python step, signature = its family); the un-migrated
    # primaries (poly + the acct-8 LBC mexc-grid on the real account) stay on their bash deployers.
    # Each signature is
    # (script-basename, native-family-or-None) so the guard captures which engine AND, for
    # native steps, which family — deploy_actions.py alone would not distinguish accum vs grid.
    _EXPECTED_2_N = [
        ("deploy_actions.py", "polymarket"),     # acct-2 poly (native, migrated 2026-07-18)
        ("deploy_actions.py", "accumulation"),   # acct-2 accum (native)
        ("deploy_actions.py", "polymarket"),     # acct-3 poly (native, migrated 2026-07-18)
        ("deploy_actions.py", "accumulation"),   # acct-3 accum (native)
        ("deploy_actions.py", "grid_binance"),   # acct-3 binance-grid (native) — distinct step
        ("deploy_actions.py", "polymarket"),     # acct-4 poly (native, migrated 2026-07-18)
        ("deploy_actions.py", "accumulation"),   # acct-4 accum (native)
        ("deploy_actions.py", "swing"),          # acct-5 swing (native, migrated 2026-07-18)
        ("deploy_actions.py", "grid"),           # acct-6 mexc-grid (native, migrated 2026-07-18)
        ("deploy_actions.py", "grid"),           # acct-8/idx-7 mexc-grid LBC (native, migrated 2026-07-18)
        ("deploy_actions.py", "accumulation"),   # acct-8/idx-7 accum LBC (native)
    ]

    @staticmethod
    def _sig(step):
        fam = step.args[0] if step.interpreter == "python" else None
        return (os.path.basename(step.script), fam)

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
        derived = [self._sig(s) for s in plan[1:]]      # drop the account-1 step
        self.assertEqual(derived, self._EXPECTED_2_N)

    def test_migrated_families_deploy_natively(self):
        # Every trading bot now deploys natively (accum ×4 + binance-grid + swing + mexc-grid ×2
        # + polymarket ×3); only acct-1's infra + multibot account_bot stay bash (not family steps).
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        native = [s for s in plan if s.interpreter == "python"]
        self.assertEqual(len(native), 11)
        self.assertTrue(all(s.script.endswith("deploy_actions.py") for s in native))
        self.assertEqual(sorted(s.args[0] for s in native),
                         ["accumulation", "accumulation", "accumulation", "accumulation",
                          "grid", "grid", "grid_binance", "polymarket", "polymarket", "polymarket", "swing"])

    def test_every_derived_script_exists(self):
        rows = deploy.load_rows(deploy.INVENTORY)
        plan = deploy.build_plan(rows, restart_infra=False)
        for s in plan:
            self.assertTrue(os.path.isfile(os.path.join(deploy.REPO, s.script)),
                            f"deploy script missing: {s.script}")


class TestScaleOut(unittest.TestCase):
    """Forward-looking: adding a bot of every family on every account (incl. account-1) must
    not silently drop any bot. account-1 trading bots are derived (only the bespoke INFRA
    scripts are skipped), and every (account, family) yields a distinct step — across BOTH bash
    preset engines AND native (deploy_actions.py) family steps, since the dedup key spans both."""

    # Hypothetical bash engines with an explicit per-account deploy_env preset — verifies the
    # dedup key keeps same-engine/different-account steps apart even for bash rows. (All real
    # trading deployers are native now; these fixtures just exercise the generic bash path.)
    _BASH = {
        "engine_a": ("some/scripts/engine_a.sh", "ENGINE_A_IDX"),
        "engine_b": ("some/scripts/engine_b.sh", "ENGINE_B_IDX"),
        "engine_c": ("some/scripts/engine_c.sh", "ENGINE_C_IDX"),
    }
    _NATIVE = "scripts/deploy_actions.py"

    def _matrix(self, n_accounts=6):
        rows = [{"account_idx": 0, "bot_name": "account_bot", "kind": "bot",
                 "deploy_script": "tradinebotte-polymarket/scripts/update_claude1.sh"}]
        for idx in range(n_accounts):
            for fam, (dep, iv) in self._BASH.items():
                rows.append({"account_idx": idx, "bot_name": fam, "kind": "bot",
                             "deployer": dep, "deploy_env": {iv: str(idx)}})
            # … plus one NATIVE family (accumulation) per account: --idx makes each distinct.
            rows.append({"account_idx": idx, "bot_name": "accum_bot", "kind": "bot",
                         "bot_type": "cex-accumulation", "deployer": self._NATIVE,
                         "single_tree": True, "strategy": f"strategies/accumulation/a{idx}.json"})
        return rows

    def test_full_matrix_every_bot_gets_a_distinct_step(self):
        rows = self._matrix()
        plan = deploy.build_plan(rows, restart_infra=False)
        trading = list(plan[1:])                  # drop the account-1 bespoke step
        self.assertEqual(len(trading), 6 * (len(self._BASH) + 1))   # 24, none dropped
        keys = {deploy._step_key(s) for s in trading}
        self.assertEqual(len(keys), len(trading))        # no collision (bash + native)

    def test_account1_trading_bot_is_derived(self):
        # A trading bot on account-1 (a non-bespoke deployer) must appear in the plan.
        rows = [
            {"account_idx": 0, "bot_name": "account_bot", "kind": "bot",
             "deploy_script": "tradinebotte-polymarket/scripts/update_claude1.sh"},
            {"account_idx": 0, "bot_name": "engine_bot", "kind": "bot",
             "deployer": "some/scripts/engine_a.sh",
             "deploy_env": {"ENGINE_A_IDX": "0"}},
        ]
        plan = deploy.build_plan(rows, restart_infra=False)
        self.assertTrue(any(s.script.endswith("engine_a.sh") for s in plan),
                        "account-1 trading bot was dropped from the derived plan")

    def test_bespoke_infra_scripts_are_skipped(self):
        # Rows deployed by the account-1 bespoke block must NOT be re-derived.
        rows = [{"account_idx": 0, "bot_name": b, "kind": k,
                 "deploy_script": f"tradinebotte-{'status' if 'data' in s else 'polymarket'}/scripts/{s}"}
                for b, k, s in [("indicators", "service", "update_claude1.sh"),
                                ("feed5m", "service", "setup_data_plane.sh")]]
        plan = deploy.build_plan(rows, restart_infra=False)
        derived = list(plan[1:])                  # after the account-1 step
        self.assertEqual(derived, [], "bespoke infra scripts should not be derived")


class TestNativeDispatch(unittest.TestCase):
    """Single-tree reconciliation: a row whose `deployer` is scripts/deploy_actions.py deploys
    through the native declarative engine (a python step) instead of a bash script. Family is
    derived from bot_type; the strategy from the row's `strategy` field; account_idx supplies
    --idx. The dedup key includes args, so two native rows on ONE account (the idx-2 accumulation
    + binance-grid case) are NOT collapsed."""

    _DA = "scripts/deploy_actions.py"

    def _accum(self, idx, strat="strategies/accumulation/x.json"):
        return {"account_idx": idx, "bot_name": f"accum-{idx}", "kind": "bot",
                "bot_type": "cex-accumulation", "deployer": self._DA,
                "strategy": strat, "single_tree": True}

    def _grid_binance(self, idx, strat="strategies/grid/g.json"):
        return {"account_idx": idx, "bot_name": f"grid-{idx}", "kind": "bot",
                "bot_type": "cex-grid-binance-sim", "deployer": self._DA,
                "strategy": strat, "single_tree": True}

    def test_native_row_is_a_python_step(self):
        step = deploy.build_plan([self._accum(1)], restart_infra=False)[1]
        self.assertEqual(step.interpreter, "python")
        self.assertTrue(step.script.endswith("deploy_actions.py"))
        self.assertEqual(step.env, {})

    def test_native_argv_family_idx_strategy_singletree(self):
        step = deploy.build_plan([self._accum(3, "strategies/accumulation/deep.json")],
                                 restart_infra=False)[1]
        self.assertEqual(step.args, ["accumulation", "--idx", "3",
                                     "--strategy", "strategies/accumulation/deep.json",
                                     "--single-tree"])

    def test_grid_binance_resolves_to_its_own_family(self):
        step = deploy.build_plan([self._grid_binance(2)], restart_infra=False)[1]
        self.assertEqual(step.args[0], "grid_binance")

    def test_single_tree_flag_absent_when_not_set(self):
        row = self._accum(1); row["single_tree"] = False
        step = deploy.build_plan([row], restart_infra=False)[1]
        self.assertNotIn("--single-tree", step.args)

    def test_two_native_rows_same_account_not_deduped(self):
        # THE pinning case: idx-2 runs BOTH accumulation and binance-grid via deploy_actions.py.
        # (script, env) — or (script, env, idx) — would collapse them; (…, args) keeps them apart.
        rows = [self._accum(2), self._grid_binance(2)]
        plan = deploy.build_plan(rows, restart_infra=False)
        native = [s for s in plan if s.interpreter == "python"]
        self.assertEqual(len(native), 2, "accumulation + binance-grid on idx-2 were collapsed")
        self.assertEqual({s.args[0] for s in native}, {"accumulation", "grid_binance"})

    def test_native_rows_differ_by_strategy_not_deduped(self):
        rows = [self._accum(1, "strategies/accumulation/a.json"),
                self._accum(1, "strategies/accumulation/b.json")]  # same idx, diff strategy
        plan = deploy.build_plan(rows, restart_infra=False)
        self.assertEqual(len([s for s in plan if s.interpreter == "python"]), 2)

    def test_non_native_deployer_is_a_bash_step(self):
        rows = [{"account_idx": 1, "bot_name": "engine", "kind": "bot",
                 "deployer": "some/scripts/engine_a.sh"}]
        step = deploy.build_plan(rows, restart_infra=False)[1]
        self.assertEqual(step.interpreter, "bash")
        self.assertEqual(step.args, [])


@_skip_without_real_inventory
class TestPostDeployInventorySync(unittest.TestCase):
    """Post-deploy inventory reconciliation (run_inventory_sync) + its gating in main().
    Prevents is_live (and the rest of the topology) drifting from the committed inventory.toml."""

    import unittest.mock as _mock

    def _run_main(self, argv, rc=0):
        calls = []

        def _fake_run(cmd, **_kw):
            calls.append([str(x) for x in cmd])
            return self._mock.Mock(returncode=rc)

        with self._mock.patch.object(deploy.subprocess, "run", side_effect=_fake_run), \
             self._mock.patch.object(sys, "argv", ["deploy.py", *argv]):
            deploy.main()
        return calls

    @staticmethod
    def _synced(calls):
        return any(any("sync_inventory.py" in x for x in c) for c in calls)

    def test_real_deploy_runs_sync(self):
        self.assertTrue(self._synced(self._run_main(["--only", "account-5"])))

    def test_verify_only_skips_sync(self):
        self.assertFalse(self._synced(self._run_main(["--only", "account-5", "--verify-only"])))

    def test_dry_run_skips_sync(self):
        self.assertFalse(self._synced(self._run_main(["--only", "account-5", "--dry-run"])))

    def test_sync_helper_returns_false_on_failure(self):
        with self._mock.patch.object(deploy.subprocess, "run",
                                     return_value=self._mock.Mock(returncode=1)):
            self.assertFalse(deploy.run_inventory_sync())


if __name__ == "__main__":
    unittest.main()
