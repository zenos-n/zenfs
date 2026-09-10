from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import flatpak


class FlatpakBackendTests(unittest.TestCase):
    def test_refresh_builds_only_the_user_view(self) -> None:
        home = Path("/Users/alice")
        target = home / ".private/Apps"
        with mock.patch.object(flatpak, "build_source_views") as build:
            flatpak.refresh_index(home, target, "alice")

        build.assert_called_once_with(home, target, "alice", scope="user")

    def test_install_is_user_scoped_and_refreshes_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            target = home / ".private/Apps"
            home.mkdir()
            run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
            with mock.patch.object(flatpak, "refresh_index") as refresh:
                flatpak.install(
                    "org.example.Editor",
                    home=home,
                    target=target,
                    user="alice",
                    run=run,
                )

            run.assert_called_once_with(
                [
                    "flatpak",
                    "install",
                    "--user",
                    "--noninteractive",
                    "zenos-flathub",
                    "org.example.Editor",
                ],
                check=True,
                text=True,
                env=flatpak.private_environment(home),
            )
            refresh.assert_called_once_with(home, target, "alice")

    def test_remove_is_user_scoped_and_refreshes_index(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with mock.patch.object(flatpak, "refresh_index") as refresh:
            flatpak.remove("org.example.Editor", run=run)

        self.assertEqual("uninstall", run.call_args.args[0][1])
        self.assertIn("--user", run.call_args.args[0])
        refresh.assert_called_once()

    def test_install_uses_configured_default_remote(self) -> None:
        run = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with (
            mock.patch.dict(
                "os.environ", {"ZENOS_FLATPAK_REMOTE": "corporate-apps"}
            ),
            mock.patch.object(flatpak, "refresh_index"),
        ):
            flatpak.install("org.example.Editor", run=run)

        self.assertIn("corporate-apps", run.call_args.args[0])

    def test_lists_structured_user_applications(self) -> None:
        run = mock.Mock(
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=(
                    "org.example.Editor\tapp/org.example.Editor/x86_64/stable\tflathub\n"
                ),
            )
        )

        self.assertEqual(
            [
                {
                    "application": "org.example.Editor",
                    "ref": "app/org.example.Editor/x86_64/stable",
                    "origin": "flathub",
                    "scope": "user",
                    "removable": True,
                }
            ],
            flatpak.list_installed(run),
        )

    def test_lists_both_scopes_for_inspection(self) -> None:
        run = mock.Mock(
            side_effect=[
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=(
                        "org.example.Editor\tapp/org.example.Editor/x86_64/stable\tflathub\n"
                    ),
                ),
                subprocess.CompletedProcess(
                    [],
                    0,
                    stdout=(
                        "org.example.Editor\tapp/org.example.Editor/x86_64/stable\tflathub\n"
                    ),
                ),
            ]
        )

        self.assertEqual(
            {
                "application": "org.example.Editor",
                "installations": [
                    {
                        "application": "org.example.Editor",
                        "ref": "app/org.example.Editor/x86_64/stable",
                        "origin": "flathub",
                        "scope": "user",
                        "removable": True,
                    },
                    {
                        "application": "org.example.Editor",
                        "ref": "app/org.example.Editor/x86_64/stable",
                        "origin": "flathub",
                        "scope": "system",
                        "removable": False,
                    },
                ],
            },
            flatpak.inspect("org.example.Editor", run),
        )
        self.assertIn("--user", run.call_args_list[0].args[0])
        self.assertIn("--system", run.call_args_list[1].args[0])

    def test_rejects_unsafe_identifiers_before_subprocess(self) -> None:
        run = mock.Mock()
        with self.assertRaisesRegex(ValueError, "invalid Flatpak"):
            flatpak.install("org.example.Editor;rm", run=run)
        run.assert_not_called()
