"""Edit page: viewer and transport on top, timeline below."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.core.project import Project
from vedit.player.engine import PlaybackEngine
from vedit.player.surface import VideoSurface
from vedit.timeline import ops
from vedit.timeline.model import TimelineError
from vedit.timeline.view import TimelinePanel

SHUTTLE_SPEEDS = (1.0, 2.0, 4.0, 8.0)


class TransportBar(QWidget):
    """Timecode readout and transport buttons under the viewer."""

    def __init__(self, project: Project, engine: PlaybackEngine, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.engine = engine

        self.timecode = QLabel("00:00:00:00")
        self.timecode.setStyleSheet(
            f"font-family: monospace; font-size: 17px; color: {theme.TEXT.name()};"
        )
        self.duration = QLabel("/ 00:00:00:00")
        self.duration.setObjectName("PlaceholderLabel")
        self.duration.setStyleSheet(
            f"font-family: monospace; color: {theme.TEXT_FAINT.name()};"
        )

        self.go_start = QPushButton("|◀")
        self.step_back = QPushButton("◀|")
        self.play_button = QPushButton("▶")
        self.step_forward = QPushButton("|▶")
        self.go_end = QPushButton("▶|")
        for button, tip in (
            (self.go_start, "Go to start (Home)"),
            (self.step_back, "Previous frame (←)"),
            (self.play_button, "Play / pause (Space)"),
            (self.step_forward, "Next frame (→)"),
            (self.go_end, "Go to end (End)"),
        ):
            button.setToolTip(tip)
            button.setFixedWidth(46)

        self.go_start.clicked.connect(lambda: engine.seek(0))
        self.step_back.clicked.connect(lambda: engine.step(-1))
        self.play_button.clicked.connect(engine.toggle)
        self.step_forward.clicked.connect(lambda: engine.step(1))
        self.go_end.clicked.connect(lambda: engine.seek(project.timeline.duration))

        self.snap_toggle = QCheckBox("Snap")
        self.snap_toggle.setChecked(True)
        self.fit_button = QPushButton("Fit")
        self.fit_button.setToolTip("Zoom timeline to fit (F)")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        layout.addWidget(self.timecode)
        layout.addWidget(self.duration)
        layout.addStretch(1)
        for button in (self.go_start, self.step_back, self.play_button, self.step_forward, self.go_end):
            layout.addWidget(button)
        layout.addStretch(1)
        layout.addWidget(self.snap_toggle)
        layout.addWidget(self.fit_button)

        engine.state_changed.connect(self._on_state)
        project.timeline_changed.connect(self.refresh)
        self.refresh()

    def _on_state(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")

    def refresh(self, *_) -> None:
        timebase = self.project.timebase
        self.timecode.setText(timebase.frames_to_timecode(self.project.playhead))
        self.duration.setText(f"/ {timebase.frames_to_timecode(self.project.timeline.duration)}")


class EditPage(QWidget):
    status_message = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project

        self.surface = VideoSurface(self)
        self.engine = PlaybackEngine(project, self.surface, self)
        self.timeline_panel = TimelinePanel(project, self)
        self.transport = TransportBar(project, self.engine, self)

        self.timeline_panel.status_message.connect(self.status_message)
        self.engine.error.connect(lambda why: self.status_message.emit(f"Playback: {why}"))
        self.transport.snap_toggle.toggled.connect(
            lambda on: setattr(self.timeline_panel.canvas, "snapping", on)
        )
        self.transport.fit_button.clicked.connect(self.timeline_panel.canvas.zoom_to_fit)

        # The engine drives the playhead while playing; the canvas drives it
        # while scrubbing. Both routes end at project.set_playhead.
        self.engine.position_changed.connect(self._on_engine_position)
        project.playhead_changed.connect(lambda *_: self.transport.refresh())

        viewer = QWidget()
        viewer_layout = QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        viewer_layout.addWidget(self.surface, 1)
        viewer_layout.addWidget(self.transport)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(viewer)
        splitter.addWidget(self.timeline_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 380])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._build_actions()

    # -- actions ---------------------------------------------------------------

    def _add(self, text: str, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut) if isinstance(shortcut, str) else shortcut)
        action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        action.triggered.connect(slot)
        self.addAction(action)
        return action

    def _build_actions(self) -> None:
        self._add("Play/Pause", Qt.Key_Space, self.engine.toggle)
        self._add("Razor", "S", self.razor_at_playhead)
        self._add("Razor (B)", "B", self.razor_at_playhead)
        self._add("Ripple Delete", QKeySequence.Delete, self.ripple_delete_selection)
        self._add("Lift", Qt.Key_Backspace, self.lift_selection)

        self._add("Previous Frame", Qt.Key_Left, lambda: self.engine.step(-1))
        self._add("Next Frame", Qt.Key_Right, lambda: self.engine.step(1))
        fps = int(round(float(self.project.timebase.fps)))
        self._add("Back 1s", "Shift+Left", lambda: self.engine.step(-fps))
        self._add("Forward 1s", "Shift+Right", lambda: self.engine.step(fps))
        self._add("Start", Qt.Key_Home, lambda: self.engine.seek(0))
        self._add("End", Qt.Key_End, lambda: self.engine.seek(self.project.timeline.duration))

        # JKL shuttle, the transport every editor's hands already know.
        self._add("Shuttle Back", "J", self.shuttle_back)
        self._add("Pause", "K", self.engine.pause)
        self._add("Shuttle Forward", "L", self.shuttle_forward)

        self._add("Zoom In", "=", lambda: self._zoom(1.3))
        self._add("Zoom In (+)", "+", lambda: self._zoom(1.3))
        self._add("Zoom Out", "-", lambda: self._zoom(1 / 1.3))
        self._add("Fit", "F", self.timeline_panel.canvas.zoom_to_fit)
        self._add("Select All", QKeySequence.SelectAll, self.select_all)
        self._add("Deselect", Qt.Key_Escape, lambda: self.project.set_selection([]))

        self._shuttle_index = 0
        self._shuttle_direction = 0

    def _zoom(self, factor: float) -> None:
        canvas = self.timeline_panel.canvas
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
        clips = self.project.selected_clips()
        if not clips:
            self.status_message.emit("Select a clip first")
            return
        try:
            self.project.edit("Ripple delete", lambda t: ops.ripple_delete(t, clips))
            self.project.set_selection([])
        except TimelineError as exc:
            self.status_message.emit(str(exc))

    def lift_selection(self) -> None:
        clips = self.project.selected_clips()
        if not clips:
            self.status_message.emit("Select a clip first")
            return
        try:
            self.project.edit("Delete", lambda t: ops.lift(t, clips))
            self.project.set_selection([])
        except TimelineError as exc:
            self.status_message.emit(str(exc))

    def select_all(self) -> None:
        self.project.set_selection([clip.clip_id for clip in self.project.timeline.all_clips()])

    # -- shuttle ---------------------------------------------------------------

    def shuttle_forward(self) -> None:
        """L steps up through the speeds, as it does in every other NLE."""
        if self._shuttle_direction != 1:
            self._shuttle_direction, self._shuttle_index = 1, 0
        elif self._shuttle_index < len(SHUTTLE_SPEEDS) - 1:
            self._shuttle_index += 1
        self.engine.play(SHUTTLE_SPEEDS[self._shuttle_index])

    def shuttle_back(self) -> None:
        if self._shuttle_direction != -1:
            self._shuttle_direction, self._shuttle_index = -1, 0
        elif self._shuttle_index < len(SHUTTLE_SPEEDS) - 1:
            self._shuttle_index += 1
        self.engine.play(-SHUTTLE_SPEEDS[self._shuttle_index])

    # -- glue ------------------------------------------------------------------

    def _on_engine_position(self, frame: int) -> None:
        self.project.set_playhead(frame)
        if self.engine.playing:
            self.timeline_panel.canvas.ensure_visible(frame)
            self.timeline_panel.canvas.update()

    def shutdown(self) -> None:
        self.engine.stop()
