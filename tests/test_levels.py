"""Decibel maths, fader travel and meter ballistics.

Fully Qt-free and device-free. The ballistics are fed a fake monotonic clock, so
"decays at 20 dB per second" is asserted as arithmetic rather than by watching a
widget for a second.
"""

from __future__ import annotations

import math

import pytest

from vedit.core.timebase import TimeBase
from vedit.timeline import levels
from vedit.timeline.model import MAX_GAIN_DB, MIN_GAIN_DB


class TestConversions:
    def test_unity_is_zero_db(self):
        assert levels.to_db(1.0) == pytest.approx(0.0)
        assert levels.from_db(0.0) == pytest.approx(1.0)

    def test_half_amplitude_is_about_minus_six(self):
        assert levels.to_db(0.5) == pytest.approx(-6.0206, abs=1e-3)

    def test_round_trip(self):
        for db in (-48.0, -12.0, -3.0, 0.0, 6.0, 12.0):
            assert levels.to_db(levels.from_db(db)) == pytest.approx(db)

    def test_zero_amplitude_floors_rather_than_diverging(self):
        assert levels.to_db(0.0) == levels.SILENCE_DB
        assert levels.to_db(-0.5) == levels.SILENCE_DB

    def test_silence_converts_back_to_exactly_zero(self):
        assert levels.from_db(levels.SILENCE_DB) == 0.0
        assert levels.from_db(-200.0) == 0.0

    def test_clamp_gain(self):
        assert levels.clamp_gain(99.0) == MAX_GAIN_DB
        assert levels.clamp_gain(-99.0) == MIN_GAIN_DB
        assert levels.clamp_gain(-3.0) == -3.0


class TestTravelCurve:
    def test_unity_sits_three_quarters_up(self):
        assert levels.db_to_fraction(0.0) == pytest.approx(levels.UNITY_FRACTION)

    def test_ends_of_the_travel(self):
        assert levels.db_to_fraction(levels.METER_FLOOR_DB) == 0.0
        assert levels.db_to_fraction(MAX_GAIN_DB) == 1.0
        assert levels.db_to_fraction(-999.0) == 0.0
        assert levels.db_to_fraction(999.0) == 1.0

    def test_is_monotonic(self):
        previous = -1.0
        db = MIN_GAIN_DB
        while db <= MAX_GAIN_DB:
            fraction = levels.db_to_fraction(db)
            assert fraction >= previous
            previous = fraction
            db += 0.5

    def test_fraction_to_db_is_an_exact_inverse(self):
        db = MIN_GAIN_DB
        while db <= MAX_GAIN_DB:
            assert levels.fraction_to_db(levels.db_to_fraction(db)) == pytest.approx(db)
            db += 0.25

    def test_fraction_is_clamped_to_the_travel(self):
        assert levels.fraction_to_db(-1.0) == MIN_GAIN_DB
        assert levels.fraction_to_db(2.0) == MAX_GAIN_DB

    def test_amplitude_to_fraction_goes_through_the_same_curve(self):
        assert levels.amplitude_to_fraction(1.0) == pytest.approx(levels.UNITY_FRACTION)
        assert levels.amplitude_to_fraction(0.0) == 0.0


def peaks_for(values, per_second=100):
    """A peak file body: one (min, max) pair per bucket, from 0..1 amplitudes."""
    return (per_second, [(int(-v * levels.FULL_SCALE), int(v * levels.FULL_SCALE)) for v in values])


class TestPeakMeasurement:
    timebase = TimeBase(30)

    def test_peak_over_the_whole_clip(self):
        # 3 seconds at 100 buckets/s, loudest right in the middle.
        peaks = peaks_for([0.1] * 100 + [0.8] * 100 + [0.1] * 100)
        peak = levels.peak_amplitude(peaks, src_in=0, src_out=90, timebase=self.timebase)
        assert peak == pytest.approx(0.8, abs=1e-3)

    def test_a_window_that_excludes_the_loud_part(self):
        """The whole reason the window is honoured: a clip trimmed off the peak
        must normalise against what it actually plays."""
        peaks = peaks_for([0.1] * 100 + [0.8] * 100 + [0.1] * 100)
        peak = levels.peak_amplitude(peaks, src_in=0, src_out=30, timebase=self.timebase)
        assert peak == pytest.approx(0.1, abs=1e-3)

    def test_empty_or_degenerate_inputs(self):
        assert levels.peak_amplitude((100, []), src_in=0, src_out=30, timebase=self.timebase) == 0.0
        assert levels.peak_amplitude(peaks_for([0.5]), src_in=30, src_out=30, timebase=self.timebase) == 0.0

    def test_normalise_a_quiet_clip(self):
        quiet = 10 ** (-12.0 / 20.0)
        peaks = peaks_for([quiet] * 100)
        gain = levels.normalise_gain_db(
            peaks, src_in=0, src_out=30, timebase=self.timebase, target_db=-3.0
        )
        assert gain == pytest.approx(9.0, abs=0.05)

    def test_normalise_clamps_rather_than_asking_for_the_impossible(self):
        peaks = peaks_for([0.0001] * 100)
        gain = levels.normalise_gain_db(peaks, src_in=0, src_out=30, timebase=self.timebase)
        assert gain == MAX_GAIN_DB

    def test_normalise_silence_leaves_the_gain_alone(self):
        peaks = peaks_for([0.0] * 100)
        assert levels.normalise_gain_db(peaks, src_in=0, src_out=30, timebase=self.timebase) == 0.0

    def test_normalise_pulls_a_loud_clip_down(self):
        peaks = peaks_for([1.0] * 100)
        gain = levels.normalise_gain_db(peaks, src_in=0, src_out=30, timebase=self.timebase)
        assert gain == pytest.approx(-3.0, abs=0.05)


class TestBallistics:
    def test_attack_is_instant(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        assert meter.level_db == pytest.approx(0.0)

    def test_decay_is_twenty_db_per_second(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.feed(0.0, 0.5)
        assert meter.level_db == pytest.approx(-10.0)
        meter.feed(0.0, 1.0)
        assert meter.level_db == pytest.approx(-20.0)

    def test_decay_stops_at_the_floor(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.feed(None, 60.0)
        assert meter.level_db == levels.METER_FLOOR_DB

    def test_none_decays_without_attacking(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.feed(None, 0.25)
        assert meter.level_db == pytest.approx(-5.0)

    def test_a_dropped_repaint_slows_the_fall_rather_than_freezing_it(self):
        """Two ticks of 0.25 s must land where one tick of 0.5 s does."""
        stepped = levels.MeterBallistics()
        stepped.feed(1.0, 0.0)
        stepped.feed(None, 0.25)
        stepped.feed(None, 0.5)

        skipped = levels.MeterBallistics()
        skipped.feed(1.0, 0.0)
        skipped.feed(None, 0.5)

        assert stepped.level_db == pytest.approx(skipped.level_db)

    def test_peak_hold_survives_its_hold_then_falls(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.feed(0.0, 1.0)
        assert meter.hold_db == pytest.approx(0.0)      # still held at 1.0 s
        meter.feed(0.0, 2.0)                            # 0.5 s past the 1.5 s hold
        assert meter.hold_db == pytest.approx(-6.0)     # 12 dB/s

    def test_hold_sits_above_the_level(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.feed(0.0, 0.5)
        assert meter.hold_db > meter.level_db

    def test_clip_latch_survives_decay_and_reset(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0, clipped=True)
        meter.feed(0.0, 5.0)
        assert meter.clipped
        meter.reset()
        assert meter.clipped
        meter.clear_clip()
        assert not meter.clipped

    def test_reset_drops_to_the_floor(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.reset()
        assert meter.level_db == levels.METER_FLOOR_DB
        assert meter.hold_db == levels.METER_FLOOR_DB

    def test_reset_clears_elapsed_so_the_next_feed_does_not_jump(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        meter.reset()
        meter.feed(0.5, 100.0)
        assert meter.level_db == pytest.approx(levels.to_db(0.5))

    def test_fractions_track_the_curve(self):
        meter = levels.MeterBallistics()
        meter.feed(1.0, 0.0)
        assert meter.level_fraction == pytest.approx(levels.UNITY_FRACTION)
        assert meter.hold_fraction == pytest.approx(levels.UNITY_FRACTION)
