"""Run only the fixture shell produced by zenpkgs/tests/zenfs-activation.nix."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(sys.argv.pop(1))


class ActivationShellTests(unittest.TestCase):
    def check_activation(self, source, symlink):
        with tempfile.TemporaryDirectory(prefix="zenfs-activation-") as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir(mode=0o750)
            before = outside.stat()
            (outside / "data").write_text("untouched")
            home = root / source / "alice"
            home.parent.mkdir()
            if symlink:
                home.symlink_to(outside)
            else:
                home.mkdir()
            sentinel = root / "users-ran"
            result = subprocess.run([SCRIPT], env=dict(os.environ,
                ZENFS_TEST_ROOT=str(root), ZENFS_TEST_SENTINEL=str(sentinel)),
                text=True, capture_output=True, timeout=30)
            self.assertEqual(1 if symlink else 0, result.returncode, result.stdout + result.stderr)
            self.assertEqual(not symlink, sentinel.exists(), result.stdout + result.stderr)
            self.assertEqual(before.st_mode, outside.stat().st_mode)
            self.assertEqual(before.st_uid, outside.stat().st_uid)
            self.assertEqual("untouched", (outside / "data").read_text())
            if symlink:
                self.assertIn("unsafe managed home", result.stderr)
                self.assertTrue(home.is_symlink())
            self.assertIn("Activation script snippet zenfs-hierarchy", SCRIPT.read_text())
            self.assertIn('trap "_status=1', SCRIPT.read_text())

    def test_current_home_failure_stops_generated_shell_before_users(self):
        self.check_activation("Users", True)

    def test_migrated_home_failure_stops_generated_shell_before_users(self):
        self.check_activation("home", True)

    def test_valid_home_allows_fake_users_action(self):
        self.check_activation("Users", False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
