"""Clock and playlist tests — the pure logic behind playback.

The clock test encodes a bug that cost real time to find: reading a coarse audio
position directly made playback advance in jumps of two or three frames, and the
video path discarded every frame that fell inside a jump. Two thirds of decoded
frames never reached the screen.
"""

from __future__ import annotations

import time
from fractions import Fraction

import pytest

from vedit.core.timebase import TimeBase
from vedit.player.clock import CORRECTION_GAIN, SNAP_THRESHOLD_FRAMES, MasterClock
from vedit.player.segments import Playlist, Segment, build_playlist


class TestMasterClock:
    def test_free_runs_at_the_frame_rate(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        time.sleep(0.2)
        # Allow generous slack for scheduling; we are checking the rate, not jitter.
        assert 4 <= clock.frame() <= 9

    def test_stopped_clock_does_not_advance(self):
        clock = MasterClock(TimeBase(30))
        clock.start(100)
        clock.stop()
        first = clock.frame()
        time.sleep(0.1)
        assert clock.frame() == first

    def test_reset_jumps(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        clock.reset(500)
        assert clock.frame() == pytest.approx(500, abs=1)

    def test_speed_scales_the_rate(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0, speed=4.0)
        time.sleep(0.1)
        assert clock.frame() >= 8, "4x should cover far more ground than 1x"

    def test_position_is_continuous_not_stepped(self):
        """The actual regression: sampling the clock repeatedly between audio
        updates must yield a smooth ramp, not a staircase."""
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        # One audio update, then many reads with nothing new from the device.
        clock.discipline(0.0)
        readings = []
        for _ in range(20):
            readings.append(clock.position())
            time.sleep(0.005)
        assert readings == sorted(readings), "must never go backwards"
        assert len(set(int(r * 100) for r in readings)) > 5, (
            "position froze between audio updates — this is the staircase bug"
        )

    def test_repeated_audio_value_is_ignored(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        clock.discipline(10.0)
        anchor = clock._anchor_frame
        clock.discipline(10.0)
        clock.discipline(10.0)
        assert clock._anchor_frame == anchor, "a stale reading must not re-steer"

    def test_small_error_is_eased_not_snapped(self):
        clock = MasterClock(TimeBase(30))
        clock.start(100)
        before = clock.position()
        clock.discipline(before + 2.0)
        after = clock.position()
        # Absorbs part of the error, not all of it.
        assert before < after < before + 2.0
        assert after - before == pytest.approx(2.0 * CORRECTION_GAIN, abs=0.3)

    def test_large_error_snaps(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        target = SNAP_THRESHOLD_FRAMES + 50
        clock.discipline(target)
        assert clock.position() == pytest.approx(target, abs=1.0)

    def test_no_audio_leaves_the_clock_free_running(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        anchor = clock._anchor_frame
        clock.discipline(None)
        assert clock._anchor_frame == anchor

    def test_discipline_does_nothing_when_stopped(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        clock.stop()
        clock.discipline(900.0)
        assert clock.frame() == pytest.approx(0, abs=1)


class TestPlaylist:
    def _playlist(self):
        return Playlist(
            [
                Segment(0, 100, "a.mp4", 0),
                Segment(100, 150, None, 0),        # gap
                Segment(150, 300, "b.mp4", 20),
            ],
            300,
        )

    def test_lookup_by_frame(self):
        playlist = self._playlist()
        assert playlist.at(0).path == "a.mp4"
        assert playlist.at(99).path == "a.mp4"
        assert playlist.at(100).is_gap
        assert playlist.at(150).path == "b.mp4"
        assert playlist.at(299).path == "b.mp4"

    def test_out_of_range_returns_nothing(self):
        playlist = self._playlist()
        assert playlist.at(-1) is None
        assert playlist.at(300) is None, "duration is exclusive"

    def test_after_walks_forward(self):
        playlist = self._playlist()
        assert playlist.after(0).is_gap
        assert playlist.after(2) is None

    def test_source_seconds_accounts_for_the_in_point(self):
        timebase = TimeBase(30)
        segment = Segment(150, 300, "b.mp4", 20)
        # 10 frames into a segment whose source starts 20 frames in -> frame 30.
        assert segment.source_seconds(160, timebase) == pytest.approx(30 / 30)

    def test_empty_playlist(self):
        playlist = Playlist([], 0)
        assert not playlist
        assert playlist.at(0) is None


class TestBuildPlaylist:
    """Gaps must become explicit segments so the player renders black and
    silence for them rather than skipping."""

    def test_gaps_become_segments(self, monkeypatch):
        from vedit.timeline.model import Clip, Timeline, Track

        timeline = Timeline.default(TimeBase(30))
        track = timeline.video_tracks[0]
        track.insert(Clip(media_id="m", src_in=0, src_out=50, tl_start=50, src_length=50))
        timeline.audio_tracks[0].insert(
            Clip(media_id="m", src_in=0, src_out=50, tl_start=50, src_length=50, kind="audio")
        )

        class FakePool:
            def info_for(self, media_id):
                return None

        class FakeProxies:
            def playback_path(self, info):
                return None

        playlist = build_playlist(timeline, track, FakePool(), FakeProxies())
        assert [(s.tl_start, s.tl_end, s.is_gap) for s in playlist.segments] == [
            (0, 50, True),
            (50, 100, True),  # media not in the pool, so it reads as a gap
        ]

    def test_no_track_yields_empty(self):
        from vedit.timeline.model import Timeline

        timeline = Timeline.default(TimeBase(30))
        playlist = build_playlist(timeline, None, None, None)
        assert not playlist
