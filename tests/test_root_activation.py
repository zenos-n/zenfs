import errno
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import layout
import session
from layout import initialize_hierarchy, initialize_home
from zenfsctl import ZenFSError


class RootActivationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manifest = {
            "directories": ["/Apps", "/Live", "/System", "/Users", "/mnt"],
            "aliases": {
                "/Boot": "/boot", "/Config": "/etc", "/home": "/Users", "/Packages": "/nix",
                "/System/Config": "/etc", "/System/Current": "/run/current-system",
                "/System/Index": "/run/current-system/sw", "/Live/Devices": "/dev",
                "/Live/Processes": "/proc", "/Live/Runtime": "/run", "/Live/System": "/sys",
                "/Live/Temporary": "/tmp", "/Live/Variable": "/var", "/Mount": "/mnt",
            },
            "hiddenRootEntries": ["home", "etc", "nix", "run", "mnt"],
        }

    def legacy_aliases(self):
        (self.root / "Live").symlink_to("/run")
        (self.root / "Packages").symlink_to("/nix/store")

    def test_full_root_layout_and_repeat_activation(self):
        (self.root / "home/alice").mkdir(parents=True)
        (self.root / "home/alice/document").write_text("kept")
        (self.root / "Config/ZenOS").mkdir(parents=True)
        source = self.root / "Config/ZenOS/main.zcfg"
        source.write_text("# source\n")
        inode = source.stat().st_ino
        (self.root / "etc").mkdir()
        self.legacy_aliases()
        initialize_hierarchy(self.manifest, self.root)
        initialize_hierarchy(self.manifest, self.root)
        for name, target in self.manifest["aliases"].items():
            self.assertEqual(target, os.readlink(self.root / name.lstrip("/")))
        for name in self.manifest["directories"]:
            path = self.root / name.lstrip("/")
            self.assertTrue(path.is_dir())
            self.assertFalse(path.is_symlink())
        self.assertEqual(inode, (self.root / "etc/ZenOS/main.zcfg").stat().st_ino)
        self.assertEqual("kept", (self.root / "Users/alice/document").read_text())

    def test_collision_does_not_remove_legacy_aliases(self):
        self.legacy_aliases()
        (self.root / "Config").mkdir()
        (self.root / "Config/unowned").write_text("kept")
        with self.assertRaises(ZenFSError):
            initialize_hierarchy(self.manifest, self.root)
        self.assertEqual("/run", os.readlink(self.root / "Live"))
        self.assertEqual("/nix/store", os.readlink(self.root / "Packages"))

    def test_late_alias_collision_does_not_migrate_home(self):
        (self.root / "home/alice").mkdir(parents=True)
        (self.root / "Mount").symlink_to("/unowned")
        with self.assertRaises(ZenFSError):
            initialize_hierarchy(self.manifest, self.root)
        self.assertTrue((self.root / "home/alice").is_dir())
        self.assertFalse(os.path.lexists(self.root / "Users"))

    def test_config_migration_refuses_symlinked_destination_parent(self):
        (self.root / "Config/ZenOS").mkdir(parents=True)
        (self.root / "Config/ZenOS/main.zcfg").write_text("source")
        outside = self.root / "outside"
        outside.mkdir()
        (self.root / "etc").symlink_to(outside)
        with self.assertRaises(ZenFSError):
            initialize_hierarchy(self.manifest, self.root)
        self.assertEqual("source", (self.root / "Config/ZenOS/main.zcfg").read_text())
        self.assertEqual([], list(outside.iterdir()))

    def test_config_destination_collision_preserves_both_sources(self):
        for base, data in (("Config", "old"), ("etc", "current")):
            directory = self.root / base / "ZenOS"
            directory.mkdir(parents=True)
            (directory / "main.zcfg").write_text(data)
        with self.assertRaises(ZenFSError):
            initialize_hierarchy(self.manifest, self.root)
        self.assertEqual("old", (self.root / "Config/ZenOS/main.zcfg").read_text())
        self.assertEqual("current", (self.root / "etc/ZenOS/main.zcfg").read_text())

    def test_cross_device_refusal_restores_users_alias(self):
        (self.root / "home/alice").mkdir(parents=True)
        (self.root / "Users").symlink_to("/home")
        with patch("layout.rename_noreplace", side_effect=OSError(errno.EXDEV, "cross-device")):
            with self.assertRaises(OSError):
                initialize_hierarchy(self.manifest, self.root)
        self.assertEqual("/home", os.readlink(self.root / "Users"))
        self.assertTrue((self.root / "home/alice").is_dir())

    def test_private_init_preserves_home_manager_xdg_symlink(self):
        home = self.root / "Users/alice"
        config = home / ".private/Config"
        config.mkdir(parents=True)
        store = self.root / "store-user-dirs"
        store.write_text('XDG_DOWNLOAD_DIR="$HOME/Incoming"\n')
        managed = config / "user-dirs.dirs"
        managed.symlink_to(store)
        inode = managed.lstat().st_ino
        initialize_home(home)
        initialize_home(home)
        self.assertEqual(str(store), os.readlink(managed))
        self.assertEqual(inode, managed.lstat().st_ino)
        self.assertIn("Incoming", managed.read_text())

    def test_disabled_hierarchy_manifest_does_not_migrate_legacy_paths(self):
        self.legacy_aliases()
        (self.root / "home/alice").mkdir(parents=True)
        initialize_hierarchy({"directories": [], "aliases": {}, "hiddenRootEntries": []}, self.root)
        self.assertEqual("/run", os.readlink(self.root / "Live"))
        self.assertEqual("/nix/store", os.readlink(self.root / "Packages"))
        self.assertTrue((self.root / "home/alice").is_dir())
        self.assertFalse((self.root / ".hidden").exists())

    def test_cli_uses_explicit_fixture_root(self):
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps(self.manifest))
        command = [sys.executable, layout.__file__, "--manifest", str(manifest), "--root", str(self.root)]
        for _ in range(2):
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("/etc", os.readlink(self.root / "Config"))
        self.assertTrue((self.root / "Apps").is_dir())

    def test_session_initializes_three_managed_homes_without_cross_talk(self):
        homes = {"alice": self.root / "Users/alice", "bob": self.root / "Users/bob",
                 "zenos": self.root / "run/zenos-oobe"}
        records = {}
        for name, home in homes.items():
            config = home / ".private/Config"
            config.mkdir(parents=True)
            source = config / "main.zcfg"
            source.write_text(f"# canonical {name}\n")
            records[name] = source.stat()
            (home / ".config").mkdir()
            (home / ".config/preferences").write_text(name)
            (home / ".bash_history").write_text(f"history for {name}")
        manifest = self.root / "session-manifest.json"
        manifest.write_text(json.dumps({"schema": "zenfs-v1", "strict": True,
            "users": {name: {"home": str(home), "group": "users"} for name, home in homes.items()}}))
        for _ in range(2):
            for name, home in homes.items():
                with patch.dict(os.environ, {"HOME": str(home)}), \
                     patch("session.pwd.getpwuid", return_value=SimpleNamespace(pw_name=name)), \
                     patch.object(sys, "argv", ["zenfs-session", "--manifest", str(manifest), "init"]):
                    self.assertEqual(0, session.main())
                    self.assertEqual(str(home / ".private/Config"), os.environ["XDG_CONFIG_HOME"])
        for name, home in homes.items():
            source = home / ".private/Config/main.zcfg"
            self.assertEqual(f"# canonical {name}\n", source.read_text())
            self.assertEqual(records[name].st_ino, source.stat().st_ino)
            self.assertEqual(records[name].st_mtime_ns, source.stat().st_mtime_ns)
            self.assertEqual(records[name].st_mode, source.stat().st_mode)
            self.assertEqual(name, (home / ".private/Config/preferences").read_text())
            self.assertEqual(f"history for {name}", (home / ".private/State/shell/bash_history").read_text())
            self.assertEqual(0o700, home.stat().st_mode & 0o777)
            self.assertEqual(0o700, (home / ".private/Apps").stat().st_mode & 0o777)
            self.assertEqual(os.getuid(), (home / ".private/Config").stat().st_uid)


if __name__ == "__main__":
    unittest.main()
