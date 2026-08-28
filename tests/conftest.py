"""Shared test setup.

Most of the codebase is deliberately Qt-free and needs nothing here. The parts
that touch QPixmap do: it is a paint-device-backed class and constructing one
without a QGuiApplication crashes the interpreter rather than raising.
"""

from __future__ import annotations

import os

import pytest

# Must be set before any Qt module initialises a platform plugin.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session", autouse=True)
def qt_app():
    """One offscreen QGuiApplication for the whole session.

    Qt allows only a single application object per process, so this is created
    once and shared rather than per-test.
    """
    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication.instance() or QGuiApplication([])
    yield app
