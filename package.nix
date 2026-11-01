{
  lib,
  stdenvNoCC,
  python3,
  makeWrapper,
  libnotify,
  util-linux,
  fuse,
  bubblewrap,
  squashfsTools,
  flatpak,
  coreutils,
  xdg-user-dirs,
  glib,
  desktop-file-utils,
}:
let
  python = python3.withPackages (ps: [ ps.psutil ps.watchdog ps.fusepy ps.mutagen ps.pygobject3 ]);
  runtimePath = lib.makeBinPath [ libnotify util-linux fuse bubblewrap squashfsTools flatpak coreutils xdg-user-dirs desktop-file-utils ];
in
stdenvNoCC.mkDerivation {
  pname = "zenfs";
  version = "2.1.0";
  src = lib.cleanSource ./.;
  nativeBuildInputs = [ makeWrapper ];
  dontBuild = true;
  installPhase = ''
    runHook preInstall
    mkdir -p "$out/bin" "$out/libexec/zenfs"
    cp -r scripts recovered-core recovered tools "$out/libexec/zenfs/"
    core="$out/libexec/zenfs/recovered-core/scripts/core"
    janitor="$out/libexec/zenfs/recovered-core/scripts/janitor"
    apps="$out/libexec/zenfs/recovered/packages/app-index"
    nautilus="$out/libexec/zenfs/recovered/packages/nautilus-apps"
    control="$out/libexec/zenfs/recovered/packages/zenfsctl"
    tools="$out/libexec/zenfs/tools"
    makeWrapper ${python}/bin/python3 "$out/bin/zenfs" \
      --add-flags "$core/dispatcher.py" --set PYTHONPATH "$core" --prefix PATH : ${runtimePath}
    makeWrapper ${python}/bin/python3 "$out/bin/zenfs-fuse" \
      --add-flags "$core/fuse_fs.py" --set PYTHONPATH "$core" --prefix PATH : ${runtimePath}
    makeWrapper ${python}/bin/python3 "$out/bin/zenfs-janitor" \
      --add-flags "$janitor/dispatcher.py" --set PYTHONPATH "$janitor:$core" --prefix PATH : ${runtimePath}
    makeWrapper ${python}/bin/python3 "$out/bin/zenfs-mint" \
      --add-flags "$out/libexec/zenfs/scripts/user/mint.py" --prefix PATH : ${runtimePath}
    for pair in zenfsctl:zenfsctl.py zen-app-index:app_index.py zen-appimage:appimage.py zen-flatpak:flatpak.py zen-compat:compat.py zen-app-launch:zen_app_launch.py zen-app-icons:zen_app_icons.py zenfs-layout:layout.py zenfs-session:session.py; do
      name="''${pair%%:*}"
      file="''${pair#*:}"
      case "$name" in
        zenfsctl) source="$control/$file" ;;
        zen-app-launch|zen-app-icons) source="$nautilus/$file" ;;
        zenfs-layout|zenfs-session) source="$tools/$file" ;;
        *) source="$apps/$file" ;;
      esac
      makeWrapper ${python}/bin/python3 "$out/bin/$name" \
        --add-flags "$source" --set PYTHONPATH "$apps:$nautilus:$control:$tools" \
        --prefix GI_TYPELIB_PATH : "${lib.getLib glib}/lib/girepository-1.0" \
        --prefix PATH : "$out/bin:${runtimePath}"
    done
    install -Dm644 "$nautilus/zenos_apps.py" "$out/share/nautilus-python/extensions/zenos_apps.py"
    install -Dm644 "$nautilus/zenos_icon_metadata.py" "$out/share/nautilus-python/extensions/zenos_icon_metadata.py"
    substituteInPlace "$out/share/nautilus-python/extensions/zenos_apps.py" \
      --replace-fail '@zen_app_launch@' "$out/bin/zen-app-launch" \
      --replace-fail '@zen_appimage@' "$out/bin/zen-appimage"
    install -Dm644 "$nautilus/zenos-managed-desktop.xml" "$out/share/mime/packages/zenos-managed-desktop.xml"
    install -Dm644 ${./tools/com.negzero.zenos.AppLauncher.desktop} "$out/share/applications/com.negzero.zenos.AppLauncher.desktop"
    substituteInPlace "$out/share/applications/com.negzero.zenos.AppLauncher.desktop" \
      --replace-fail '@launcher@' "$out/bin/zen-app-launch"
    install -Dm755 ${./tests/run-vm.sh} "$out/libexec/zenfs/run-tests"
    wrapProgram "$out/libexec/zenfs/run-tests" --set ZENFS_TEST_PYTHON ${python}/bin/python3 \
      --set ZENFS_TEST_FLATPAK ${flatpak}/bin/flatpak \
      --prefix PATH : ${lib.makeBinPath [ util-linux coreutils ]}
    cp -r tests "$out/libexec/zenfs/tests"
    runHook postInstall
  '';
  # Runtime acceptance is invoked explicitly inside a ZenOS VM, never at build time.
  passthru.testCommand = "libexec/zenfs/run-tests";
  meta = {
    description = "ZenFS core tools, safe hierarchy initialization, and scoped application integration";
    license = lib.licenses.napl or lib.licenses.unfree;
    platforms = lib.platforms.linux;
    mainProgram = "zenfsctl";
  };
}
