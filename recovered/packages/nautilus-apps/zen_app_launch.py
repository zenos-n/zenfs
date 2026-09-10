#!/usr/bin/env python3
"""Validate and launch a generated ZenOS /Apps entry."""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import stat
import sys
from typing import Mapping, Sequence

from app_registry import REGISTRY_SCHEMA, valid_token


INDEX_SCHEMA = 4
MAX_FILE_BYTES = 4 * 1024 * 1024
SOURCE_LABELS = {
    "nix-config": "Nix (Config)",
    "nix-imperative": "Nix (Imperative)",
    "flatpak": "Flatpak",
    "manual": "Manually Installed",
}


def source_roots(home: Path, user: str | None = None) -> dict[str, tuple[Path, ...]]:
    username = user or home.name
    return {
        "manual": (home / ".private/Packages/applications",),
        "flatpak": (
            home / ".private/Packages/flatpak/exports/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
        ),
        "nix-imperative": (
            home / ".private/State/nix/profiles/profile/share/applications",
            Path(
                f"/nix/var/nix/profiles/per-user/{username}/profile/share/applications"
            ),
        ),
        "nix-config": (
            Path("/run/current-system/sw/share/applications"),
            Path(f"/etc/profiles/per-user/{username}/share/applications"),
        ),
    }


def _simple_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and len(value.encode("utf-8")) <= 255
        and "\x00" not in value
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def _read_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            raise ValueError(f"not a bounded regular file: {path}")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > maximum:
            raise ValueError(f"file is too large: {path}")
        return contents
    finally:
        os.close(descriptor)


def _metadata(contents: bytes) -> tuple[configparser.ConfigParser, dict[str, str]]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(contents.decode("utf-8"))
    entry = parser["Desktop Entry"]
    normalized: dict[str, str] = {}
    for option, value in entry.items():
        key = option.strip().casefold()
        if key.startswith("x-zenos-") and key in normalized:
            raise ValueError(f"duplicate index metadata: {option}")
        normalized[key] = value.strip()
    return parser, normalized


def _real_directory(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    details = os.lstat(absolute)
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError(f"not a real directory: {absolute}")
    if absolute.resolve(strict=True) != absolute:
        raise ValueError(f"directory contains a symlink: {absolute}")
    return absolute


def _manifest_contains(view: Path, launcher_name: str) -> bool:
    try:
        manifest = json.loads(
            _read_regular(view / ".zenos-app-index.json", 1024 * 1024)
        )
        applications = manifest["applications"]
    except (KeyError, OSError, TypeError, ValueError, UnicodeError):
        return False
    return (
        manifest.get("schema") == INDEX_SCHEMA
        and isinstance(applications, list)
        and all(_simple_basename(name) for name in applications)
        and launcher_name in applications
    )


def _registry_records(view: Path) -> list[dict[str, str]]:
    try:
        registry = json.loads(
            _read_regular(view / ".zenos-app-registry.json", 1024 * 1024)
        )
        applications = registry["applications"]
    except (KeyError, OSError, TypeError, ValueError, UnicodeError):
        return []
    if (
        registry.get("schema") != REGISTRY_SCHEMA
        or not isinstance(applications, list)
        or len(applications) > 10000
    ):
        return []
    records = []
    for application in applications:
        if not isinstance(application, dict):
            return []
        record = {
            key: application.get(key)
            for key in ("token", "launcher", "desktopId", "source", "sourcePath")
        }
        if (
            not valid_token(record["token"])
            or not _simple_basename(record["launcher"])
            or not _simple_basename(record["desktopId"])
            or record["source"] not in SOURCE_LABELS
            or not isinstance(record["sourcePath"], str)
            or not Path(record["sourcePath"]).is_absolute()
        ):
            return []
        records.append(record)
    return records


def _registry_contains(
    view: Path, launcher_name: str, metadata: Mapping[str, str]
) -> bool:
    expected = {
        "token": metadata.get("x-zenos-apptoken"),
        "launcher": launcher_name,
        "desktopId": metadata.get("x-zenos-desktopid"),
        "source": metadata.get("x-zenos-source"),
        "sourcePath": metadata.get("x-zenos-sourcepath"),
    }
    return any(record == expected for record in _registry_records(view))


def validate_launcher(
    value: Path,
    apps_root: Path = Path("/Apps"),
    roots: Mapping[str, Sequence[Path]] | None = None,
) -> Path:
    apps_root = _real_directory(apps_root)
    candidate = Path(os.path.abspath(value))
    views = {"all": apps_root}
    for source in SOURCE_LABELS:
        view = apps_root / ".sources" / source
        if view.exists():
            views[source] = _real_directory(view)
    view_key = next(
        (key for key, view in views.items() if candidate.parent == view), None
    )
    if view_key is None or not _simple_basename(candidate.name):
        raise ValueError("launcher is outside the generated /Apps source views")
    contents = _read_regular(candidate, MAX_FILE_BYTES)
    if not _manifest_contains(views[view_key], candidate.name):
        raise ValueError("launcher is not recorded by the app index")
    _parser, metadata = _metadata(contents)
    source = metadata.get("x-zenos-source")
    token = metadata.get("x-zenos-apptoken")
    desktop_id = metadata.get("x-zenos-desktopid")
    source_path_value = metadata.get("x-zenos-sourcepath")
    if (
        metadata.get("x-zenos-managed", "").casefold() != "true"
        or metadata.get("x-zenos-indexversion") != str(INDEX_SCHEMA)
        or not valid_token(token)
        or source not in SOURCE_LABELS
        or metadata.get("x-zenos-sourcelabel") != SOURCE_LABELS[source]
        or not _simple_basename(desktop_id)
        or not desktop_id.endswith(".desktop")
        or not source_path_value
    ):
        raise ValueError("launcher has invalid index provenance metadata")
    if view_key != "all" and view_key != source:
        raise ValueError("launcher source does not match its /Apps view")

    source_path = Path(source_path_value)
    if not source_path.is_absolute() or source_path.name != desktop_id:
        raise ValueError("launcher source path does not match its desktop ID")
    allowed_roots = roots or source_roots(Path.home(), os.environ.get("USER"))
    allowed = {Path(os.path.abspath(root)) for root in allowed_roots[source]}
    if Path(os.path.abspath(source_path.parent)) not in allowed:
        raise ValueError("launcher source path is outside its classified source")
    try:
        if not source_path.resolve(strict=True).is_file():
            raise ValueError("launcher source is not a desktop file")
    except OSError as error:
        raise ValueError("launcher source no longer exists") from error
    if not _registry_contains(views[view_key], candidate.name, metadata):
        raise ValueError("launcher is not recorded by the app registry")
    return candidate


def validate_ordinary_launcher(value: Path) -> Path:
    candidate = Path(os.path.abspath(value))
    contents = _read_regular(candidate, MAX_FILE_BYTES)
    _parser, metadata = _metadata(contents)
    if metadata.get("type") != "Application":
        raise ValueError("ordinary desktop file is not an application")
    if any(key.startswith("x-zenos-") for key in metadata):
        raise ValueError("managed launcher metadata is invalid outside Apps")
    return candidate


def resolve_launcher(
    value: Path,
    apps_root: Path = Path("/Apps"),
    roots: Mapping[str, Sequence[Path]] | None = None,
) -> Path:
    root = _real_directory(apps_root)
    candidate = Path(os.path.abspath(value))
    try:
        candidate.relative_to(root)
    except ValueError:
        return validate_ordinary_launcher(candidate)
    return validate_launcher(candidate, root, roots)


def resolve_path(
    value: Path,
    apps_roots: Sequence[Path],
    roots: Mapping[str, Sequence[Path]] | None = None,
) -> Path:
    candidate = Path(os.path.abspath(value))
    for apps_root in apps_roots:
        try:
            root = _real_directory(apps_root)
            candidate.relative_to(root)
        except (FileNotFoundError, ValueError):
            continue
        return validate_launcher(candidate, root, roots)
    return validate_ordinary_launcher(candidate)


def resolve_token(
    token: str,
    apps_root: Path = Path("/Apps"),
    roots: Mapping[str, Sequence[Path]] | None = None,
) -> Path:
    if not valid_token(token):
        raise ValueError("invalid application token")
    root = _real_directory(apps_root)
    views = [root]
    for source in SOURCE_LABELS:
        view = root / ".sources" / source
        if view.exists():
            views.append(_real_directory(view))
    for view in views:
        for record in _registry_records(view):
            if record["token"] == token:
                return validate_launcher(view / record["launcher"], root, roots)
    raise ValueError("application token is not registered")


def main() -> int:
    arguments = sys.argv[1:]
    apps_root = Path(os.environ.get("ZENOS_APPS_DIR", "/Apps"))
    if len(arguments) >= 2 and arguments[0] == "--apps-root":
        apps_root = Path(arguments[1])
        arguments = arguments[2:]
    if len(arguments) == 2 and arguments[0] == "--token":
        mode = "token"
        value = arguments[1]
    elif len(arguments) == 1:
        mode = "path"
        value = arguments[0]
    else:
        return 2
    try:
        if mode == "token":
            launcher = resolve_token(value, apps_root)
        else:
            user_root = Path(
                os.environ.get(
                    "ZENOS_USER_APPS_DIR", str(Path.home() / ".private/Apps")
                )
            )
            launcher = resolve_path(Path(value), (apps_root, user_root))
    except (configparser.Error, KeyError, OSError, UnicodeError, ValueError) as error:
        print(f"zen-app-launch: {error}", file=sys.stderr)
        return 1

    from gi.repository import Gio

    app = Gio.DesktopAppInfo.new_from_filename(str(launcher))
    if app is None:
        return 1
    app.launch([], None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
