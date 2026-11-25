"""Shared test setup.

Most of the codebase is deliberately Qt-free and needs nothing here. The parts
that touch QPixmap do: it is a paint-device-backed class and constructing one
without a QGuiApplication crashes the interpreter rather than raising.

A `QApplication` rather than a `QGuiApplication` because the mixer tests build
real widgets, and `QWidget` requires the wider class. `QApplication` *is* a
`QGuiApplication`, so nothing that only needed the narrower one is affected.
"""

from __future__ import annotations

import os

import pytest

# Must be set before any Qt module initialises a platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Autosaves and the log go to the state directory. Tests build real windows and
# dirty real projects, and none of that belongs in the developer's own.
import tempfile  # noqa: E402

os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="vedit-test-state-")
# QSettings follows this one; the tests must not read or write real preferences.
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="vedit-test-config-")


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    """One offscreen QApplication for the whole session.

    Qt allows only a single application object per process, so this is created
    once and shared rather than per-test.
    """
    from PySide6.QtWidgets import QApplication

    from vedit import theme

    app = QApplication.instance() or QApplication([])
    # The stylesheet is applied because several object names (#MuteButton,
    # #SoloButton) only resolve through it, and a rule that fails to parse
    # should fail a test rather than merely look wrong.
    app.setStyleSheet(theme.stylesheet())
    yield app
