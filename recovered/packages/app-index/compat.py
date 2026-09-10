#!/usr/bin/env python3
"""Manage per-application compatibility settings and synthetic homes."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
from typing import NoReturn, Sequence

from app_registry import valid_token


SETTINGS_SCHEMA = 1
SHAREABLE_DIRECTORIES = frozenset(
    {
        "Desktop",
        "Documents",
        "Downloads",
        "Music",
        "Pictures",
        "Public",
        "Templates",
        "Videos",
    }
)


def app_state(home: Path, token: str) -> Path:
    if not valid_token(token):
        raise ValueError("invalid application token")
    return home / ".private/State/zenos/apps" / token


def default_settings() -> dict[str, object]:
    return {
        "schema": SETTINGS_SCHEMA,
        "syntheticHome": False,
        "sharedDirectories": [],
    }


def _validate_settings(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("schema") != SETTINGS_SCHEMA:
        raise ValueError("unsupported compatibility settings schema")
    synthetic_home = value.get("syntheticHome")
    shared = value.get("sharedDirectories")
    if not isinstance(synthetic_home, bool):
        raise ValueError("syntheticHome must be a boolean")
    if (
        not isinstance(shared, list)
        or not all(isinstance(name, str) and name in SHAREABLE_DIRECTORIES for name in shared)
        or len(shared) != len(set(shared))
    ):
        raise ValueError("sharedDirectories contains an invalid directory")
    return {
        "schema": SETTINGS_SCHEMA,
        "syntheticHome": synthetic_home,
        "sharedDirectories": sorted(shared),
    }


def load_settings(home: Path, token: str) -> dict[str, object]:
    path = app_state(home, token) / "compatibility.json"
    try:
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
            raise ValueError("compatibility settings are not a regular file")
        return _validate_settings(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return default_settings()
    except (json.JSONDecodeError, OSError, UnicodeError) as error:
        raise ValueError("cannot read compatibility settings") from error


def save_settings(home: Path, token: str, settings: object) -> dict[str, object]:
    validated = _validate_settings(settings)
    directory = app_state(home, token)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".compatibility-", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(validated, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, directory / "compatibility.json")
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return validated


def configure(
    home: Path,
    token: str,
    *,
    synthetic_home: bool | None = None,
    shared_directories: Sequence[str] | None = None,
) -> dict[str, object]:
    settings = load_settings(home, token)
    if synthetic_home is not None:
        settings["syntheticHome"] = synthetic_home
    if shared_directories is not None:
        settings["sharedDirectories"] = list(shared_directories)
    return save_settings(home, token, settings)


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise ValueError(f"compatibility path is not a real directory: {path}")
    os.chmod(path, 0o700)


def sandbox_command(
    home: Path,
    token: str,
    command: Sequence[str],
    settings: dict[str, object],
    *,
    runtime_directory: Path,
    bwrap: str = "bwrap",
) -> list[str]:
    settings = _validate_settings(settings)
    if not settings["syntheticHome"]:
        return list(command)
    if not command:
        raise ValueError("no application command was provided")

    private = home / ".private"
    if not private.is_dir() or private.is_symlink():
        raise ValueError("the ZenFS private directory is unavailable")

    state = app_state(home, token)
    synthetic_home = state / "home"
    _private_directory(synthetic_home)
    _private_directory(synthetic_home / ".private")

    arguments = [
        bwrap,
        "--ro-bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--proc",
        "/proc",
        "--bind",
        "/tmp",
        "/tmp",
        "--bind",
        str(runtime_directory),
        str(runtime_directory),
        "--bind",
        str(synthetic_home),
        str(home),
        "--bind",
        str(private),
        str(home / ".private"),
    ]

    for name in settings["sharedDirectories"]:
        source = home / name
        if not source.is_dir() or source.is_symlink():
            continue
        destination = synthetic_home / name
        _private_directory(destination)
        arguments.extend(["--bind", str(source), str(home / name)])

    arguments.extend(
        [
            "--chdir",
            str(home),
            "--setenv",
            "HOME",
            str(home),
            "--setenv",
            "ZENOS_APP_TOKEN",
            token,
            "--",
            *command,
        ]
    )
    return arguments


def execute(
    home: Path,
    token: str,
    command: Sequence[str],
    *,
    environ: dict[str, str] = os.environ,
) -> NoReturn:
    settings = load_settings(home, token)
    if not settings["syntheticHome"]:
        os.execvp(command[0], list(command))
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        raise ValueError("bubblewrap is required for synthetic homes")
    runtime = environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        raise ValueError("XDG_RUNTIME_DIR is required for synthetic homes")
    arguments = sandbox_command(
        home,
        token,
        command,
        settings,
        runtime_directory=Path(runtime),
        bwrap=bwrap,
    )
    os.execvp(arguments[0], arguments)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("show")
    show.add_argument("token")
    configure_parser = commands.add_parser("configure")
    configure_parser.add_argument("token")
    mode = configure_parser.add_mutually_exclusive_group()
    mode.add_argument("--synthetic-home", action="store_true", dest="synthetic_home")
    mode.add_argument("--no-synthetic-home", action="store_false", dest="synthetic_home")
    configure_parser.set_defaults(synthetic_home=None)
    sharing = configure_parser.add_mutually_exclusive_group()
    sharing.add_argument(
        "--share", action="append", choices=sorted(SHAREABLE_DIRECTORIES)
    )
    sharing.add_argument("--clear-shares", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("token")
    run.add_argument("application", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    try:
        if args.command == "show":
            settings = load_settings(args.home, args.token)
        elif args.command == "configure":
            settings = configure(
                args.home,
                args.token,
                synthetic_home=args.synthetic_home,
                shared_directories=[] if args.clear_shares else args.share,
            )
        else:
            application = args.application
            if application and application[0] == "--":
                application = application[1:]
            if not application:
                parser.error("run requires an application command after --")
            execute(args.home, args.token, application)
        if args.command != "run":
            print(json.dumps(settings, indent=2))
    except (OSError, ValueError) as error:
        print(f"zen-compat: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
