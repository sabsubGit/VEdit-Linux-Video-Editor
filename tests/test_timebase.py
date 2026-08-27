from fractions import Fraction

import pytest

from vedit.core.timebase import STANDARD_RATES, TimeBase, nearest_standard_rate

ALL_RATES = [
    Fraction(24000, 1001),
    Fraction(24),
    Fraction(25),
    Fraction(30000, 1001),
    Fraction(30),
    Fraction(50),
    Fraction(60000, 1001),
    Fraction(60),
]


@pytest.mark.parametrize("fps", ALL_RATES)
def test_frames_seconds_roundtrip_is_exact(fps):
    tb = TimeBase(fps)
    for frame in (0, 1, 2, 999, 1000, 86399, 123457):
        assert tb.seconds_to_frames(tb.frames_to_seconds(frame)) == frame


@pytest.mark.parametrize("fps", ALL_RATES)
def test_no_drift_over_two_hours(fps):
    """The failure this guards against: accumulating float error until a cut lands
    on the wrong frame. Stepping frame by frame must stay exact for hours."""
    tb = TimeBase(fps)
    total = int(float(fps) * 7200)
    position = Fraction(0)
    for _ in range(2000):
        position += tb.frame_duration()
    assert tb.seconds_to_frames(position) == 2000
    # And the far end of a two-hour timeline still round-trips.
    assert tb.seconds_to_frames(tb.frames_to_seconds(total)) == total


def test_fractional_rate_is_not_approximated():
    tb = TimeBase(Fraction(30000, 1001))
    # One hour of 29.97 frames is deliberately NOT one hour of wall clock.
    one_hour_of_frames = 107892
    seconds = tb.frames_to_seconds(one_hour_of_frames)
    assert seconds == Fraction(107892 * 1001, 30000)
    assert float(seconds) == pytest.approx(3599.9964, abs=1e-4)


class TestRounding:
    def test_seconds_to_frames_rounds_half_away_from_zero(self):
        tb = TimeBase(30)
        # Exactly half a frame past frame 2 rounds up, not to even.
        assert tb.seconds_to_frames(Fraction(5, 2) / 30) == 3
        assert tb.seconds_to_frames(Fraction(3, 2) / 30) == 2

    def test_floor_and_ceil_differ_on_partial_frames(self):
        tb = TimeBase(25)
        partial = Fraction(1, 25) * Fraction(7, 2)  # 3.5 frames
        assert tb.seconds_to_frames_floor(partial) == 3
        assert tb.seconds_to_frames_ceil(partial) == 4

    def test_ceil_is_exact_on_whole_frames(self):
        tb = TimeBase(25)
        assert tb.seconds_to_frames_ceil(tb.frames_to_seconds(4)) == 4


class TestSourceFrames:
    """Guards a real one-frame mismatch: rounding a source length up made the
    timeline claim a frame the file could not supply, so the export came out one
    frame shorter than the timeline said it was."""

    def test_partial_trailing_frame_is_not_counted(self):
        tb = TimeBase(30)
        # 5.005s at 30fps is 150.15 frames — only 150 of them are complete.
        assert tb.source_frames(Fraction(5005, 1000)) == 150
        assert tb.seconds_to_frames_ceil(Fraction(5005, 1000)) == 151

    def test_exact_durations_are_unaffected(self):
        assert TimeBase(30).source_frames(10) == 300
        assert TimeBase(25).source_frames(8) == 200
        # 30 frames at 29.97 is 30 * 1001/30000 seconds.
        assert TimeBase(Fraction(30000, 1001)).source_frames(Fraction(1001, 1000)) == 30

    def test_float_noise_does_not_lose_a_frame(self):
        # A container reporting a hair under 10s must still yield 300 frames.
        assert TimeBase(30).source_frames(9.99999999) == 300

    def test_zero_and_tiny_durations(self):
        tb = TimeBase(30)
        assert tb.source_frames(0) == 0
        assert tb.source_frames(Fraction(1, 1000)) == 0

    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_never_exceeds_the_available_duration(self, fps):
        tb = TimeBase(fps)
        for millis in (1234, 5005, 8000, 33333, 60000):
            seconds = Fraction(millis, 1000)
            frames = tb.source_frames(seconds)
            # The claimed frames must fit inside the source, allowing the epsilon.
            assert tb.frames_to_seconds(frames) <= seconds + Fraction(1, 1000)


class TestTimecode:
    @pytest.mark.parametrize("fps", ALL_RATES)
    def test_timecode_roundtrip(self, fps):
        tb = TimeBase(fps)
        for frame in (0, 1, 50, 1500, 90000, 250000):
            assert tb.timecode_to_frames(tb.frames_to_timecode(frame)) == frame

    def test_timecode_format(self):
        tb = TimeBase(25)
        assert tb.frames_to_timecode(0) == "00:00:00:00"
        assert tb.frames_to_timecode(24) == "00:00:00:24"
        assert tb.frames_to_timecode(25) == "00:00:01:00"
        assert tb.frames_to_timecode(25 * 60) == "00:01:00:00"
        assert tb.frames_to_timecode(25 * 3600 + 25 * 61 + 7) == "01:01:01:07"

    def test_negative_timecode(self):
        tb = TimeBase(30)
        assert tb.frames_to_timecode(-31) == "-00:00:01:01"
        assert tb.timecode_to_frames("-00:00:01:01") == -31

    def test_ndf_timecode_uses_30_frames_at_2997(self):
        tb = TimeBase(Fraction(30000, 1001))
        assert tb.frames_to_timecode(29) == "00:00:00:29"
        assert tb.frames_to_timecode(30) == "00:00:01:00"

    @pytest.mark.parametrize("bad", ["00:00:01", "aa:bb:cc:dd", "", "1:2:3:4:5"])
    def test_malformed_timecode_raises(self, bad):
        with pytest.raises(ValueError):
            TimeBase(25).timecode_to_frames(bad)

    def test_frame_field_beyond_rate_raises(self):
        with pytest.raises(ValueError, match="exceeds"):
            TimeBase(25).timecode_to_frames("00:00:00:25")


class TestNearestStandardRate:
    @pytest.mark.parametrize(
        "probed,expected",
        [
            (23.976, Fraction(24000, 1001)),
            (23.976023976, Fraction(24000, 1001)),
            (24.0, Fraction(24)),
            (25.0, Fraction(25)),
            (29.97, Fraction(30000, 1001)),
            (29.970029970, Fraction(30000, 1001)),
            (30.0, Fraction(30)),
            (59.94, Fraction(60000, 1001)),
        ],
    )
    def test_snaps_probed_rates(self, probed, expected):
        assert nearest_standard_rate(probed) == expected

    def test_unusual_rate_is_preserved(self):
        # A 15 fps timelapse is real and must not be snapped to 24.
        assert nearest_standard_rate(15.0) == Fraction(15)

    def test_all_standard_rates_are_fixed_points(self):
        for rate in STANDARD_RATES:
            assert nearest_standard_rate(rate) == rate

    def test_from_probe_builds_exact_timebase(self):
        assert TimeBase.from_probe(29.97).fps == Fraction(30000, 1001)


class TestRescale:
    def test_25fps_source_into_30fps_timeline(self):
        source, timeline = TimeBase(25), TimeBase(30)
        # One second of source is one second of timeline.
        assert timeline.rescale(25, source) == 30
        assert timeline.rescale(50, source) == 60

    def test_rescale_is_identity_for_same_rate(self):
        tb = TimeBase(Fraction(24000, 1001))
        assert tb.rescale(1234, TimeBase(Fraction(24000, 1001))) == 1234


class TestConstruction:
    @pytest.mark.parametrize("bad", [0, -1, -0.5])
    def test_non_positive_rate_raises(self, bad):
        with pytest.raises(ValueError, match="positive"):
            TimeBase(bad)

    def test_equality_and_hash(self):
        assert TimeBase(30) == TimeBase(Fraction(30, 1))
        assert TimeBase(25) != TimeBase(30)
        assert len({TimeBase(30), TimeBase(Fraction(60, 2))}) == 1

    def test_repr(self):
        assert repr(TimeBase(30)) == "TimeBase(30)"
        assert repr(TimeBase(Fraction(30000, 1001))) == "TimeBase(30000/1001)"
