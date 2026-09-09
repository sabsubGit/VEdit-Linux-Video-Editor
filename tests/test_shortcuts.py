"""Page-scoped keyboard shortcuts, and the focus they depend on.

The Edit and Audio pages install identical transport actions with
`Qt.WidgetWithChildrenShortcut`, so a page's copy only fires while the keyboard
focus is inside that page. That makes focus part of the feature rather than an
incidental detail: clicking a tab in the page bar used to leave focus on the
button — outside every page — and Space silently stopped playing until something
inside a page was clicked. These tests pin down where focus goes.
"""

from __future__ import annotations

import pytest

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from vedit.core.project import Project

MEDIA, EDIT, AUDIO, RENDER = 0, 1, 2, 3


@pytest.fixture
def window(qt_app):
    from vedit.app import MainWindow

    win = MainWindow(Project())
    win.resize(1400, 850)
    win.show()
    qt_app.processEvents()
    yield win
    win.project.undo.mark_clean()   # nothing to save; skip the close dialog
    win.engine.stop()
    win.close()


def click_tab(qt_app, window, index: int) -> None:
    QTest.mouseClick(window.page_bar.group.button(index), Qt.LeftButton)
    qt_app.processEvents()


def play_action(page: QWidget) -> QAction:
    return next(a for a in QWidget.actions(page) if a.text() == "Play/Pause")


def space_toggles(qt_app, window) -> bool:
    """Send Space where the user's keystroke would go, and report whether the
    current page's Play/Pause acted on it."""
    fired = []
    page = window.stack.currentWidget()
    action = play_action(page)
    action.triggered.connect(lambda *_: fired.append(True))
    try:
        QTest.keyClick(qt_app.focusWidget() or window, Qt.Key_Space)
        qt_app.processEvents()
    finally:
        action.triggered.disconnect()
    return bool(fired)


class TestFocusFollowsThePage:
    def test_clicking_a_tab_leaves_focus_inside_the_new_page(self, qt_app, window):
        for index in (AUDIO, EDIT, MEDIA, AUDIO):
            click_tab(qt_app, window, index)
            focused = qt_app.focusWidget()
            assert focused is not None, "no focus widget: every shortcut is dormant"
            assert window.stack.currentWidget().isAncestorOf(focused), (
                "focus escaped the current page"
            )

    def test_the_page_bar_never_takes_focus(self, qt_app, window):
        click_tab(qt_app, window, EDIT)
        focused = qt_app.focusWidget()
        assert focused.objectName() != "PageButton"

    def test_the_view_menu_route_focuses_too(self, qt_app, window):
        window.show_page(AUDIO)
        qt_app.processEvents()
        assert window.audio_page.isAncestorOf(qt_app.focusWidget())


class TestSpacePlays:
    def test_space_works_on_the_audio_page(self, qt_app, window):
        click_tab(qt_app, window, AUDIO)
        assert space_toggles(qt_app, window)

    def test_space_still_works_after_switching_pages(self, qt_app, window):
        """The reported bug: Space died on the Edit page after being used on the
        Audio page, and stayed dead on the way back."""
        click_tab(qt_app, window, AUDIO)
        assert space_toggles(qt_app, window)

        click_tab(qt_app, window, EDIT)
        assert space_toggles(qt_app, window), "Space died after switching page"

        click_tab(qt_app, window, AUDIO)
        assert space_toggles(qt_app, window), "and stayed dead on the way back"

    def test_only_the_visible_page_responds(self, qt_app, window):
        """Both pages carry the same shortcut; the hidden one must stay quiet,
        or Qt would call it ambiguous and disable both."""
        click_tab(qt_app, window, EDIT)
        fired = []
        play_action(window.audio_page).triggered.connect(lambda *_: fired.append(True))
        QTest.keyClick(qt_app.focusWidget(), Qt.Key_Space)
        qt_app.processEvents()
        assert not fired
