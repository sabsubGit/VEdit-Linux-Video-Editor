"""Audio page: a small viewer, an audio-only timeline, and the mixer.

The Edit page is organised around picture. This one is organised around sound —
the same timeline, the same clips, the same engine, but only the audio lanes,
drawn tall enough that a waveform is something you can edit against rather than
decorate a clip with, and with the faders and meters the Edit page has no room
for.

The viewer stays because sound is cut to picture: it is small and it is context,
not the subject.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSplitter, QVBoxLayout, QWidget

from vedit.core.project import Project
from vedit.mixer.panel import MixerPanel
from vedit.pages.actions import TimelineActions
from vedit.pages.edit_page import TransportBar
from vedit.player.engine import PlaybackEngine
from vedit.player.surface import VideoSurface
from vedit.timeline.view import (
    AUDIO_TRACK_HEIGHT_TALL,
    VIDEO_TRACK_HEIGHT,
    TimelinePanel,
)


class AudioPage(QWidget):
    status_message = Signal(str)

    def __init__(self, project: Project, engine: PlaybackEngine, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.engine = engine

        self.surface = VideoSurface(self)
        self.transport = TransportBar(project, engine, self)
        self.timeline_panel = TimelinePanel(
            project,
            self,
            kinds=("audio",),
            track_heights={
                "video": VIDEO_TRACK_HEIGHT,
                "audio": AUDIO_TRACK_HEIGHT_TALL,
            },
            fade_handles=True,
            volume_lines=True,
        )
        self.mixer = MixerPanel(project, engine.audio, self)

        self.timeline_panel.status_message.connect(self.status_message)
        self.mixer.status_message.connect(self.status_message)
        self.transport.snap_toggle.toggled.connect(
            lambda on: setattr(self.timeline_panel.canvas, "snapping", on)
        )
        self.transport.fit_button.clicked.connect(self.timeline_panel.canvas.zoom_to_fit)
        # Thumbnails are a picture control and there are no video lanes here.
        self.transport.thumbs_toggle.hide()

        engine.position_changed.connect(self._on_engine_position)
        engine.state_changed.connect(self.mixer.on_playing)
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
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([260, 380])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(splitter, 1)
        # The mixer sits below the splitter at a fixed height rather than as a
        # third pane: a mixer that can be dragged down to twenty pixels is a
        # mixer nobody can use.
        layout.addWidget(self.mixer)

        self.actions = TimelineActions(self, project, engine, self.timeline_panel)
        self.actions.status_message.connect(self.status_message)

    def _on_engine_position(self, frame: int) -> None:
        self.project.set_playhead(frame)
        if self.engine.playing:
            self.timeline_panel.canvas.ensure_visible(frame)
            self.timeline_panel.canvas.update()
