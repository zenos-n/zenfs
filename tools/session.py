#!/usr/bin/env python3
"""Managed-user session initialization and application policy."""

import argparse
import base64
import configparser
import os
from pathlib import Path
import pwd
import shlex
import subprocess
import sys
import tempfile

from app_index import build_source_views
from flatpak import validate_remote
from layout import initialize_home
from zenfsctl import ZenFSError, command_verify_marker, load_json_object


def managed_identity(manifest, *, username=None, home=None):
    username = username if username is not None else pwd.getpwuid(os.getuid()).pw_name
    home = Path(home if home is not None else os.environ.get("HOME", "/var/empty"))
    user = manifest["users"].get(username)
    if user is None or home.resolve() != Path(user["home"]):
        return None
    return username, Path(user["home"])


def environment(home):
    private = home / ".private"
    paths = {
        "XDG_CACHE_HOME": "Live", "XDG_CONFIG_HOME": "Config",
        "XDG_DATA_HOME": "Packages", "XDG_STATE_HOME": "State",
        "ANDROID_USER_HOME": "Config/android", "ANDROID_EMULATOR_HOME": "Config/android",
        "ANDROID_AVD_HOME": "Packages/android/avd", "GRADLE_USER_HOME": "Packages/gradle",
        "CARGO_HOME": "Packages/cargo", "GIT_CONFIG_GLOBAL": "Config/git/config",
        "CODEX_HOME": "Packages/codex",
        "ZDOTDIR": "Config/zsh",
        "GNUPGHOME": "Config/gnupg", "GOPATH": "Packages/go",
        "HISTFILE": "State/shell/history", "LESSHISTFILE": "State/less/history",
        "NPM_CONFIG_USERCONFIG": "Config/npm/npmrc", "NPM_CONFIG_CACHE": "Live/npm",
        "MESA_SHADER_CACHE_DIR": "Live/mesa_shader_cache",
        "PASSWORD_STORE_DIR": "Packages/password-store",
        "PYTHON_HISTORY": "State/python/history", "RUSTUP_HOME": "Packages/rustup",
        "ZENOS_USER_APPS_DIR": "Apps",
    }
    result = {name: str(private / path) for name, path in paths.items()}
    # Gradle's mavenLocal() also reads Maven's JVM repository property.
    repository = private / "Packages/maven/.m2/repository"
    properties = {
        "GRADLE_OPTS": {"maven.repo.local": repository},
        "MAVEN_OPTS": {"maven.repo.local": repository, "user.home": private / "Packages/maven"},
    }
    for name, values in properties.items():
        arguments = shlex.split(os.environ.get(name, ""))
        arguments = [argument for argument in arguments if not any(
            argument.startswith(f"-D{key}=") for key in values
        )]
        result[name] = shlex.join(arguments + [f"-D{key}={value}" for key, value in values.items()])
    return result


def reconcile_flatpak(policy, scope, *, run=subprocess.run):
    if not policy["enable"]:
        return
    name = validate_remote(policy["name"])
    descriptor = Path(policy["descriptor"])
    parser = configparser.ConfigParser(interpolation=None)
    with descriptor.open(encoding="utf-8") as stream:
        parser.read_file(stream)
    remote = parser["Flatpak Repo"]
    url = remote["Url"]
    if not url.startswith("https://"):
        raise ValueError("the owned Flatpak remote requires an HTTPS URL")
    key = base64.b64decode(remote["GPGKey"], validate=True)
    if not key:
        raise ValueError("the Flatpak remote has no signing key")
    run(["flatpak", "remote-add", f"--{scope}", "--if-not-exists", "--from", name, str(descriptor)], check=True)
    with tempfile.NamedTemporaryFile() as keyfile:
        keyfile.write(key)
        keyfile.flush()
        run(["flatpak", "remote-modify", f"--{scope}", "--gpg-verify", f"--gpg-import={keyfile.name}", f"--url={url}", name], check=True)


def verify_drive(drive):
    root = Path(drive["mountPoint"])
    if not os.path.ismount(root):
        raise ZenFSError(f"roaming drive is not mounted: {root}")
    canonical = root.resolve(strict=True)
    marker = canonical / drive["markerFile"]
    if marker.resolve(strict=True) != marker:
        raise ZenFSError("roaming marker contains a symlink")
    command_verify_marker(argparse.Namespace(path=marker, id=drive["markerId"]))
    for mount in drive["privateMounts"]:
        source = canonical / Path(mount["sourcePath"]).relative_to(root)
        target = Path(mount["targetPath"])
        for path in (source, target):
            if not path.is_dir() or path.resolve(strict=True) != path:
                raise ZenFSError(f"roaming bind path is not a real directory: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("/etc/zenfs/manifest.json"))
    parser.add_argument("action", choices=("environment", "init", "index", "flatpak-user", "flatpak-system", "verify-drive"))
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--drive")
    args = parser.parse_args()
    try:
        manifest = load_json_object(args.manifest)
        if args.action == "verify-drive":
            verify_drive(manifest["roaming"][args.drive])
            return 0
        if args.action == "flatpak-system":
            reconcile_flatpak(manifest["flatpak"], "system")
            return 0
        identity = managed_identity(manifest)
        if identity is None:
            return 0
        username, home = identity
        values = environment(home)
        if args.action == "environment":
            for name, value in values.items():
                print(f"export {name}={shlex.quote(value)}" if args.shell else f"{name}={value}")
            return 0
        os.environ.update(values)
        if args.action == "init":
            initialize_home(home, manifest["strict"])
        elif args.action == "index":
            build_source_views(home, home / ".private/Apps", username, scope="user")
            for target in (Path("/Apps"), home / ".private/Apps"):
                if target.is_dir():
                    subprocess.run(["zen-app-icons", str(target)], check=True)
        else:
            reconcile_flatpak(manifest["flatpak"], "user")
    except (OSError, ValueError, KeyError, configparser.Error, ZenFSError, subprocess.CalledProcessError) as error:
        print(f"zenfs-session: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
