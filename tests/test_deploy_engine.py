"""Unit tests for the bounded-parallel deploy engine (scripts/deploy_engine.py, Phase A).

No SSH: subprocess.run is stubbed to record concurrency, so we assert the scheduler's two
invariants — (1) at most `jobs` steps run at once, (2) steps sharing a serialize_key (account)
never overlap and keep plan order — plus that independent accounts DO overlap.
"""
import os
import sys
import threading
import time
import types
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import deploy as _d            # noqa: E402
import deploy_engine as eng    # noqa: E402


def _step(label):
    return _d.Step(label=label, script="noop.sh")


class TestSerializeKey(unittest.TestCase):
    def test_account_prefix(self):
        self.assertEqual(eng._serialize_key(_step("account-3 — foo (bar)")), "account-3")
        self.assertEqual(eng._serialize_key(_step("account-1 — data plane")), "account-1")


class TestScheduler(unittest.TestCase):
    def _run_with_recorder(self, plan, jobs):
        """Run run_parallel with a stubbed subprocess.run that records (key, enter, exit)."""
        events = []          # (serialize_key, "enter"/"exit", monotonic)
        active = {"n": 0, "max": 0}
        lock = threading.Lock()
        # map the cmd back to a serialize_key via the label embedded in the plan order:
        # simpler — stub keys off a counter matching submission; instead record via label.
        label_by_cmd = {}
        for s in plan:
            label_by_cmd[os.path.join(_d.REPO, s.script)] = None  # not unique; use env marker

        def fake_run(cmd, **kw):
            # cmd = ["bash", <script>, *args]; recover the serialize key from TRADINEBOTTE_TESTKEY
            key = kw.get("env", {}).get("TRADINEBOTTE_TESTKEY", "?")
            with lock:
                active["n"] += 1
                active["max"] = max(active["max"], active["n"])
                events.append((key, "enter", time.monotonic()))
            time.sleep(0.05)
            with lock:
                active["n"] -= 1
                events.append((key, "exit", time.monotonic()))
            return types.SimpleNamespace(returncode=0)

        # tag each step's env with its serialize key so the stub can see it
        for s in plan:
            s.env = {**s.env, "TRADINEBOTTE_TESTKEY": eng._serialize_key(s)}

        orig = eng.subprocess.run
        eng.subprocess.run = fake_run
        try:
            rc = eng.run_parallel(plan, [], jobs=jobs, dry_run=False)
        finally:
            eng.subprocess.run = orig
        return rc, events, active["max"]

    def test_concurrency_cap_and_serialization(self):
        # 2 steps on account-1 (must serialize), 1 each on account-2/3/4 (independent).
        plan = [_step("account-1 — a"), _step("account-1 — b"),
                _step("account-2 — c"), _step("account-3 — d"), _step("account-4 — e")]
        rc, events, max_active = self._run_with_recorder(plan, jobs=2)
        self.assertEqual(rc, 0)
        self.assertLessEqual(max_active, 2, "exceeded the jobs=2 concurrency cap")

        # account-1's two steps must NOT overlap (share a serialize domain).
        def interval(key, nth=0):
            ins = [t for k, ev, t in events if k == key and ev == "enter"]
            outs = [t for k, ev, t in events if k == key and ev == "exit"]
            return list(zip(sorted(ins), sorted(outs)))
        a1 = interval("account-1")
        self.assertEqual(len(a1), 2)
        self.assertLessEqual(a1[0][1], a1[1][0] + 1e-6, "account-1 steps overlapped")

    def test_independent_accounts_overlap(self):
        plan = [_step("account-2 — c"), _step("account-3 — d")]
        rc, events, max_active = self._run_with_recorder(plan, jobs=2)
        self.assertEqual(rc, 0)
        self.assertEqual(max_active, 2, "independent accounts did not run in parallel")


class TestNativeExecPlan(unittest.TestCase):
    """The Phase-E native cutover plan: inventory rows → NativeSteps for deploy_family/deploy_infra."""

    def test_family_row_resolves_target_and_strategy(self):
        rows = [{"account_idx": 4, "bot_name": "sw", "bot_type": "cex-swing",
                 "deploy_env": {"TEST_SWING_STRATEGY": "strategies/swing/swing_BTCUSDT.json"}}]
        steps, skipped = eng.build_native_exec_plan(rows)
        self.assertEqual(skipped, [])
        self.assertEqual(len(steps), 1)
        s = steps[0]
        self.assertEqual((s.kind, s.target, s.idx), ("family", "swing", 4))
        self.assertEqual(s.strategy, "strategies/swing/swing_BTCUSDT.json")
        # label must carry the 'account-N —' prefix so the scheduler's serialize_key = account works
        self.assertTrue(s.label.startswith("account-5 —"))
        self.assertEqual(eng._serialize_key(s), "account-5")

    def test_infra_row_maps_to_deploy_infra(self):
        rows = [{"account_idx": 0, "bot_name": "cf", "bot_type": "infra-cex-feed"}]
        steps, _ = eng.build_native_exec_plan(rows)
        self.assertEqual((steps[0].kind, steps[0].target, steps[0].idx), ("infra", "cexfeed", 0))

    def test_unknown_bot_type_is_skipped_not_crashed(self):
        rows = [{"account_idx": 1, "bot_name": "ob", "bot_type": "cex-orderbook"}]
        steps, skipped = eng.build_native_exec_plan(rows)
        self.assertEqual(steps, [])
        self.assertEqual(len(skipped), 1)

    def test_account1_infra_gate_predicate(self):
        # the main() gate drops idx==0 infra unless --restart-infra; lock the predicate it relies on
        rows = [{"account_idx": 0, "bot_name": "f", "bot_type": "infra-feed-15m"},
                {"account_idx": 2, "bot_name": "g", "bot_type": "cex-grid-mexc-sim",
                 "deploy_env": {"TEST_GRID_MEXC_STRATEGY": "s.json"}}]
        steps, _ = eng.build_native_exec_plan(rows)
        gated = [s for s in steps if s.idx == 0 and s.kind == "infra"]
        kept = [s for s in steps if not (s.idx == 0 and s.kind == "infra")]
        self.assertEqual(len(gated), 1)                  # the feed would be gated off by default
        self.assertEqual([s.target for s in kept], ["grid"])  # the trading bot survives


if __name__ == "__main__":
    unittest.main()
