"""Playback engine: turns the timeline into moving pictures and sound.

The clock is the important design decision. When there is audio, the audio device
is the master and video chases it — audio glitches are far more noticeable than a
dropped frame, and a video-mastered clock forces the audio to resample or skip.
With no audio, a monotonic wall clock stands in.

Nothing here decodes. The video decoder and audio streamer each run their own
thread; this object only reads the clock, asks for the frame that belongs at that
instant, and reports position.

There is exactly one engine per window, shared by every page that shows a viewer.
Two engines would mean two decoder threads and two audio devices contending for
the same timeline, so the viewer is pointed at the engine rather than the other
way round — see `set_surface`.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from vedit.core.project import Project
from vedit.player.audio import AudioStreamer
from vedit.player.clock import MasterClock
from vedit.player.decoder import VideoDecoder
from vedit.player.segments import audio_playlists, build_video_playlist
from vedit.player.surface import VideoSurface


class PlaybackEngine(QObject):
    position_changed = Signal(int)
    state_changed = Signal(bool)      # playing?
    error = Signal(str)

    def __init__(
        self,
        project: Project,
        surface: VideoSurface | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        # Starts without one: the window builds the engine before the pages that
        # own the viewers, then points it at the first of them.
        self.surface = surface

        self.video = VideoDecoder(project.timebase)
        self.audio = AudioStreamer(project.timebase)
        self.clock = MasterClock(project.timebase)
        self.video.start()

        self._playing = False
        self._speed = 1.0
        self._position = 0
        self._stale = True
        # True while the engine is announcing its own position, so following the
        # playhead cannot turn into the engine chasing itself.
        self._emitting = False

        # Tick a little faster than the frame rate so we never systematically
        # miss a presentation deadline by rounding.
        interval = max(4, int(1000 / (float(project.timebase.fps) * 2)))
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setInterval(interval)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

        project.timeline_changed.connect(self.invalidate)
        project.playhead_changed.connect(self._on_playhead)

    # -- viewer -----------------------------------------------------------------

    def set_surface(self, surface: VideoSurface | None) -> None:
        """Point the engine at whichever page's viewer is on screen.

        Switching pages mid-playback keeps playing: the sound is uninterrupted
        and only the picture moves to the other widget.
        """
        if surface is self.surface:
            return
        self.surface = surface
        # The new surface has whatever was last blitted to it, or nothing. Next
        # tick fills it; clearing avoids a stale frame from a previous project.
        if not self._playing:
            self.video.seek(self._position)

    # -- playlist ---------------------------------------------------------------

    def invalidate(self) -> None:
        """Mark the flattened playlist stale; rebuilt on the next use."""
        self._stale = True

    def _rebuild(self) -> None:
        """Flatten the timeline into what the decoders should read.

        Video collapses every lane into one playlist with the topmost clip
        winning; audio stays one playlist per lane so the mixer can sum them.
        """
        timeline = self.project.timeline
        pool, proxies = self.project.pool, self.project.proxies

        self.video.set_playlist(
            build_video_playlist(timeline, pool, proxies), self._position
        )
        self.audio.set_playlists(
            audio_playlists(timeline, pool, proxies), self._position
        )
        # Fader positions live on the model; the mixer state is the copy the
        # decode thread reads. Resyncing here keeps a fader drag that was never
        # committed from surviving an undo.
        self.audio.mixer.load(timeline)
        self._stale = False

    def _ensure_fresh(self) -> None:
        if self._stale:
            self._rebuild()

    # -- transport --------------------------------------------------------------

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def position(self) -> int:
        return self._position

    def play(self, speed: float = 1.0) -> None:
        self._ensure_fresh()
        duration = self.project.timeline.duration
        if duration <= 0:
            return
        if self._position >= duration:
            self.seek(0)

        self._speed = speed
        self._playing = True
        # Audio only plays at normal speed; shuttling stays silent rather than
        # producing chipmunk artefacts.
        if abs(speed - 1.0) < 1e-6:
            self.audio.start(self._position)
        else:
            self.audio.stop()
        # Started after the audio, because whether a device actually opened
        # decides whether the clock should wait for it.
        self.clock.start(self._position, speed, wait_for_audio=self.audio.active)
        self.state_changed.emit(True)

    def pause(self) -> None:
        if not self._playing:
            return
        self._playing = False
        self.clock.stop()
        self.audio.stop()
        self.state_changed.emit(False)

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def seek(self, frame: int) -> None:
        """Jump to a frame, continuing to play if we already were."""
        self._ensure_fresh()
        frame = max(0, int(frame))
        self._position = frame
        self.video.seek(frame)
        if self._playing and abs(self._speed - 1.0) < 1e-6:
            self.audio.start(frame)
        else:
            self.audio.stop()
        # Same wait as on play: a seek restarts the device, so the clock would
        # otherwise run ahead of it and be dragged back a few frames later.
        self.clock.reset(frame, wait_for_audio=self.audio.active)
        self._announce(frame)

    def step(self, frames: int) -> None:
        self.pause()
        self.seek(self._position + frames)

    def stop(self) -> None:
        self._timer.stop()
        self.audio.stop()
        self.audio.shutdown()
        self.video.stop()

    # -- clock ------------------------------------------------------------------

    def _clock_frame(self) -> int:
        """Where playback should be right now.

        The clock free-runs and is steered by the audio device, so the position
        advances smoothly frame by frame while still following the only timebase
        that matches what the listener hears.
        """
        self.clock.discipline(self.audio.clock_frame())
        return self.clock.frame()

    def _on_playhead(self, frame: int) -> None:
        """Follow the playhead whenever something *else* moves it.

        This has to work during playback too: clicking the timeline mid-play
        should jump there and carry on. Previously it was ignored while playing,
        so the click set the playhead and the next tick immediately overwrote it
        from the clock — the marker snapped back and playback continued from
        where it had been.
        """
        if self._emitting or frame == self._position:
            return
        self.seek(frame)

    def _announce(self, frame: int) -> None:
        self._emitting = True
        try:
            self.position_changed.emit(frame)
        finally:
            self._emitting = False

    # -- the tick ---------------------------------------------------------------

    def _tick(self) -> None:
        error = self.video.take_error()
        if error:
            self.error.emit(error)

        target = self._clock_frame() if self._playing else self._position

        if self._playing:
            duration = self.project.timeline.duration
            if target >= duration:
                self._position = duration
                self._announce(self._position)
                self.pause()
                return
            if target != self._position:
                self._position = target
                self._announce(target)

        decoded = self.video.frame_for(target)
        if decoded is None and not self._playing:
            # While parked, show whatever arrived even if it is not an exact
            # match — an approximate frame beats a blank viewer during a scrub.
            decoded = self.video.frame_for(target + 2)

        if decoded is not None and self.surface is not None:
            if decoded.image.isNull():
                self.surface.clear("")
            else:
                self.surface.set_image(decoded.image)
