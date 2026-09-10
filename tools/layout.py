#!/usr/bin/env python3
"""Initialize the ZenFS hierarchy without replacing existing data."""

import argparse
import os
from pathlib import Path
import stat
import sys

from zenfsctl import ZenFSError, load_json_object, rename_noreplace


PRIVATE_DIRECTORIES = (
    "Apps", "Config", "Config/gnupg", "Config/ssh", "Legacy", "Live", "Local",
    "Mount", "Packages", "Packages/lib", "State", "State/nix/profiles", "State/shell",
)
XDG_DIRECTORIES = (
    "Desktop", "Documents", "Downloads", "Music", "Pictures", "Public", "Templates", "Videos",
)


def directory(path: Path, mode: int = 0o700) -> None:
    if path == path.parent:
        return
    # Never chmod or populate a destination through a symlinked parent.
    if path.parent.resolve() != path.parent:
        raise ZenFSError(f"directory parent contains a symlink: {path}")
    if not path.parent.exists():
        directory(path.parent, mode)
    if not os.path.lexists(path):
        path.mkdir(mode=mode)
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ZenFSError(f"refusing non-directory: {path}")
    path.chmod(mode)


def check_merge(source: Path, target: Path) -> None:
    if not os.path.lexists(source) or not os.path.lexists(target):
        return
    if source.is_dir() and not source.is_symlink() and target.is_dir() and not target.is_symlink():
        for child in source.iterdir():
            check_merge(child, target / child.name)
        return
    raise ZenFSError(f"refusing migration collision: {source} -> {target}")


def merge(source: Path, target: Path) -> None:
    if not os.path.lexists(source):
        return
    check_merge(source, target)
    directory(target.parent)
    if not os.path.lexists(target):
        rename_noreplace(source, target)
    else:
        for child in list(source.iterdir()):
            merge(child, target / child.name)
        source.rmdir()


def link(path: Path, target: Path) -> None:
    if path.is_symlink() and os.readlink(path) == str(target):
        return
    if os.path.lexists(path):
        raise ZenFSError(f"refusing to replace existing path: {path}")
    if path.parent.resolve() != path.parent:
        raise ZenFSError(f"link parent contains a symlink: {path}")
    path.symlink_to(target)


def initialize_home(home: Path, strict: bool = True) -> None:
    home = Path(os.path.abspath(home))
    directory(home)
    private = home / ".private"
    directory(private)
    for name in PRIVATE_DIRECTORIES:
        directory(private / name)
    migrations = [
        (".config", "Config"), (".cache", "Live"),
        (".local/lib", "Packages/lib"), (".local/share", "Packages"),
        (".local/state", "State"), (".local", "Local"),
        (".gnupg", "Config/gnupg"), (".ssh", "Config/ssh"),
        (".mozilla", "Config/mozilla"), (".bash_history", "State/shell/bash_history"),
        (".zsh_history", "State/shell/zsh_history"),
        (".python_history", "State/python/history"), (".lesshst", "State/less/history"),
    ]
    for old, new in migrations:
        source, target = home / old, private / new
        # .local itself may already be the compatibility link.
        if source.parent != home and source.parent.is_symlink():
            continue
        if source.is_symlink():
            if os.readlink(source) == str(target):
                source.unlink()
            elif strict:
                merge(source, private / "Legacy" / old.lstrip("."))
            else:
                raise ZenFSError(f"unexpected compatibility symlink: {source}")
        else:
            merge(source, target)
        if not strict and "/" not in old and old not in {".bash_history", ".zsh_history", ".python_history", ".lesshst"}:
            link(source, target)
    for name, target in (("lib", "Packages/lib"), ("share", "Packages"), ("state", "State")):
        link(private / "Local" / name, private / target)
    if strict:
        for source in sorted(home.iterdir()):
            # Flatpak's CLI and libflatpak use ~/.var/app independently of XDG.
            # Keep this exception until the complete stack redirects app data.
            if source.name.startswith(".") and source.name not in {".private", ".var"}:
                merge(source, private / "Legacy" / source.name[1:])
    for name in XDG_DIRECTORIES:
        directory(home / name, 0o750)
    # Setup's main.zcfg and any user-edited XDG settings remain authoritative.
    user_dirs = private / "Config/user-dirs.dirs"
    if not os.path.lexists(user_dirs):
        with user_dirs.open("x", encoding="utf-8") as stream:
            for name in XDG_DIRECTORIES:
                key = "DOWNLOAD" if name == "Downloads" else "PUBLICSHARE" if name == "Public" else name.upper()
                stream.write(f'XDG_{key}_DIR="$HOME/{name}"\n')
        user_dirs.chmod(0o600)


def initialize_hierarchy(manifest: dict, root: Path = Path("/")) -> None:
    if not manifest["directories"] and not manifest["aliases"] and not manifest.get("users"):
        return
    root = Path(os.path.abspath(root))
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise ZenFSError(f"hierarchy root is not a real directory: {root}")

    def at(path: str) -> Path:
        if not path.startswith("/") or ".." in Path(path).parts or path == "/":
            raise ZenFSError(f"unsafe hierarchy path: {path}")
        if path == "/Drives" or path.startswith("/Drives/"):
            raise ZenFSError("/Drives is outside ZenFS ownership")
        return root / path.lstrip("/")

    directories = {at(name) for name in manifest["directories"]}
    aliases = {at(name): Path(target) for name, target in manifest["aliases"].items()}
    removed = set()
    old_links = {}
    for name, old in (("/Live", "/run"), ("/Packages", "/nix/store")):
        path = at(name)
        if (path in directories or path in aliases) and path.is_symlink() and os.readlink(path) == old:
            old_links[path] = old
            removed.add(path)
    config = at("/Config")
    migrate_config = aliases.get(config) == Path("/etc") and config.is_dir() and not config.is_symlink()
    config_source = None
    if migrate_config:
        entries = list(config.iterdir())
        if any(entry.name != "ZenOS" for entry in entries):
            raise ZenFSError("refusing non-empty /Config outside the known ZenOS migration")
        if entries:
            config_source = config / "ZenOS"
            destination = at("/etc/ZenOS")
            if not destination.parent.is_dir() or destination.parent.resolve() != destination.parent:
                raise ZenFSError("refusing a non-directory or symlinked /etc migration parent")
            if os.path.lexists(destination):
                raise ZenFSError("refusing to merge both /Config/ZenOS and /etc/ZenOS")
            if config_source.lstat().st_dev != destination.parent.stat().st_dev:
                raise ZenFSError("refusing a cross-filesystem /Config migration")
        removed.add(config)
    users, old_home = at("/Users"), at("/home")
    migrate_home = users in directories and old_home.is_dir() and not old_home.is_symlink()
    old_users_alias = users.is_symlink() and os.readlink(users) == "/home"
    if migrate_home:
        if os.path.ismount(old_home):
            raise ZenFSError("refusing to migrate separately mounted /home")
        if os.path.lexists(users) and not old_users_alias:
            raise ZenFSError("refusing to merge both /home and /Users")
        if old_home.stat().st_dev != users.parent.stat().st_dev:
            raise ZenFSError("refusing a cross-filesystem /home migration")
        removed.update((users, old_home))

    # Check the complete projected layout before removing or migrating anything.
    # Children below a replaced legacy symlink do not yet exist in that layout.
    for path in directories | aliases.keys():
        for parent in path.parents:
            if parent == root:
                break
            if parent not in removed and parent not in directories and os.path.lexists(parent):
                if parent.is_symlink() or not parent.is_dir():
                    raise ZenFSError(f"refusing non-directory hierarchy parent: {parent}")
        if path in removed or any(parent in removed for parent in path.parents):
            continue
        if not os.path.lexists(path):
            continue
        if path in directories:
            if path.is_symlink() or not path.is_dir():
                raise ZenFSError(f"refusing non-directory hierarchy entry: {path}")
        elif not path.is_symlink() or os.readlink(path) != str(aliases[path]):
            raise ZenFSError(f"refusing to replace existing path: {path}")

    # NixOS account activation can chmod/chown these paths as root. Inspect each
    # existing component without following links, using /home's contents when
    # they are about to become /Users, before changing the root layout at all.
    for name, user in manifest.get("users", {}).items():
        home = at(user["home"])
        if migrate_home and (home == users or users in home.parents):
            home = old_home / home.relative_to(users)
        component = root
        for part in home.relative_to(root).parts:
            component /= part
            try:
                details = component.lstat()
            except FileNotFoundError:
                break
            if not stat.S_ISDIR(details.st_mode):
                raise ZenFSError(f"unsafe managed home for {name}: non-directory or symlink at {component}")

    if not directories and not aliases:
        return

    if migrate_home:
        if old_users_alias:
            users.unlink()
        try:
            rename_noreplace(old_home, users)
        except Exception:
            if old_users_alias and not os.path.lexists(users):
                users.symlink_to("/home")
            raise
    if migrate_config:
        if config_source is not None:
            rename_noreplace(config_source, at("/etc/ZenOS"))
        config.rmdir()
    for path, old in old_links.items():
        if not path.is_symlink() or os.readlink(path) != old:
            raise ZenFSError(f"legacy alias changed during activation: {path}")
        path.unlink()
    for name in manifest["directories"]:
        directory(at(name), 0o755)
    for name, target in manifest["aliases"].items():
        link(at(name), Path(target))
    hidden = at("/.hidden")
    if not os.path.lexists(hidden):
        with hidden.open("x", encoding="utf-8") as stream:
            stream.write("\n".join(manifest["hiddenRootEntries"]) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("/etc/zenfs/manifest.json"))
    parser.add_argument("--root", type=Path, default=Path("/"), help="alternate root for fixture or offline initialization")
    args = parser.parse_args()
    try:
        initialize_hierarchy(load_json_object(args.manifest), args.root)
    except (OSError, ZenFSError) as error:
        print(f"zenfs-layout: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
