"""Application shell: the window, the Media/Edit/Render page switcher, the menus.

The three pages mirror how the work actually splits up — bring media in, cut it,
push it out — which is the same reason Resolve is organised this way.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.pages.edit_page import EditPage
from vedit.pages.media_page import MediaPage
from vedit.pages.render_page import RenderPage

PAGES = ("Media", "Edit", "Render")


class PageBar(QWidget):
    """The top strip of page buttons. Exclusive, like tabs but styled as a header."""

    def __init__(self, on_change, parent=None):
        super().__init__(parent)
        self.setObjectName("PageBar")
        self.setFixedHeight(38)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 12, 0)
        layout.setSpacing(0)

        title = QLabel("vedit")
        title.setObjectName("AppTitle")
        layout.addWidget(title)
        layout.addSpacing(18)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for index, name in enumerate(PAGES):
            button = QPushButton(name)
            button.setObjectName("PageButton")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setChecked(index == 0)
            self.group.addButton(button, index)
            layout.addWidget(button)

        layout.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("PlaceholderLabel")
        layout.addWidget(self.status)

        self.group.idClicked.connect(on_change)

    def select(self, index: int) -> None:
        button = self.group.button(index)
        if button is not None:
            button.setChecked(True)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("vedit")
        self.resize(1600, 950)

        self.media_page = MediaPage()
        self.edit_page = EditPage()
        self.render_page = RenderPage()

        self.stack = QStackedWidget()
        for page in (self.media_page, self.edit_page, self.render_page):
            self.stack.addWidget(page)

        self.page_bar = PageBar(self.show_page)

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.page_bar)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._build_menus()
        self.statusBar().showMessage("Ready")

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        view_menu = self.menuBar().addMenu("&View")
        for index, name in enumerate(PAGES):
            action = QAction(f"&{name}", self)
            # Shift+1/2/3 rather than plain digits, which the timeline will want.
            action.setShortcut(QKeySequence(f"Shift+{index + 1}"))
            action.triggered.connect(lambda _=False, i=index: self.show_page(i))
            view_menu.addAction(action)

    def show_page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.page_bar.select(index)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("vedit")
    app.setApplicationDisplayName("vedit")
    app.setStyle("Fusion")
    app.setStyleSheet(theme.stylesheet())

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
