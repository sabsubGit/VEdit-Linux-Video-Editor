"""Decibels, fader travel and meter ballistics.

Qt-free and FFmpeg-free on purpose. The travel curve and the ballistics are the
parts of a mixer most likely to be got subtly wrong — a fader whose 0 dB does not
line up with the meter's 0 dB reads as broken long before anyone can say why —
and they are worth being able to test without a window or an audio device.

One curve serves three widgets: the channel fader, the level meter, and the
volume line drawn across an audio clip. That is deliberate. If they disagreed,
a clip at unity and a fader at unity would sit at different heights and the page
would stop reading as one instrument.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from vedit.core.timebase import TimeBase
from vedit.timeline.model import MAX_GAIN_DB, MIN_GAIN_DB

# A zero sample has no logarithm. This stands in for it: far below anything the
# fader can reach, so it always lands at the bottom of the travel.
SILENCE_DB = -100.0

# Bottom of both the meter and the fader. Matches MIN_GAIN_DB so a fader pulled
# all the way down and a meter reading nothing agree about where "off" is.
METER_FLOOR_DB = MIN_GAIN_DB

# Where unity sits along the travel. Three-quarters up leaves a visible quarter
# of headroom above it, so a clip pushed to +6 dB looks pushed rather than pinned
# to the top.
UNITY_FRACTION = 0.75

FULL_SCALE = 32768.0     # int16, the amplitude a peak of 1.0 corresponds to


# -- conversions ---------------------------------------------------------------


def to_db(amplitude: float) -> float:
    """Linear amplitude (0..1) to dBFS, floored rather than diverging at zero."""
    if amplitude <= 0.0:
        return SILENCE_DB
    return max(SILENCE_DB, 20.0 * math.log10(amplitude))


def from_db(db: float) -> float:
    """dB to a linear multiplier. At or below `SILENCE_DB` this is exactly zero."""
    if db <= SILENCE_DB:
        return 0.0
    return 10.0 ** (db / 20.0)


def clamp_gain(db: float) -> float:
    return max(MIN_GAIN_DB, min(MAX_GAIN_DB, db))


# -- the shared travel curve ---------------------------------------------------


def db_to_fraction(db: float) -> float:
    """Position along the travel, 0 at the floor and 1 at `MAX_GAIN_DB`.

    Piecewise linear with a break at unity rather than a single straight line:
    the bottom three-quarters cover -60..0 dB, where the useful adjustments are,
    and the top quarter covers 0..+12 dB. A single linear mapping would give the
    boost range a third of the travel, which is a third of the fader spent on the
    part of the range you touch least.
    """
    if db <= METER_FLOOR_DB:
        return 0.0
    if db >= MAX_GAIN_DB:
        return 1.0
    if db <= 0.0:
        return UNITY_FRACTION * (db - METER_FLOOR_DB) / (0.0 - METER_FLOOR_DB)
    return UNITY_FRACTION + (1.0 - UNITY_FRACTION) * (db / MAX_GAIN_DB)


def fraction_to_db(fraction: float) -> float:
    """Exact inverse of `db_to_fraction` over the whole travel."""
    fraction = max(0.0, min(1.0, fraction))
    if fraction <= UNITY_FRACTION:
        return METER_FLOOR_DB + (fraction / UNITY_FRACTION) * (0.0 - METER_FLOOR_DB)
    return ((fraction - UNITY_FRACTION) / (1.0 - UNITY_FRACTION)) * MAX_GAIN_DB


def amplitude_to_fraction(amplitude: float) -> float:
    """Meter bar height for a linear peak, through the same curve as the fader."""
    return db_to_fraction(to_db(amplitude))


# -- measuring a clip from its peak file ---------------------------------------


Peaks = tuple[int, list[tuple[int, int]]]


def peak_amplitude(peaks: Peaks, *, src_in: int, src_out: int, timebase: TimeBase) -> float:
    """Loudest sample in a clip's own window of its source, as 0..1.

    Only `[src_in, src_out)` is measured. Normalising against the whole file
    would be wrong for a clip trimmed away from the loud part — the usual case
    when someone reaches for Normalise at all.

    Reads the peak file made at ingest, so this costs nothing and touches no
    media: it is the same data the waveform is already drawn from.
    """
    per_second, samples = peaks
    if not samples or per_second <= 0 or src_out <= src_in:
        return 0.0

    first = int(float(timebase.frames_to_seconds(src_in)) * per_second)
    last = int(math.ceil(float(timebase.frames_to_seconds(src_out)) * per_second))
    window = samples[max(0, first) : min(len(samples), max(last, first + 1))]
    if not window:
        return 0.0

    loudest = max(max(abs(low), abs(high)) for low, high in window)
    return min(1.0, loudest / FULL_SCALE)


def normalise_gain_db(
    peaks: Peaks,
    *,
    src_in: int,
    src_out: int,
    timebase: TimeBase,
    target_db: float = -3.0,
) -> float:
    """Gain that puts the clip's loudest peak at `target_db`, clamped to range.

    Peak normalisation, not loudness normalisation: it is one subtraction against
    data that already exists, and for the "this clip is too quiet" case that is
    what people actually want. Proper LUFS would need the audio decoded.
    """
    peak = peak_amplitude(peaks, src_in=src_in, src_out=src_out, timebase=timebase)
    if peak <= 0.0:
        return 0.0
    return clamp_gain(target_db - to_db(peak))


# -- meter ballistics ----------------------------------------------------------


@dataclass(slots=True)
class MeterBallistics:
    """How a meter bar moves: instant attack, timed decay, with a held peak.

    Decay is computed against elapsed seconds rather than per tick, so a dropped
    repaint slows the fall rather than freezing the bar. That matters because the
    meter's timer competes with painting the timeline.

    The held peak falls rather than dropping instantly once its hold expires: a
    falling line reads as a decaying peak, an instant drop reads as a glitch.
    """

    decay_db_per_second: float = 20.0
    hold_seconds: float = 1.5
    hold_fall_db_per_second: float = 12.0

    level_db: float = METER_FLOOR_DB
    hold_db: float = METER_FLOOR_DB
    clipped: bool = False

    _hold_until: float = field(default=0.0, repr=False)
    _last: float | None = field(default=None, repr=False)

    def feed(self, peak: float | None, now: float, *, clipped: bool = False) -> None:
        """Advance to `now`, optionally attacking to `peak` (linear, 0..1).

        `peak is None` means nothing has played since the last call — the bar
        decays but never attacks. `peak == 0.0` means silence *did* play, which
        decays identically; the distinction only matters at the very start of
        playback, where None keeps the meter at rest.
        """
        previous = self._last
        elapsed = 0.0 if previous is None else max(0.0, now - previous)
        self._last = now

        self.level_db = max(METER_FLOOR_DB, self.level_db - self.decay_db_per_second * elapsed)

        # Only the part of the interval that fell outside the hold window counts
        # against the held peak. Using the whole interval would make a hold that
        # expires mid-tick fall by a tick's worth in one step.
        if previous is not None:
            falling = max(0.0, now - max(self._hold_until, previous))
            if falling > 0.0:
                self.hold_db = max(
                    METER_FLOOR_DB, self.hold_db - self.hold_fall_db_per_second * falling
                )

        if peak is not None:
            level = to_db(peak)
            if level > self.level_db:
                self.level_db = level
            if level >= self.hold_db:
                self.hold_db = level
                self._hold_until = now + self.hold_seconds

        # The latch is never cleared by decay: an overload you looked away from
        # is exactly the one worth still being told about.
        self.clipped = self.clipped or clipped

    def reset(self) -> None:
        """Drop to the floor — playback stopped. The clip latch survives."""
        self.level_db = METER_FLOOR_DB
        self.hold_db = METER_FLOOR_DB
        self._hold_until = 0.0
        self._last = None

    def clear_clip(self) -> None:
        self.clipped = False

    @property
    def level_fraction(self) -> float:
        return db_to_fraction(self.level_db)

    @property
    def hold_fraction(self) -> float:
        return db_to_fraction(self.hold_db)
