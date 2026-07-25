"""Every deployed connector×strategy pairing must satisfy botcore.connectors.validate().

validate() is enforced at bot startup (live_bot), but a connector that loses a method its
strategy needs would then only fail on that bot's next deploy. This test checks the whole
matrix at CI, sourced from deploy_actions.FAMILIES (the deploy's own connector+role table)
plus the pairings deployed by the bash paths that aren't FAMILIES-driven.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tradinebotte-cex"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tradinebotte-polymarket"))

import deploy_actions  # noqa: E402  — FAMILIES: family -> {connector, role(=strategy_type)}
import connectors      # noqa: E402  — load(), validate()

# Deployed by bash paths (not FAMILIES): the MEXC-futures grid on the futures account.
_EXTRA_PAIRINGS = [("mexc_futures", "grid")]


class TestConnectorContract(unittest.TestCase):

    def _pairings(self):
        seen = set()
        for spec in deploy_actions.FAMILIES.values():
            seen.add((spec["connector"], spec["role"]))
        seen.update(_EXTRA_PAIRINGS)
        return sorted(seen)

    def test_every_deployed_pairing_satisfies_validate(self):
        for connector, strategy_type in self._pairings():
            with self.subTest(connector=connector, strategy=strategy_type):
                module = connectors.load(connector)
                # raises RuntimeError if the connector is missing a required method
                connectors.validate(module, strategy_type)

    def test_validate_rejects_an_incomplete_connector(self):
        # Negative guard so the matrix test can't pass vacuously: polymarket lacks the CEX
        # grid methods, so validating it against 'grid' MUST raise.
        with self.assertRaises(RuntimeError):
            connectors.validate(connectors.load("polymarket"), "grid")


if __name__ == "__main__":
    unittest.main()
