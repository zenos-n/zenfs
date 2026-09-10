#!/usr/bin/env python3
"""Install and manage per-user AppImages registered with ZenOS."""

from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import selectors
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from typing import Callable, Mapping

from app_index import build_source_views


APPIMAGE_TYPE2_MAGIC = b"AI\x02"
SQUASHFS_MAGIC = b"hsqs"
PSEUDO_DATA_MARKER = b"#\n# START OF DATA - DO NOT MODIFY\n#\n"
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
ICON_EXTENSIONS = (".png", ".svg", ".xpm")
COPY_CHUNK_SIZE = 1024 * 1024
MAX_APPIMAGE_BYTES = 16 * 1024 * 1024 * 1024
MAX_MAGIC_CANDIDATES = 64
MAX_ARCHIVE_ENTRIES = 100000
MAX_ARCHIVE_BYTES = 4 * 1024 * 1024 * 1024
MAX_LISTING_BYTES = 16 * 1024 * 1024
MAX_DESKTOP_BYTES = 1024 * 1024
MAX_ICON_BYTES = 16 * 1024 * 1024
SANDBOX_TIMEOUT_SECONDS = 15
LIMIT_CPU_SECONDS = 10
LIMIT_ADDRESS_SPACE = 512 * 1024 * 1024
LIMIT_FILE_SIZE = 32 * 1024 * 1024
LIMIT_PROCESSES = 256
LIMIT_DESCRIPTORS = 64


@dataclass(frozen=True)
class AppImageSnapshot:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    kind: str
    size: int | None = None
    target: str | None = None


@dataclass(frozen=True)
class SquashfsArchive:
    offset: int
    entries: dict[str, ArchiveEntry]


SandboxRunner = Callable[[Path, Path, list[str], bool], bytes]


class SandboxUnavailable(ValueError):
    pass


def safe_id(value: str) -> str:
    value = re.sub(r"(?i)\.appimage$", "", value)
    identifier = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not identifier or not ID_PATTERN.fullmatch(identifier):
        raise ValueError(f"cannot derive a safe AppImage ID from {value!r}")
    return identifier[:80].rstrip("-")


def validate_id(identifier: str) -> str:
    if not ID_PATTERN.fullmatch(identifier) or len(identifier) > 80:
        raise ValueError(f"invalid AppImage ID: {identifier!r}")
    return identifier


def validate_appimage(path: Path) -> Path:
    source = Path(os.path.abspath(path.expanduser()))
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("AppImage must be a non-symlink regular file") from error
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > MAX_APPIMAGE_BYTES:
            raise ValueError("AppImage must be a bounded regular file")
        header = os.read(descriptor, 11)
    finally:
        os.close(descriptor)
    if (
        len(header) < 11
        or header[:4] != b"\x7fELF"
        or header[8:11] != APPIMAGE_TYPE2_MAGIC
    ):
        raise ValueError("file is not a type 2 AppImage")
    return source


def snapshot_appimage(
    path: Path,
    temporary: Path,
    chunk_hook: Callable[[], None] | None = None,
) -> AppImageSnapshot:
    """Copy and validate one immutable candidate without reopening the source."""
    source = Path(os.path.abspath(path.expanduser()))
    try:
        source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ValueError("AppImage must be a non-symlink regular file") from error
    destination = temporary / "source.AppImage"
    destination_fd = -1
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_APPIMAGE_BYTES:
            raise ValueError("AppImage must be a bounded regular file")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        digest = hashlib.sha256()
        header = bytearray()
        copied = 0
        hooked = False
        while True:
            chunk = os.read(source_fd, COPY_CHUNK_SIZE)
            if not chunk:
                break
            if len(header) < 11:
                header.extend(chunk[: 11 - len(header)])
                if len(header) >= 11 and (
                    bytes(header[:4]) != b"\x7fELF"
                    or bytes(header[8:11]) != APPIMAGE_TYPE2_MAGIC
                ):
                    raise ValueError("file is not a type 2 AppImage")
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
            copied += len(chunk)
            if chunk_hook is not None and not hooked:
                hooked = True
                chunk_hook()
        os.fsync(destination_fd)
        after = os.fstat(source_fd)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or copied != before.st_size:
            raise ValueError("AppImage changed while it was being copied")
        if len(header) < 11:
            raise ValueError("file is not a type 2 AppImage")
        return AppImageSnapshot(destination, digest.hexdigest(), copied)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def scan_squashfs_offsets(snapshot: AppImageSnapshot) -> list[int]:
    offsets: list[int] = []
    with snapshot.path.open("rb") as image:
        absolute = 0
        overlap = b""
        while True:
            chunk = image.read(COPY_CHUNK_SIZE)
            if not chunk:
                break
            data = overlap + chunk
            search_at = 0
            while True:
                found = data.find(SQUASHFS_MAGIC, search_at)
                if found < 0:
                    break
                offset = absolute - len(overlap) + found
                if offset >= 11:
                    offsets.append(offset)
                    if len(offsets) > MAX_MAGIC_CANDIDATES:
                        raise ValueError(
                            "AppImage has too many SquashFS magic candidates"
                        )
                search_at = found + 1
            overlap = data[-(len(SQUASHFS_MAGIC) - 1) :]
            absolute += len(chunk)
    return offsets


def sandbox_command(
    snapshot: Path,
    output: Path,
    unsquashfs_arguments: list[str],
    bwrap_path: str = "bwrap",
    unsquashfs_path: str = "unsquashfs",
) -> list[str]:
    return [
        bwrap_path,
        "--unshare-all",
        "--unshare-user",
        "--disable-userns",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "LANG",
        "C",
        "--ro-bind",
        "/nix/store",
        "/nix/store",
        "--ro-bind",
        str(snapshot),
        "/source.AppImage",
        "--bind",
        str(output),
        "/output",
        "--size",
        str(32 * 1024 * 1024),
        "--tmpfs",
        "/tmp",
        "--dev",
        "/dev",
        "--chdir",
        "/output",
        "--",
        unsquashfs_path,
        *unsquashfs_arguments,
    ]


def apply_resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (LIMIT_CPU_SECONDS, LIMIT_CPU_SECONDS))
    resource.setrlimit(resource.RLIMIT_AS, (LIMIT_ADDRESS_SPACE, LIMIT_ADDRESS_SPACE))
    resource.setrlimit(resource.RLIMIT_FSIZE, (LIMIT_FILE_SIZE, LIMIT_FILE_SIZE))
    resource.setrlimit(resource.RLIMIT_NPROC, (LIMIT_PROCESSES, LIMIT_PROCESSES))
    resource.setrlimit(resource.RLIMIT_NOFILE, (LIMIT_DESCRIPTORS, LIMIT_DESCRIPTORS))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_unsquashfs_sandbox(
    snapshot: Path,
    output: Path,
    arguments: list[str],
    pseudo_listing: bool = False,
) -> bytes:
    bwrap = shutil.which("bwrap")
    unsquashfs = shutil.which("unsquashfs")
    if bwrap is None or unsquashfs is None:
        raise SandboxUnavailable(
            "bubblewrap and unsquashfs are required for AppImage installation"
        )
    command = sandbox_command(snapshot, output, arguments, bwrap, unsquashfs)
    with tempfile.TemporaryFile() as stderr:
        stdout: int | object = (
            subprocess.PIPE if pseudo_listing else tempfile.TemporaryFile()
        )
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            close_fds=True,
            start_new_session=True,
            preexec_fn=apply_resource_limits,
        )
        if pseudo_listing:
            assert process.stdout is not None
            output_bytes = _read_pseudo_listing(process)
            return_code = process.returncode
        else:
            try:
                return_code = process.wait(timeout=SANDBOX_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                process.kill()
                process.wait()
                raise ValueError("sandboxed unsquashfs timed out") from error
            assert not isinstance(stdout, int)
            stdout.seek(0)
            output_bytes = stdout.read(MAX_LISTING_BYTES + 1)
            stdout.close()
        stderr.seek(0)
        error_bytes = stderr.read(4096)
    if len(output_bytes) > MAX_LISTING_BYTES:
        raise ValueError("sandboxed unsquashfs output exceeded its limit")
    if pseudo_listing:
        return output_bytes
    if return_code != 0:
        detail = error_bytes.decode("utf-8", "replace").strip().splitlines()
        message = detail[-1] if detail else f"exit status {return_code}"
        if message.startswith("bwrap:"):
            raise SandboxUnavailable(f"AppImage sandbox is unavailable: {message}")
        raise ValueError(f"sandboxed unsquashfs failed: {message}")
    return output_bytes


def _read_pseudo_listing(process: subprocess.Popen[bytes]) -> bytes:
    assert process.stdout is not None
    descriptor = process.stdout.fileno()
    deadline = time.monotonic() + SANDBOX_TIMEOUT_SECONDS
    listing = bytearray()
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("sandboxed unsquashfs timed out")
            if not selector.select(remaining):
                raise ValueError("sandboxed unsquashfs timed out")
            chunk = os.read(descriptor, 65536)
            if not chunk:
                raise ValueError(
                    "unsquashfs did not produce a complete archive listing"
                )
            listing.extend(chunk)
            marker = listing.find(PSEUDO_DATA_MARKER)
            if marker >= 0:
                return bytes(listing[:marker])
            if len(listing) > MAX_LISTING_BYTES:
                raise ValueError("sandboxed unsquashfs listing exceeded its limit")
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()


def _safe_archive_path(value: str) -> str:
    path = PurePosixPath(value)
    if value == "/":
        return value
    if path.is_absolute() or not value or len(value.encode("utf-8")) > 4096:
        raise ValueError(f"unsafe SquashFS path: {value!r}")
    if any(
        component in {"", ".", ".."}
        or len(component.encode("utf-8")) > 255
        or "\x00" in component
        for component in path.parts
    ):
        raise ValueError(f"unsafe SquashFS path: {value!r}")
    return value


def _safe_symlink_target(link_path: str, target: str) -> str:
    target_path = PurePosixPath(target)
    if target_path.is_absolute() or not target or "\x00" in target:
        raise ValueError("SquashFS contains an unsafe symlink target")
    components = list(PurePosixPath(link_path).parent.parts)
    for component in target_path.parts:
        if component in {"", "."}:
            continue
        if component == "..":
            if not components:
                raise ValueError("SquashFS symlink target escapes the archive")
            components.pop()
            continue
        if len(component.encode("utf-8")) > 255:
            raise ValueError("SquashFS contains an unsafe symlink target")
        components.append(component)
    if not components:
        raise ValueError("SquashFS symlink target resolves to the archive root")
    return str(PurePosixPath(*components))


def parse_pseudo_listing(listing: bytes) -> dict[str, ArchiveEntry]:
    entries: dict[str, ArchiveEntry] = {}
    total_size = 0
    try:
        text = listing.decode("utf-8")
    except UnicodeError as error:
        raise ValueError("SquashFS listing is not UTF-8") from error
    for raw_line in text.splitlines():
        if not raw_line or raw_line.startswith("#"):
            continue
        try:
            fields = shlex.split(raw_line, posix=True)
        except ValueError as error:
            raise ValueError("malformed SquashFS pseudo-file listing") from error
        if len(fields) < 2:
            raise ValueError("malformed SquashFS pseudo-file listing")
        path = _safe_archive_path(fields[0])
        kind = fields[1]
        if path in entries:
            raise ValueError(f"duplicate SquashFS path: {path}")
        size = None
        target = None
        if kind == "R":
            if len(fields) != 9:
                raise ValueError("malformed SquashFS regular-file entry")
            try:
                size = int(fields[6])
            except ValueError as error:
                raise ValueError("invalid SquashFS regular-file size") from error
            if size < 0:
                raise ValueError("invalid SquashFS regular-file size")
            total_size += size
            if total_size > MAX_ARCHIVE_BYTES:
                raise ValueError("SquashFS uncompressed size exceeds its limit")
        elif kind == "D":
            if len(fields) != 6:
                raise ValueError("malformed SquashFS directory entry")
        elif kind in {"L", "S"}:
            expected_fields = 3 if kind == "L" else 7
            if len(fields) != expected_fields:
                raise ValueError("malformed SquashFS link entry")
            target = fields[-1]
            target = (
                _safe_archive_path(target)
                if kind == "L"
                else _safe_symlink_target(path, target)
            )
        else:
            raise ValueError("SquashFS contains a device or unsupported special file")
        entries[path] = ArchiveEntry(path, kind, size, target)
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise ValueError("SquashFS entry count exceeds its limit")
    if entries.get("/") != ArchiveEntry("/", "D", None, None):
        raise ValueError("SquashFS listing has no valid root directory")
    return entries


def _validate_superblock(
    stat_output: bytes, snapshot: AppImageSnapshot, offset: int
) -> None:
    text = stat_output.decode("utf-8", "replace")
    if "Found a valid SQUASHFS 4:0 superblock" not in text:
        raise ValueError("candidate is not a SquashFS 4.0 filesystem")
    size_match = re.search(r"^Filesystem size (\d+) bytes", text, re.MULTILINE)
    inode_match = re.search(r"^Number of inodes (\d+)$", text, re.MULTILINE)
    if size_match is None or inode_match is None:
        raise ValueError("SquashFS stat output is incomplete")
    filesystem_size = int(size_match.group(1))
    inode_count = int(inode_match.group(1))
    if filesystem_size <= 0 or offset + filesystem_size > snapshot.size:
        raise ValueError("SquashFS filesystem extends beyond the AppImage snapshot")
    if inode_count <= 0 or inode_count > MAX_ARCHIVE_ENTRIES:
        raise ValueError("SquashFS inode count exceeds its limit")


def select_squashfs_archive(
    snapshot: AppImageSnapshot,
    temporary: Path,
    runner: SandboxRunner = run_unsquashfs_sandbox,
) -> SquashfsArchive:
    valid: list[SquashfsArchive] = []
    candidates = scan_squashfs_offsets(snapshot)
    for offset in candidates:
        validation_output = temporary / f"validate-{offset}"
        validation_output.mkdir(mode=0o700)
        try:
            stat_output = runner(
                snapshot.path,
                validation_output,
                [
                    "-stat",
                    "-offset",
                    str(offset),
                    "-processors",
                    "1",
                    "-mem",
                    "64M",
                    "/source.AppImage",
                ],
                False,
            )
            _validate_superblock(stat_output, snapshot, offset)
            listing = runner(
                snapshot.path,
                validation_output,
                [
                    "-pf",
                    "-",
                    "-no-xattrs",
                    "-offset",
                    str(offset),
                    "-processors",
                    "1",
                    "-mem",
                    "64M",
                    "/source.AppImage",
                ],
                True,
            )
            valid.append(SquashfsArchive(offset, parse_pseudo_listing(listing)))
        except SandboxUnavailable:
            raise
        except (OSError, ValueError):
            continue
    if not valid:
        raise ValueError("AppImage has no valid SquashFS payload offset")
    if len(valid) != 1:
        raise ValueError("AppImage has ambiguous SquashFS payload offsets")
    return valid[0]


def _validate_selected_entry(
    archive: SquashfsArchive,
    path: str,
    maximum_size: int,
) -> ArchiveEntry:
    entry = archive.entries.get(path)
    if entry is None:
        raise ValueError(f"SquashFS metadata path is missing: {path}")
    if entry.kind != "R" or entry.size is None:
        raise ValueError(f"SquashFS metadata path is not a regular file: {path}")
    if any(
        candidate.kind == "L" and candidate.target == path
        for candidate in archive.entries.values()
    ):
        raise ValueError(f"SquashFS metadata path has a hardlink alias: {path}")
    if entry.size > maximum_size:
        raise ValueError(f"SquashFS metadata path is too large: {path}")
    parent = PurePosixPath(path).parent
    while str(parent) != ".":
        parent_name = "/" if str(parent) == "." else str(parent)
        parent_entry = archive.entries.get(parent_name)
        if parent_entry is None or parent_entry.kind != "D":
            raise ValueError(
                f"SquashFS metadata ancestor is not a directory: {parent_name}"
            )
        if parent_name == "/":
            break
        parent = parent.parent
    return entry


def select_desktop_entry(archive: SquashfsArchive) -> ArchiveEntry:
    root_candidates = [
        entry
        for entry in archive.entries.values()
        if "/" not in entry.path and entry.path.endswith(".desktop")
    ]
    share_candidates = [
        entry
        for entry in archive.entries.values()
        if entry.path.startswith("usr/share/applications/")
        and "/" not in entry.path.removeprefix("usr/share/applications/")
        and entry.path.endswith(".desktop")
    ]
    candidates = root_candidates or share_candidates
    if len(candidates) != 1:
        raise ValueError("AppImage must contain exactly one unambiguous desktop entry")
    return _validate_selected_entry(archive, candidates[0].path, MAX_DESKTOP_BYTES)


def _icon_score(path: str) -> tuple[int, int, str]:
    if path.endswith(".svg"):
        return (3, 0, path)
    size = 0
    match = re.search(r"/(\d+)x(\d+)/", path)
    if match:
        size = min(int(match.group(1)), int(match.group(2)))
    return (2 if "/usr/share/icons/" in f"/{path}" else 1, size, path)


def select_icon_entry(
    archive: SquashfsArchive, icon_value: str | None
) -> ArchiveEntry | None:
    if not icon_value:
        return None
    if not _safe_icon_name(icon_value):
        raise ValueError("AppImage desktop entry has an unsafe icon name")
    names = {icon_value}
    if PurePosixPath(icon_value).suffix.lower() not in ICON_EXTENSIONS:
        names.update(icon_value + extension for extension in ICON_EXTENSIONS)
    matches = []
    for entry in archive.entries.values():
        path = PurePosixPath(entry.path)
        if path.name not in names:
            continue
        if (
            len(path.parts) == 1
            or entry.path.startswith("usr/share/icons/")
            or (entry.path.startswith("usr/share/pixmaps/") and len(path.parts) == 4)
        ):
            matches.append(entry)
    if not matches:
        return None
    if any(entry.kind != "R" for entry in matches):
        raise ValueError(
            "matching AppImage icon is a symlink, hardlink, or special file"
        )
    selected = max(matches, key=lambda entry: _icon_score(entry.path))
    return _validate_selected_entry(archive, selected.path, MAX_ICON_BYTES)


def _safe_icon_name(value: str) -> bool:
    return (
        value not in {"", ".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and len(value.encode("utf-8")) <= 255
    )


def _verify_extracted_file(root: Path, archive_path: str, maximum_size: int) -> Path:
    expected = root.joinpath(*PurePosixPath(archive_path).parts)
    allowed = {root}
    current = expected
    while current != root:
        allowed.add(current)
        current = current.parent
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in directory_names + file_names:
            candidate = base / name
            details = os.lstat(candidate)
            if candidate not in allowed:
                raise ValueError(
                    f"unsquashfs extracted an unexpected path: {candidate}"
                )
            if stat.S_ISLNK(details.st_mode):
                raise ValueError("unsquashfs extracted a symbolic link")
            if candidate == expected:
                if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                    raise ValueError(
                        "extracted metadata is not an independent regular file"
                    )
                if details.st_size > maximum_size:
                    raise ValueError("extracted metadata exceeds its size limit")
            elif not stat.S_ISDIR(details.st_mode):
                raise ValueError("unsquashfs extracted a non-directory ancestor")
    if not expected.exists():
        raise ValueError("unsquashfs did not extract the requested metadata")
    return expected


def _extract_entry(
    snapshot: AppImageSnapshot,
    archive: SquashfsArchive,
    entry: ArchiveEntry,
    temporary: Path,
    label: str,
    maximum_size: int,
    runner: SandboxRunner,
) -> Path:
    output = temporary / f"extract-{label}"
    output.mkdir(mode=0o700)
    runner(
        snapshot.path,
        output,
        [
            "-dest",
            "/output",
            "-offset",
            str(archive.offset),
            "-no-xattrs",
            "-no-progress",
            "-processors",
            "1",
            "-mem",
            "64M",
            "-strict-errors",
            "-match",
            "-no-wildcards",
            "/source.AppImage",
            entry.path,
        ],
        False,
    )
    return _verify_extracted_file(output, entry.path, maximum_size)


def _read_desktop(
    path: Path,
) -> tuple[configparser.ConfigParser, configparser.SectionProxy]:
    details = os.lstat(path)
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError("AppImage desktop metadata is not a regular file")
    if details.st_size > MAX_DESKTOP_BYTES:
        raise ValueError("AppImage desktop metadata is too large")
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with path.open(encoding="utf-8") as desktop_file:
        parser.read_file(desktop_file)
    entry = parser["Desktop Entry"]
    if entry.get("Type") != "Application" or not entry.get("Name"):
        raise ValueError("AppImage desktop entry is not an application")
    return parser, entry


def extract_metadata(
    snapshot: AppImageSnapshot,
    temporary: Path,
    runner: SandboxRunner = run_unsquashfs_sandbox,
) -> tuple[Path, Path | None]:
    archive = select_squashfs_archive(snapshot, temporary, runner)
    desktop_entry = select_desktop_entry(archive)
    desktop = _extract_entry(
        snapshot,
        archive,
        desktop_entry,
        temporary,
        "desktop",
        MAX_DESKTOP_BYTES,
        runner,
    )
    _parser, entry = _read_desktop(desktop)
    icon_entry = select_icon_entry(archive, entry.get("Icon"))
    icon = None
    if icon_entry is not None:
        icon = _extract_entry(
            snapshot,
            archive,
            icon_entry,
            temporary,
            "icon",
            MAX_ICON_BYTES,
            runner,
        )
    return desktop, icon


def appimage_paths(home: Path, identifier: str) -> tuple[Path, Path, Path]:
    app_dir = home / ".private/Packages/zenos/appimages" / identifier
    registration = (
        home
        / ".private/Packages/applications"
        / f"com.negzero.zenos.appimage.{identifier}.desktop"
    )
    return app_dir, app_dir / f"{identifier}.AppImage", registration


def _field_codes(exec_value: str) -> str:
    for code in ("%U", "%F", "%u", "%f"):
        if code in exec_value:
            return code
    return ""


def _copy_payload(source: Path, destination: Path, expected_sha256: str | None) -> str:
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    destination_fd = -1
    try:
        source_details = os.fstat(source_fd)
        if not stat.S_ISREG(source_details.st_mode):
            raise ValueError("validated AppImage snapshot is not a regular file")
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o755,
        )
        digest = hashlib.sha256()
        copied = 0
        while True:
            chunk = os.read(source_fd, COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
            copied += len(chunk)
        os.fsync(destination_fd)
        actual_sha256 = digest.hexdigest()
        if copied != source_details.st_size or (
            expected_sha256 is not None and actual_sha256 != expected_sha256
        ):
            raise ValueError("installed AppImage does not match its validated snapshot")
        return actual_sha256
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)


def register_appimage(
    source: Path | AppImageSnapshot,
    extracted_desktop: Path,
    extracted_icon: Path | None,
    home: Path,
    identifier: str | None = None,
) -> str:
    _parser, extracted_entry = _read_desktop(extracted_desktop)
    source_path = source.path if isinstance(source, AppImageSnapshot) else source
    expected_sha256 = source.sha256 if isinstance(source, AppImageSnapshot) else None
    app_id = validate_id(identifier) if identifier else safe_id(source_path.stem)
    app_dir, installed, registration = appimage_paths(home, app_id)
    if app_dir.exists() or registration.exists():
        raise FileExistsError(f"AppImage {app_id!r} is already installed")

    app_dir.mkdir(parents=True, mode=0o755)
    registration.parent.mkdir(parents=True, mode=0o755)
    try:
        installed_sha256 = _copy_payload(source_path, installed, expected_sha256)
        icon_target = None
        if extracted_icon is not None:
            icon_details = os.lstat(extracted_icon)
            if not stat.S_ISREG(icon_details.st_mode) or stat.S_ISLNK(
                icon_details.st_mode
            ):
                raise ValueError("AppImage icon metadata is not a regular file")
            icon_target = app_dir / f"icon{extracted_icon.suffix.lower()}"
            shutil.copyfile(extracted_icon, icon_target, follow_symlinks=False)

        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.add_section("Desktop Entry")
        entry = parser["Desktop Entry"]
        entry["Type"] = "Application"
        for key in (
            "Name",
            "GenericName",
            "Comment",
            "Categories",
            "Keywords",
            "MimeType",
            "StartupNotify",
            "Terminal",
        ):
            if extracted_entry.get(key):
                entry[key] = extracted_entry[key]
        quoted_installed = str(installed).replace("\\", "\\\\").replace('"', '\\"')
        entry["Exec"] = f'"{quoted_installed}"'
        field_code = _field_codes(extracted_entry.get("Exec", ""))
        if field_code:
            entry["Exec"] += f" {field_code}"
        entry["Icon"] = (
            str(icon_target)
            if icon_target
            else extracted_entry.get("Icon", "application-x-executable")
        )
        entry["X-ZenOS-Origin"] = "managed-appimage"
        entry["X-ZenOS-Source"] = "manual"
        entry["X-ZenOS-AppImageId"] = app_id
        entry["X-ZenOS-AppImageFile"] = str(installed)
        entry["X-ZenOS-Package"] = str(app_dir)
        with registration.open("w", encoding="utf-8") as desktop_file:
            parser.write(desktop_file, space_around_delimiters=False)
        registration.chmod(0o644)
        (app_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "id": app_id,
                    "name": extracted_entry["Name"],
                    "sha256": installed_sha256,
                    "appimage": str(installed),
                    "desktop": str(registration),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except BaseException:
        shutil.rmtree(app_dir, ignore_errors=True)
        registration.unlink(missing_ok=True)
        raise
    return app_id


def install(
    path: Path,
    home: Path,
    identifier: str | None = None,
    runner: SandboxRunner = run_unsquashfs_sandbox,
) -> str:
    app_id = validate_id(identifier) if identifier else safe_id(path.stem)
    with tempfile.TemporaryDirectory(prefix="zen-appimage-") as directory:
        temporary = Path(directory)
        snapshot = snapshot_appimage(path, temporary)
        desktop, icon = extract_metadata(snapshot, temporary, runner)
        return register_appimage(snapshot, desktop, icon, home, app_id)


def remove(identifier: str, home: Path) -> None:
    app_id = validate_id(identifier)
    app_dir, _installed, registration = appimage_paths(home, app_id)
    if not app_dir.is_dir() and not registration.exists():
        raise FileNotFoundError(f"AppImage {app_id!r} is not installed")
    registration.unlink(missing_ok=True)
    if app_dir.exists():
        shutil.rmtree(app_dir, ignore_errors=False)


def list_installed(home: Path) -> list[dict[str, str]]:
    root = home / ".private/Packages/zenos/appimages"
    installed: list[dict[str, str]] = []
    if not root.is_dir():
        return installed
    for manifest in sorted(root.glob("*/manifest.json")):
        try:
            record = json.loads(manifest.read_text(encoding="utf-8"))
            validate_id(record["id"])
            installed.append({"id": record["id"], "name": record["name"]})
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return installed


def refresh_index(home: Path, target: Path) -> None:
    try:
        build_source_views(home, target, os.environ.get("USER"), scope="user")
    except PermissionError:
        print(
            f"warning: could not refresh {target}; the desktop registration was updated",
            file=sys.stderr,
        )


def default_apps_dir(home: Path, environ: Mapping[str, str] = os.environ) -> Path:
    configured = environ.get("ZENOS_APPS_DIR")
    return Path(configured) if configured else home / ".private/Apps"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--home", type=Path, default=Path.home(), help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--apps-dir",
        type=Path,
        default=None,
        help=argparse.SUPPRESS,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser(
        "install", help="install and register a type 2 AppImage"
    )
    install_parser.add_argument("file", type=Path)
    remove_parser = commands.add_parser("remove", help="remove a managed AppImage")
    remove_parser.add_argument("id")
    commands.add_parser("list", help="list managed AppImages")
    args = parser.parse_args()
    apps_dir = args.apps_dir or default_apps_dir(args.home)

    try:
        if args.command == "install":
            identifier = install(args.file, args.home)
            refresh_index(args.home, apps_dir)
            print(identifier)
        elif args.command == "remove":
            remove(args.id, args.home)
            refresh_index(args.home, apps_dir)
        else:
            for item in list_installed(args.home):
                print(f"{item['id']}\t{item['name']}")
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as error:
        parser.exit(1, f"zen-appimage: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
