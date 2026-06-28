"""
Flat-deploy import guard (Plan D step 3).

The unit suites all run in the *monorepo* layout, where `botcore` / `connectors` /
`strategy_engines` resolve via per-test sys.path inserts and self-bootstraps. Production
runs the *flat* layout: install.sh copies every module into one INSTALL_DIR and the bot is
launched from there, so those packages resolve purely because INSTALL_DIR is on sys.path —
a completely different resolution path. live_bot now imports `botcore` and `connectors` at
module top, so a flat-resolution mistake would crash-loop every bot at startup while every
monorepo test stays green.

This builds a throwaway flat install (the exact file set install.sh copies) and asserts the
two entrypoints — live_bot and account_bot (the 2nd importer, runs on the data-plane account)
— import cleanly from it, with botcore/connectors resolved flat (not leaked from the repo).
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_PM = os.path.join(_REPO, "tradinebotte-polymarket")
_CEX = os.path.join(_REPO, "tradinebotte-cex")
_CORE = os.path.join(_REPO, "tradinebotte-core")

# The file set install.sh lays down flat in INSTALL_DIR (scripts/install.sh).
_FLAT_FILES = [
    (_PM, "live_bot.py"), (_PM, "api_polymarket.py"), (_PM, "bot_utils.py"),
    (_PM, "feed.py"), (_PM, "account_bot.py"),
    (_CEX, "api_binance.py"), (_CEX, "api_mexc.py"),
]
_FLAT_PKGS = [
    (_CORE, "botcore", ["__init__.py", "strategy.py"]),
    (_CEX, "connectors", ["__init__.py"]),
    (_CEX, "strategy_engines",
     ["__init__.py", "base.py", "grid.py", "swing.py", "swinghold.py", "dca.py"]),
]


class TestFlatDeployImport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (os.path.isdir(_PM) and os.path.isdir(_CEX) and os.path.isdir(_CORE)):
            raise unittest.SkipTest("monorepo package dirs not present in this checkout")

    def _build_flat(self, dst):
        for src, name in _FLAT_FILES:
            shutil.copy(os.path.join(src, name), os.path.join(dst, name))
        for src, pkg, files in _FLAT_PKGS:
            os.makedirs(os.path.join(dst, pkg), exist_ok=True)
            for f in files:
                shutil.copy(os.path.join(src, pkg, f), os.path.join(dst, pkg, f))

    def test_entrypoints_import_clean_from_flat_install(self):
        flat = tempfile.mkdtemp(prefix="flatsim.")
        try:
            self._build_flat(flat)
            # Run from the flat dir with a minimal env, the way ExecStart launches the bot.
            probe = (
                "import os, sys\n"
                "leaks = [p for p in sys.path if p.endswith("
                "('tradinebotte-core','tradinebotte-cex','tradinebotte-polymarket'))]\n"
                "assert not leaks, f'repo pkg dirs leaked onto sys.path: {leaks}'\n"
                "import live_bot, account_bot, botcore, connectors\n"
                "flat = os.path.realpath(os.getcwd())\n"
                "for m in (live_bot, account_bot, botcore, connectors):\n"
                "    f = os.path.realpath(m.__file__)\n"
                "    assert f.startswith(flat), f'{m.__name__} resolved outside flat: {f}'\n"
                "assert botcore.Strategy in live_bot.ThresholdStrategy.__mro__\n"
                "print('OK')\n"
            )
            env = dict(os.environ)
            data_dir = os.path.join(flat, "_data")
            os.makedirs(data_dir, exist_ok=True)  # account_bot opens a log here at import
            env["TRADINEBOTTE_DIR"] = data_dir
            env.pop("PYTHONPATH", None)  # don't leak the repo onto the child's path
            res = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=flat, env=env, capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(
                res.returncode, 0,
                f"flat import failed (a bot would crash-loop at startup):\n"
                f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
            )
            self.assertIn("OK", res.stdout)
        finally:
            shutil.rmtree(flat, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
