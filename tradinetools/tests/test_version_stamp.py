"""Unit tests for tradinetools.read_version_stamp."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tradinetools import read_version_stamp


class TestReadVersionStampEnvVar(unittest.TestCase):

    def setUp(self):
        os.environ.pop("TRADINEBOTTE_VERSION", None)

    def tearDown(self):
        os.environ.pop("TRADINEBOTTE_VERSION", None)

    def test_env_var_takes_priority_over_file(self):
        with tempfile.TemporaryDirectory() as d:
            stamp = os.path.join(d, "version.stamp")
            with open(stamp, "w", encoding="utf-8") as f:
                f.write("file_hash")
            os.environ["TRADINEBOTTE_VERSION"] = "env_hash"
            self.assertEqual(read_version_stamp(install_dir=d), "env_hash")

    def test_env_var_whitespace_stripped(self):
        os.environ["TRADINEBOTTE_VERSION"] = "  abc1234  \n"
        self.assertEqual(read_version_stamp(), "abc1234")

    def test_empty_env_var_falls_through_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            stamp = os.path.join(d, "version.stamp")
            with open(stamp, "w", encoding="utf-8") as f:
                f.write("file_hash")
            os.environ["TRADINEBOTTE_VERSION"] = ""
            self.assertEqual(read_version_stamp(install_dir=d), "file_hash")


class TestReadVersionStampFile(unittest.TestCase):

    def setUp(self):
        os.environ.pop("TRADINEBOTTE_VERSION", None)

    def tearDown(self):
        os.environ.pop("TRADINEBOTTE_VERSION", None)

    def test_reads_stamp_from_install_dir(self):
        with tempfile.TemporaryDirectory() as d:
            stamp = os.path.join(d, "version.stamp")
            with open(stamp, "w", encoding="utf-8") as f:
                f.write("e983770")
            self.assertEqual(read_version_stamp(install_dir=d), "e983770")

    def test_strips_trailing_newline(self):
        with tempfile.TemporaryDirectory() as d:
            stamp = os.path.join(d, "version.stamp")
            with open(stamp, "w", encoding="utf-8") as f:
                f.write("e983770\n")
            self.assertEqual(read_version_stamp(install_dir=d), "e983770")

    def test_no_stamp_file_returns_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            result = read_version_stamp(install_dir=d)
            self.assertEqual(result, "unknown")

    def test_missing_install_dir_returns_unknown(self):
        result = read_version_stamp(install_dir="/nonexistent/path/that/does/not/exist")
        self.assertEqual(result, "unknown")

    def test_no_args_returns_unknown_when_no_stamp(self):
        with tempfile.TemporaryDirectory() as d:
            orig_cwd = os.getcwd()
            try:
                os.chdir(d)
                result = read_version_stamp()
                self.assertIn(result, ("unknown", ))
            finally:
                os.chdir(orig_cwd)

    def test_empty_stamp_file_returns_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            stamp = os.path.join(d, "version.stamp")
            with open(stamp, "w", encoding="utf-8") as f:
                f.write("")
            self.assertEqual(read_version_stamp(install_dir=d), "unknown")

    def test_install_dir_takes_priority_over_cwd(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            with open(os.path.join(d1, "version.stamp"), "w", encoding="utf-8") as f:
                f.write("hash_install")
            with open(os.path.join(d2, "version.stamp"), "w", encoding="utf-8") as f:
                f.write("hash_cwd")
            orig_cwd = os.getcwd()
            try:
                os.chdir(d2)
                self.assertEqual(read_version_stamp(install_dir=d1), "hash_install")
            finally:
                os.chdir(orig_cwd)


if __name__ == "__main__":
    unittest.main()
