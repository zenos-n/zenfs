from pathlib import Path
import tempfile
import unittest

from app_index import build_index
from zen_app_icons import icon_updates


class AppIconTests(unittest.TestCase):
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
