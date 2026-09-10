#!/usr/bin/env python3
"""Small, dependency-free administration tool for ZenFS."""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA = "zenfs-v1"
MARKER_SCHEMA = "zenfs-roaming-v1"
RENAME_NOREPLACE = 1
AT_FDCWD = -100


class ZenFSError(Exception):
    pass


def load_json_object(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as error:
        raise ZenFSError(f"cannot read {path}: {error}") from error
    if not isinstance(value, dict):
        raise ZenFSError(f"{path} must contain a JSON object")
    return value


def inspect_alias(alias: str, target: str) -> dict[str, Any]:
    path = Path(alias)
    result: dict[str, Any] = {"path": alias, "target": target}
    try:
        if not os.path.lexists(path):
            return result | {"status": "missing"}
        if not path.is_symlink():
            return result | {"status": "clobber", "actual": "non-symlink"}
        actual = os.readlink(path)
    except OSError as error:
        return result | {"status": "error", "actual": str(error)}
    if actual != target:
        return result | {"status": "wrong-target", "actual": actual}
    return result | {"status": "ok"}


def inspect_roaming_drive(name: str, drive: dict[str, Any]) -> dict[str, Any]:
    enabled = drive.get("enable")
    mount_point = drive.get("mountPoint")
    marker_file = drive.get("markerFile")
    marker_id = drive.get("markerId")
    if not isinstance(enabled, bool) or not all(
        isinstance(value, str) for value in (mount_point, marker_file, marker_id)
    ):
        raise ZenFSError(f"roaming drive {name!r} has an invalid manifest entry")

    result = {"name": name, "mountPoint": mount_point, "markerId": marker_id}
    if not enabled:
        return result | {"status": "disabled"}
    if not os.path.ismount(mount_point):
        return result | {"status": "unmounted"}

    marker_path = Path(mount_point) / marker_file
    try:
        marker = load_json_object(marker_path)
    except ZenFSError as error:
        return result | {"status": "marker-invalid", "actual": str(error)}
    if marker.get("schema") != MARKER_SCHEMA or marker.get("id") != marker_id:
        return result | {
            "status": "marker-invalid",
            "actual": {"schema": marker.get("schema"), "id": marker.get("id")},
        }
    return result | {"status": "mounted"}


def command_status(args: argparse.Namespace) -> int:
    manifest = load_json_object(args.manifest)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ZenFSError(
            f"{args.manifest} has schema {manifest.get('schema')!r}, "
            f"expected {MANIFEST_SCHEMA!r}"
        )
    aliases = manifest.get("aliases")
    if not isinstance(aliases, dict) or not all(
        isinstance(path, str) and isinstance(target, str)
        for path, target in aliases.items()
    ):
        raise ZenFSError(f"{args.manifest} has an invalid aliases object")

    roaming = manifest.get("roaming", {})
    if not isinstance(roaming, dict) or not all(
        isinstance(name, str) and isinstance(drive, dict)
        for name, drive in roaming.items()
    ):
        raise ZenFSError(f"{args.manifest} has an invalid roaming object")

    results = [inspect_alias(path, target) for path, target in sorted(aliases.items())]
    drive_results = [
        inspect_roaming_drive(name, drive) for name, drive in sorted(roaming.items())
    ]
    healthy = all(item["status"] == "ok" for item in results) and all(
        item["status"] != "marker-invalid" for item in drive_results
    )
    output = {
        "healthy": healthy,
        "manifest": str(args.manifest),
        "aliases": results,
        "roaming": drive_results,
    }
    if args.json:
        print(json.dumps(output, indent=2, sort_keys=True))
    else:
        for item in results:
            detail = ""
            if "actual" in item:
                detail = f" (actual: {item['actual']})"
            print(f"{item['status']:>12}  {item['path']} -> {item['target']}{detail}")
        for item in drive_results:
            detail = f" ({item['actual']})" if "actual" in item else ""
            print(f"{item['status']:>12}  {item['name']} at {item['mountPoint']}{detail}")
        print("ZenFS hierarchy is healthy" if healthy else "ZenFS hierarchy needs attention")
    return 0 if healthy else 1


def command_verify_marker(args: argparse.Namespace) -> int:
    descriptor = os.open(args.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > 65536:
            raise ZenFSError("roaming marker must be a bounded regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            marker = json.loads(stream.read(65537))
        if not isinstance(marker, dict):
            raise ZenFSError("roaming marker must be a JSON object")
    except (ValueError, UnicodeError) as error:
        raise ZenFSError(f"invalid roaming marker: {error}") from error
    finally:
        os.close(descriptor)
    schema = marker.get("schema")
    marker_id = marker.get("id")
    if schema != MARKER_SCHEMA:
        raise ZenFSError(
            f"{args.path} has schema {schema!r}, expected {MARKER_SCHEMA!r}"
        )
    if marker_id != args.id:
        raise ZenFSError(
            f"{args.path} identifies {marker_id!r}, expected {args.id!r}"
        )
    print(f"verified roaming marker {args.id!r} at {args.path}")
    return 0


def rename_noreplace(source: Path, destination: Path) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise ZenFSError(
            "renameat2 is unavailable; refusing migration because atomic "
            "no-clobber rename cannot be guaranteed"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        AT_FDCWD,
        os.fsencode(source),
        AT_FDCWD,
        os.fsencode(destination),
        RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise ZenFSError(f"destination already exists: {destination}")
    if error_number == errno.EXDEV:
        raise ZenFSError(
            "source and destination are on different filesystems; refusing "
            "a non-transactional copy/delete migration"
        )
    raise ZenFSError(
        f"cannot rename {source} to {destination}: {os.strerror(error_number)}"
    )


def validate_migration_paths(source: Path, destination: Path) -> None:
    if not source.is_absolute() or not destination.is_absolute():
        raise ZenFSError("source and destination must be absolute paths")
    if source == Path("/") or destination == Path("/"):
        raise ZenFSError("refusing to migrate the filesystem root")
    if not os.path.lexists(source):
        raise ZenFSError(f"source does not exist: {source}")
    if os.path.lexists(destination):
        raise ZenFSError(f"destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise ZenFSError(f"destination parent is not a directory: {destination.parent}")
    source_mode = os.lstat(source).st_mode
    if stat.S_ISDIR(source_mode):
        source_real = source.resolve()
        destination_parent_real = destination.parent.resolve()
        if destination_parent_real == source_real or source_real in destination_parent_real.parents:
            raise ZenFSError("destination cannot be inside the source directory")


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def command_migrate_hierarchy(args: argparse.Namespace) -> int:
    source = args.source
    destination = args.destination
    validate_migration_paths(source, destination)
    if args.dry_run:
        print(f"would atomically rename {source} to {destination} without replacement")
        return 0

    source_parent = source.parent.resolve()
    destination_parent = destination.parent.resolve()
    rename_noreplace(source, destination)
    try:
        fsync_directory(destination_parent)
        if source_parent != destination_parent:
            fsync_directory(source_parent)
    except OSError as error:
        raise ZenFSError(
            f"migration completed, but directory metadata could not be synced: {error}"
        ) from error
    print(f"migrated {source} to {destination} with atomic no-replace rename")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zenfsctl", description="Inspect and safely migrate ZenFS")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status_parser = subparsers.add_parser("status", help="check configured hierarchy aliases")
    status_parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("/etc/zenfs/manifest.json"),
        help="ZenFS manifest (default: /etc/zenfs/manifest.json)",
    )
    status_parser.add_argument("--json", action="store_true", help="emit JSON")
    status_parser.set_defaults(handler=command_status)

    marker_parser = subparsers.add_parser("verify-marker", help="verify a roaming-drive marker")
    marker_parser.add_argument("path", type=Path, help="marker JSON path")
    marker_parser.add_argument("--id", required=True, help="expected marker identifier")
    marker_parser.set_defaults(handler=command_verify_marker)

    migrate_parser = subparsers.add_parser(
        "migrate-hierarchy",
        help="atomically rename one hierarchy entry without replacing a destination",
    )
    migrate_parser.add_argument("source", type=Path)
    migrate_parser.add_argument("destination", type=Path)
    migrate_parser.add_argument("--dry-run", action="store_true")
    migrate_parser.set_defaults(handler=command_migrate_hierarchy)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ZenFSError, OSError) as error:
        print(f"zenfsctl: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
