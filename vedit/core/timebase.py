"""Frame/time conversion.

The whole editor counts in **integer frames** at the project frame rate. Floats are
only allowed at the FFmpeg boundary, where we hand over or receive seconds. Keeping
this rule is what stops cuts from drifting a frame off on long timelines.
"""

from __future__ import annotations

from fractions import Fraction

# The rates that actually turn up in the wild. Probed rates get snapped to these so
# that a file reporting 29.97 is treated as exactly 30000/1001 rather than a float.
STANDARD_RATES: tuple[Fraction, ...] = (
    Fraction(24000, 1001),
    Fraction(24, 1),
    Fraction(25, 1),
    Fraction(30000, 1001),
    Fraction(30, 1),
    Fraction(48, 1),
    Fraction(50, 1),
    Fraction(60000, 1001),
    Fraction(60, 1),
    Fraction(120, 1),
)


def nearest_standard_rate(rate: float | Fraction, tolerance: float = 0.01) -> Fraction:
    """Snap a probed frame rate to a standard one when it is close enough.

    Containers report 23.976 or 24000/1001 depending on the muxer's mood; both must
    end up as the same exact Fraction. Anything genuinely unusual is preserved as a
    limited-denominator rational rather than being forced into the list.
    """
    rate = Fraction(rate).limit_denominator(1000000)
    for standard in STANDARD_RATES:
        if abs(float(standard) - float(rate)) <= tolerance:
            return standard
    return rate


class TimeBase:
    """Converts between frames, seconds and timecode at a fixed frame rate.

    Timecode is non-drop-frame: at 29.97 the displayed timecode drifts from wall
    clock, which is correct for NDF and matches what the frame count says.
    """

    __slots__ = ("fps",)

    def __init__(self, fps: float | Fraction = Fraction(30, 1)) -> None:
        fps = Fraction(fps).limit_denominator(1000000)
        if fps <= 0:
            raise ValueError(f"frame rate must be positive, got {fps}")
        self.fps = fps

    # -- construction ----------------------------------------------------------

    @classmethod
    def from_probe(cls, rate: float | Fraction) -> TimeBase:
        """Build from a rate reported by ffprobe, snapping to a standard rate."""
        return cls(nearest_standard_rate(rate))

    # -- frames <-> seconds ----------------------------------------------------

    def frames_to_seconds(self, frames: int) -> Fraction:
        """Exact start time of `frames`, as a Fraction so it never accumulates error."""
        return Fraction(frames) / self.fps

    def seconds_to_frames(self, seconds: float | Fraction) -> int:
        """Nearest frame to `seconds`.

        Rounds half away from zero rather than using Python's banker's rounding, so
        that a boundary lands where a user dragging the playhead expects it to.
        """
        exact = Fraction(seconds) * self.fps
        floor = exact.numerator // exact.denominator
        remainder = exact - floor
        return floor + 1 if remainder >= Fraction(1, 2) else floor

    def seconds_to_frames_floor(self, seconds: float | Fraction) -> int:
        """Frame containing `seconds`. Use when mapping a source time to a frame."""
        exact = Fraction(seconds) * self.fps
        return exact.numerator // exact.denominator

    def seconds_to_frames_ceil(self, seconds: float | Fraction) -> int:
        """Smallest frame count covering `seconds`. Use for durations, so that a
        clip is never silently truncated by a fraction of a frame."""
        exact = Fraction(seconds) * self.fps
        floor = exact.numerator // exact.denominator
        return floor if exact == floor else floor + 1

    def source_frames(self, seconds: float | Fraction) -> int:
        """Whole frames a source of this length can actually supply.

        Deliberately floors rather than rounding up. A 5.005-second file on a
        30 fps timeline contains 150 complete frames, not 151; claiming 151 makes
        the timeline promise a frame that no renderer can produce, so the preview
        and the export end up one frame apart.

        The epsilon absorbs float noise, so a file reporting 9.99999999 seconds
        still yields 300 frames at 30 fps rather than 299.
        """
        exact = Fraction(seconds) * self.fps + Fraction(1, 1000)
        return max(0, exact.numerator // exact.denominator)

    def frame_duration(self) -> Fraction:
        """Length of a single frame in seconds."""
        return Fraction(1) / self.fps

    # -- timecode --------------------------------------------------------------

    def frames_to_timecode(self, frames: int) -> str:
        """Format as HH:MM:SS:FF (non-drop-frame)."""
        sign = "-" if frames < 0 else ""
        frames = abs(frames)
        # Timecode counts whole frames per second, so 29.97 shows 30 frames per
        # second of timecode and simply runs slow against the wall clock.
        per_second = self.fps.numerator // self.fps.denominator
        if self.fps.denominator != 1:
            per_second = round(float(self.fps))
        per_second = max(per_second, 1)

        ff = frames % per_second
        total_seconds = frames // per_second
        ss = total_seconds % 60
        mm = (total_seconds // 60) % 60
        hh = total_seconds // 3600
        return f"{sign}{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"

    def timecode_to_frames(self, timecode: str) -> int:
        """Parse HH:MM:SS:FF (non-drop-frame) back to a frame count."""
        text = timecode.strip()
        negative = text.startswith("-")
        if negative:
            text = text[1:]

        parts = text.split(":")
        if len(parts) != 4:
            raise ValueError(f"expected HH:MM:SS:FF, got {timecode!r}")
        try:
            hh, mm, ss, ff = (int(p) for p in parts)
        except ValueError as exc:
            raise ValueError(f"non-numeric field in timecode {timecode!r}") from exc

        per_second = round(float(self.fps))
        per_second = max(per_second, 1)
        if ff >= per_second:
            raise ValueError(f"frame field {ff} exceeds {per_second} fps in {timecode!r}")

        total = ((hh * 60 + mm) * 60 + ss) * per_second + ff
        return -total if negative else total

    # -- rate conversion -------------------------------------------------------

    def rescale(self, frames: int, other: TimeBase) -> int:
        """Convert a frame count in `other`'s rate into this timebase.

        Needed whenever a 25 fps source is cut into a 30 fps timeline: the source
        in/out points are counted in source frames and have to land on real frames
        here without drifting.
        """
        return self.seconds_to_frames(other.frames_to_seconds(frames))

    # -- dunder ----------------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TimeBase) and self.fps == other.fps

    def __hash__(self) -> int:
        return hash(self.fps)

    def __repr__(self) -> str:
        if self.fps.denominator == 1:
            return f"TimeBase({self.fps.numerator})"
        return f"TimeBase({self.fps.numerator}/{self.fps.denominator})"
