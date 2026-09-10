import configparser
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import appimage
from appimage import (
    AppImageSnapshot,
    apply_resource_limits,
    install,
    list_installed,
    parse_pseudo_listing,
    register_appimage,
    remove,
    safe_id,
    sandbox_command,
    select_desktop_entry,
    select_squashfs_archive,
    snapshot_appimage,
    validate_appimage,
    validate_id,
)


STAT_OUTPUT = b"""Found a valid SQUASHFS 4:0 superblock on /source.AppImage.
Filesystem size 64 bytes (0.06 Kbytes / 0.00 Mbytes)
Block size 131072
Number of inodes 4
"""


class FakeUnsquashfs:
    def __init__(self, valid_offsets: set[int], mutate=None) -> None:
        self.valid_offsets = valid_offsets
        self.mutate = mutate
        self.commands: list[tuple[list[str], bool]] = []
        self.called = False
        self.desktop = (
            b"[Desktop Entry]\n"
            b"Type=Application\n"
            b"Name=Editor\n"
            b"Exec=AppRun %U\n"
            b"Icon=editor\n"
        )
        self.icon = b"icon"

    def listing(self) -> bytes:
        return (
            b"/ D 0 755 0 0\n"
            + f"editor.desktop R 0 644 0 0 {len(self.desktop)} 0 0\n".encode()
            + f"editor.png R 0 644 0 0 {len(self.icon)} 0 0\n".encode()
        )

    def __call__(
        self,
        _snapshot: Path,
        output: Path,
        arguments: list[str],
        pseudo_listing: bool,
    ) -> bytes:
        self.commands.append((arguments, pseudo_listing))
        if not self.called and self.mutate is not None:
            self.called = True
            self.mutate()
        offset = int(arguments[arguments.index("-offset") + 1])
        if offset not in self.valid_offsets:
            raise ValueError("invalid candidate")
        if "-stat" in arguments:
            return STAT_OUTPUT
        if "-pf" in arguments:
            return self.listing()
        archive_path = arguments[-1]
        destination = output / archive_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        if archive_path == "editor.desktop":
            destination.write_bytes(self.desktop)
        elif archive_path == "editor.png":
            destination.write_bytes(self.icon)
        else:
            raise AssertionError(f"unexpected extraction path: {archive_path}")
        return b""


class AppImageTests(unittest.TestCase):
    def test_defaults_refresh_to_private_apps(self) -> None:
        home = Path("/Users/alice")
        self.assertEqual(
            home / ".private/Apps", appimage.default_apps_dir(home, {})
        )
        self.assertEqual(
            Path("/tmp/custom-apps"),
            appimage.default_apps_dir(
                home, {"ZENOS_APPS_DIR": "/tmp/custom-apps"}
            ),
        )

    def write_type2(self, path: Path, offsets: tuple[int, ...] = (64,)) -> bytes:
        payload = bytearray(b"\x7fELF\x00\x00\x00\x00AI\x02")
        payload.extend(b"\x00" * (256 - len(payload)))
        for offset in offsets:
            payload[offset : offset + 4] = b"hsqs"
        contents = bytes(payload)
        path.write_bytes(contents)
        return contents

    def make_snapshot(
        self, root: Path, offsets: tuple[int, ...] = (64,)
    ) -> AppImageSnapshot:
        source = root / "Editor.AppImage"
        self.write_type2(source, offsets)
        snapshot_dir = root / "snapshot"
        snapshot_dir.mkdir()
        return snapshot_appimage(source, snapshot_dir)

    def test_safe_ids(self) -> None:
        self.assertEqual("my-editor-x86-64", safe_id("My Editor_x86_64.AppImage"))
        self.assertEqual("org.example-app", validate_id("org.example-app"))
        for value in ("", "../escape", "Uppercase", "two words"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_id(value)

    def test_validates_appimage_magic_and_rejects_type1(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.AppImage"
            self.write_type2(valid)
            self.assertEqual(valid, validate_appimage(valid))
            type1 = root / "legacy.AppImage"
            type1.write_bytes(b"\x7fELF\x00\x00\x00\x00AI\x01payload")
            with self.assertRaisesRegex(ValueError, "type 2"):
                validate_appimage(type1)

    def test_snapshots_exact_validated_payload_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "editor.AppImage"
            payload = self.write_type2(image)
            snapshot_dir = root / "snapshot"
            snapshot_dir.mkdir()

            snapshot = snapshot_appimage(image, snapshot_dir)
            image.write_bytes(b"changed after snapshot")

            self.assertEqual(payload, snapshot.path.read_bytes())
            self.assertEqual(hashlib.sha256(payload).hexdigest(), snapshot.sha256)
            self.assertEqual(len(payload), snapshot.size)

    def test_rejects_payload_race_during_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "editor.AppImage"
            self.write_type2(image)
            with image.open("ab") as appimage_file:
                appimage_file.write(b"a" * (2 * 1024 * 1024))
            snapshot_dir = root / "snapshot"
            snapshot_dir.mkdir()

            def mutate() -> None:
                with image.open("r+b") as appimage_file:
                    appimage_file.seek(-1, os.SEEK_END)
                    appimage_file.write(b"b")

            with self.assertRaisesRegex(ValueError, "changed while"):
                snapshot_appimage(image, snapshot_dir, mutate)

    def test_selects_one_valid_offset_and_rejects_missing_or_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self.make_snapshot(root, (64, 128))
            runner = FakeUnsquashfs({64})
            archive = select_squashfs_archive(snapshot, root, runner)
            self.assertEqual(64, archive.offset)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self.make_snapshot(root, (64, 128))
            with self.assertRaisesRegex(ValueError, "ambiguous"):
                select_squashfs_archive(snapshot, root, FakeUnsquashfs({64, 128}))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = self.make_snapshot(root)
            with self.assertRaisesRegex(ValueError, "no valid"):
                select_squashfs_archive(snapshot, root, FakeUnsquashfs(set()))

    def test_sandbox_command_has_required_isolation(self) -> None:
        command = sandbox_command(
            Path("/private/source.AppImage"),
            Path("/private/output"),
            ["-stat", "/source.AppImage"],
            "/bin/bwrap",
            "/bin/unsquashfs",
        )
        self.assertIn("--unshare-all", command)
        self.assertIn("--unshare-user", command)
        self.assertNotIn("--share-net", command)
        self.assertIn("--disable-userns", command)
        self.assertIn("--die-with-parent", command)
        self.assertIn("--new-session", command)
        self.assertIn("--clearenv", command)
        self.assertIn("/nix/store", command)
        source_bind = command.index("/private/source.AppImage")
        self.assertEqual("--ro-bind", command[source_bind - 1])
        self.assertEqual("/source.AppImage", command[source_bind + 1])
        self.assertIn("--tmpfs", command)

    def test_applies_all_resource_limits(self) -> None:
        with mock.patch("appimage.resource.setrlimit") as set_limit:
            apply_resource_limits()
        resources = {call.args[0] for call in set_limit.call_args_list}
        self.assertEqual(
            {
                appimage.resource.RLIMIT_CPU,
                appimage.resource.RLIMIT_AS,
                appimage.resource.RLIMIT_FSIZE,
                appimage.resource.RLIMIT_NPROC,
                appimage.resource.RLIMIT_NOFILE,
                appimage.resource.RLIMIT_CORE,
            },
            resources,
        )

    def test_rejects_traversal_special_files_and_metadata_hardlinks(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsafe SquashFS path"):
            parse_pseudo_listing(b"/ D 0 755 0 0\n../evil.desktop R 0 644 0 0 1 0 0\n")
        with self.assertRaisesRegex(ValueError, "escapes the archive"):
            parse_pseudo_listing(b"/ D 0 755 0 0\nlink S 0 777 0 0 ../../etc/passwd\n")
        with self.assertRaisesRegex(ValueError, "device or unsupported"):
            parse_pseudo_listing(b"/ D 0 755 0 0\ndev C 0 600 0 0 1 3\n")
        entries = parse_pseudo_listing(
            b"/ D 0 755 0 0\n"
            b"editor.desktop R 0 644 0 0 10 0 0\n"
            b"alias L editor.desktop\n"
        )
        archive = appimage.SquashfsArchive(64, entries)
        with self.assertRaisesRegex(ValueError, "hardlink alias"):
            select_desktop_entry(archive)

    def test_rejects_decompression_and_metadata_size_limits(self) -> None:
        oversized = appimage.MAX_ARCHIVE_BYTES + 1
        with self.assertRaisesRegex(ValueError, "uncompressed size"):
            parse_pseudo_listing(
                f"/ D 0 755 0 0\nlarge R 0 644 0 0 {oversized} 0 0\n".encode()
            )
        entries = parse_pseudo_listing(
            f"/ D 0 755 0 0\neditor.desktop R 0 644 0 0 "
            f"{appimage.MAX_DESKTOP_BYTES + 1} 0 0\n".encode()
        )
        with self.assertRaisesRegex(ValueError, "too large"):
            select_desktop_entry(appimage.SquashfsArchive(64, entries))

    def test_successful_install_uses_exact_snapshot_and_registers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            image = root / "Editor.AppImage"
            original = self.write_type2(image)

            def mutate_original() -> None:
                image.write_bytes(b"changed after validated snapshot")

            runner = FakeUnsquashfs({64}, mutate_original)
            identifier = install(image, home, runner=runner)

            self.assertEqual("editor", identifier)
            installed = home / ".private/Packages/zenos/appimages/editor/editor.AppImage"
            self.assertEqual(original, installed.read_bytes())
            manifest = json.loads(
                (installed.parent / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(hashlib.sha256(original).hexdigest(), manifest["sha256"])
            registration = (
                home
                / ".private/Packages/applications/com.negzero.zenos.appimage.editor.desktop"
            )
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(registration, encoding="utf-8")
            self.assertEqual(
                "managed-appimage", parser["Desktop Entry"]["X-ZenOS-Origin"]
            )
            self.assertEqual([{"id": "editor", "name": "Editor"}], list_installed(home))
            for arguments, _pseudo_listing in runner.commands:
                self.assertIn("-processors", arguments)
                self.assertEqual("1", arguments[arguments.index("-processors") + 1])
                self.assertIn("-mem", arguments)
                self.assertEqual("64M", arguments[arguments.index("-mem") + 1])

            remove("editor", home)
            self.assertFalse(registration.exists())

    def test_rejects_malformed_desktop_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "Editor.AppImage"
            self.write_type2(image)
            runner = FakeUnsquashfs({64})
            runner.desktop = b"not a desktop entry"

            with self.assertRaises((configparser.Error, KeyError, ValueError)):
                install(image, root / "home", runner=runner)

    def test_registration_strips_desktop_actions_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            source = root / "Editor.AppImage"
            source.write_bytes(b"test image")
            desktop = root / "editor.desktop"
            desktop.write_text(
                "[Desktop Entry]\n"
                "Type=Application\n"
                "Name=Editor\n"
                "Exec=AppRun %U\n"
                "Actions=Dangerous;\n"
                "\n[Desktop Action Dangerous]\n"
                "Name=Dangerous\n"
                "Exec=run-untrusted-script\n",
                encoding="utf-8",
            )

            register_appimage(source, desktop, None, home, "editor")

            registration = (
                home
                / ".private/Packages/applications/com.negzero.zenos.appimage.editor.desktop"
            )
            parser = configparser.ConfigParser(interpolation=None)
            parser.optionxform = str
            parser.read(registration, encoding="utf-8")
            self.assertNotIn("Actions", parser["Desktop Entry"])
            self.assertNotIn("Desktop Action Dangerous", parser)


if __name__ == "__main__":
    unittest.main()
