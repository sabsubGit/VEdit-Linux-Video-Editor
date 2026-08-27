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


class MasterClock:
    """Continuous playback position, optionally disciplined by an audio device."""

    def __init__(self, timebase: TimeBase) -> None:
        self.timebase = timebase
        self.speed = 1.0
        self._anchor_frame = 0.0
        self._anchor_time = 0.0
        self._running = False
        self._last_audio: float | None = None

    # -- control ---------------------------------------------------------------

    def start(self, frame: int, speed: float = 1.0) -> None:
        self.speed = speed
        self._anchor_frame = float(frame)
        self._anchor_time = time.monotonic()
        self._last_audio = None
        self._running = True

    def stop(self) -> None:
        self._running = False

    def reset(self, frame: int) -> None:
        """Jump to a frame, keeping whatever run state we had."""
        self._anchor_frame = float(frame)
        self._anchor_time = time.monotonic()
        self._last_audio = None

    @property
    def running(self) -> bool:
        return self._running

    # -- reading ---------------------------------------------------------------

    def position(self) -> float:
        if not self._running:
            return self._anchor_frame
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
        if not self._running or audio_frame is None:
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
