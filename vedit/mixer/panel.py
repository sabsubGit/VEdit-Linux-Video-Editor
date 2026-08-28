"""The mixer panel: a strip per audio lane, plus the master.

This is where the two halves meet — the model, which owns the fader positions and
is what gets saved and undone, and the audio thread, which owns the peaks and
runs the better part of a second ahead of the speaker.

A fader drag therefore takes two routes. Every mouse move goes straight to the
streamer's live `MixerState`, so the level changes without the playlist being
rebuilt or the device restarted. Exactly one `project.edit` happens on release,
so the gesture is one undo step rather than sixty.
"""

from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from vedit.core.project import Project
from vedit.mixer.strip import ChannelStrip, MasterStrip
from vedit.player.audio import AudioStreamer
from vedit.timeline import levels, ops

REFRESH_MS = 33          # 30 Hz: fast enough to look continuous, cheap enough to ignore
PANEL_HEIGHT = 226


class MixerPanel(QWidget):
    """Channel strips across the foot of the Audio page."""

    status_message = Signal(str)

    def __init__(self, project: Project, streamer: AudioStreamer, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("MixerPanel")
        self.project = project
        self.streamer = streamer
        self.strips: dict[str, ChannelStrip] = {}

        self.master = MasterStrip(self)
        self.master.gain_moved.connect(self._master_moved)
        self.master.gain_committed.connect(self._master_committed)
        self.master.clips_cleared.connect(self.clear_clip_indicators)

        self._lanes = QHBoxLayout()
        self._lanes.setContentsMargins(0, 0, 0, 0)
        self._lanes.setSpacing(6)

        divider = QFrame(self)
        divider.setFrameShape(QFrame.VLine)
        divider.setFixedWidth(1)

        self._empty = QLabel("No audio lanes")
        self._empty.setObjectName("PlaceholderLabel")
        self._empty.setAlignment(Qt.AlignCenter)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        layout.addLayout(self._lanes)
        layout.addWidget(self._empty)
        layout.addStretch(1)
        layout.addWidget(divider)
        layout.addWidget(self.master)

        self.setFixedHeight(PANEL_HEIGHT)

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self._tick)

        project.timeline_changed.connect(self.refresh)
        self.refresh()

    # -- lifetime ---------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # No reason to drain the meter queue while the Edit page is showing.
        self._timer.start()

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def on_playing(self, playing: bool) -> None:
        """Drop every bar to the floor when playback stops.

        Left alone they would decay over three seconds after the sound had
        already gone, which reads as the meters being stuck.
        """
        if not playing:
            for strip in self.strips.values():
                strip.meter.reset()
            self.master.meter.reset()

    def clear_clip_indicators(self) -> None:
        for strip in self.strips.values():
            strip.meter.clear_clip()
        self.master.meter.clear_clip()

    # -- strips -----------------------------------------------------------------

    def refresh(self) -> None:
        """Take the strips' state from the model, rebuilding only if needed.

        Rebuilt only when the *set* of lanes changes. Rebuilding on every
        `timeline_changed` would destroy the widget under a fader mid-drag, and
        adding or removing a lane is rare enough to pay for the rare case.
        """
        tracks = self.project.timeline.audio_tracks
        if [track.track_id for track in tracks] != list(self.strips):
            self._rebuild(tracks)
        else:
            for track in tracks:
                self.strips[track.track_id].refresh(track)

        self._empty.setVisible(not tracks)
        self.master.refresh(self.project.timeline.master_gain_db)

    def _rebuild(self, tracks) -> None:
        while self._lanes.count():
            item = self._lanes.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.strips.clear()

        for track in tracks:
            strip = ChannelStrip(track, self)
            strip.gain_moved.connect(self._lane_moved)
            strip.gain_committed.connect(self._lane_committed)
            strip.mute_toggled.connect(self._mute)
            strip.solo_toggled.connect(self._solo)
            self._lanes.addWidget(strip)
            self.strips[track.track_id] = strip

    # -- fader routing ----------------------------------------------------------

    def _lane_moved(self, track_id: str, db: float) -> None:
        self.streamer.mixer.set_lane(track_id, levels.from_db(db))

    def _lane_committed(self, track_id: str, db: float) -> None:
        try:
            track = self.project.timeline.track_by_id(track_id)
        except Exception:  # noqa: BLE001 - the lane was deleted mid-drag
            return
        self.project.edit(f"{track.name} gain", lambda t: ops.set_track_gain(t, track, db))

    def _master_moved(self, db: float) -> None:
        self.streamer.mixer.set_master(levels.from_db(db))

    def _master_committed(self, db: float) -> None:
        self.project.edit("Master gain", lambda t: ops.set_master_gain(t, db))

    def _mute(self, track_id: str, muted: bool) -> None:
        track = self.project.timeline.track_by_id(track_id)
        label = ("Mute " if muted else "Unmute ") + track.name
        self.project.edit(label, lambda t: ops.set_track_muted(t, track, muted))

    def _solo(self, track_id: str, solo: bool) -> None:
        track = self.project.timeline.track_by_id(track_id)
        label = ("Solo " if solo else "Unsolo ") + track.name
        self.project.edit(label, lambda t: ops.set_track_solo(t, track, solo))

    # -- metering ---------------------------------------------------------------

    def _tick(self) -> None:
        """Feed every meter with what has actually been heard since the last tick.

        `meter_peaks()` returns None when nothing new has played — while parked,
        while the timeline is silent, and for the first fraction of a second
        after pressing play, where the decoder has run ahead but the speaker has
        not caught up. Passing that None through as "no attack" is what makes the
        bars start moving at the instant sound comes out rather than before it.
        """
        now = time.monotonic()
        frame = self.streamer.meter_peaks()

        for track_id, strip in self.strips.items():
            peak = frame.lanes.get(track_id, 0.0) if frame is not None else None
            clipped = bool(frame and frame.lane_clipped.get(track_id))
            strip.meter.feed(peak, now, clipped=clipped)

        self.master.meter.feed(
            frame.master if frame is not None else None,
            now,
            clipped=bool(frame and frame.master_clipped),
        )
