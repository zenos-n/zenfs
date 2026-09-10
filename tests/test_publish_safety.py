import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from layout import initialize_hierarchy, initialize_home
from session import environment
from zenfsctl import ZenFSError


class PublishSafetyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = {
            "directories": ["/Users", "/Live"],
            "aliases": {"/home": "/Users"},
            "hiddenRootEntries": ["home"],
            "users": {"alice": {"home": "/Users/alice"}},
        }

    def test_managed_home_and_ancestor_symlinks_are_refused_before_mutation(self):
        for location, home, migrate in (
            ("Users/alice", "/Users/alice", False),
            ("Users/team", "/Users/team/alice", False),
            ("home/alice", "/Users/alice", True),
            ("home/team", "/Users/team/alice", True),
            ("run/zenos-oobe", "/run/zenos-oobe", False),
            ("run", "/run/zenos-oobe", False),
        ):
            with self.subTest(location=location), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                root = Path(temporary)
                outside = root / "outside"
                outside.mkdir(mode=0o750)
                (outside / "sentinel").write_text("untouched")
                before = outside.stat()
                path = root / location
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(outside)
                (root / "Live").symlink_to("/run")
                manifest = dict(self.manifest, users={"alice": {"home": home}})
                with self.assertRaisesRegex(ZenFSError, "unsafe managed home"):
                    initialize_hierarchy(manifest, root)
                self.assertTrue(path.is_symlink())
                self.assertEqual("/run", os.readlink(root / "Live"))
                self.assertEqual(before.st_mode, outside.stat().st_mode)
                self.assertEqual(before.st_uid, outside.stat().st_uid)
                self.assertEqual("untouched", (outside / "sentinel").read_text())
                if migrate:
                    self.assertFalse(os.path.lexists(root / "Users"))

    def test_managed_home_file_is_refused(self):
        (self.root / "Users").mkdir()
        (self.root / "Users/alice").write_text("not a home")
        with self.assertRaisesRegex(ZenFSError, "unsafe managed home"):
            initialize_hierarchy(self.manifest, self.root)
        self.assertFalse((self.root / "Live").exists())

    def test_managed_home_checks_remain_active_without_root_layout(self):
        (self.root / "run").mkdir()
        (self.root / "run/zenos-oobe").symlink_to(self.root)
        manifest = {"directories": [], "aliases": {}, "users": {"zenos": {"home": "/run/zenos-oobe"}}}
        with self.assertRaisesRegex(ZenFSError, "unsafe managed home"):
            initialize_hierarchy(manifest, self.root)
        self.assertFalse((self.root / ".hidden").exists())

    def test_real_migrated_home_is_accepted_and_data_preserved(self):
        (self.root / "home/alice").mkdir(parents=True)
        (self.root / "home/alice/document").write_text("kept")
        (self.root / "Users").symlink_to("/home")
        initialize_hierarchy(self.manifest, self.root)
        self.assertEqual("kept", (self.root / "Users/alice/document").read_text())

    def test_strict_init_preserves_flatpak_var_tree_and_symlink(self):
        for symlink in (False, True):
            with self.subTest(symlink=symlink), tempfile.TemporaryDirectory(dir=self.root) as temporary:
                root = Path(temporary)
                home = root / "home"
                home.mkdir()
                data = root / "data" if symlink else home / ".var"
                app = data / "app/org.example.Test/config"
                app.mkdir(parents=True)
                (app / "settings").write_text("real Flatpak data")
                if symlink:
                    (home / ".var").symlink_to(data)
                before = (home / ".var").lstat()
                initialize_home(home)
                initialize_home(home)
                self.assertEqual("real Flatpak data", (home / ".var/app/org.example.Test/config/settings").read_text())
                self.assertEqual(before.st_ino, (home / ".var").lstat().st_ino)
                self.assertEqual(before.st_mode, (home / ".var").lstat().st_mode)
                self.assertFalse((home / ".private/Legacy/var").exists())

    def test_real_flatpak_still_uses_var_app_despite_private_xdg(self):
        binary = os.environ.get("ZENFS_TEST_FLATPAK", "flatpak")
        home = self.root / "home"
        app = home / ".var/app/org.example.ZenFSTest"
        app.mkdir(parents=True)
        (app / "data").write_text("fixture to delete explicitly")
        private = home / ".private/Packages/flatpak-apps/org.example.ZenFSTest"
        private.mkdir(parents=True)
        (private / "data").write_text("not Flatpak's data path")
        initialize_home(home)
        env = dict(os.environ, **environment(home), HOME=str(home),
            FLATPAK_USER_DIR=str(home / ".private/Packages/flatpak"),
            FLATPAK_SYSTEM_DIR=str(self.root / "system-flatpak"),
            FLATPAK_CONFIG_DIR=str(self.root / "flatpak-config"),
            DBUS_SESSION_BUS_ADDRESS=f"unix:path={self.root}/no-session-bus", LC_ALL="C")
        result = subprocess.run([binary, "uninstall", "--user", "--delete-data", "--noninteractive"],
            env=env, capture_output=True, text=True, timeout=30)
        # Upstream deletes the data before trying to reset permissions on D-Bus.
        # A deliberately absent bus prevents contact with the real user session.
        self.assertFalse(app.exists(), result.stderr)
        self.assertEqual("not Flatpak's data path", (private / "data").read_text())
        self.assertEqual(1, result.returncode)
        self.assertIn("Could not connect", result.stderr)


if __name__ == "__main__":
    unittest.main()
