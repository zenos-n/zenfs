"""Publish profile launchers where running desktops can observe profile changes."""
import json
import os
from pathlib import Path
import secrets
import subprocess

from app_index import _atomic_write, _read_regular_at, simple_basename
from layout import directory


def export_desktop_entries(home: Path, profile: Path) -> None:
    profile = profile.resolve(strict=True)
    target = home / '.private/Packages/applications'
    state = home / '.private/State/zenos'
    directory(target)
    directory(state)
    fd = os.open(state, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        raw = _read_regular_at(fd, 'desktop-exports.json', 1024 * 1024)
    finally:
        os.close(fd)
    previous = json.loads(raw) if raw is not None else {}
    if not isinstance(previous, dict) or not all(
        simple_basename(name) and name.endswith('.desktop') and isinstance(source, str)
        for name, source in previous.items()
    ):
        raise ValueError('invalid desktop export ownership manifest')
    entries = profile / 'share/applications'
    current = {p.name: str(p) for p in entries.glob('*.desktop') if p.is_file()}
    owned = {}
    for name in previous.keys() | current.keys():
        link = target / name
        ours = link.is_symlink() and os.readlink(link) == previous.get(name)
        # Unmanaged launchers and user-modified links remain authoritative.
        if os.path.lexists(link) and not ours:
            continue
        source = current.get(name)
        if source is None:
            if ours:
                link.unlink()
            continue
        if not ours or os.readlink(link) != source:
            temporary = target / ('.zenfs-desktop-' + secrets.token_hex(12))
            try:
                temporary.symlink_to(source)
                os.replace(temporary, link)
            finally:
                temporary.unlink(missing_ok=True)
        owned[name] = source
    _atomic_write(state, 'desktop-exports.json', json.dumps(owned, sort_keys=True).encode(), 0o600)
    subprocess.run(['update-desktop-database', '--quiet', str(target)], check=True)
