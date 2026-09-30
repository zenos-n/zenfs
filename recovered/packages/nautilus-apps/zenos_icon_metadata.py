"""Select metadata for the themed or standalone icon of an app launcher."""

from pathlib import Path


def icon_metadata(icon_name, icon_file, has_icon=None):
    themed = (
        isinstance(icon_name, str)
        and bool(icon_name)
        and icon_name not in {".", ".."}
        and "/" not in icon_name
        and "\\" not in icon_name
        and ":" not in icon_name
    )
    # Theme lookup handles installed SVG icons directly. File icons take
    # Nautilus's separate image-decoder path instead of the icon theme.
    if themed and (has_icon is None or has_icon(icon_name)):
        return "metadata::custom-icon-name", icon_name
    if icon_file:
        path = Path(icon_file)
        if path.is_absolute() and path.is_file():
            return "metadata::custom-icon", path.as_uri()
    if themed:
        return "metadata::custom-icon-name", icon_name
    return None
