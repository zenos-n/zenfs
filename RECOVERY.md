# ZenFS integration recovery

`package.nix` is the single package owner. It exports the existing `zenfs`,
`zenfs-fuse`, and `zenfs-janitor` commands together with `zenfs-mint`, `zenfsctl`,
`zenfs-layout`, `zenfs-session`, `zen-app-index`, `zen-app-launch`, `zen-app-icons`,
`zen-appimage`, `zen-flatpak`, and `zen-compat`. It also installs the Nautilus
extension, managed-app MIME definition, and launcher desktop entry.

## Provenance

- Publish this source to the checkout's existing origin, `doromiert/ZenFS`.
  The distinct `zenos-n/zenfs` repository below is provenance for recovered core
  scripts only, not the publication target for this combined package.
- `scripts/` is the pre-existing checkout and is unchanged.
- `recovered-core/` contains the exact scripts from the previously packaged
  `zenos-n/zenfs` revision `c0b7552f3111b5a4169f98a288ed6d98b78c4e9b`.
- `recovered/` contains app-index, Nautilus integration, zenfsctl, and their tests
  from `zenos-next` revision `c24244cbf`. Historical Nix package/module recipes
  are deliberately not imported. Subsequent fixes preserve private Flatpak
  paths, fix icon/AppImage package discovery, and refuse app-view collisions.
- `tools/` implements current safe initialization and session policy.

None of the old core daemons are started by the restored integration. They are
retained as explicit commands, not an offloading or drive-management policy.

## Composition contract

The ZenPkgs ZPKG should call this repository's `package.nix` and expose its sole
output at `pkgs.zenos.system.zenfs`. Do not retain a second compatibility recipe
owner or import this repository's historical NixOS modules.

Import the backend as a NixOS module:

```nix
import (zenpkgs + "/lib/zenfs-runtime.nix") {
  managedUsers = {
    alice = { home = "/Users/alice"; group = "users"; };
    zenos = { home = "/run/zenos-oobe"; group = "users"; };
  };
  # Optional override; defaults to pkgs.zenos.system.zenfs.
  package = pkgs.zenos.system.zenfs;
}
```

Composition derives these records from current account definitions. The backend
does not declare accounts. Enable `zenos.system.zenfs.enable`; public policy,
including `apps.appimage.runPackage`, `apps.flatpak.remote`, and typed optional
`roaming.drives`, is declared only in `modules/system/zenfs.zmdl`. The backend
lowers AppImage `runPackage` to the pinned NixOS `programs.appimage.package`.
Roaming private targets are relative to the supplied home and confined to
`.private/Mount/`. `/Drives` is not managed. No `NIX_PROFILE` is set.

The runtime leaves `.private/Config/main.zcfg` and existing XDG directory
configuration intact. Conflicting migration leaves are refused, not overwritten;
cross-filesystem moves are refused by `renameat2(RENAME_NOREPLACE)`.
Root activation preflights the complete layout before mutating paths, refuses
symlinked migration parents, and treats an empty hierarchy manifest as inert.
It validates every existing managed-home ancestor, including the source under
`/home` when that tree will become `/Users`. Home validation remains active when
root-layout creation is disabled. The backend's `|| exit 1` guard terminates
the NixOS activation shell on refusal, before its account activation snippet.
`zenfs-layout --root PATH --manifest FILE` permits fixture/offline initialization;
normal activation omits `--root` and therefore uses `/`.

## Flatpak limitation

The packaged upstream Flatpak 1.16.6 CLI and libflatpak use `~/.var/app` for
application data independently of `XDG_DATA_HOME`. Strict initialization now
preserves `.var` unchanged, including an existing symlink, rather than moving
it into Legacy. No new compatibility symlink or HOME-changing wrapper is used.
Private XDG installation/export paths do not imply private application data.

Full app-data relocation is not implemented. No patched `passthru.flatpak` or
global overlay is supplied. Such integration must cover launch/data-directory
creation, persistent filesystem mappings, dconf migration, uninstall cleanup,
and all CLI/libflatpak consumers together before migrating existing data.
Data already moved by an older implementation into `.private/Legacy/var` is
not automatically restored or merged; this change only preserves current `.var`.

## Verification hooks

Runtime tests must be run in the independent ZenOS VM, not the host or the
user's active VM. The package includes `libexec/zenfs/run-tests`, which enforces
a VM guard and runs all recovered suites plus new migration/session tests.
It does not activate services, install Flatpaks, or modify the VM root hierarchy:
filesystem tests use temporary fixtures and remote operations use fake runners.
The hook contains 84 test cases: 53 recovered cases (with safety expectations
updated where behavior was hardened), 15 initial migration/integration cases,
10 root-activation/multiple-home regression cases, and 6 publish-safety cases.
They cover two normal homes and an OOBE-shaped home without modifying any real
user home. The real Flatpak cleanup-path probe only deletes its own temporary
fixture, blocks access to the actual session bus, and performs no network install.

`zenpkgs/tests/zenfs-runtime.nix` accepts `nixpkgsPath`, a compiler-produced
`zenfsModule`, and optionally `zenfsPackage`. Every returned boolean must be
true. It checks typed options, disabled behavior, service ordering, OOBE users,
AppImage/MIME/Flatpak wiring, mount restrictions, and absent unsafe services.
For a standalone evaluation, compile the public module with the current
`zen-dsl compile modules/system/zenfs.zmdl --root . --output /tmp/zenfs-module.nix`
from the ZenPkgs checkout, then pass that output as `zenfsModule` to the check.
The 22 checks include per-account ownership/modes (including a non-default
group), user-manager execution without root impersonation, and absence of
automatically scheduled core daemons.

`zenpkgs/tests/zenfs-activation.nix` builds a real NixOS-generated activation
shell with a fixture-root package proxy, no unrelated activation snippets, and
a fake `users` sentinel action. Run `tests/activation_shell.py SCRIPT` in the VM
against that output: all 3 cases must pass. This tests shell failure propagation
for current and migrated-home symlinks as well as the successful control case.
The fake users action exits before NixOS's real `/run/current-system` postamble.

Graphical acceptance still requires checking Nautilus source navigation, icons,
ordinary and managed launcher activation, and real AppImage/Flatpak execution
in the independent VM after composition installs the package.

## Android and recorder compatibility homes

ZenPkgs wraps adb35.0.2 (which ignores ANDROID_USER_HOME) with HOME under
.private/Packages/android-tools and a private .android link to Config/android.
Existing pairing keys migrate to Config/android unchanged. Restart an existing
ADB server after migration, since it retains the environment it launched with.
GPU Screen Recorder5.13.8 installs an NVIDIA performance profile unconditionally;
its command HOME is .private/Packages/gpu-screen-recorder. Old .nv data migrates
there, including custom profiles. These environment wrappers preserve the
working directory, other environment variables and full filesystem access.
They activate only for a home containing .private. No visible home symlinks
are created. GUI/XDG settings and absolute recording output paths stay shared.

## Native desktop launchers

User activation exports profile .desktop links into the stable real directory
.private/Packages/applications. GNOME/GLib can miss changes behind a switched
Nix profile symlink. Stable-directory entry replacement emits a discovery event
without requiring a desktop restart. Desktop IDs stay unchanged. The private
State/zenos/desktop-exports.json manifest records ownership; user overrides are
preserved, updates replace only owned links, and removals remove only owned links.
Only top-level profile .desktop entries are exported. /Apps remains separate.

Android build analytics31.13.2 (AGP8.13.2) uses a separate legacy path reader:
AnalyticsPaths ignores ANDROID_USER_HOME and reads ANDROID_PREFS_ROOT directly,
falling back to Java user.home/.android. Session initialization sets both
variables to Config/android. This is distinct from the ADB HOME workaround.
Modern AndroidLocations prefers ANDROID_USER_HOME; do not set ANDROID_SDK_HOME
to the SDK location as a preferences workaround. Already running build processes
retain their environment; apply the variables to subsequent build launches.
