"""Unit tests for the native declarative deployer (scripts/deploy_actions.py).

No SSH — these exercise the pure helpers + the FAMILIES/INFRA spec tables. The load-bearing one
is the test-account test-port offset: every TCP bind must shift by exactly TEST_PORT_OFFSET so the
whole stack runs self-contained on the shared host, and IPC/None addrs must stay untouched.
"""
import importlib.util
import os
import unittest
from types import SimpleNamespace

_HERE = os.path.dirname(__file__)
_PATH = os.path.join(_HERE, "..", "scripts", "deploy_actions.py")
_spec = importlib.util.spec_from_file_location("deploy_actions", _PATH)
da = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(da)


class TestOffsetAddr(unittest.TestCase):
    def test_tcp_shifts_port(self):
        self.assertEqual(da._offset_addr("tcp://127.0.0.1:5563", 10), "tcp://127.0.0.1:5573")
        self.assertEqual(da._offset_addr("tcp://127.0.0.1:5557", 10), "tcp://127.0.0.1:5567")

    def test_ipc_and_none_untouched(self):
        # per-user IPC is already isolated — never offset it
        self.assertEqual(da._offset_addr("ipc:///run/user/1/x.sock", 10), "ipc:///run/user/1/x.sock")
        self.assertIsNone(da._offset_addr(None, 10))


class TestTestEnv(unittest.TestCase):
    def test_addr_services_get_tcp_offset(self):
        self.assertEqual(da._test_env(da.INFRA["cexfeed"]), {"TRADINEBOTTE_CEX_FEED_ADDR": "tcp://127.0.0.1:5573"})
        self.assertEqual(da._test_env(da.INFRA["feed5m"]),  {"TRADINEBOTTE_FEED_ADDR": "tcp://127.0.0.1:5567"})
        self.assertEqual(da._test_env(da.INFRA["status"]),  {"TRADINEBOTTE_STATUS_ADDR": "tcp://127.0.0.1:5572"})

    def test_indicators_port_base_is_bare_number(self):
        # TRADINEBOTTE_PORT_BASE wants a number, not a tcp:// addr (it shifts all 3 indicator addrs)
        self.assertEqual(da._test_env(da.INFRA["indicators"]), {"TRADINEBOTTE_PORT_BASE": "5567"})

    def test_ipc_services_have_no_offset(self):
        self.assertEqual(da._test_env(da.INFRA["feed"]), {})      # 15M feed — per-user IPC
        self.assertEqual(da._test_env(da.INFRA["account"]), {})   # account_bot — per-user IPC


class TestSpecTables(unittest.TestCase):
    def test_every_infra_addr_service_offsets_within_free_range(self):
        # every offset TCP port must land in the free range (> the 5557–5563 base band), so a test
        # stack never collides with prod's singletons. Bare-number PORT_BASE is exempt from the tcp check.
        for name, spec in da.INFRA.items():
            if not spec.get("port_env"):
                continue
            self.assertGreaterEqual(spec["base_port"] + da.TEST_PORT_OFFSET, 5564,
                                    f"{name}: offset port overlaps the prod base band")

    def test_families_and_infra_targets_disjoint(self):
        self.assertEqual(set(da.FAMILIES) & set(da.INFRA), set())


class TestNativeTarget(unittest.TestCase):
    def test_families(self):
        self.assertEqual(da.native_target("cex-accumulation-mexc"), ("family", "accumulation"))
        self.assertEqual(da.native_target("cex-grid-mexc-sim"), ("family", "grid"))
        self.assertEqual(da.native_target("cex-swing"), ("family", "swing"))
        # a Polymarket bot's type contains "grid"/"threshold" but must still map to the poly family
        self.assertEqual(da.native_target("polymarket-grid"), ("family", "polymarket"))
        self.assertEqual(da.native_target("polymarket-threshold"), ("family", "polymarket"))

    def test_binance_grid_is_deliberately_bash_only(self):
        # cex-grid-binance runs a divergent layout (grid.service + ~/tradinebotte-grid); mapping it
        # to the mexc 'grid' family would clobber the account's poly bot → it must stay bash (None).
        self.assertIsNone(da.native_target("cex-grid-binance-sim"))

    def test_infra(self):
        self.assertEqual(da.native_target("infra-cex-feed"), ("infra", "cexfeed"))
        self.assertEqual(da.native_target("infra-feed-15m"), ("infra", "feed"))
        self.assertEqual(da.native_target("infra-feed-5m"), ("infra", "feed5m"))
        self.assertEqual(da.native_target("infra-indicators"), ("infra", "indicators"))
        self.assertEqual(da.native_target("infra-status"), ("infra", "status"))

    def test_account_bot_before_generic_polymarket(self):
        # polymarket-multibot (account_bot, infra) must win over the generic polymarket family rule
        self.assertEqual(da.native_target("polymarket-multibot"), ("infra", "account"))

    def test_unknown_returns_none(self):
        self.assertIsNone(da.native_target("cex-orderbook"))
        self.assertIsNone(da.native_target(""))

    def test_every_target_is_a_real_deployer(self):
        # a rule can never point at a target that deploy_family/deploy_infra doesn't implement
        for _prefix, (kind, target) in da._NATIVE_TARGET_RULES:
            table = da.FAMILIES if kind == "family" else da.INFRA
            self.assertIn(target, table, f"{kind} target {target!r} not implemented")


class _FakeHost:
    """Captures the shell scripts passed to ssh() and returns a canned stdout so the action's
    success check passes — lets us assert on WHAT the action would run without any SSH."""
    def __init__(self, stdout=""):
        self.user = "tester"
        self.scripts: list[str] = []
        self._stdout = stdout

    def ssh(self, script):
        self.scripts.append(script)
        return SimpleNamespace(stdout=self._stdout, stderr="", returncode=0)


class TestActConfigFilename(unittest.TestCase):
    def test_write_uses_config_name(self):
        # _rp maps ~ → $HOME, so the remote path is $HOME/tradinebotte/config_grid.json
        h = _FakeHost(stdout="ok")
        self.assertTrue(da.act_config(h, "~/tradinebotte", {"a": 1}, "write", "config_grid.json"))
        self.assertIn("$HOME/tradinebotte/config_grid.json", h.scripts[0])
        self.assertNotIn("config.json", h.scripts[0])  # not the fixed name

    def test_write_defaults_to_legacy_name(self):
        h = _FakeHost(stdout="ok")
        self.assertTrue(da.act_config(h, "~/tradinebotte", {"a": 1}, "write"))
        self.assertIn("$HOME/tradinebotte/config.json", h.scripts[0])

    def test_merge_uses_config_name(self):
        h = _FakeHost(stdout="cfg-merged")
        self.assertTrue(da.act_config(h, "~/tradinebotte", {"data_source": "feed"}, "merge",
                                      "config_threshold.json"))
        self.assertIn("config_threshold.json", h.scripts[0])


class TestSingleTreeDropin(unittest.TestCase):
    def test_writes_dir_and_instance(self):
        h = _FakeHost(stdout="dropin-ok")
        self.assertTrue(da.act_single_tree_dropin(h, "tradinebotte-accumulation.service", "accumulation"))
        s = h.scripts[0]
        # the drop-in must both point at the shared tree AND set the instance suffix
        self.assertIn("Environment=TRADINEBOTTE_DIR=%h/tradinebotte", s)
        self.assertIn("Environment=TRADINEBOTTE_INSTANCE=accumulation", s)
        self.assertIn("tradinebotte-accumulation.service.d/single-tree.conf", s)
        self.assertIn("daemon-reload", s)

    def test_empty_instance_clears(self):
        h = _FakeHost(stdout="done")
        self.assertTrue(da.act_single_tree_dropin(h, "tradinebotte-live.service", ""))
        s = h.scripts[0]
        self.assertIn("rm -f", s)
        self.assertIn("single-tree.conf", s)
        self.assertNotIn("Environment=", s)  # a clear must not write any env


class TestSingleTreeInstanceMatchesBotId(unittest.TestCase):
    def test_family_role_is_the_instance_and_botid_key(self):
        # single-tree uses instance = spec["role"]; live_bot writes bot_id_<strategy_type> and reads
        # config_<TRADINEBOTTE_INSTANCE>.json — these only stay coherent because role == strategy_type
        # for every family. Guard that the roles are exactly the four strategy_type values.
        self.assertEqual({f: s["role"] for f, s in da.FAMILIES.items()},
                         {"grid": "grid", "swing": "swing",
                          "accumulation": "accumulation", "polymarket": "threshold"})


if __name__ == "__main__":
    unittest.main()
