"""Keyboard actions shared by the pages that have a viewer and a timeline.

The Edit page and the Audio page want the same transport and the same edit
commands — there is no version of "cut at the playhead" that makes sense only
next to picture. Keeping the set in one place is what stops the two pages
drifting apart, which is what happens the moment twenty `addAction` calls get
copied from one file to another.

The actions install themselves on the page widget with
`Qt.WidgetWithChildrenShortcut`, so whichever page is on screen owns the keys and
the other one's copies stay dormant.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QWidget

from vedit.core.project import Project
from vedit.player.engine import PlaybackEngine
from vedit.timeline import ops
from vedit.timeline.model import TimelineError
from vedit.timeline.view import TimelinePanel

SHUTTLE_SPEEDS = (1.0, 2.0, 4.0, 8.0)
ZOOM_STEP = 1.3


class TimelineActions(QObject):
    """Transport, zoom and edit shortcuts for one page.

    Owns the shuttle state, which has to be per-page: J pressed twice on the
    Audio page should step up through the speeds there without the Edit page's
    copy having an opinion about it.
    """

    status_message = Signal(str)

    def __init__(
        self,
        widget: QWidget,
        project: Project,
        engine: PlaybackEngine,
        panel: TimelinePanel,
    ) -> None:
        super().__init__(widget)
        self.widget = widget
        self.project = project
        self.engine = engine
        self.panel = panel

        self._shuttle_index = 0
        self._shuttle_direction = 0

        self._install()

    # -- installation ----------------------------------------------------------

    def _add(self, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self.widget)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut) if isinstance(shortcut, str) else shortcut)
        action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        action.triggered.connect(slot)
        self.widget.addAction(action)
        return action

    def _install(self) -> None:
        engine, project = self.engine, self.project

        self._add("Play/Pause", Qt.Key_Space, engine.toggle)
        self._add("Razor", "X", self.razor_at_playhead)
        self._add("Ripple Delete", QKeySequence.Delete, self.ripple_delete_selection)
        self._add("Lift", Qt.Key_Backspace, self.lift_selection)

        self._add("Previous Frame", Qt.Key_Left, lambda: engine.step(-1))
        self._add("Next Frame", Qt.Key_Right, lambda: engine.step(1))
        fps = int(round(float(project.timebase.fps)))
        self._add("Back 1s", "Shift+Left", lambda: engine.step(-fps))
        self._add("Forward 1s", "Shift+Right", lambda: engine.step(fps))
        self._add("Start", Qt.Key_Home, lambda: engine.seek(0))
        self._add("End", Qt.Key_End, lambda: engine.seek(project.timeline.duration))

        # JKL shuttle, the transport every editor's hands already know.
        self._add("Shuttle Back", "J", self.shuttle_back)
        self._add("Pause", "K", engine.pause)
        self._add("Shuttle Forward", "L", self.shuttle_forward)

        self._add("Zoom In", "=", lambda: self.zoom(ZOOM_STEP))
        self._add("Zoom In (+)", "+", lambda: self.zoom(ZOOM_STEP))
        self._add("Zoom Out", "-", lambda: self.zoom(1 / ZOOM_STEP))
        self._add("Fit", "F", self.panel.canvas.zoom_to_fit)
        self._add("Select All", QKeySequence.SelectAll, self.select_all)
        self._add("Deselect", Qt.Key_Escape, lambda: project.set_selection([]))

    # -- zoom ------------------------------------------------------------------

    def zoom(self, factor: float) -> None:
        canvas = self.panel.canvas
        canvas.release_auto_fit()
        canvas.set_zoom(canvas.px_per_frame * factor)
        canvas.zoom_changed.emit()
        canvas.update()

    # -- edit commands ---------------------------------------------------------

    def razor_at_playhead(self) -> None:
        frame = self.project.playhead
        created = self.project.edit("Razor", lambda t: ops.razor(t, frame))
        if not created:
            self.status_message.emit("Nothing to cut at the playhead")

    def ripple_delete_selection(self) -> None:
        self._delete_with("Ripple delete", ops.ripple_delete)

    def lift_selection(self) -> None:
        self._delete_with("Delete", ops.lift)

    def _delete_with(self, label: str, operation) -> None:
        clips = self.project.selected_clips()
        if not clips:
            self.status_message.emit("Select a clip first")
            return
        try:
            self.project.edit(label, lambda t: operation(t, clips))
            self.project.set_selection([])
        except TimelineError as exc:
            self.status_message.emit(str(exc))

    def select_all(self) -> None:
        self.project.set_selection(
            [clip.clip_id for clip in self.project.timeline.all_clips()]
        )

    # -- shuttle ---------------------------------------------------------------

    def shuttle_forward(self) -> None:
        """L steps up through the speeds, as it does in every other NLE."""
        self._shuttle(1)

    def shuttle_back(self) -> None:
        self._shuttle(-1)

    def _shuttle(self, direction: int) -> None:
        if self._shuttle_direction != direction:
            self._shuttle_direction, self._shuttle_index = direction, 0
        elif self._shuttle_index < len(SHUTTLE_SPEEDS) - 1:
            self._shuttle_index += 1
        self.engine.play(direction * SHUTTLE_SPEEDS[self._shuttle_index])
