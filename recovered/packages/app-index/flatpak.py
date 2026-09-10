#!/usr/bin/env python3
"""Manage user-scoped Flatpak applications and refresh the ZenOS app index."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

from app_index import build_source_views


APP_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
APP_REF = re.compile(
    r"^app/[A-Za-z0-9][A-Za-z0-9._-]*/(?:x86_64|aarch64)/[A-Za-z0-9._-]+$"
)
REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SCOPES = ("user", "system")


def private_environment(home: Path) -> dict[str, str]:
    return dict(
        os.environ,
        XDG_DATA_HOME=str(home / ".private/Packages"),
        XDG_CONFIG_HOME=str(home / ".private/Config"),
        XDG_CACHE_HOME=str(home / ".private/Live"),
        XDG_STATE_HOME=str(home / ".private/State"),
    )


def validate_app(value: str) -> str:
    if APP_ID.fullmatch(value) is None and APP_REF.fullmatch(value) is None:
        raise ValueError(f"invalid Flatpak application identifier: {value!r}")
    return value


def validate_remote(value: str) -> str:
    if REMOTE.fullmatch(value) is None:
        raise ValueError(f"invalid Flatpak remote: {value!r}")
    return value


def refresh_index(home: Path, target: Path, user: str | None = None) -> None:
    build_source_views(home, target, user, scope="user")


def install(
    application: str,
    remote: str | None = None,
    *,
    home: Path = Path.home(),
    target: Path | None = None,
    user: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    application = validate_app(application)
    remote = validate_remote(
        remote or os.environ.get("ZENOS_FLATPAK_REMOTE", "zenos-flathub")
    )
    run(
        [
            "flatpak",
            "install",
            "--user",
            "--noninteractive",
            remote,
            application,
        ],
        check=True,
        text=True,
        env=private_environment(home),
    )
    refresh_index(home, target or home / ".private/Apps", user)


def remove(
    application: str,
    *,
    home: Path = Path.home(),
    target: Path | None = None,
    user: str | None = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    application = validate_app(application)
    run(
        [
            "flatpak",
            "uninstall",
            "--user",
            "--noninteractive",
            application,
        ],
        check=True,
        text=True,
        env=private_environment(home),
    )
    refresh_index(home, target or home / ".private/Apps", user)


def list_installed(
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    *,
    scope: str = "user",
) -> list[dict[str, object]]:
    if scope not in (*SCOPES, "all"):
        raise ValueError(f"invalid Flatpak scope: {scope!r}")
    if scope == "all":
        return [
            application
            for installation in SCOPES
            for application in list_installed(run, scope=installation)
        ]
    result = run(
        [
            "flatpak",
            "list",
            f"--{scope}",
            "--app",
            "--columns=application,ref,origin",
        ],
        check=True,
        text=True,
        capture_output=True,
        env=private_environment(Path.home()),
    )
    applications = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) != 3 or APP_ID.fullmatch(fields[0]) is None:
            raise ValueError("Flatpak returned an invalid application record")
        applications.append(
            {
                "application": fields[0],
                "ref": fields[1],
                "origin": fields[2],
                "scope": scope,
                "removable": scope == "user",
            }
        )
    return applications


def inspect(
    application: str,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    application = validate_app(application)
    installations = [
        record
        for record in list_installed(run, scope="all")
        if record["application"] == application
    ]
    return {"application": application, "installations": installations}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--target", type=Path)
    parser.add_argument("--user")
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install")
    install_parser.add_argument("application")
    install_parser.add_argument("--remote")
    remove_parser = commands.add_parser("remove")
    remove_parser.add_argument("application")
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--scope", choices=(*SCOPES, "all"), default="user")
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("application")
    args = parser.parse_args()

    try:
        if args.command == "install":
            install(
                args.application,
                args.remote,
                home=args.home,
                target=args.target,
                user=args.user,
            )
        elif args.command == "remove":
            remove(
                args.application,
                home=args.home,
                target=args.target,
                user=args.user,
            )
        elif args.command == "list":
            print(json.dumps(list_installed(scope=args.scope), indent=2))
        else:
            print(json.dumps(inspect(args.application), indent=2))
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        print(f"zen-flatpak: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
