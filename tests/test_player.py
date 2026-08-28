"""Clock and playlist tests — the pure logic behind playback.

The clock test encodes a bug that cost real time to find: reading a coarse audio
position directly made playback advance in jumps of two or three frames, and the
video path discarded every frame that fell inside a jump. Two thirds of decoded
frames never reached the screen.
"""

from __future__ import annotations

import subprocess
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


class TestLaneReaderPosition:
    """Guards the bug that silenced audio partway through playback.

    Position was advanced by converting each decoded block to whole frames. A
    1024-sample AAC block is 0.64 frames at 30fps, and rounding that to 1 made
    the reader believe it was 1.56x further along than the audio it had actually
    produced. It ran off the end of the timeline early and the lane went silent
    for the rest of playback — on an 18-second timeline, after about 11 seconds.
    """

    def _reader(self, duration_frames=540, start=0):
        from vedit.player.audio import _LaneReader

        playlist = Playlist([Segment(0, duration_frames, None, 0)], duration_frames)
        return _LaneReader(playlist, TimeBase(30), start)

    def test_position_tracks_samples_not_rounded_frames(self):
        reader = self._reader()
        # Emit exactly one AAC block's worth: 0.64 frames, so still frame 0.
        reader._sample_pos += 1024
        assert reader.position == 0, "a partial frame must not count as a whole one"

        # Twenty blocks is 20480 samples = 12.8 frames.
        reader._sample_pos = 20480
        assert reader.position == 12

    def test_no_drift_over_a_long_timeline(self):
        """The failure mode: after a full timeline's worth of blocks the reader
        must be at the end, not far past it."""
        reader = self._reader(duration_frames=540)      # 18 seconds
        blocks = 0
        while not reader.finished and blocks < 100_000:
            reader._sample_pos += 1024
            blocks += 1

        produced_seconds = blocks * 1024 / 48000
        assert produced_seconds == pytest.approx(18.0, abs=0.05), (
            f"ran out after {produced_seconds:.1f}s of audio for an 18s timeline"
        )

    def test_finished_is_false_partway_through(self):
        reader = self._reader(duration_frames=540)
        reader._sample_pos = reader.samples_at(345)     # where it used to die
        assert not reader.finished

    def test_samples_at_matches_the_timebase(self):
        reader = self._reader()
        assert reader.samples_at(0) == 0
        assert reader.samples_at(30) == 48000, "one second at 30fps"
        assert reader.samples_at(540) == 48000 * 18

    def test_silence_fills_exactly_to_a_frame(self):
        reader = self._reader()
        reader._silence_to(30)
        assert reader._pending.size // 2 == 48000
        assert reader.position == 30

    def test_starting_partway_in(self):
        reader = self._reader(start=150)
        assert reader.position == 150
        assert reader.elapsed_seconds == pytest.approx(5.0)


class TestFrameConversion:
    """Guards a bug that left the viewer black while audio played on.

    swscale pads each output row up to an alignment boundary, so a frame whose
    row length is not already aligned comes back non-contiguous — 484 pixels is
    1452 bytes per row but arrives with a stride of 1488. Wrapping that buffer
    directly raises BufferError, which killed the decode thread for the whole
    session.

    The widths below are not exotic: 854 is standard 480p widescreen and 1080 is
    the width of any portrait phone video.
    """

    @staticmethod
    def _make(path, width, height=240):
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"testsrc2=size={width}x{height}:rate=30",
                "-t", "0.3", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuv420p", str(path),
            ],
            check=True,
            capture_output=True,
        )
        return path

    @pytest.mark.parametrize("width", [484, 486, 642, 854, 1080, 1234])
    def test_unaligned_widths_convert(self, width, tmp_path):
        import av

        from vedit.player.decoder import _to_qimage

        path = self._make(tmp_path / f"{width}.mp4", width)
        container = av.open(str(path))
        stream = container.streams.video[0]
        try:
            frame = next(container.decode(stream))
            # Confirm the fixture really does exercise the padded path.
            assert not frame.to_ndarray(format="rgb24").flags["C_CONTIGUOUS"]

            image = _to_qimage(frame)
            assert not image.isNull()
            assert image.width() == width
        finally:
            container.close()

    @pytest.mark.parametrize("width", [320, 640, 1280, 1920])
    def test_aligned_widths_still_convert(self, width, tmp_path):
        import av

        from vedit.player.decoder import _to_qimage

        path = self._make(tmp_path / f"{width}.mp4", width)
        container = av.open(str(path))
        try:
            image = _to_qimage(next(container.decode(container.streams.video[0])))
            assert not image.isNull()
            assert image.width() == width
        finally:
            container.close()

    def test_converted_image_survives_the_source_array(self, tmp_path):
        """The QImage must own its pixels; it outlives the ndarray it came from."""
        import gc

        import av

        from vedit.player.decoder import _to_qimage

        path = self._make(tmp_path / "life.mp4", 484)
        container = av.open(str(path))
        try:
            image = _to_qimage(next(container.decode(container.streams.video[0])))
        finally:
            container.close()
        gc.collect()
        assert image.pixelColor(10, 10).isValid()
