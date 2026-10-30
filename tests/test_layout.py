import json
import os
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch

from layout import initialize_hierarchy, initialize_home
from session import environment, managed_identity, reconcile_flatpak, verify_drive
from zenfsctl import ZenFSError


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "Users/alice"

    def test_private_migration_preserves_canonical_config_and_is_idempotent(self):
        config = self.home / ".private/Config"
        config.mkdir(parents=True)
        canonical = config / "main.zcfg"
        contents = b'# Setup source\nusers.alice.legacy.home = "/Users/alice";\n'
        canonical.write_bytes(contents)
        before = canonical.stat()
        old = self.home / ".config"
        old.mkdir()
        (old / "editor").write_text("preferences")
        share = self.home / ".local/share"
        share.mkdir(parents=True)
        (share / "document").write_text("data")
        (self.home / ".bash_history").write_text("commands")
        (self.home / ".unknown").write_text("unknown")
        initialize_home(self.home)
        initialize_home(self.home)
        self.assertEqual(contents, canonical.read_bytes())
        self.assertEqual(before.st_ino, canonical.stat().st_ino)
        self.assertEqual(before.st_mtime_ns, canonical.stat().st_mtime_ns)
        self.assertEqual("preferences", (config / "editor").read_text())
        self.assertEqual("data", (self.home / ".private/Packages/document").read_text())
        self.assertEqual("unknown", (self.home / ".private/Legacy/unknown").read_text())
        self.assertEqual([".private"], sorted(p.name for p in self.home.iterdir() if p.name.startswith(".")))
        self.assertEqual(0o700, (self.home / ".private").stat().st_mode & 0o777)

    def test_collision_refuses_to_overwrite_either_file(self):
        old = self.home / ".config"
        new = self.home / ".private/Config"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "main.zcfg").write_text("legacy")
        (new / "main.zcfg").write_text("canonical")
        with self.assertRaisesRegex(ZenFSError, "collision"):
            initialize_home(self.home)
        self.assertEqual("legacy", (old / "main.zcfg").read_text())
        self.assertEqual("canonical", (new / "main.zcfg").read_text())

    def test_non_strict_to_strict_migration(self):
        initialize_home(self.home, strict=False)
        self.assertTrue((self.home / ".config").is_symlink())
        initialize_home(self.home, strict=False)
        initialize_home(self.home, strict=True)
        self.assertFalse(os.path.lexists(self.home / ".config"))
        self.assertTrue((self.home / ".private/Local/share").is_symlink())

    def test_identical_cache_collision_preserves_destination_and_completes(self):
        old = self.home / ".cache/mesa_shader_cache/73"
        new = self.home / ".private/Live/mesa_shader_cache/73"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "shader").write_bytes(b"same cached shader")
        (new / "shader").write_bytes(b"same cached shader")
        inode = (new / "shader").stat().st_ino
        initialize_home(self.home)
        initialize_home(self.home)
        self.assertFalse(old.exists())
        self.assertEqual(inode, (new / "shader").stat().st_ino)
        self.assertEqual(b"same cached shader", (new / "shader").read_bytes())

    def test_different_cache_collision_preserves_both_files(self):
        old = self.home / ".cache"
        new = self.home / ".private/Live"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (old / "shader").write_bytes(b"old")
        (new / "shader").write_bytes(b"new")
        with self.assertRaisesRegex(ZenFSError, "collision"):
            initialize_home(self.home)
        self.assertEqual(b"old", (old / "shader").read_bytes())
        self.assertEqual(b"new", (new / "shader").read_bytes())

    def test_cache_symlink_collision_is_not_deduplicated(self):
        old = self.home / ".cache"
        new = self.home / ".private/Live"
        old.mkdir(parents=True)
        new.mkdir(parents=True)
        (new / "shader").write_bytes(b"same")
        (old / "shader").symlink_to(new / "shader")
        with self.assertRaisesRegex(ZenFSError, "collision"):
            initialize_home(self.home)
        self.assertTrue((old / "shader").is_symlink())
        self.assertEqual(b"same", (new / "shader").read_bytes())

    def test_private_symlink_is_refused(self):
        self.home.mkdir(parents=True)
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".private").symlink_to(outside)
        with self.assertRaises(ZenFSError):
            initialize_home(self.home)
        self.assertEqual([], list(outside.iterdir()))

    def test_user_directory_configuration_is_not_replaced(self):
        initialize_home(self.home)
        settings = self.home / ".private/Config/user-dirs.dirs"
        settings.write_text('XDG_DOWNLOAD_DIR="$HOME/Incoming"\n')
        initialize_home(self.home)
        self.assertIn("Incoming", settings.read_text())

    def test_root_migration_leaves_drives_untouched(self):
        (self.root / "home/alice").mkdir(parents=True)
        (self.root / "home/alice/data").write_text("kept")
        (self.root / "Users").symlink_to("/home")
        (self.root / "Drives").mkdir()
        (self.root / "Drives/sentinel").write_text("untouched")
        manifest = {"directories": ["/Users", "/Apps", "/Live"],
                    "aliases": {"/home": "/Users", "/Live/Runtime": "/run"},
                    "hiddenRootEntries": ["home", "run"]}
        initialize_hierarchy(manifest, self.root)
        initialize_hierarchy(manifest, self.root)
        self.assertEqual("kept", (self.root / "Users/alice/data").read_text())
        self.assertEqual("untouched", (self.root / "Drives/sentinel").read_text())
        self.assertEqual("/Users", os.readlink(self.root / "home"))

    def test_root_collision_and_mount_are_refused(self):
        (self.root / "home").mkdir()
        (self.root / "Users").mkdir()
        manifest = {"directories": ["/Users"], "aliases": {}, "hiddenRootEntries": []}
        with self.assertRaises(ZenFSError):
            initialize_hierarchy(manifest, self.root)
        with patch("layout.os.path.ismount", return_value=True):
            with self.assertRaisesRegex(ZenFSError, "separately mounted"):
                initialize_hierarchy(manifest, self.root)

    def test_wrong_root_alias_is_refused(self):
        (self.root / "Config").symlink_to("/other")
        with self.assertRaises(ZenFSError):
            initialize_hierarchy({"directories": [], "aliases": {"/Config": "/etc"}, "hiddenRootEntries": []}, self.root)
        self.assertEqual("/other", os.readlink(self.root / "Config"))

    def test_managed_identity_includes_oobe_but_excludes_other_homes(self):
        manifest = {"users": {"zenos": {"home": "/run/zenos-oobe"}, "alice": {"home": "/Users/alice"}}}
        self.assertEqual(("zenos", Path("/run/zenos-oobe")), managed_identity(manifest, username="zenos", home="/run/zenos-oobe"))
        self.assertIsNone(managed_identity(manifest, username="root", home="/root"))
        self.assertIsNone(managed_identity(manifest, username="alice", home="/tmp/alice"))
        self.assertNotIn("NIX_PROFILE", environment(self.home))
        self.assertEqual(str(self.home / ".private/Config"), environment(self.home)["XDG_CONFIG_HOME"])
        for key, path in {"CODEX_HOME": "Packages/codex", "NPM_CONFIG_CACHE": "Live/npm",
                          "MESA_SHADER_CACHE_DIR": "Live/mesa_shader_cache"}.items():
            self.assertEqual(str(self.home / ".private" / path), environment(self.home)[key])

    def test_steam_bootstrap_links_and_pipe_move_to_private_home(self):
        token = "68561f488261e6f7d6d63a8f8765d6f75091a558409550efeb2cc3c111f5b280"
        steam = self.home / ".steam"
        steam.mkdir(parents=True)
        (steam / "registry.vdf").write_text("preferences")
        os.mkfifo(steam / "steam.pipe")
        (self.home / ".steampath").symlink_to(steam / "sdk32/steam")
        (self.home / ".steampid").symlink_to(steam / "steam.pid")
        initialize_home(self.home, strict=True)
        initialize_home(self.home, strict=True)
        private_home = self.home / ".private/State/zenos/apps" / token / "home"
        self.assertEqual((private_home / ".steam/registry.vdf").read_text(), "preferences")
        self.assertTrue((private_home / ".steam/steam.pipe").is_fifo())
        self.assertEqual((private_home / ".steampath").readlink(), steam / "sdk32/steam")
        self.assertEqual((private_home / ".steampid").readlink(), steam / "steam.pid")
        for name in [".steam", ".steampath", ".steampid"]:
            self.assertFalse(os.path.lexists(self.home / name))

    def test_nvidia_profile_migrates_to_recorder_private_home(self):
        profile = self.home / ".nv/nvidia-application-profiles-rc.d/custom"
        profile.parent.mkdir(parents=True)
        profile.write_text("profile sentinel")
        initialize_home(self.home, strict=True)
        initialize_home(self.home, strict=True)
        target = self.home / ".private/Packages/gpu-screen-recorder/.nv/nvidia-application-profiles-rc.d/custom"
        self.assertEqual(target.read_text(), "profile sentinel")
        self.assertFalse((self.home / ".nv").exists())

    def test_android_build_data_migrates_to_private_runtime_paths(self):
        destinations = {".android": "Config/android", ".gradle": "Packages/gradle", ".m2": "Packages/maven/.m2"}
        for old in destinations:
            source = self.home / old
            source.mkdir(parents=True)
            (source / "data").write_text(old)
        initialize_home(self.home, strict=True)
        initialize_home(self.home, strict=True)
        for old, destination in destinations.items():
            self.assertFalse((self.home / old).exists())
            self.assertEqual((self.home / ".private" / destination / "data").read_text(), old)
        with patch.dict(os.environ, {"GRADLE_OPTS": "-Xmx1g -Dmaven.repo.local=/old"}):
            env = environment(self.home)
            self.assertIn("-Xmx1g", env["GRADLE_OPTS"])
            self.assertNotIn("/old", env["GRADLE_OPTS"])
            self.assertIn(str(self.home / ".private/Packages/maven/.m2/repository"), env["GRADLE_OPTS"])
            self.assertEqual(env["GRADLE_USER_HOME"], str(self.home / ".private/Packages/gradle"))
            self.assertEqual(env["ANDROID_USER_HOME"], str(self.home / ".private/Config/android"))
            with patch.dict(os.environ, env):
                self.assertEqual(environment(self.home), env)

    def test_codex_and_npm_migrate_to_their_runtime_paths(self):
        for name in [".codex", ".npm"]:
            (self.home / name).mkdir(parents=True, exist_ok=True)
            (self.home / name / "data").write_text(name)
        with sqlite3.connect(self.home / ".codex/state_5.sqlite") as db:
            db.execute("CREATE TABLE threads (rollout_path TEXT)")
            db.execute("INSERT INTO threads VALUES (?)", (str(self.home / ".codex/sessions/thread.jsonl"),))
        initialize_home(self.home)
        self.assertEqual(".codex", (self.home / ".private/Packages/codex/data").read_text())
        self.assertEqual(".npm", (self.home / ".private/Live/npm/data").read_text())
        with sqlite3.connect(self.home / ".private/Packages/codex/state_5.sqlite") as db:
            self.assertEqual(str(self.home / ".private/Packages/codex/sessions/thread.jsonl"),
                             db.execute("SELECT rollout_path FROM threads").fetchone()[0])

    def test_live_codex_migration_waits_until_the_daemon_stops(self):
        daemon = self.home / ".codex/app-server-daemon"
        daemon.mkdir(parents=True)
        (daemon / "daemon.pid").write_text(json.dumps({"pid": 12345}))
        with patch("layout.Path.read_bytes", return_value=b"codex\0app-server"):
            initialize_home(self.home)
        self.assertTrue(daemon.exists())
        target = self.home / ".private/Packages/codex"
        target.symlink_to(self.home / ".codex")
        with patch("layout.Path.read_bytes", side_effect=FileNotFoundError):
            initialize_home(self.home)
        self.assertFalse((self.home / ".codex").exists())
        self.assertTrue((self.home / ".private/Packages/codex/app-server-daemon/daemon.pid").exists())

    def test_flatpak_reconciliation_does_not_delete_remote(self):
        descriptor = self.root / "test.flatpakrepo"
        descriptor.write_text("[Flatpak Repo]\nUrl=https://example.org/repo\nGPGKey=dGVzdA==\n")
        calls = []
        reconcile_flatpak({"enable": True, "name": "zenos-flathub", "descriptor": str(descriptor)}, "user", run=lambda args, **kwargs: calls.append(args))
        self.assertEqual(["remote-add", "remote-modify"], [args[1] for args in calls])
        self.assertTrue(all("--user" in args for args in calls))
        self.assertIn("--gpg-verify", calls[1])

    def test_roaming_rejects_wrong_marker_and_symlinked_bind_source(self):
        drive_root = self.root / "Mount/work"
        source = drive_root / "alice/docs"
        source.mkdir(parents=True)
        target = self.home / ".private/Mount/docs"
        target.mkdir(parents=True)
        marker = drive_root / ".zenfs-roaming.json"
        marker.write_text(json.dumps({"schema": "zenfs-roaming-v1", "id": "work"}))
        drive = {"mountPoint": str(drive_root), "markerFile": marker.name, "markerId": "work",
                 "privateMounts": [{"sourcePath": str(source), "targetPath": str(target)}]}
        with patch("session.os.path.ismount", return_value=True):
            verify_drive(drive)
            source.rmdir()
            source.symlink_to(self.root)
            with self.assertRaisesRegex(ZenFSError, "real directory"):
                verify_drive(drive)
            drive["markerId"] = "other"
            with self.assertRaises(ZenFSError):
                verify_drive(drive)


if __name__ == "__main__":
    unittest.main()
