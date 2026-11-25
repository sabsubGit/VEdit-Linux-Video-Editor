"""Keyboard layouts: vedit's own, and ones for hands trained elsewhere.

Every shortcut that differs between editors is named here once, and each layout
says which keys that name gets. Nothing else in the program spells a key out:
actions are bound by name, and menus and tooltips ask what the key currently is,
so switching layout changes all of them together.

The layouts cover only what vedit can do. Premiere and Resolve each have
hundreds of shortcuts, almost all for features that do not exist here; what
carries over is the dozen or so keys an editor's hands reach for without
thinking — the tools, the split, the two kinds of delete, the zoom.

Where another editor uses a key for something vedit has no equivalent of
(Premiere's `M` is a marker, its `R` a rate-stretch tool), vedit's meaning is
kept in that layout rather than leaving the key dead.
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QAction, QKeySequence

SETTINGS_KEY = "keys/layout"
DEFAULT = "vedit"

# What every layout shares. The transport is the same in all three editors, so
# it is written once rather than three times where a typo could split them.
COMMON: dict[str, tuple[str, ...]] = {
    "play_pause": ("Space",),
    "shuttle_back": ("J",),
    "pause": ("K",),
    "shuttle_forward": ("L",),
    "prev_frame": ("Left",),
    "next_frame": ("Right",),
    "back_second": ("Shift+Left",),
    "forward_second": ("Shift+Right",),
    "go_start": ("Home",),
    "go_end": ("End",),
    "select_all": ("Ctrl+A",),
    "deselect": ("Esc",),
    "add_title": ("Ctrl+T",),
    "mute_clip": ("M",),
    "reverse_clip": ("R",),
    "tool.reframe": ("T",),
    "tool.zoom": ("Z",),
}

LAYOUTS: dict[str, dict[str, tuple[str, ...]]] = {
    "vedit": {
        "tool.pointer": ("V",),
        "tool.cut": ("C",),
        "split": ("X",),
        "ripple_delete": ("Del",),
        "lift": ("Backspace",),
        "zoom_in": ("=", "+"),
        "zoom_out": ("-",),
        "fit": ("F",),
        "snap": ("S",),
    },
    "premiere": {
        "tool.pointer": ("V",),
        "tool.cut": ("C",),
        "split": ("Ctrl+K",),
        # Premiere's plain Delete clears and leaves the gap; Shift closes it.
        "ripple_delete": ("Shift+Del",),
        "lift": ("Del", "Backspace"),
        "zoom_in": ("=", "+"),
        "zoom_out": ("-",),
        "fit": ("\\",),
        "snap": ("S",),
    },
    "resolve": {
        "tool.pointer": ("A",),
        "tool.cut": ("B",),
        "split": ("Ctrl+B", "Ctrl+\\"),
        "ripple_delete": ("Shift+Backspace", "Del"),
        "lift": ("Backspace",),
        "zoom_in": ("Ctrl+=", "Ctrl++"),
        "zoom_out": ("Ctrl+-",),
        "fit": ("Shift+Z",),
        "snap": ("N",),
    },
}

LAYOUT_NAMES = {"vedit": "vedit", "premiere": "Premiere", "resolve": "Resolve"}

for _keys in LAYOUTS.values():
    for _name, _value in COMMON.items():
        _keys.setdefault(_name, _value)


class Keymap(QObject):
    """The layout in force, and every action that has to follow it."""

    changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._layout = DEFAULT
        self._bound: list[tuple[weakref.ref, str]] = []

    @property
    def layout(self) -> str:
        return self._layout

    # -- asking ----------------------------------------------------------------

    def keys(self, name: str) -> tuple[str, ...]:
        """The keys for an action in the current layout, first one preferred."""
        return LAYOUTS[self._layout].get(name, ())

    def text(self, name: str) -> str:
        """The preferred key as the user's platform writes it, for a label."""
        keys = self.keys(name)
        if not keys:
            return ""
        return QKeySequence(keys[0]).toString(QKeySequence.NativeText)

    def labelled(self, label: str, name: str) -> str:
        """`label` with its key in a menu's right-hand column, if it has one."""
        key = self.text(name)
        return f"{label}\t{key}" if key else label

    # -- binding ---------------------------------------------------------------

    def bind(self, action: QAction, name: str) -> QAction:
        """Give an action its keys, now and whenever the layout changes."""
        if name not in LAYOUTS[DEFAULT]:
            raise KeyError(f"no shortcut is named {name!r}")
        action.setShortcuts([QKeySequence(key) for key in self.keys(name)])
        self._bound.append((weakref.ref(action), name))
        return action

    def set_layout(self, layout: str, *, save: bool = True) -> None:
        if layout not in LAYOUTS:
            raise ValueError(f"{layout!r} is not one of {', '.join(LAYOUTS)}")
        self._layout = layout

        alive = []
        for ref, name in self._bound:
            action = ref()
            if action is None:
                continue
            try:
                action.setShortcuts([QKeySequence(key) for key in self.keys(name)])
            except RuntimeError:
                # The Qt object behind it is gone though Python still holds it.
                continue
            alive.append((ref, name))
        self._bound = alive

        if save:
            QSettings("vedit", "vedit").setValue(SETTINGS_KEY, layout)
        self.changed.emit(layout)

    def load(self) -> None:
        """Take up the layout chosen last time, if it is still one we have."""
        saved = QSettings("vedit", "vedit").value(SETTINGS_KEY, DEFAULT, type=str)
        self.set_layout(saved if saved in LAYOUTS else DEFAULT, save=False)


# One for the process: a layout is a property of the person at the keyboard,
# not of a window or a project.
keymap = Keymap()
