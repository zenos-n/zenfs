from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest

import compat


TOKEN = "a" * 64


class CompatibilityTests(unittest.TestCase):
    def test_steam_uses_a_private_home_with_public_folders(self) -> None:
        settings = compat.default_settings(compat.STEAM_TOKEN)
        self.assertTrue(settings["syntheticHome"])
        self.assertEqual(sorted(compat.PUBLIC_DIRECTORIES), settings["sharedDirectories"])

    def test_gnosis_defaults_to_private_home_with_public_folders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = compat.load_settings(Path(directory), compat.GNOSIS_TOKEN)
            self.assertTrue(settings["syntheticHome"])
            self.assertEqual(sorted(compat.SHAREABLE_DIRECTORIES), settings["sharedDirectories"])

    @unittest.skipUnless(os.environ.get("ZENFS_TEST_BWRAP"), "requires bubblewrap")
    def test_browser_reads_project_html_and_assets_without_leaking_home_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            (home / ".private").mkdir(parents=True)
            project = home / "Projects" / "demo"
            project.mkdir(parents=True)
            (project / "index ł #1.html").write_text("local HTML")
            (project / "style.css").write_text("relative asset")
            (home / ".host-only").write_text("hidden")
            script = (
                "from pathlib import Path; p=Path.home(); "
                "assert (p/'Projects/demo/index ł #1.html').read_text() == 'local HTML'; "
                "assert (p/'Projects/demo/style.css').read_text() == 'relative asset'; "
                "assert not (p/'.host-only').exists(); "
                "(p/'.browser-cache').write_text('private')"
            )
            command = compat.sandbox_command(
                home, compat.GNOSIS_TOKEN, [sys.executable, "-c", script],
                compat.default_settings(compat.GNOSIS_TOKEN),
                runtime_directory=Path(directory), bwrap=os.environ["ZENFS_TEST_BWRAP"],
            )
            subprocess.run(command, check=True, timeout=10)
            self.assertFalse((home / ".browser-cache").exists())
            self.assertEqual("private", (compat.app_state(home, compat.GNOSIS_TOKEN) / "home/.browser-cache").read_text())

    def test_explicit_gnosis_shares_remain_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            compat.configure(home, compat.GNOSIS_TOKEN, shared_directories=["Documents"])
            self.assertEqual(["Documents"], compat.load_settings(home, compat.GNOSIS_TOKEN)["sharedDirectories"])

    def test_seahorse_defaults_to_a_synthetic_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = compat.app_token("nix-config", "org.gnome.seahorse.Application.desktop")
            self.assertTrue(compat.load_settings(Path(directory), token)["syntheticHome"])

    def test_defaults_do_not_enable_synthetic_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                {
                    "schema": 1,
                    "syntheticHome": False,
                    "sharedDirectories": [],
                },
                compat.load_settings(Path(directory), TOKEN),
            )

    def test_settings_are_validated_and_written_privately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = compat.configure(
                home,
                TOKEN,
                synthetic_home=True,
                shared_directories=["Downloads", "Documents"],
            )

            self.assertEqual(["Documents", "Downloads"], settings["sharedDirectories"])
            path = compat.app_state(home, TOKEN) / "compatibility.json"
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertEqual(settings, compat.load_settings(home, TOKEN))

    def test_rejects_unknown_shared_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "invalid directory"):
                compat.configure(
                    Path(directory),
                    TOKEN,
                    shared_directories=[".ssh"],
                )

    def test_can_clear_shared_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            compat.configure(home, TOKEN, shared_directories=["Documents"])
            settings = compat.configure(home, TOKEN, shared_directories=[])
            self.assertEqual([], settings["sharedDirectories"])

    def test_sandbox_hides_home_and_rebinds_private_and_public_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "Users/alice"
            runtime = root / "run/user/1000"
            (home / ".private").mkdir(parents=True)
            (home / "Documents").mkdir()
            runtime.mkdir(parents=True)
            settings = {
                "schema": 1,
                "syntheticHome": True,
                "sharedDirectories": ["Documents", "Downloads"],
            }

            command = compat.sandbox_command(
                home,
                TOKEN,
                ["example", "--flag"],
                settings,
                runtime_directory=runtime,
                bwrap="/bin/bwrap",
            )

            state = compat.app_state(home, TOKEN)
            self.assertEqual(["/bin/bwrap", "--bind", "/", "/"], command[:4])
            self.assertNotIn("--clearenv", command)
            self.assertIn(["--dev-bind", "/dev", "/dev"],
                          [command[index:index + 3] for index in range(len(command) - 2)])
            self.assertIn(
                ["--bind", str(state / "home"), str(home)],
                [command[index : index + 3] for index in range(len(command) - 2)],
            )
            self.assertIn(
                ["--bind", str(home / ".private"), str(home / ".private")],
                [command[index : index + 3] for index in range(len(command) - 2)],
            )
            self.assertIn(
                ["--bind", str(home / "Documents"), str(home / "Documents")],
                [command[index : index + 3] for index in range(len(command) - 2)],
            )
            self.assertNotIn(str(home / "Downloads"), command)
            self.assertEqual(["--", "example", "--flag"], command[-3:])

    def test_disabled_sandbox_returns_original_command(self) -> None:
        command = ["example", "--flag"]
        self.assertEqual(
            command,
            compat.sandbox_command(
                Path("/Users/alice"),
                TOKEN,
                command,
                compat.default_settings(),
                runtime_directory=Path("/run/user/1000"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
