from pathlib import Path
import tempfile
import unittest

from app_index import build_index
from zen_app_icons import icon_updates
from zenos_icon_metadata import icon_metadata


class AppIconTests(unittest.TestCase):
    def test_installed_svg_uses_theme_instead_of_file_decoder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "package/share/applications"
            source.mkdir(parents=True)
            icon = root / "package/share/icons/hicolor/scalable/apps/editor.svg"
            icon.parent.mkdir(parents=True)
            icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            (source / "editor.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Editor\nExec=true\nIcon=editor\n"
            )
            build_index(source, root / "Apps")
            self.assertEqual(
                [(root / "Apps/Editor", "metadata::custom-icon-name", "editor")],
                icon_updates(root / "Apps"),
            )

    def test_standalone_icon_and_missing_theme_use_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            icon = Path(directory) / "icon.svg"
            icon.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
            expected = ("metadata::custom-icon", icon.as_uri())
            self.assertEqual(expected, icon_metadata(str(icon), str(icon)))
            self.assertEqual(expected, icon_metadata("editor", str(icon), lambda _: False))
            self.assertIsNone(icon_metadata("../icon", None))

    def test_derives_icon_metadata_from_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "Apps"
            source.mkdir()
            (source / "editor.desktop").write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Editor\n"
                "Exec=true\n"
                "Icon=editor\n",
                encoding="utf-8",
            )

            build_index(source, target)

            self.assertEqual(
                [(target / "Editor", "metadata::custom-icon-name", "editor")],
                icon_updates(target),
            )

    def test_rejects_registry_token_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "Apps"
            source.mkdir()
            (source / "editor.desktop").write_text(
                "[Desktop Entry]\nType=Application\nName=Editor\nExec=true\n",
                encoding="utf-8",
            )
            build_index(source, target)
            launcher = target / "Editor"
            launcher.write_text(
                launcher.read_text(encoding="utf-8").replace(
                    "X-ZenOS-AppToken=", "X-ZenOS-AppToken=0"
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "does not match"):
                icon_updates(target)
