"""Clock and playlist tests — the pure logic behind playback.

The clock test encodes a bug that cost real time to find: reading a coarse audio
position directly made playback advance in jumps of two or three frames, and the
video path discarded every frame that fell inside a jump. Two thirds of decoded
frames never reached the screen.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from fractions import Fraction

import pytest

import numpy as np
from PySide6.QtGui import QImage

from vedit.core.timebase import TimeBase
from vedit.player.audio import (
    MeterFrame,
    MeterQueue,
    MixerState,
    _signature,
    envelope,
)
from vedit.player.clock import (
    CORRECTION_GAIN,
    HOLD_TIMEOUT_SECONDS,
    SNAP_THRESHOLD_FRAMES,
    MasterClock,
)
from vedit.player.decoder import DecodedFrame
from vedit.player.segments import Playlist, Segment, build_playlist
from vedit.timeline.model import Timeline


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

    def test_waiting_for_audio_holds_instead_of_running_ahead(self):
        """The lurch-backwards bug: a device takes a moment to make its first
        sound, and the clock must not cover that ground and be dragged back."""
        clock = MasterClock(TimeBase(30))
        clock.start(100, wait_for_audio=True)
        time.sleep(0.15)
        # The device is open but still filling its buffer: it keeps reporting the
        # frame we started on.
        clock.discipline(100.0)
        assert clock.position() == 100.0, "must not advance before the sound does"

        # First real progress: the clock takes it and runs on from there.
        clock.discipline(100.5)
        first = clock.position()
        assert first == pytest.approx(100.5, abs=0.2)
        time.sleep(0.05)
        assert clock.position() > first

    def test_waiting_never_moves_backwards(self):
        clock = MasterClock(TimeBase(30))
        clock.start(100, wait_for_audio=True)
        readings = []
        for i in range(30):
            clock.discipline(100.0 if i < 15 else 100.0 + (i - 14) * 0.5)
            readings.append(clock.position())
            time.sleep(0.005)
        assert readings == sorted(readings), "the playhead must never go back"

    def test_hold_times_out_if_the_device_never_speaks(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0, wait_for_audio=True)
        clock.discipline(None)
        assert clock.position() == 0.0
        time.sleep(HOLD_TIMEOUT_SECONDS + 0.05)
        clock.discipline(None)
        assert clock.position() > 0.0, "a dead device must not freeze the playhead"

    def test_not_waiting_starts_immediately(self):
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        time.sleep(0.1)
        assert clock.position() > 0.0

    def test_reset_can_wait_for_the_device_too(self):
        """A seek mid-playback restarts the sink, so it needs the same wait."""
        clock = MasterClock(TimeBase(30))
        clock.start(0)
        clock.reset(500, wait_for_audio=True)
        time.sleep(0.1)
        assert clock.position() == 500.0
        clock.discipline(501.0)
        assert clock.position() == pytest.approx(501.0, abs=0.2)

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


class TestEnvelope:
    """The fade curve. Pure numpy, no device and no file.

    The mid-fade test is the one that matters: blocks arrive about 50 ms at a
    time, so a two-second fade is spread over forty of them and a curve derived
    from the position within a block would restart the ramp forty times.
    """

    def test_no_fades_is_a_flat_gain(self):
        curve = envelope(64, offset=0, length=1000, fade_in=0, fade_out=0, gain=0.5)
        assert curve.shape == (64,)
        assert np.allclose(curve, 0.5)

    def test_a_fade_in_ramps_from_zero_to_one(self):
        curve = envelope(100, offset=0, length=1000, fade_in=100, fade_out=0, gain=1.0)
        assert curve[0] == pytest.approx(0.0)
        assert curve[50] == pytest.approx(0.5)
        assert curve[-1] == pytest.approx(0.99)

    def test_a_block_starting_mid_fade_continues_the_ramp(self):
        curve = envelope(500, offset=1000, length=100_000, fade_in=4000, fade_out=0, gain=1.0)
        assert curve[0] == pytest.approx(0.25)
        assert curve[-1] == pytest.approx(1499 / 4000)

    def test_a_block_past_the_fade_is_flat(self):
        curve = envelope(64, offset=9000, length=100_000, fade_in=4000, fade_out=0, gain=1.0)
        assert np.allclose(curve, 1.0)

    def test_a_fade_out_falls_to_zero_at_the_end(self):
        curve = envelope(100, offset=900, length=1000, fade_in=0, fade_out=100, gain=1.0)
        assert curve[0] == pytest.approx(1.0)
        assert curve[-1] == pytest.approx(0.01)

    def test_overlapping_fades_take_the_quieter_of_the_two(self):
        curve = envelope(100, offset=0, length=100, fade_in=60, fade_out=60, gain=1.0)
        assert curve.max() < 1.0
        assert curve[0] == pytest.approx(0.0)
        assert curve[-1] == pytest.approx(1 / 60, abs=1e-6)

    def test_gain_scales_the_whole_curve(self):
        plain = envelope(50, offset=0, length=1000, fade_in=100, fade_out=0, gain=1.0)
        loud = envelope(50, offset=0, length=1000, fade_in=100, fade_out=0, gain=2.0)
        assert np.allclose(loud, plain * 2.0)

    def test_never_goes_negative_past_the_end(self):
        """A clip trimmed shorter than its fade would otherwise invert the audio."""
        curve = envelope(200, offset=950, length=1000, fade_in=0, fade_out=100, gain=1.0)
        assert curve.min() >= 0.0

    def test_returns_float32(self):
        curve = envelope(16, offset=0, length=100, fade_in=8, fade_out=0, gain=1.0)
        assert curve.dtype == np.float32


def meter_frame(sample, count, *, master=0.0, lanes=None, clipped=False):
    return MeterFrame(
        sample=sample,
        count=count,
        lanes=lanes or {},
        lane_clipped={},
        master=master,
        master_clipped=clipped,
    )


class TestMeterQueue:
    """Latency compensation for the meters.

    `drain` takes the heard position as an argument rather than reading the
    device, which is what makes all of this testable with no QAudioSink in the
    process at all.
    """

    def test_a_block_is_held_until_it_has_fully_played(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 2400, master=0.5))
        assert queue.drain(0) is None
        assert queue.drain(2399) is None
        assert queue.drain(2400) is not None

    def test_a_drained_block_is_not_returned_twice(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 2400, master=0.5))
        assert queue.drain(5000) is not None
        assert queue.drain(5000) is None

    def test_several_blocks_reduce_to_the_loudest(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 100, master=0.2, lanes={"t1": 0.2}))
        queue.push(meter_frame(100, 100, master=0.9, lanes={"t1": 0.9}))
        queue.push(meter_frame(200, 100, master=0.4, lanes={"t1": 0.4}))
        merged = queue.drain(300)
        assert merged.master == pytest.approx(0.9)
        assert merged.lanes["t1"] == pytest.approx(0.9)

    def test_a_clip_flag_survives_the_merge(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 100))
        queue.push(meter_frame(100, 100, clipped=True))
        assert queue.drain(200).master_clipped is True

    def test_lanes_that_appear_in_only_some_blocks_are_kept(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 100, lanes={"t1": 0.3}))
        queue.push(meter_frame(100, 100, lanes={"t2": 0.7}))
        merged = queue.drain(200)
        assert merged.lanes == {"t1": pytest.approx(0.3), "t2": pytest.approx(0.7)}

    def test_only_played_blocks_are_taken(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 100, master=0.2))
        queue.push(meter_frame(100, 100, master=0.9))
        assert queue.drain(100).master == pytest.approx(0.2)
        assert queue.drain(200).master == pytest.approx(0.9)

    def test_clear_empties_it(self):
        queue = MeterQueue()
        queue.push(meter_frame(0, 100))
        queue.clear()
        assert queue.drain(10_000) is None

    def test_the_oldest_frames_are_dropped_when_it_overflows(self):
        queue = MeterQueue(capacity=4)
        for index in range(8):
            queue.push(meter_frame(index * 100, 100, master=index / 10))
        merged = queue.drain(10_000)
        assert merged.sample == 400, "the first four blocks were discarded"

    def test_the_underrun_padding_correction_holds_a_block_back(self):
        """Silence handed back on an underrun advances the device's clock but was
        never metered. Without discounting it the meters run permanently ahead of
        the sound, and the error accumulates with every hiccup."""
        queue = MeterQueue()
        queue.push(meter_frame(0, 2400, master=0.5))
        heard, padded = 2400.0, 1000.0
        assert queue.drain(heard) is not None

        queue.push(meter_frame(2400, 2400, master=0.5))
        assert queue.drain(4800 - padded) is None, "padding must not release it early"
        assert queue.drain(4800) is not None


class TestMixerState:
    def test_lanes_and_master_default_to_unity(self):
        state = MixerState()
        lanes, master = state.snapshot()
        assert lanes == {} and master == 1.0

    def test_set_lane_is_visible_in_the_snapshot(self):
        state = MixerState()
        state.set_lane("t1", 0.5)
        assert state.snapshot()[0]["t1"] == 0.5

    def test_load_converts_the_model_from_db(self):
        timeline = Timeline.default(TimeBase(30))
        timeline.audio_tracks[0].gain_db = -6.0
        timeline.master_gain_db = 6.0
        state = MixerState()
        state.load(timeline)
        lanes, master = state.snapshot()
        assert lanes[timeline.audio_tracks[0].track_id] == pytest.approx(0.501187, abs=1e-5)
        assert master == pytest.approx(1.995262, abs=1e-5)

    def test_a_snapshot_is_a_copy(self):
        state = MixerState()
        state.set_lane("t1", 0.5)
        lanes, _ = state.snapshot()
        lanes["t1"] = 99.0
        assert state.snapshot()[0]["t1"] == 0.5


class TestPlaylistSignature:
    """The guard on "moving a fader does not restart the device"."""

    def make(self, *, gain=1.0, fade_in=0):
        segment = Segment(0, 100, Path("/a.mp4"), 0, "m1", 1.0, "c1", gain, fade_in, 0)
        return [Playlist([segment], 100, "t1")]

    def test_identical_playlists_match(self):
        assert _signature(self.make()) == _signature(self.make())

    def test_a_clip_gain_change_is_visible(self):
        assert _signature(self.make()) != _signature(self.make(gain=0.5))

    def test_a_fade_change_is_visible(self):
        assert _signature(self.make()) != _signature(self.make(fade_in=30))

    def test_a_track_gain_change_is_not(self):
        """Lane gain is read live by the mixer, so it never reaches the playlist
        and must never cost a restart."""
        timeline = Timeline.default(TimeBase(30))
        before = _signature(self.make())
        timeline.audio_tracks[0].gain_db = -6.0
        assert _signature(self.make()) == before


class TestReversedPreview:
    """Reverse in the *preview*, which cannot work the way the render does.

    The renderer hands the whole span to ffmpeg's `reverse`, which buffers it.
    Playback cannot: a long clip would eat the machine. So video re-seeks once
    per frame and audio decodes short spans and flips them, and both have to
    come out in the same order the export will.
    """

    @staticmethod
    def _split_source(path, seconds=2.0):
        """Black and silent for the first half, white and loud for the second."""
        half = seconds / 2
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-f", "lavfi", "-i", f"color=c=black:s=160x120:r=30:d={half}",
                "-f", "lavfi", "-i", f"color=c=white:s=160x120:r=30:d={half}",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                "-filter_complex",
                f"[0:v][1:v]concat=n=2:v=1:a=0[v];[2:a]volume=0:enable='lt(t,{half})'[a]",
                "-map", "[v]", "-map", "[a]",
                "-c:v", "libx264", "-preset", "ultrafast", "-g", "6",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
            ],
            check=True,
            capture_output=True,
        )
        return path

    @staticmethod
    def _brightness(image):
        """Mean luma of a handful of pixels — enough to tell black from white."""
        samples = [
            image.pixelColor(x, y).lightness()
            for x in (20, 80, 140)
            for y in (20, 60, 100)
        ]
        return sum(samples) / len(samples)

    def _decode_frame(self, path, frame, *, backwards, length=60):
        from vedit.player.decoder import VideoDecoder

        segment = Segment(0, length, Path(path), src_start=length if backwards else 0,
                          reversed=backwards)
        decoder = VideoDecoder(TimeBase(30))
        decoder.set_playlist(Playlist([segment], length), frame)
        decoder.start()
        try:
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                decoded = decoder.frame_for(frame)
                if decoded is not None and not decoded.image.isNull():
                    return decoded
                time.sleep(0.02)
            error = decoder.take_error()
            raise AssertionError(f"no frame for {frame} within the timeout ({error})")
        finally:
            decoder.stop()

    def test_the_first_frame_back_is_the_end_of_the_source(self, tmp_path):
        path = self._split_source(tmp_path / "split.mp4")
        forward = self._decode_frame(path, 0, backwards=False)
        backwards = self._decode_frame(path, 0, backwards=True)

        assert self._brightness(forward.image) < 40, "the source starts black"
        assert self._brightness(backwards.image) > 200, (
            "played backwards it has to start on the white half"
        )

    def test_the_last_frame_back_is_the_start_of_the_source(self, tmp_path):
        path = self._split_source(tmp_path / "split.mp4")
        backwards = self._decode_frame(path, 55, backwards=True)
        assert self._brightness(backwards.image) < 40

    def test_audio_comes_out_loud_end_first(self, tmp_path):
        from vedit.player.audio import _LaneReader

        path = self._split_source(tmp_path / "split.mp4")
        segment = Segment(0, 60, Path(path), src_start=60, reversed=True)
        reader = _LaneReader(Playlist([segment], 60), TimeBase(30), 0)
        stop = threading.Event()
        try:
            # Half a second from each end of the two-second clip.
            head = reader.read(24000, stop)
            reader.read(24000 * 2, stop)
            tail = reader.read(24000, stop)
        finally:
            reader.close()

        # Compared as a ratio: the reader hands back int16-scaled floats, and
        # what matters is that one window is loud and the other is not.
        assert np.abs(tail).mean() * 100 < np.abs(head).mean(), (
            "the tone should be at the head now and the silence at the tail"
        )

    def test_audio_forwards_is_the_other_way_round(self, tmp_path):
        """The control, so the test above is measuring the reversal."""
        from vedit.player.audio import _LaneReader

        path = self._split_source(tmp_path / "split.mp4")
        segment = Segment(0, 60, Path(path), src_start=0)
        reader = _LaneReader(Playlist([segment], 60), TimeBase(30), 0)
        stop = threading.Event()
        try:
            head = reader.read(24000, stop)
            reader.read(24000 * 2, stop)
            tail = reader.read(24000, stop)
        finally:
            reader.close()

        assert np.abs(head).mean() * 100 < np.abs(tail).mean()

    def test_a_reversed_lane_produces_the_length_it_should(self, tmp_path):
        """Reading a reversed segment must end where a forward one would, or the
        lane drifts out of step with every other lane on the timeline."""
        from vedit.player.audio import _LaneReader

        path = self._split_source(tmp_path / "split.mp4")
        segment = Segment(0, 60, Path(path), src_start=60, reversed=True)
        reader = _LaneReader(Playlist([segment], 60), TimeBase(30), 0)
        stop = threading.Event()
        try:
            for _ in range(20):
                reader.read(4800, stop)
        finally:
            reader.close()

        assert reader.finished
        assert reader.position == pytest.approx(60, abs=1)



class TestKeepingUpWithTheClock:
    """What happens when decode is slower than playback.

    The queue is drained by `frame_for`, so a decoder that has fallen behind is
    never blocked — it just keeps decoding frames the clock went past, each one
    delaying the next. Nothing pulled it back towards the clock, so on media it
    could not decode in real time the picture drifted seconds behind the sound
    and stayed there; pausing then left the backlog to play itself out, which
    looked like the video carrying on for several seconds after the sound
    stopped.
    """

    def _decoder(self):
        from vedit.player.decoder import VideoDecoder

        decoder = VideoDecoder(TimeBase(30))
        decoder._playlist = Playlist([Segment(0, 10000, "a.mp4", 0)], 10000)
        return decoder

    def test_the_wanted_frame_is_recorded_even_with_nothing_queued(self):
        """The only channel the decode thread has to the clock."""
        decoder = self._decoder()
        assert decoder.frame_for(400) is None
        assert decoder._wanted == 400

    def test_running_ahead_is_not_lateness(self):
        decoder = self._decoder()
        decoder.frame_for(100)
        decoder._position = 108          # prefetching, the healthy case
        assert decoder._lateness(decoder._position) == -8

    def test_a_decoder_nobody_has_asked_anything_of_is_not_late(self):
        decoder = self._decoder()
        decoder._position = 500
        assert decoder._lateness(500) is None

    def test_slightly_behind_still_converts_every_frame(self, monkeypatch):
        """Under the threshold nothing changes: a hiccup must not drop frames."""
        decoder = self._decoder()
        decoder.frame_for(105)
        decoder._position = 100
        assert decoder._lateness(100) < decoder._late_frames

    def test_far_behind_seeks_instead_of_grinding_forward(self):
        """Two seconds back, walking the stream will never close the gap."""
        decoder = self._decoder()
        decoder.frame_for(400)
        decoder._position = 100
        decoder._queue = [DecodedFrame(100, QImage()), DecodedFrame(101, QImage())]

        sought = []
        decoder._do_seek = lambda playlist, target, generation: sought.append(target)
        decoder._decode_one(decoder._playlist, decoder._generation)

        assert sought == [400], "should have jumped to where the clock is"
        assert decoder._queue == [], "the queued frames are all in the past now"

    def test_a_seek_forgets_what_was_wanted_before_it(self):
        """Otherwise the first decode after jumping back reads as hopelessly
        late and immediately skips forward again, undoing the jump."""
        decoder = self._decoder()
        decoder.frame_for(900)
        decoder.seek(30)
        assert decoder._wanted is None
        assert decoder._lateness(30) is None

    def test_a_new_playlist_forgets_it_too(self):
        decoder = self._decoder()
        decoder.frame_for(900)
        decoder.set_playlist(Playlist([Segment(0, 100, "b.mp4", 0)], 100), 0)
        assert decoder._wanted is None

    @pytest.mark.parametrize("fps,late,skip", [(30, 10, 60), (60, 20, 120)])
    def test_the_thresholds_are_a_duration_not_a_frame_count(self, fps, late, skip):
        """Ten frames is a third of a second at 30 and a sixth at 60."""
        from vedit.player.decoder import VideoDecoder

        decoder = VideoDecoder(TimeBase(fps))
        assert decoder._late_frames == late
        assert decoder._skip_frames == skip


class TestPauseLandsOnThePlayhead:
    """Pause must show the frame under the playhead, not the queued past.

    With decode behind, the tick keeps asking for the paused frame and the
    decoder keeps handing over the backlog it is still working through — one
    frame per tick, for as long as the backlog lasts. From the outside the
    video simply carries on playing for several seconds after the sound cut.
    """

    def _engine(self):
        from vedit.core.project import Project
        from vedit.player.engine import PlaybackEngine

        project = Project(TimeBase(30))
        engine = PlaybackEngine(project)
        engine._timer.stop()          # drive the tick by hand
        return engine

    def test_pausing_drops_the_video_backlog(self):
        engine = self._engine()
        try:
            engine._playing = True
            engine._position = 300
            engine.video._queue = [DecodedFrame(n, QImage()) for n in (150, 151, 152)]
            engine.pause()
            assert engine.video._queue == []
        finally:
            engine.stop()

    def test_pausing_drops_the_dissolve_backlog_too(self):
        """Otherwise the transition under the picture plays on by itself."""
        engine = self._engine()
        try:
            engine._playing = True
            engine._position = 300
            engine.dissolve._queue = [DecodedFrame(150, QImage())]
            engine.pause()
            assert engine.dissolve._queue == []
            assert engine._under_frame is None
        finally:
            engine.stop()

    def test_the_decoders_are_pointed_at_the_paused_frame(self):
        engine = self._engine()
        try:
            engine._playing = True
            engine._position = 275
            engine.pause()
            assert engine.video._seek_to == 275
            assert engine.dissolve._seek_to == 275
        finally:
            engine.stop()

    def test_pausing_when_already_paused_does_nothing(self):
        """`pause` is wired to a toggle and to end-of-timeline; re-seeking on a
        call that changes nothing would restart decode for no reason."""
        engine = self._engine()
        try:
            engine._position = 275
            engine.video._seek_to = None
            engine.pause()
            assert engine.video._seek_to is None
        finally:
            engine.stop()


class TestCatchingUpCannotMakeItWorse:
    """What the catch-up does on media that is *never* fast enough.

    The first version of it assumed decode is normally quicker than realtime and
    only occasionally behind. When that does not hold — a heavy codec, a busy
    machine — both halves failed badly and in the same direction: the skip-
    conversion rule discarded every frame for as long as playback lasted, and
    the skip-seek came due again the moment it finished, so the decoder spent
    the whole time seeking. Either way the viewer is handed almost nothing,
    which looks far worse than a picture that is merely running late.
    """

    def _decoder(self):
        from vedit.player.decoder import VideoDecoder

        decoder = VideoDecoder(TimeBase(30))
        decoder._playlist = Playlist([Segment(0, 100000, "a.mp4", 0)], 100000)
        return decoder

    def test_a_run_of_dropped_frames_is_broken_by_one_that_is_shown(self):
        """Hopelessly behind and staying behind: the picture must still move."""
        from vedit.player.decoder import MAX_DROPPED_RUN

        decoder = self._decoder()
        decoder.frame_for(500)          # far ahead, and it never catches up
        decoder._position = 100

        drops = []
        for _ in range(MAX_DROPPED_RUN * 3):
            late = decoder._lateness(decoder._position)
            drop = late is not None and late > decoder._late_frames
            if drop and decoder._dropped_run >= MAX_DROPPED_RUN:
                drop = False
            decoder._dropped_run = decoder._dropped_run + 1 if drop else 0
            drops.append(drop)

        assert False in drops, "every single frame was thrown away"
        longest = 0
        run = 0
        for dropped in drops:
            run = run + 1 if dropped else 0
            longest = max(longest, run)
        assert longest <= MAX_DROPPED_RUN

    def test_the_catch_up_seek_is_rate_limited(self):
        """A seek that does not close the gap is due again immediately."""
        decoder = self._decoder()
        decoder.frame_for(5000)
        decoder._position = 100

        sought = []
        decoder._do_seek = lambda playlist, target, generation: sought.append(target)

        decoder._decode_one(decoder._playlist, decoder._generation)
        assert sought == [5000], "the first one should go through"

        # Still hopelessly behind, immediately afterwards. The playlist becomes
        # gaps so the call that is *not* allowed to seek has something harmless
        # to do instead of opening a file that does not exist.
        decoder._playlist = Playlist([Segment(0, 100000, None, 0)], 100000)
        decoder._segment = None
        decoder.frame_for(6000)
        decoder._position = 200
        decoder._decode_one(decoder._playlist, decoder._generation)
        assert sought == [5000], "a second seek straight away would be thrash"

    def test_it_can_seek_again_once_the_interval_has_passed(self):
        from vedit.player.decoder import SKIP_INTERVAL_SECONDS

        decoder = self._decoder()
        decoder.frame_for(5000)
        decoder._position = 100
        sought = []
        decoder._do_seek = lambda playlist, target, generation: sought.append(target)

        decoder._decode_one(decoder._playlist, decoder._generation)
        decoder._last_skip -= SKIP_INTERVAL_SECONDS + 1
        decoder._segment = None
        decoder.frame_for(6000)
        decoder._position = 200
        decoder._decode_one(decoder._playlist, decoder._generation)
        assert sought == [5000, 6000]

    def test_keeping_up_normally_drops_nothing_at_all(self):
        """The healthy case: the decoder runs ahead filling the queue, and none
        of this machinery may touch it."""
        decoder = self._decoder()
        decoder.frame_for(100)
        decoder._position = 108
        assert decoder._lateness(decoder._position) < 0
        assert decoder._dropped_run == 0

    def test_a_seek_clears_the_dropped_run(self):
        decoder = self._decoder()
        decoder._dropped_run = 3
        decoder.seek(42)
        assert decoder._dropped_run == 0
