#!/usr/bin/env python3
"""Seed Nautilus icon metadata from a validated ZenOS app registry."""

from __future__ import annotations

import configparser
import json
import os
from pathlib import Path
import stat
import sys

from app_registry import REGISTRY_SCHEMA, valid_token


MAX_REGISTRY_BYTES = 1024 * 1024
MAX_LAUNCHER_BYTES = 4 * 1024 * 1024


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


def _simple_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value not in {".", ".."}
        and Path(value).name == value
        and "/" not in value
        and "\\" not in value
    )


def _entry(path: Path) -> dict[str, str]:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(_read_regular(path, MAX_LAUNCHER_BYTES).decode("utf-8"))
    return {
        option.strip().casefold(): value.strip()
        for option, value in parser["Desktop Entry"].items()
    }


def icon_updates(root: Path) -> list[tuple[Path, str, str]]:
    root = Path(os.path.abspath(root))
    details = os.lstat(root)
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError(f"not a real Apps directory: {root}")
    registry = json.loads(
        _read_regular(root / ".zenos-app-registry.json", MAX_REGISTRY_BYTES)
    )
    applications = registry.get("applications")
    if (
        registry.get("schema") != REGISTRY_SCHEMA
        or not isinstance(applications, list)
        or len(applications) > 10000
    ):
        raise ValueError("invalid application registry")

    updates = []
    for record in applications:
        if not isinstance(record, dict) or not valid_token(record.get("token")):
            raise ValueError("invalid application registry record")
        launcher_name = record.get("launcher")
        if not _simple_basename(launcher_name):
            raise ValueError("invalid application launcher name")
        launcher = root / launcher_name
        metadata = _entry(launcher)
        if (
            metadata.get("x-zenos-managed", "").casefold() != "true"
            or metadata.get("x-zenos-indexversion") != "4"
            or metadata.get("x-zenos-apptoken") != record["token"]
        ):
            raise ValueError("launcher does not match the application registry")
        icon_file = metadata.get("x-zenos-iconfile")
        if icon_file:
            icon_path = Path(icon_file)
            try:
                icon_details = os.lstat(icon_path)
            except OSError:
                continue
            if (
                icon_path.is_absolute()
                and stat.S_ISREG(icon_details.st_mode)
                and not stat.S_ISLNK(icon_details.st_mode)
            ):
                updates.append(
                    (launcher, "metadata::custom-icon", icon_path.as_uri())
                )
                continue
        icon_name = metadata.get("icon")
        if icon_name and _simple_basename(icon_name):
            updates.append((launcher, "metadata::custom-icon-name", icon_name))
    return updates


def seed_icons(root: Path) -> int:
    from gi.repository import Gio, GLib

    changed = 0
    for launcher, attribute, value in icon_updates(root):
        try:
            Gio.File.new_for_path(str(launcher)).set_attribute_string(
                attribute,
                value,
                Gio.FileQueryInfoFlags.NONE,
                None,
            )
            changed += 1
        except GLib.Error:
            continue
    return changed


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    try:
        seed_icons(Path(sys.argv[1]))
    except (configparser.Error, KeyError, OSError, UnicodeError, ValueError) as error:
        print(f"zen-app-icons: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
