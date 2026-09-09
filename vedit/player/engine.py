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
from vedit.player.segments import (
    audio_playlists,
    build_dissolve_playlist,
    build_video_playlist,
)
from vedit.player.surface import VideoSurface
from vedit.timeline.framing import IDENTITY, Framing


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
        # A second decoder, for the outgoing half of a cross dissolve. It is the
        # one moment two shots are on screen together, and a decoder reads one
        # file at a time — so rather than complicate the first, there is a
        # second, whose playlist is gaps everywhere except across transitions.
        self.dissolve = VideoDecoder(project.timebase)
        self.audio = AudioStreamer(project.timebase)
        self.clock = MasterClock(project.timebase)
        self.video.start()
        self.dissolve.start()

        self._playing = False
        self._speed = 1.0
        self._position = 0
        # Set while a reframe drag is in flight; see `preview_picture`.
        self._preview_picture: Framing | None = None
        # Kept so the tick can ask whether the frame on screen is mid-dissolve
        # without re-flattening the timeline sixty times a second.
        self._video_playlist = None
        # The outgoing frame of a dissolve, held between decodes; see
        # `_apply_dissolve`.
        self._under_frame = None
        # The frame the viewer is currently showing, so a late outgoing frame
        # can still be mixed into it.
        self._shown_frame: int | None = None
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
        if surface is not None:
            timeline = self.project.timeline
            surface.set_frame_size(timeline.width, timeline.height)
        # The new surface has whatever was last blitted to it, or nothing. Next
        # tick fills it; clearing avoids a stale frame from a previous project.
        if not self._playing:
            self.video.seek(self._position)
            self.dissolve.seek(self._position)

    # -- playlist ---------------------------------------------------------------

    def invalidate(self) -> None:
        """Mark the flattened playlist stale; rebuilt on the next use.

        The picture is refreshed straight away rather than waiting for that,
        because framing does not change what is decoded — only how the frame in
        hand is drawn. Parked on a clip, rotating it has to show immediately;
        the tick would not repaint until the decoder produced a new frame, and
        while parked it has no reason to produce one.
        """
        self._stale = True
        self._apply_picture(self._position)

    def _rebuild(self) -> None:
        """Flatten the timeline into what the decoders should read.

        Video collapses every lane into one playlist with the topmost clip
        winning; audio stays one playlist per lane so the mixer can sum them.
        """
        timeline = self.project.timeline
        pool, proxies = self.project.pool, self.project.proxies

        self._video_playlist = build_video_playlist(timeline, pool, proxies)
        self.video.set_playlist(self._video_playlist, self._position)
        # The outgoing half of every cross dissolve. Its playlist is gaps from
        # end to end unless the edit actually contains a transition, so a
        # timeline with none costs a thread that never decodes anything.
        self.dissolve.set_playlist(
            build_dissolve_playlist(timeline, pool, proxies), self._position
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
        # Land on the frame under the playhead. If decode had fallen behind,
        # the queue still holds frames from before the pause, and the tick would
        # hand them to the viewer one after another — the picture carries on
        # playing for as long as it takes to work through the backlog, seconds
        # after the sound stopped. Re-seeking throws that away and decodes the
        # one frame that is actually being looked at.
        self.video.seek(self._position)
        self.dissolve.seek(self._position)
        self._under_frame = None
        self._shown_frame = None
        self.state_changed.emit(False)

    def toggle(self) -> None:
        self.pause() if self._playing else self.play()

    def seek(self, frame: int) -> None:
        """Jump to a frame, continuing to play if we already were."""
        self._ensure_fresh()
        frame = max(0, int(frame))
        self._position = frame
        self.video.seek(frame)
        self.dissolve.seek(frame)
        self._under_frame = None
        self._shown_frame = None
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
        self.dissolve.stop()

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
                # A gap — but a title card is words over a gap, so the titles
                # for this frame still have to be worked out and drawn.
                self._shown_frame = decoded.frame_index
                self.surface.clear("")
                self._apply_picture(decoded.frame_index)
            else:
                self._shown_frame = decoded.frame_index
                self._apply_picture(decoded.frame_index)
                self.surface.set_image(decoded.image)

        # Every tick, not only the ones that produced a picture. The two
        # decoders do not arrive in step, and gating this on the main one meant
        # a transition whose outgoing frame turned up a moment late was never
        # composited at all — the tick that would have done it never came,
        # because parked playback decodes nothing more.
        if self._shown_frame is not None:
            self._apply_dissolve(self._shown_frame)

    def _apply_dissolve(self, frame: int) -> None:
        """Put the outgoing shot of a cross dissolve under the incoming one.

        Read from the playlist rather than the model, because unlike framing
        this genuinely needs a second stream decoded, and the playlist is what
        the second decoder was pointed at. The mix runs 0 to 1 across the
        transition, matching `xfade`'s linear ramp in the export.
        """
        if self.surface is None:
            return
        playlist = self._video_playlist
        segment = playlist.at(frame) if playlist is not None else None
        if segment is None or not segment.dissolve:
            self._under_frame = None
            self.surface.set_dissolve(None)
            return

        # `frame_for` *consumes*: it hands a frame over and drops it from the
        # queue. Most ticks therefore find nothing waiting, and clearing the mix
        # on those would strobe the transition between blended and not. The main
        # picture keeps the last frame it was given for exactly this reason, and
        # the outgoing half has to do the same.
        fresh = self.dissolve.frame_for(frame)
        if fresh is not None and not fresh.image.isNull():
            self._under_frame = fresh
        if self._under_frame is None:
            # Nothing decoded yet — early in a scrub into a transition. Showing
            # the incoming shot alone would be wrong at the head of a dissolve,
            # so nothing is drawn over the previous frame until it arrives.
            return

        under = self._under_frame
        # Matches `xfade`, which is fully the outgoing shot at offset zero and
        # fully the incoming one a transition-length later. Biasing this by a
        # frame would make the preview and the export disagree by a frame the
        # whole way through.
        elapsed = frame - segment.dissolve_from
        mix = max(0.0, min(1.0, elapsed / segment.dissolve))
        outgoing = self._outgoing_clip(frame)
        self.surface.set_dissolve(
            under.image,
            outgoing.framing_at(frame) if outgoing else IDENTITY,
            outgoing.rotation if outgoing else 0,
            outgoing.flipped if outgoing else False,
            mix,
        )

    def _outgoing_clip(self, frame: int):
        """The clip a dissolve at `frame` is coming *from*, for its picture."""
        timeline = self.project.timeline
        for track in timeline.video_tracks:
            if track.muted:
                continue
            clip = track.clip_at(frame)
            if clip is None or not clip.enabled:
                continue
            found = track.dissolve_before(clip)
            if found is not None and frame < clip.tl_start + found[1]:
                return found[0]
        return None

    def _apply_picture(self, frame: int) -> None:
        """Tell the surface how the clip on screen is framed.

        Read from the model rather than from the flattened playlist, for two
        reasons. Framing does not affect decoding, so it must stay out of the
        decoder's restart path — routing it through `invalidate` would clear the
        queue and force a reseek on every step of a drag. And the playlist is
        only rebuilt on play or seek, so a framing edit made while parked would
        not show until the next one.

        The lookup uses the frame the decoder actually handed back, not the one
        that was asked for: while parked the tick will accept a frame from a
        little further on, and across a cut that belongs to the next clip. Using
        the requested frame would flash the neighbour's framing for a moment.
        """
        if self.surface is None:
            return
        if self._preview_picture is not None:
            # A reframe drag is in flight. It owns the viewer until it commits,
            # so the model value would only fight it.
            return
        timeline = self.project.timeline
        self.surface.set_frame_size(timeline.width, timeline.height)
        self.surface.set_titles(
            [clip.title for clip in timeline.titles_at(frame)]
        )
        clip = timeline.video_clip_at(frame)
        if clip is None:
            self.surface.set_picture(IDENTITY)
        else:
            # `framing_at` rather than `framing`: a clip whose framing travels
            # shows a different part of itself at every frame, and the viewer
            # has to be the place you can watch that happen.
            self.surface.set_picture(
                clip.framing_at(frame), clip.rotation, clip.flipped
            )

    def preview_picture(
        self, framing: Framing | None, rotation: int = 0, flipped: bool = False
    ) -> None:
        """Hold the viewer at a framing that is not on the model yet.

        What makes a reframe drag feel live: the overlay pushes each mouse move
        straight through to the surface, which repaints from the frame already
        decoded. Passing `None` hands the viewer back to the timeline.
        """
        self._preview_picture = framing
        if self.surface is None:
            return
        if framing is None:
            self._apply_picture(self._position)
        else:
            self.surface.set_picture(framing, rotation, flipped)
