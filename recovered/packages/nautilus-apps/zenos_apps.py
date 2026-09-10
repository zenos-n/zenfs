"""Nautilus integration for source-aware ZenOS application views."""

from __future__ import annotations

import configparser
import os
from pathlib import Path
import re
import stat
from typing import List
from urllib.parse import unquote, urlparse

from gi.repository import Gio, GLib, GObject, Gtk, Nautilus


SYSTEM_APPS_ROOT = Path("/Apps")
USER_APPS_ROOT = Path(
    os.environ.get("ZENOS_USER_APPS_DIR", str(Path.home() / ".private/Apps"))
)
APPS_ROOTS = (SYSTEM_APPS_ROOT, USER_APPS_ROOT)
APP_TOKEN = re.compile(r"^[0-9a-f]{64}$")
ZEN_APP_LAUNCH = "@zen_app_launch@"
ZEN_APPIMAGE = "@zen_appimage@"
SOURCE_VIEW_SPECS = (
    ("All Sources", None),
    ("Nix (Config)", "nix-config"),
    ("Nix (Imperative)", "nix-imperative"),
    ("Flatpak", "flatpak"),
    ("Manually Installed", "manual"),
)


def source_views(root: Path) -> tuple[tuple[str, Path], ...]:
    return tuple(
        (label, root if source is None else root / ".sources" / source)
        for label, source in SOURCE_VIEW_SPECS
    )


def apps_root_for_view(path: Path) -> Path | None:
    for root in APPS_ROOTS:
        if path in {view_path for _label, view_path in source_views(root)}:
            return root
    return None


def apps_view(path: Path) -> bool:
    return apps_root_for_view(path) is not None


def launcher_path(file_info: Nautilus.FileInfo) -> Path | None:
    if file_info.get_uri_scheme() != "file":
        return None
    path = Path(unquote(urlparse(file_info.get_uri()).path))
    try:
        details = os.lstat(path)
    except OSError:
        return None
    if (
        not apps_view(path.parent)
        or not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
    ):
        return None
    return path


def appimage_path(file_info: Nautilus.FileInfo) -> Path | None:
    if file_info.get_uri_scheme() != "file":
        return None
    path = Path(unquote(urlparse(file_info.get_uri()).path))
    try:
        details = os.lstat(path)
    except OSError:
        return None
    if (
        path.suffix.lower() != ".appimage"
        or not stat.S_ISREG(details.st_mode)
        or stat.S_ISLNK(details.st_mode)
    ):
        return None
    return path


def desktop_details(
    path: Path,
) -> tuple[Gio.DesktopAppInfo | None, str | None, str | None, str | None]:
    app = Gio.DesktopAppInfo.new_from_filename(str(path))
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    try:
        parser.read(path, encoding="utf-8")
        metadata = {
            option.strip().casefold(): value.strip()
            for option, value in parser["Desktop Entry"].items()
        }
        if (
            metadata.get("x-zenos-managed", "").casefold() != "true"
            or metadata.get("x-zenos-indexversion") != "4"
            or APP_TOKEN.fullmatch(metadata.get("x-zenos-apptoken", "")) is None
            or metadata.get("x-zenos-source")
            not in {
                "nix-config",
                "nix-imperative",
                "flatpak",
                "manual",
            }
        ):
            return None, None, None, None
        package = metadata.get("x-zenos-package")
        icon_file = metadata.get("x-zenos-iconfile")
        token = metadata["x-zenos-apptoken"]
        if package and (not Path(package).is_absolute() or not Path(package).is_dir()):
            package = None
        if icon_file and (
            not Path(icon_file).is_absolute() or not Path(icon_file).is_file()
        ):
            icon_file = None
    except (configparser.Error, KeyError, OSError, UnicodeError):
        package = None
        icon_file = None
        token = None
    return app, package, icon_file, token


class ZenOSAppsExtension(
    GObject.GObject,
    Nautilus.InfoProvider,
    Nautilus.MenuProvider,
):
    def update_file_info(self, file_info: Nautilus.FileInfo) -> None:
        path = launcher_path(file_info)
        if path is None:
            return
        app, package, icon_file, _token = desktop_details(path)
        if app is None:
            return

        icon = app.get_string("Icon")
        custom_icon = Path(icon_file).as_uri() if icon_file else None
        if custom_icon or icon:
            try:
                location = file_info.get_location()
                attribute = (
                    "metadata::custom-icon"
                    if custom_icon
                    else "metadata::custom-icon-name"
                )
                value = custom_icon or icon
                current = location.query_info(
                    attribute,
                    Gio.FileQueryInfoFlags.NONE,
                    None,
                ).get_attribute_string(attribute)
                if current != value:
                    location.set_attribute_string(
                        attribute,
                        value,
                        Gio.FileQueryInfoFlags.NONE,
                        None,
                    )
            except GLib.Error:
                pass
        file_info.add_string_attribute("zenos_app_name", app.get_name())
        if package:
            file_info.add_string_attribute("zenos_package", package)

    def _launch(self, _item: Nautilus.MenuItem, token: str, root: Path) -> None:
        Gio.Subprocess.new(
            [ZEN_APP_LAUNCH, "--apps-root", str(root), "--token", token],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
        )

    def _open_package(self, _item: Nautilus.MenuItem, package: str) -> None:
        Gio.AppInfo.launch_default_for_uri(Path(package).as_uri(), None)

    def _install_appimage(self, _item: Nautilus.MenuItem, path: Path) -> None:
        Gio.Subprocess.new(
            [ZEN_APPIMAGE, "install", str(path)],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE,
        )

    def _navigate(self, _button: Gtk.ToggleButton, path: Path, window: object) -> None:
        location = Gio.File.new_for_path(str(path))
        if hasattr(window, "open_location"):
            for arguments in ((location, 0, None), (location, 0), (location,)):
                try:
                    window.open_location(*arguments)
                    return
                except TypeError:
                    continue
        Gio.AppInfo.launch_default_for_uri(location.get_uri(), None)

    def get_file_items(self, files: List[Nautilus.FileInfo]) -> List[Nautilus.MenuItem]:
        if len(files) != 1:
            return []
        image = appimage_path(files[0])
        if image is not None:
            install = Nautilus.MenuItem(
                name="ZenOSApps::InstallAppImage",
                label="Install AppImage",
            )
            install.connect("activate", self._install_appimage, image)
            return [install]
        path = launcher_path(files[0])
        if path is None:
            return []
        root = apps_root_for_view(path.parent)
        if root is None:
            return []
        app, package, _icon_file, token = desktop_details(path)
        if app is None or token is None:
            return []

        launch = Nautilus.MenuItem(
            name="ZenOSApps::Launch",
            label=f"Launch {app.get_name()}",
        )
        launch.connect("activate", self._launch, token, root)
        items = [launch]

        if package:
            open_package = Nautilus.MenuItem(
                name="ZenOSApps::OpenPackage",
                label="Open Package Contents",
            )
            open_package.connect("activate", self._open_package, package)
            items.append(open_package)
        return items

    def _open_source(self, _item: Nautilus.MenuItem, path: Path) -> None:
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def get_background_items(
        self, current_folder: Nautilus.FileInfo
    ) -> List[Nautilus.MenuItem]:
        current = Path(unquote(urlparse(current_folder.get_uri()).path))
        root = apps_root_for_view(current)
        if root is None:
            return []

        submenu = Nautilus.Menu()
        for label, path in source_views(root):
            item = Nautilus.MenuItem(
                name=f"ZenOSApps::Source::{path.name}",
                label=label,
            )
            item.connect("activate", self._open_source, path)
            submenu.append_item(item)
        source_menu = Nautilus.MenuItem(
            name="ZenOSApps::Sources",
            label="Application Sources",
            menu=submenu,
        )
        return [source_menu]


def location_widget(uri: str, window: object) -> Gtk.Widget | None:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    current = Path(unquote(parsed.path))
    root = apps_root_for_view(current)
    if root is None:
        return None

    navigation = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)
    navigation.add_css_class("linked")
    navigation.set_halign(Gtk.Align.CENTER)
    first_button = None
    extension = ZenOSAppsExtension()
    for label, path in source_views(root):
        button = Gtk.ToggleButton.new_with_label(label)
        if first_button is None:
            first_button = button
        else:
            button.set_group(first_button)
        button.set_active(path == current)
        button.set_tooltip_text(f"Show {label}")
        button.connect("clicked", extension._navigate, path, window)
        navigation.append(button)
    return navigation


if hasattr(Nautilus, "LocationWidgetProvider"):

    class ZenOSAppsLocationExtension(GObject.GObject, Nautilus.LocationWidgetProvider):
        def get_widget(self, uri: str, window: object) -> Gtk.Widget | None:
            return location_widget(uri, window)
