from pathlib import Path
import tempfile
import unittest

import compat


TOKEN = "a" * 64


class CompatibilityTests(unittest.TestCase):
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
