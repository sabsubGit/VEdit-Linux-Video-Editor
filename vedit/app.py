"""Application shell: the window, the Media/Edit/Render page switcher, the menus.

The three pages mirror how the work actually splits up — bring media in, cut it,
push it out — which is the same reason Resolve is organised this way.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.core.project import Project
from vedit.core.projectfile import SUFFIX, ProjectFileError
from vedit.pages.edit_page import EditPage
from vedit.pages.media_page import MediaPage
from vedit.pages.render_page import RenderPage
from vedit.timeline import ops

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
    def __init__(self, project: Project | None = None):
        super().__init__()
        self.setWindowTitle("vedit")
        self.resize(1600, 950)

        self.project = project or Project()

        self.media_page = MediaPage(self.project)
        self.edit_page = EditPage(self.project)
        self.render_page = RenderPage(self.project)

        # Sending media to the timeline is what moves you between pages, so both
        # routes into the Edit page live here rather than inside a page.
        self.media_page.append_requested.connect(self.append_media)
        self.media_page.media_activated.connect(self.append_media)
        self.edit_page.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.render_page.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.project.pool.import_failed.connect(self._on_import_failed)
        self.project.proxies.failed.connect(
            lambda media_id, why: self.statusBar().showMessage(f"Ingest failed: {why}", 8000)
        )

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
        for text, shortcut, slot in (
            ("&New Project", QKeySequence.New, self.new_project),
            ("&Open Project…", QKeySequence.Open, self.open_project),
            ("&Save Project", QKeySequence.Save, self.save_project),
            ("Save Project &As…", QKeySequence.SaveAs, self.save_project_as),
        ):
            action = QAction(text, self)
            action.setShortcut(shortcut)
            action.triggered.connect(slot)
            file_menu.addAction(action)
        file_menu.addSeparator()

        import_action = QAction("&Import Media…", self)
        import_action.setShortcut(QKeySequence("Ctrl+I"))
        import_action.triggered.connect(self.media_page.pool_panel.choose_files)
        file_menu.addAction(import_action)
        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = self.menuBar().addMenu("&Edit")
        self.undo_action = QAction("&Undo", self)
        self.undo_action.setShortcut(QKeySequence.Undo)
        self.undo_action.triggered.connect(self._undo)
        self.redo_action = QAction("&Redo", self)
        self.redo_action.setShortcut(QKeySequence.Redo)
        self.redo_action.triggered.connect(self._redo)
        edit_menu.addAction(self.undo_action)
        edit_menu.addAction(self.redo_action)
        self.project.timeline_changed.connect(self._refresh_history_actions)
        self._refresh_history_actions()

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

    # -- actions ---------------------------------------------------------------

    def append_media(self, media_id: str) -> None:
        """Put a pool item at the end of the timeline and switch to the Edit page."""
        info = self.project.media_for(media_id)
        if info is None:
            return
        try:
            self.project.edit(f"Append {info.name}", lambda t: ops.append_media(t, info))
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.statusBar().showMessage(str(exc), 8000)
            return
        self.show_page(1)
        self.statusBar().showMessage(f"Appended {info.name}", 4000)

    def _on_import_failed(self, path: str, reason: str) -> None:
        self.statusBar().showMessage(f"Could not import {path}: {reason}", 8000)

    # -- history ---------------------------------------------------------------

    def _undo(self) -> None:
        label = self.project.undo_edit()
        self.statusBar().showMessage(f"Undo {label}" if label else "Nothing to undo", 3000)

    def _redo(self) -> None:
        label = self.project.redo_edit()
        self.statusBar().showMessage(f"Redo {label}" if label else "Nothing to redo", 3000)

    def _refresh_history_actions(self) -> None:
        undo = self.project.undo
        self.undo_action.setEnabled(undo.can_undo)
        self.undo_action.setText(f"&Undo {undo.undo_label}" if undo.can_undo else "&Undo")
        self.redo_action.setEnabled(undo.can_redo)
        self.redo_action.setText(f"&Redo {undo.redo_label}" if undo.can_redo else "&Redo")
        self._update_title()

    def _update_title(self) -> None:
        marker = "• " if self.project.is_dirty else ""
        self.setWindowTitle(f"{marker}{self.project.display_name} — vedit")

    # -- project files ---------------------------------------------------------

    def _confirm_discard(self) -> bool:
        """Ask before throwing away unsaved edits. True means "carry on"."""
        if not self.project.is_dirty:
            return True
        answer = QMessageBox.question(
            self,
            "Unsaved changes",
            f"{self.project.display_name} has unsaved changes.\n\nSave before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            return self.save_project()
        return True

    def new_project(self) -> None:
        if not self._confirm_discard():
            return
        self.project.reset()
        self._update_title()
        self.statusBar().showMessage("New project", 3000)

    def open_project(self) -> None:
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open project", "", f"vedit projects (*{SUFFIX});;All files (*)"
        )
        if not path:
            return
        try:
            result = self.project.load_from(path)
        except ProjectFileError as exc:
            QMessageBox.warning(self, "Cannot open project", str(exc))
            return

        self.edit_page.engine.invalidate()
        self.edit_page.timeline_panel.canvas.zoom_to_fit()
        self._update_title()

        if result.missing:
            listed = "\n".join(result.missing[:8])
            QMessageBox.warning(
                self,
                "Missing media",
                f"{len(result.missing)} file(s) could not be found and their clips were "
                f"dropped:\n\n{listed}",
            )
        self.statusBar().showMessage(f"Opened {Path(path).name}", 4000)

    def save_project(self) -> bool:
        if self.project.path is None:
            return self.save_project_as()
        return self._write(self.project.path)

    def save_project_as(self) -> bool:
        suggested = self.project.path or str(Path.home() / f"untitled{SUFFIX}")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", suggested, f"vedit projects (*{SUFFIX})"
        )
        if not path:
            return False
        return self._write(path)

    def _write(self, path: str) -> bool:
        try:
            written = self.project.save_to(path)
        except OSError as exc:
            QMessageBox.warning(self, "Cannot save", str(exc))
            return False
        self._update_title()
        self.statusBar().showMessage(f"Saved {written.name}", 4000)
        return True

    def closeEvent(self, event) -> None:
        if not self._confirm_discard():
            event.ignore()
            return
        # Stop every child process so no ffmpeg outlives the window.
        self.edit_page.shutdown()
        self.render_page.shutdown()
        self.project.shutdown()
        super().closeEvent(event)


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
