"""Edit page: viewer and transport on top, timeline below."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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
from vedit.pages.actions import TimelineActions
from vedit.player.engine import PlaybackEngine
from vedit.player.surface import VideoSurface
from vedit.timeline.view import TimelinePanel


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
        self.thumbs_toggle = QCheckBox("Thumbs")
        self.thumbs_toggle.setChecked(True)
        self.thumbs_toggle.setToolTip("Show frame thumbnails along video clips")
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
        layout.addWidget(self.thumbs_toggle)
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

    def __init__(self, project: Project, engine: PlaybackEngine, parent=None) -> None:
        super().__init__(parent)
        self.project = project

        self.surface = VideoSurface(self)
        # The engine is owned by the window and shared with the Audio page: two
        # engines would mean two decoder threads and two audio devices fighting
        # over one timeline.
        self.engine = engine
        self.timeline_panel = TimelinePanel(project, self)
        self.transport = TransportBar(project, self.engine, self)

        self.timeline_panel.status_message.connect(self.status_message)
        self.engine.error.connect(lambda why: self.status_message.emit(f"Playback: {why}"))
        self.transport.snap_toggle.toggled.connect(
            lambda on: setattr(self.timeline_panel.canvas, "snapping", on)
        )
        self.transport.fit_button.clicked.connect(self.timeline_panel.canvas.zoom_to_fit)
        self.transport.thumbs_toggle.toggled.connect(self._set_filmstrips)

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

        self.actions = TimelineActions(self, project, engine, self.timeline_panel)
        self.actions.status_message.connect(self.status_message)

    def _set_filmstrips(self, enabled: bool) -> None:
        self.timeline_panel.canvas.show_filmstrips = enabled
        self.timeline_panel.canvas.update()

    # -- glue ------------------------------------------------------------------

    def _on_engine_position(self, frame: int) -> None:
        self.project.set_playhead(frame)
        if self.engine.playing:
            self.timeline_panel.canvas.ensure_visible(frame)
            self.timeline_panel.canvas.update()
