from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from app_index import AppSource, build_index, build_source_views, desktop_entry
import flatpak


class AppIntegrationTests(unittest.TestCase):
    def test_index_finds_package_icon(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "package/share/applications"
            source.mkdir(parents=True)
            icon = root / "package/share/icons/hicolor/scalable/apps/editor.svg"
            icon.parent.mkdir(parents=True)
            icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            (source / "editor.desktop").write_text("[Desktop Entry]\nType=Application\nName=Editor\nExec=true\nIcon=editor\n")
            build_index(source, root / "Apps")
            self.assertEqual(str(icon), desktop_entry(root / "Apps/Editor")["X-ZenOS-IconFile"])

    def test_appimage_package_is_discovered_under_private_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            source = home / ".private/Packages/applications"
            source.mkdir(parents=True)
            package = home / ".private/Packages/zenos/appimages/editor"
            package.mkdir(parents=True)
            installed = package / "editor.AppImage"
            installed.write_bytes(b"fixture")
            (source / "com.negzero.zenos.appimage.editor.desktop").write_text("[Desktop Entry]\nType=Application\nName=Editor\nExec=true\n")
            target = home / ".private/Apps"
            build_source_views(home, target, sources=(AppSource("manual", "Manually Installed", (source,)),), scope="user")
            metadata = desktop_entry(target / "Editor")
            self.assertEqual(str(package), metadata["X-ZenOS-Package"])
            self.assertEqual(str(installed), metadata["X-ZenOS-AppImageFile"])

    def test_unowned_app_collision_preserves_data_and_other_launchers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "Apps"
            source.mkdir()
            (source / "editor.desktop").write_text("[Desktop Entry]\nType=Application\nName=Editor\nExec=true\n")
            build_index(source, target)
            (source / "notes.desktop").write_text("[Desktop Entry]\nType=Application\nName=Notes\nExec=true\n")
            (target / "Notes").write_text("personal notes")
            with self.assertRaisesRegex(ValueError, "unowned application"):
                build_index(source, target)
            self.assertEqual("personal notes", (target / "Notes").read_text())
            self.assertTrue((target / "Editor").is_file())

    def test_private_flatpak_environment_does_not_depend_on_login_shell(self):
        run = Mock()
        with patch.dict("os.environ", {"XDG_DATA_HOME": "/wrong"}), patch.object(flatpak, "refresh_index"):
            flatpak.install("org.example.Editor", home=Path("/Users/alice"), run=run)
        self.assertEqual("/Users/alice/.private/Packages", run.call_args.kwargs["env"]["XDG_DATA_HOME"])


if __name__ == "__main__":
    unittest.main()
