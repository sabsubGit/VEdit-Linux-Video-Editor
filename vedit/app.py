"""Application shell: the window, the Media/Edit/Render page switcher, the menus.

The three pages mirror how the work actually splits up — bring media in, cut it,
push it out — which is the same reason Resolve is organised this way.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer
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
from vedit.pages.audio_page import AudioPage
from vedit.pages.edit_page import EditPage
from vedit.pages.media_page import MediaPage
from vedit.tools import Tool
from vedit.pages.render_page import RenderPage
from vedit.player.engine import PlaybackEngine
from vedit.timeline import ops

PAGES = ("Media", "Edit", "Audio", "Render")
# Pages that own a viewer. Switching to one of these hands it the shared engine's
# picture; every other page leaves playback alone.
VIEWER_PAGES = (1, 2)

# Pause between dismissing the unsaved-changes dialog and destroying the window.
# See closeEvent: this is not our bug to fix, but it is cheap to stop provoking.
DIALOG_SETTLE_MS = 120


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
            # A tab strip has no business holding the keyboard: clicking one used
            # to park focus here, outside every page, which left the pages'
            # shortcuts — Space among them — with nothing to fire from.
            button.setFocusPolicy(Qt.NoFocus)
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
        self._closing = False

        # One engine for the whole window. Every page that shows a viewer is
        # pointed at it in turn; two engines would mean two decoder threads and
        # two audio devices contending for the same timeline.
        self.media_page = MediaPage(self.project)
        self.engine = PlaybackEngine(self.project, parent=self)
        self.edit_page = EditPage(self.project, self.engine)
        self.audio_page = AudioPage(self.project, self.engine)
        self.render_page = RenderPage(self.project)
        self.engine.set_surface(self.edit_page.surface)

        # Sending media to the timeline is what moves you between pages, so both
        # routes into the Edit page live here rather than inside a page.
        self.media_page.append_requested.connect(self.append_media)
        self.media_page.media_activated.connect(self.append_media)
        self.media_page.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.edit_page.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.audio_page.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.render_page.status_message.connect(lambda m: self.statusBar().showMessage(m, 6000))
        self.project.pool.import_failed.connect(self._on_import_failed)
        self.project.proxies.failed.connect(
            lambda media_id, why: self.statusBar().showMessage(f"Ingest failed: {why}", 8000)
        )

        self.stack = QStackedWidget()
        for page in (self.media_page, self.edit_page, self.audio_page, self.render_page):
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

    # -- edit-menu entries -----------------------------------------------------

    def _edit_canvas(self):
        """The Edit page's timeline, which owns these commands.

        The menu is reachable from any page, so it brings you to the one where
        the thing it does is visible rather than quietly acting off-screen.
        """
        if self.stack.currentWidget() is not self.edit_page:
            self.show_page(1)
        return self.edit_page.timeline_panel.canvas

    def add_title(self) -> None:
        self._edit_canvas().add_title()

    def _toggle_reframe(self, on: bool) -> None:
        self._edit_canvas()
        self.edit_page.set_tool(Tool.REFRAME if on else Tool.POINTER)

    def _selected_video_clip(self):
        """The clip the picture and dissolve menus act on.

        The selection if there is one, otherwise whatever is under the playhead
        — so the menu works when you have simply parked on a shot, which is how
        you would be looking at it in the first place.
        """
        for clip in self.project.selected_clips():
            if clip.kind == "video" and not clip.is_title:
                return clip
        return self.project.timeline.video_clip_at(self.project.playhead)

    def _fill_picture_menu(self) -> None:
        self.picture_menu.clear()
        clip = self._selected_video_clip()
        if clip is None:
            self.picture_menu.addAction("Select a clip first").setEnabled(False)
            return
        canvas = self.edit_page.timeline_panel.canvas
        canvas._add_picture_menu(self.picture_menu, clip, [clip])
        # `_add_picture_menu` opens with a separator and its own submenu, which
        # is right inside a clip's context menu and one level too deep here.
        nested = next((a.menu() for a in self.picture_menu.actions() if a.menu()), None)
        if nested is not None:
            self.picture_menu.clear()
            for action in nested.actions():
                self.picture_menu.addAction(action)

    def _fill_dissolve_menu(self) -> None:
        self.dissolve_menu.clear()
        clip = self._selected_video_clip()
        if clip is None:
            self.dissolve_menu.addAction("Select a clip first").setEnabled(False)
            return
        canvas = self.edit_page.timeline_panel.canvas
        canvas._add_transition_menu(self.dissolve_menu, clip)
        nested = next((a.menu() for a in self.dissolve_menu.actions() if a.menu()), None)
        if nested is not None:
            self.dissolve_menu.clear()
            for action in nested.actions():
                self.dissolve_menu.addAction(action)

    def _title_backdrop(self):
        """A still of what the viewer is showing, for the title dialog to draw
        over. Writing a caption against a blank rectangle is how you find out it
        was invisible against the sky only after exporting."""
        surface = getattr(self.edit_page, "surface", None)
        if surface is None or not surface.has_image:
            return None
        return surface._image

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

        # Everything below used to live only in a checkbox on the transport bar
        # and in right-click menus on the timeline. That is fine once you know
        # they are there and no use at all before — the menu bar is the one
        # place people look for "what can this program do".
        #
        # No `setShortcut` on any of them: the real shortcuts are installed
        # per-page with `WidgetWithChildrenShortcut`, and registering the same
        # key at window level as well makes Qt call it ambiguous and fire
        # neither. The key is spelled out in the label instead.
        edit_menu.addSeparator()

        title_action = QAction("Add &Title…\tCtrl+T", self)
        title_action.triggered.connect(self.add_title)
        edit_menu.addAction(title_action)

        self.reframe_action = QAction("&Reframe Picture\tT", self)
        self.reframe_action.setCheckable(True)
        self.reframe_action.triggered.connect(self._toggle_reframe)
        edit_menu.addAction(self.reframe_action)
        self.edit_page.transport.reframe_toggle.toggled.connect(
            self.reframe_action.setChecked
        )

        self.picture_menu = edit_menu.addMenu("&Picture")
        self.dissolve_menu = edit_menu.addMenu("&Dissolve In")
        # Rebuilt each time it drops down, because what belongs in it depends
        # on which clip is selected.
        self.picture_menu.aboutToShow.connect(self._fill_picture_menu)
        self.dissolve_menu.aboutToShow.connect(self._fill_dissolve_menu)

        view_menu = self.menuBar().addMenu("&View")
        for index, name in enumerate(PAGES):
            action = QAction(f"&{name}", self)
            # Shift+1/2/3 rather than plain digits, which the timeline will want.
            action.setShortcut(QKeySequence(f"Shift+{index + 1}"))
            action.triggered.connect(lambda _=False, i=index: self.show_page(i))
            view_menu.addAction(action)

    def show_page(self, index: int) -> None:
        page = self.stack.widget(index)
        self.stack.setCurrentIndex(index)
        self.page_bar.select(index)
        # Playback carries on across the switch; only the picture moves.
        if index in VIEWER_PAGES:
            self.engine.set_surface(page.surface)
        # Hiding a page clears the focus it held, so the incoming page is told to
        # take it. Without this the window is left with no focus widget and every
        # page-scoped shortcut is dormant until something is clicked.
        focus = getattr(page, "focus_default", None)
        focus() if focus is not None else page.setFocus()

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

        # An explicit instance rather than QMessageBox.question, so the dialog
        # can be told to delete itself. See closeEvent for why that matters.
        box = QMessageBox(
            QMessageBox.Question,
            "Unsaved changes",
            f"{self.project.display_name} has unsaved changes.\n\nSave before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            self,
        )
        box.setDefaultButton(QMessageBox.Save)
        answer = box.exec()
        box.deleteLater()
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

        self.engine.invalidate()
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

    def _finish_close(self) -> None:
        """Close once the dialog is genuinely gone, not merely hidden."""
        # deleteLater only queues the destruction; flush it so the dialog's
        # surface is really released before this window's goes too.
        QApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        QApplication.processEvents()
        self.close()

    def _teardown(self) -> None:
        """Stop every child process so no ffmpeg outlives the window."""
        self.engine.stop()
        self.render_page.shutdown()
        self.project.shutdown()

    def closeEvent(self, event) -> None:
        # Second pass: the dialog has had an event-loop turn to disappear.
        if self._closing:
            self._teardown()
            super().closeEvent(event)
            return

        asked = self.project.is_dirty
        if not self._confirm_discard():
            event.ignore()
            return

        self._closing = True
        if asked:
            # Tearing this window down from inside the dialog's own call stack
            # destroys both Wayland surfaces at once. Shell clients that track
            # toplevels can still be holding the dialog's object when it goes,
            # and referencing it gets them disconnected by the compositor — on
            # this machine that reliably killed quickshell on every discard.
            # That is a bug in the shell, not here, but a plain Qt app with a
            # modal dialog reproduces it in twelve lines, so the cheapest cure
            # is to stop provoking it: let the dialog be destroyed for real,
            # then close.
            event.ignore()
            QTimer.singleShot(DIALOG_SETTLE_MS, self._finish_close)
            return

        self._teardown()
        super().closeEvent(event)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("vedit")
    app.setApplicationDisplayName("vedit")
    app.setStyle("Fusion")
    app.setPalette(theme.palette())
    app.setStyleSheet(theme.stylesheet())

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
