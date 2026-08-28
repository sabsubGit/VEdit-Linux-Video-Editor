"""The master clock.

An audio device reports its position in coarse steps — it hands out a buffer,
then reports nothing new until it wants the next one. Reading that value directly
makes playback advance in jumps of two or three frames and sit still in between,
so the video presentation drops every frame that falls inside a jump.

So the clock runs continuously off `time.monotonic()` and is *steered* by the
audio position rather than being read from it. Small errors are corrected
gradually, which keeps the picture smooth; large ones snap, because after a seek
or an underrun the old estimate is simply wrong.
"""

from __future__ import annotations

import time

from vedit.core.timebase import TimeBase

# Beyond this the estimate is not drifting, it is wrong — jump instead of easing.
SNAP_THRESHOLD_FRAMES = 5.0
# Fraction of the remaining error absorbed per correction. Low enough to stay
# invisible, high enough to converge in well under a second.
CORRECTION_GAIN = 0.15
# How long the clock will wait for the audio device to make its first sound
# before giving up and free-running. Opening a sink and filling its buffer takes
# a couple of hundred milliseconds; past that something is wrong with the device
# and a frozen playhead would be worse than an unsteered one.
HOLD_TIMEOUT_SECONDS = 0.4


class MasterClock:
    """Continuous playback position, optionally disciplined by an audio device."""

    def __init__(self, timebase: TimeBase) -> None:
        self.timebase = timebase
        self.speed = 1.0
        self._anchor_frame = 0.0
        self._anchor_time = 0.0
        self._running = False
        self._last_audio: float | None = None
        # See `_hold`: waiting for the audio device to start making sound.
        self._holding = False
        self._hold_until = 0.0

    # -- control ---------------------------------------------------------------

    def start(self, frame: int, speed: float = 1.0, wait_for_audio: bool = False) -> None:
        self.speed = speed
        self._anchor_frame = float(frame)
        self._anchor_time = time.monotonic()
        self._last_audio = None
        self._running = True
        self._hold(wait_for_audio)

    def stop(self) -> None:
        self._running = False

    def reset(self, frame: int, wait_for_audio: bool = False) -> None:
        """Jump to a frame, keeping whatever run state we had."""
        self._anchor_frame = float(frame)
        self._anchor_time = time.monotonic()
        self._last_audio = None
        self._hold(wait_for_audio)

    def _hold(self, waiting: bool) -> None:
        """Park the clock until the audio device reports its first progress.

        A sink does not make a sound the instant it is started: the thread has to
        decode a block and the device buffer has to fill, which is a good tenth
        of a second. Left free-running through that, the clock reaches frame N+k
        while the audio is still reporting N, and the first real reading drags it
        back — seen as the playhead lurching backwards a few frames just after
        play is pressed, then carrying on.

        Waiting instead costs the same delay, but spends it standing still, which
        is invisible, and starts the picture on the frame the sound starts on.
        """
        self._holding = waiting
        self._hold_until = time.monotonic() + HOLD_TIMEOUT_SECONDS

    def _release(self, at_frame: float | None = None) -> None:
        self._holding = False
        if at_frame is not None:
            self._anchor_frame = float(at_frame)
        self._anchor_time = time.monotonic()

    @property
    def running(self) -> bool:
        return self._running

    # -- reading ---------------------------------------------------------------

    def position(self) -> float:
        if not self._running:
            return self._anchor_frame
        if self._holding:
            if time.monotonic() < self._hold_until:
                return self._anchor_frame
            self._release()
        elapsed = time.monotonic() - self._anchor_time
        return self._anchor_frame + elapsed * float(self.timebase.fps) * self.speed

    def frame(self) -> int:
        return int(self.position())

    # -- steering --------------------------------------------------------------

    def discipline(self, audio_frame: float | None) -> None:
        """Nudge the clock toward the audio device's reported position.

        Called every tick. `audio_frame` repeating the same value simply means the
        device has not moved on yet, so it is ignored — acting on a stale reading
        is what produces the staircase this class exists to avoid.
        """
        if not self._running:
            return
        if self._holding:
            # Only a reading that has moved past the frame we started on proves
            # the device is really playing; it reports the start frame for as
            # long as its buffer is still filling.
            if audio_frame is not None and audio_frame > self._anchor_frame:
                self._release(audio_frame)
                self._last_audio = audio_frame
            elif time.monotonic() >= self._hold_until:
                self._release()
            return
        if audio_frame is None:
            return
        if self._last_audio is not None and audio_frame == self._last_audio:
            return
        self._last_audio = audio_frame

        error = audio_frame - self.position()
        if abs(error) > SNAP_THRESHOLD_FRAMES:
            self._anchor_frame = float(audio_frame)
            self._anchor_time = time.monotonic()
            return

        # Shift the anchor by part of the error. Because position() is measured
        # from the anchor, this eases the estimate across rather than stepping it.
        self._anchor_frame += error * CORRECTION_GAIN
