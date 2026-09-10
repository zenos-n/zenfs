#!/usr/bin/env python3
"""Populate scoped ZenOS application views with validated launchers."""

from __future__ import annotations

import argparse
import configparser
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat

from app_registry import REGISTRY_SCHEMA, app_token, valid_token


INDEX_SCHEMA = 4
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_LAUNCHER_BYTES = 4 * 1024 * 1024
SOURCE_VIEWS = (
    ("all", "All Sources"),
    ("nix-config", "Nix (Config)"),
    ("nix-imperative", "Nix (Imperative)"),
    ("flatpak", "Flatpak"),
    ("manual", "Manually Installed"),
)
SOURCE_LABELS = dict(SOURCE_VIEWS)
APPIMAGE_REGISTRATION = re.compile(
    r"^com\.negzero\.zenos\.appimage\.([a-z0-9._-]+)\.desktop$"
)
FLATPAK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class AppSource:
    key: str
    label: str
    directories: tuple[Path, ...]


def source_catalog(
    home: Path, user: str | None = None, scope: str = "all"
) -> tuple[AppSource, ...]:
    """Return desktop-entry sources in XDG override order."""
    if scope not in {"all", "system", "user"}:
        raise ValueError(f"invalid application scope: {scope!r}")
    username = user or home.name
    include_user = scope in {"all", "user"}
    include_system = scope in {"all", "system"}
    user_flatpak = (
        (home / ".private/Packages/flatpak/exports/share/applications",)
        if include_user
        else ()
    )
    system_flatpak = (
        (Path("/var/lib/flatpak/exports/share/applications"),)
        if include_system
        else ()
    )
    system_nix = (
        (Path("/run/current-system/sw/share/applications"),)
        if include_system
        else ()
    )
    user_nix = (
        (Path(f"/etc/profiles/per-user/{username}/share/applications"),)
        if include_user
        else ()
    )
    return (
        AppSource(
            "manual",
            "Manually Installed",
            (home / ".private/Packages/applications",) if include_user else (),
        ),
        AppSource(
            "flatpak",
            "Flatpak",
            user_flatpak + system_flatpak,
        ),
        AppSource(
            "nix-imperative",
            "Nix (Imperative)",
            (
                (
                    home / ".private/State/nix/profiles/profile/share/applications",
                    Path(
                        f"/nix/var/nix/profiles/per-user/{username}/profile/share/applications"
                    ),
                )
                if include_user
                else ()
            ),
        ),
        AppSource(
            "nix-config",
            "Nix (Config)",
            system_nix + user_nix,
        ),
    )


def classify_source(path: Path, home: Path, user: str | None = None) -> str | None:
    candidate = Path(os.path.abspath(path))
    for source in source_catalog(home, user):
        for directory in source.directories:
            try:
                candidate.relative_to(Path(os.path.abspath(directory)))
            except ValueError:
                continue
            return source.key
    return None


def desktop_entry(path: Path) -> configparser.SectionProxy:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    with path.open(encoding="utf-8") as desktop_file:
        parser.read_file(desktop_file)
    return parser["Desktop Entry"]


def entry_value(entry: configparser.SectionProxy, key: str) -> str | None:
    expected = key.strip().casefold()
    value = None
    for option, candidate in entry.items():
        if option.strip().casefold() == expected:
            value = candidate.strip()
    return value


def _desktop_boolean(entry: configparser.SectionProxy, key: str) -> bool:
    value = entry_value(entry, key)
    if value is None:
        return False
    normalized = value.casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid desktop boolean for {key}")


def desktop_visible(path: Path, desktop: str = "GNOME") -> bool:
    try:
        entry = desktop_entry(path)
        if entry_value(entry, "Type") != "Application":
            return False
        if _desktop_boolean(entry, "Hidden") or _desktop_boolean(entry, "NoDisplay"):
            return False
    except (configparser.Error, KeyError, OSError, UnicodeError, ValueError):
        return False

    only_show_in = set(
        filter(None, (entry_value(entry, "OnlyShowIn") or "").split(";"))
    )
    not_show_in = set(filter(None, (entry_value(entry, "NotShowIn") or "").split(";")))
    if only_show_in and desktop not in only_show_in:
        return False
    if desktop in not_show_in:
        return False

    try_exec = entry_value(entry, "TryExec")
    if try_exec and not (
        (os.path.isabs(try_exec) and os.access(try_exec, os.X_OK))
        or shutil.which(try_exec)
    ):
        return False
    return True


def _real_directory(path: Path, create: bool = False) -> Path:
    absolute = Path(os.path.abspath(path))
    parent = absolute.parent.resolve(strict=True)
    if parent != absolute.parent:
        raise ValueError(f"directory parent contains a symlink: {absolute}")
    if create:
        try:
            os.mkdir(absolute, 0o755)
        except FileExistsError:
            pass
    details = os.lstat(absolute)
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError(f"not a real directory: {absolute}")
    if absolute.resolve(strict=True) != absolute:
        raise ValueError(f"directory contains a symlink: {absolute}")
    return absolute


def simple_basename(value: object) -> bool:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        return False
    if len(value.encode("utf-8")) > 255 or "\x00" in value:
        return False
    return Path(value).name == value and "/" not in value and "\\" not in value


def _read_regular_at(directory_fd: int, name: str, maximum: int) -> bytes | None:
    if not simple_basename(name):
        return None
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    except OSError:
        return None
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
            return None
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        return data if len(data) <= maximum else None
    finally:
        os.close(descriptor)


def _atomic_write(directory: Path, name: str, contents: bytes, mode: int) -> None:
    if not simple_basename(name):
        raise ValueError(f"unsafe index filename: {name!r}")
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".zen-app-index-{secrets.token_hex(12)}"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            mode,
            dir_fd=directory_fd,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), mode)
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _metadata_from_bytes(contents: bytes) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(contents.decode("utf-8"))
    entry = parser["Desktop Entry"]
    return {option.strip().casefold(): value.strip() for option, value in entry.items()}


def _managed_launcher(contents: bytes, legacy: bool) -> bool:
    try:
        metadata = _metadata_from_bytes(contents)
    except (configparser.Error, KeyError, UnicodeError):
        return False
    source = metadata.get("x-zenos-source")
    label = metadata.get("x-zenos-sourcelabel")
    desktop_id = metadata.get("x-zenos-desktopid")
    if source not in SOURCE_LABELS or label != SOURCE_LABELS[source]:
        return False
    if not simple_basename(desktop_id) or not desktop_id.endswith(".desktop"):
        return False
    if legacy:
        return True
    return (
        metadata.get("x-zenos-managed", "").casefold() == "true"
        and metadata.get("x-zenos-indexversion") == str(INDEX_SCHEMA)
        and valid_token(metadata.get("x-zenos-apptoken"))
    )


def _clean_view(target: Path) -> None:
    target = _real_directory(target)
    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw_manifest = _read_regular_at(
            directory_fd, ".zenos-app-index.json", MAX_MANIFEST_BYTES
        )
        if raw_manifest is None:
            return
        try:
            manifest = json.loads(raw_manifest)
            schema = manifest["schema"]
            applications = manifest["applications"]
        except (KeyError, TypeError, ValueError):
            return
        if schema not in {1, 2, 3, INDEX_SCHEMA} or not isinstance(applications, list):
            return
        if len(applications) > 10000 or not all(
            simple_basename(name) for name in applications
        ):
            return
        if len(set(applications)) != len(applications):
            return
        for name in applications:
            contents = _read_regular_at(directory_fd, name, MAX_LAUNCHER_BYTES)
            if contents is None or not _managed_launcher(
                contents, legacy=schema != INDEX_SCHEMA
            ):
                continue
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(directory_fd)


def package_root(path: Path, source_key: str) -> Path | None:
    if source_key == "manual":
        match = APPIMAGE_REGISTRATION.fullmatch(path.name)
        if match:
            try:
                home = path.parents[3]
            except IndexError:
                return None
            app_dir = home / ".private/Packages/zenos/appimages" / match.group(1)
            try:
                details = os.lstat(app_dir)
            except OSError:
                return None
            if stat.S_ISDIR(details.st_mode) and not stat.S_ISLNK(details.st_mode):
                return app_dir
        return None

    try:
        entry = desktop_entry(path)
    except (configparser.Error, KeyError, OSError, UnicodeError):
        return None
    if source_key == "flatpak":
        flatpak_id = entry_value(entry, "X-Flatpak")
        if flatpak_id and FLATPAK_ID.fullmatch(flatpak_id):
            export_root = path.parent.parent.parent
            flatpak_root = export_root.parent / "app" / flatpak_id
            if flatpak_root.is_dir():
                return flatpak_root

    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None
    if resolved.parent.name != "applications" or resolved.parent.parent.name != "share":
        return None
    if source_key in {"nix-config", "nix-imperative", "flatpak"}:
        return resolved.parent.parent.parent
    return None


def _appimage_metadata(path: Path, package: Path | None) -> list[str]:
    match = APPIMAGE_REGISTRATION.fullmatch(path.name)
    if package is None or match is None:
        return []
    identifier = match.group(1)
    installed = package / f"{identifier}.AppImage"
    try:
        details = os.lstat(installed)
    except OSError:
        return []
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        return []
    return [
        "X-ZenOS-Origin=managed-appimage",
        f"X-ZenOS-AppImageId={identifier}",
        f"X-ZenOS-AppImageFile={installed}",
    ]


def _bounded_name(value: str, fallback: str) -> str:
    sanitized = "".join(
        "-" if ord(character) < 32 else character for character in value
    )
    sanitized = (
        sanitized.replace("/", "-").replace("\\", "-").strip().strip(".") or fallback
    )
    while len(sanitized.encode("utf-8")) > 200:
        sanitized = sanitized[:-1]
    return sanitized or fallback


def launcher_name(display_name: str, desktop_id: str, used: set[str]) -> str:
    safe_name = _bounded_name(display_name, desktop_id)
    candidate = safe_name
    if candidate in used:
        candidate = _bounded_name(
            f"{safe_name} - {desktop_id.removesuffix('.desktop')}", desktop_id
        )
    suffix = 2
    while candidate in used:
        candidate = _bounded_name(
            f"{safe_name} - {desktop_id.removesuffix('.desktop')} ({suffix})",
            desktop_id,
        )
        suffix += 1
    return candidate


def icon_file(source: Path, icon: str | None) -> Path | None:
    if not icon:
        return None
    direct = Path(icon)
    if direct.is_absolute() and direct.is_file():
        return direct
    if "/" in icon or "\\" in icon:
        return None

    icons_root = source.parent.parent / "icons"
    patterns = [
        f"*/scalable/apps/{icon}.svg",
        f"*/512x512/apps/{icon}.png",
        f"*/256x256/apps/{icon}.png",
        f"*/128x128/apps/{icon}.png",
        f"*/64x64/apps/{icon}.png",
        f"*/symbolic/apps/{icon}.svg",
    ]
    for pattern in patterns:
        match = next(icons_root.glob(pattern), None)
        if match is not None:
            return match.resolve()
    return None


def _metadata_contents(
    path: Path, source_key: str, source_label: str, token: str
) -> bytes:
    contents = path.read_text(encoding="utf-8")
    entry = desktop_entry(path)
    package = package_root(path, source_key)
    resolved_icon = icon_file(path, entry_value(entry, "Icon"))
    metadata = [
        "X-ZenOS-Managed=true",
        f"X-ZenOS-IndexVersion={INDEX_SCHEMA}",
        f"X-ZenOS-AppToken={token}",
        f"X-ZenOS-DesktopId={path.name}",
        f"X-ZenOS-Source={source_key}",
        f"X-ZenOS-SourceLabel={source_label}",
        f"X-ZenOS-SourcePath={Path(os.path.abspath(path))}",
    ]
    if package is not None:
        metadata.append(f"X-ZenOS-Package={package}")
    if resolved_icon is not None:
        metadata.append(f"X-ZenOS-IconFile={resolved_icon}")
    metadata.extend(_appimage_metadata(path, package))

    filtered = []
    for line in contents.splitlines():
        option, separator, _value = line.partition("=")
        if separator and option.strip().casefold().startswith("x-zenos-"):
            continue
        filtered.append(line)
    contents = "\n".join(filtered) + "\n"
    section = "[Desktop Entry]\n"
    if section not in contents:
        raise ValueError("desktop entry has no canonical Desktop Entry section")
    rendered = contents.replace(section, section + "\n".join(metadata) + "\n", 1)
    return rendered.encode("utf-8")


def _write_view(entries: list[tuple[Path, AppSource]], target: Path) -> list[str]:
    target = _real_directory(target, create=True)
    rendered: list[tuple[str, bytes, dict[str, str]]] = []
    used_names: set[str] = set()
    for desktop_file, source in entries:
        try:
            entry = desktop_entry(desktop_file)
            destination_name = launcher_name(
                entry_value(entry, "Name") or desktop_file.stem,
                desktop_file.name,
                used_names,
            )
            token = app_token(source.key, desktop_file.name)
            contents = _metadata_contents(
                desktop_file, source.key, source.label, token
            )
        except (configparser.Error, KeyError, OSError, UnicodeError, ValueError):
            continue
        used_names.add(destination_name)
        rendered.append(
            (
                destination_name,
                contents,
                {
                    "token": token,
                    "launcher": destination_name,
                    "desktopId": desktop_file.name,
                    "source": source.key,
                    "sourcePath": str(Path(os.path.abspath(desktop_file))),
                },
            )
        )

    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            previous = json.loads(_read_regular_at(directory_fd, ".zenos-app-index.json", MAX_MANIFEST_BYTES) or b"{}")
            owned = previous["applications"] if previous["schema"] in {1, 2, 3, INDEX_SCHEMA} else []
            if not isinstance(owned, list) or not all(simple_basename(name) for name in owned):
                owned = []
        except (OSError, ValueError, KeyError, TypeError):
            owned = []
        for name, _contents, _registration in rendered:
            destination = target / name
            if not os.path.lexists(destination):
                continue
            contents = _read_regular_at(directory_fd, name, MAX_LAUNCHER_BYTES)
            if name not in owned or contents is None:
                raise ValueError(f"refusing to replace unowned application entry: {destination}")
            if not _managed_launcher(contents, legacy=previous["schema"] != INDEX_SCHEMA):
                raise ValueError(f"refusing to replace changed application entry: {destination}")
    finally:
        os.close(directory_fd)
    _clean_view(target)
    indexed = []
    registrations = []
    for destination_name, contents, registration in rendered:
        if os.path.lexists(target / destination_name):
            raise ValueError(f"refusing to replace unowned application entry: {target / destination_name}")
        _atomic_write(target, destination_name, contents, 0o755)
        indexed.append(destination_name)
        registrations.append(registration)
    manifest = (
        json.dumps(
            {"schema": INDEX_SCHEMA, "applications": indexed},
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write(target, ".zenos-app-index.json", manifest, 0o644)
    registry = (
        json.dumps(
            {"schema": REGISTRY_SCHEMA, "applications": registrations},
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write(target, ".zenos-app-registry.json", registry, 0o644)
    return indexed


def _source_entries(source: AppSource) -> list[tuple[Path, AppSource]]:
    entries: list[tuple[Path, AppSource]] = []
    seen_ids: set[str] = set()
    for directory in source.directories:
        if not directory.is_dir():
            continue
        for desktop_file in sorted(directory.glob("*.desktop")):
            if desktop_file.name in seen_ids or not desktop_visible(desktop_file):
                continue
            seen_ids.add(desktop_file.name)
            entries.append((desktop_file, source))
    return entries


def build_source_views(
    home: Path,
    target: Path,
    user: str | None = None,
    sources: tuple[AppSource, ...] | None = None,
    *,
    scope: str = "all",
) -> dict[str, list[str]]:
    target = _real_directory(target, create=True)
    sources = sources if sources is not None else source_catalog(home, user, scope)
    entries_by_source = {source.key: _source_entries(source) for source in sources}
    all_entries: list[tuple[Path, AppSource]] = []
    seen_ids: set[str] = set()
    for source in sources:
        for desktop_file, entry_source in entries_by_source[source.key]:
            if desktop_file.name in seen_ids:
                continue
            seen_ids.add(desktop_file.name)
            all_entries.append((desktop_file, entry_source))

    source_root = _real_directory(target / ".sources", create=True)
    results = {"all": _write_view(all_entries, target)}
    for source in sources:
        view = _real_directory(source_root / source.key, create=True)
        results[source.key] = _write_view(entries_by_source[source.key], view)
    source_manifest = (
        json.dumps(
            {
                "schema": 1,
                "views": [
                    {
                        "key": key,
                        "label": label,
                        "path": str(target if key == "all" else source_root / key),
                    }
                    for key, label in SOURCE_VIEWS
                ],
            },
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )
    _atomic_write(source_root, ".zenos-source-views.json", source_manifest, 0o644)
    return results


def build_index(source: Path, target: Path) -> list[str]:
    """Build one view for compatibility with the original indexer API."""
    legacy = AppSource("nix-config", "Nix (Config)", (source,))
    return _write_view(_source_entries(legacy), target)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "source", nargs="?", type=Path, help="legacy single desktop-entry source"
    )
    parser.add_argument(
        "legacy_target", nargs="?", type=Path, help="legacy single-view target"
    )
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--target", type=Path, default=Path("/Apps"))
    parser.add_argument("--user")
    parser.add_argument("--scope", choices=("system", "user"), default="system")
    args = parser.parse_args()
    if args.source is not None:
        if args.legacy_target is None:
            parser.error("the legacy source argument requires a target argument")
        build_index(args.source, args.legacy_target)
    else:
        build_source_views(args.home, args.target, args.user, scope=args.scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
