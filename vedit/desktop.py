"""Putting vedit in the desktop's launcher, and taking it out again.

vedit is installed with pip into a virtual environment, which gives a desktop
nothing to find: no menu entry, no icon, nothing to open a `.vedit` file with.
`vedit --install-desktop` writes those three things into the user's own data
directory — no root, and `--uninstall-desktop` removes exactly what was written.

The entry starts vedit with the Python that ran the install, so it keeps working
from inside a virtual environment without that environment being on `PATH`.

The name `vedit` is load-bearing in three places that have to agree: the
`.desktop` file's name, the icon's name, and the application id the window
announces (`QGuiApplication.setDesktopFileName`). A Wayland shell matches a
window to its launcher entry — and so to its icon — by that id alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "vedit"
MIME_TYPE = "application/x-vedit-project"
# Bitmap copies for launchers and file managers that do not render SVG.
PNG_SIZES = (16, 24, 32, 48, 64, 128, 256)

DATA = Path(__file__).parent / "data"


def icon_file() -> Path:
    """The icon as shipped inside the package."""
    return DATA / f"{APP_ID}.svg"


def data_home() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base)


def _quote(argument: str) -> str:
    """One argument of a desktop entry's Exec line.

    The Desktop Entry spec wants reserved characters inside double quotes with
    `"`, `` ` ``, `$` and `\\` escaped; a literal per cent is doubled so it is
    not read as a field code. A path with a space in it is the ordinary case
    this exists for.
    """
    escaped = argument.replace("\\", "\\\\")
    for char in ('"', "`", "$"):
        escaped = escaped.replace(char, f"\\{char}")
    return '"' + escaped.replace("%", "%%") + '"'


def desktop_entry(python: str | None = None) -> str:
    python = python or sys.executable
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=vedit",
            "GenericName=Video Editor",
            "Comment=Import anything, cut it, export it",
            f"Exec={_quote(python)} -m vedit %f",
            f"Icon={APP_ID}",
            "Terminal=false",
            "Categories=AudioVideo;Video;AudioVideoEditing;",
            "Keywords=video;editor;cut;trim;montage;film;",
            f"MimeType={MIME_TYPE};",
            f"StartupWMClass={APP_ID}",
            "StartupNotify=true",
            "",
        ]
    )


def mime_definition() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info">
  <mime-type type="{MIME_TYPE}">
    <comment>vedit project</comment>
    <sub-class-of type="application/json"/>
    <icon name="{APP_ID}"/>
    <glob pattern="*.vedit"/>
  </mime-type>
</mime-info>
"""


def targets(root: Path | None = None) -> dict[str, Path]:
    """Every file an install writes, by what it is."""
    root = root or data_home()
    icons = root / "icons" / "hicolor"
    found = {
        "desktop": root / "applications" / f"{APP_ID}.desktop",
        "mime": root / "mime" / "packages" / f"{APP_ID}.xml",
        "svg": icons / "scalable" / "apps" / f"{APP_ID}.svg",
    }
    for size in PNG_SIZES:
        found[f"png{size}"] = icons / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
    return found


def _render_png(size: int, target: Path) -> bool:
    """Draw the icon at one size. False if this Qt cannot render SVG."""
    try:
        from PySide6.QtCore import QRectF, Qt
        from PySide6.QtGui import QGuiApplication, QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except ImportError:
        return False

    if QGuiApplication.instance() is None:
        # Nothing is shown, so no display is needed to run an install.
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _render_png.app = QGuiApplication([])  # kept alive on the function

    renderer = QSvgRenderer(str(icon_file()))
    if not renderer.isValid():
        return False
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image.save(str(target), "PNG")


def _refresh(root: Path) -> None:
    """Tell the desktop its databases changed. Each tool is optional: without
    it the entry still appears, only later, when the desktop next looks."""
    for command in (
        ["update-desktop-database", str(root / "applications")],
        ["update-mime-database", str(root / "mime")],
        ["gtk-update-icon-cache", "-f", "-t", str(root / "icons" / "hicolor")],
    ):
        if shutil.which(command[0]) is None:
            continue
        subprocess.run(command, capture_output=True, check=False)


def install(root: Path | None = None, python: str | None = None) -> list[Path]:
    """Write the launcher entry, the icons and the file type. Returns what was
    written."""
    root = root or data_home()
    paths = targets(root)
    written: list[Path] = []
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)

    paths["desktop"].write_text(desktop_entry(python), encoding="utf-8")
    paths["mime"].write_text(mime_definition(), encoding="utf-8")
    shutil.copyfile(icon_file(), paths["svg"])
    written += [paths["desktop"], paths["mime"], paths["svg"]]

    for size in PNG_SIZES:
        target = paths[f"png{size}"]
        if _render_png(size, target):
            written.append(target)

    _refresh(root)
    return written


def uninstall(root: Path | None = None) -> list[Path]:
    """Remove what `install` wrote. Returns what was there to remove."""
    root = root or data_home()
    removed: list[Path] = []
    for path in targets(root).values():
        if path.exists():
            path.unlink()
            removed.append(path)
    _refresh(root)
    return removed


def run_command(arguments: list[str]) -> int | None:
    """Handle `--install-desktop` / `--uninstall-desktop`.

    Returns an exit code if one of them was given, or None to carry on and
    start the editor.
    """
    if "--install-desktop" in arguments:
        written = install()
        print("vedit is now in your application launcher.")
        for path in written[:3]:
            print(f"  {path}")
        print(f"  … and {len(written) - 3} icon sizes under {data_home() / 'icons' / 'hicolor'}")
        print("Remove it again with:  vedit --uninstall-desktop")
        return 0
    if "--uninstall-desktop" in arguments:
        removed = uninstall()
        print(f"Removed {len(removed)} file(s)." if removed else "Nothing was installed.")
        return 0
    return None
