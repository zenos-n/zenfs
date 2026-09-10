import json
from pathlib import Path
import tempfile
import unittest

from app_index import build_index
from app_registry import app_token
from zen_app_launch import resolve_launcher, resolve_path, resolve_token, validate_launcher


class AppLaunchTests(unittest.TestCase):
    def make_index(self, root: Path) -> tuple[Path, Path]:
        source = root / "source"
        target = root / "Apps"
        source.mkdir()
        (source / "editor.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Editor\nExec=true\n",
            encoding="utf-8",
        )
        build_index(source, target)
        return source, target

    def roots(self, source: Path) -> dict[str, tuple[Path, ...]]:
        return {
            "nix-config": (source,),
            "nix-imperative": (),
            "flatpak": (),
            "manual": (),
        }

    def test_accepts_recorded_launcher_with_valid_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.make_index(Path(directory))

            self.assertEqual(
                target / "Editor",
                validate_launcher(target / "Editor", target, self.roots(source)),
            )

    def test_rejects_launcher_outside_apps_views(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self.make_index(root)
            outside = root / "outside"
            outside.write_text(
                (target / "Editor").read_text(encoding="utf-8"), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "outside"):
                validate_launcher(outside, target, self.roots(source))

    def test_resolves_registered_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.make_index(Path(directory))

            self.assertEqual(
                target / "Editor",
                resolve_token(
                    app_token("nix-config", "editor.desktop"),
                    target,
                    self.roots(source),
                ),
            )

    def test_allows_ordinary_desktop_file_outside_apps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self.make_index(root)
            ordinary = root / "ordinary.desktop"
            ordinary.write_text(
                "[Desktop Entry]\nType=Application\nName=Ordinary\nExec=true\n",
                encoding="utf-8",
            )

            self.assertEqual(
                ordinary,
                resolve_launcher(ordinary, target, self.roots(source)),
            )

    def test_resolves_managed_launcher_from_user_apps_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, user_target = self.make_index(root)
            system_target = root / "SystemApps"
            system_target.mkdir()

            self.assertEqual(
                user_target / "Editor",
                resolve_path(
                    user_target / "Editor",
                    (system_target, user_target),
                    self.roots(source),
                ),
            )

    def test_never_falls_back_for_copied_managed_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self.make_index(root)
            outside = root / "copied.desktop"
            outside.write_text(
                (target / "Editor").read_text(encoding="utf-8"), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "managed launcher metadata"):
                resolve_launcher(outside, target, self.roots(source))

    def test_rejects_symlink_launcher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.make_index(Path(directory))
            launcher = target / "Editor"
            original = target / "original"
            launcher.rename(original)
            launcher.symlink_to(original)

            with self.assertRaises(OSError):
                validate_launcher(launcher, target, self.roots(source))

    def test_rejects_unrecorded_and_source_spoofed_launchers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = self.make_index(root)
            launcher = target / "Editor"
            contents = launcher.read_text(encoding="utf-8")
            launcher.write_text(
                contents.replace(
                    f"X-ZenOS-SourcePath={source / 'editor.desktop'}",
                    f"X-ZenOS-SourcePath={root / 'outside/editor.desktop'}",
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "outside its classified source"):
                validate_launcher(launcher, target, self.roots(source))

            (target / ".zenos-app-index.json").write_text(
                json.dumps({"schema": 3, "applications": []}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "not recorded"):
                validate_launcher(launcher, target, self.roots(source))

    def test_rejects_launcher_missing_from_token_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source, target = self.make_index(Path(directory))
            (target / ".zenos-app-registry.json").write_text(
                json.dumps({"schema": 1, "applications": []}), encoding="utf-8"
            )

            with self.assertRaisesRegex(ValueError, "app registry"):
                validate_launcher(target / "Editor", target, self.roots(source))


if __name__ == "__main__":
    unittest.main()
