"""
Import-purity guard: importing a bot module must NOT persist a fleet identity.

`resolve_bot_id` PERSISTS a generated id (`bot_id_<role>`) the first time it runs, so any
module that resolves its id at *import* time writes a stray `bot_id_*` file into whatever
directory it can reach — the source tree, or the importer's cwd — the moment it is imported
(a test collecting it, a REPL, a lint that imports). Two harms: it litters the tree with an
untracked file, and it mints a NEW random id every time (the file is regenerated, not reused),
so a stray one carries no meaning and could in principle be adopted by a hand-run and collide
with the real fleet id. The fix is to resolve the id inside the async runner, never at module
top; this guard makes the wart impossible to reintroduce silently.

Each module is imported in a fresh interpreter with cwd = an empty tmp dir and TRADINEBOTTE_DIR
unset (the harshest case — resolve_bot_id would fall back to cwd / the module dir). We assert
no `bot_id_*` appears in the cwd NOR next to the module's source.
"""

import os
import subprocess
import sys
import tempfile
import unittest

_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

# (package dir on sys.path, module name) for every bot/infra entrypoint.
_MODULES = [
    ("tradinebotte-indicators", "indicators"),
    ("tradinebotte-cex", "cex_feed"),
    ("tradinebotte-polymarket", "feed"),
    ("tradinebotte-polymarket", "live_bot"),
]


class TestImportPersistsNoBotId(unittest.TestCase):
    def _assert_clean_import(self, pkg_rel, mod):
        pkg = os.path.join(_REPO, pkg_rel)
        if not os.path.isdir(pkg):
            self.skipTest(f"{pkg_rel} not present in this checkout")
        # snapshot any bot_id_* already sitting in the pkg dir (there should be none, but a
        # concurrent/leftover one must not make us falsely pass OR falsely fail).
        before = {f for f in os.listdir(pkg) if f.startswith("bot_id_")}
        workdir = tempfile.mkdtemp(prefix="botidguard.")
        probe = (
            "import sys\n"
            f"sys.path.insert(0, {pkg!r})\n"
            f"import {mod}\n"
            "print('OK')\n"
        )
        env = dict(os.environ)
        env.pop("TRADINEBOTTE_DIR", None)  # force the worst-case fallback path
        env.pop("PYTHONPATH", None)
        try:
            res = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=workdir, env=env, capture_output=True, text=True, timeout=120, check=False,
            )
            self.assertEqual(
                res.returncode, 0,
                f"import {mod} failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
            )
            cwd_stray = [f for f in os.listdir(workdir) if f.startswith("bot_id_")]
            self.assertFalse(
                cwd_stray,
                f"importing {mod} wrote a bot_id into the importer's cwd: {cwd_stray} "
                f"(resolve it inside the runner, not at module top)",
            )
            pkg_stray = {f for f in os.listdir(pkg) if f.startswith("bot_id_")} - before
            self.assertFalse(
                pkg_stray,
                f"importing {mod} wrote a bot_id into its own source tree: {sorted(pkg_stray)} "
                f"(resolve it inside the runner, not at module top)",
            )
        finally:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)

    def test_indicators_import_persists_no_bot_id(self):
        self._assert_clean_import("tradinebotte-indicators", "indicators")

    def test_cex_feed_import_persists_no_bot_id(self):
        self._assert_clean_import("tradinebotte-cex", "cex_feed")

    def test_feed_import_persists_no_bot_id(self):
        self._assert_clean_import("tradinebotte-polymarket", "feed")

    def test_live_bot_import_persists_no_bot_id(self):
        self._assert_clean_import("tradinebotte-polymarket", "live_bot")


if __name__ == "__main__":
    unittest.main()
