"""Render graph and preset tests.

These assert on the generated ffmpeg arguments rather than running ffmpeg, so
they stay fast. The end-to-end check that the command actually produces a correct
file lives in `tests/test_smoke.py`.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from vedit.core.timebase import TimeBase
from vedit.render.graph import RenderError, build_command
from vedit.render.presets import ALL_PRESETS, available_presets, preset_by_key
from vedit.timeline.model import Clip, Timeline


class FakeInfo:
    def __init__(self, media_id, path):
        self.media_id = media_id
        self.path = Path(path)


class FakePool:
    def __init__(self, mapping):
        self._mapping = mapping

    def info_for(self, media_id):
        return self._mapping.get(media_id)


@pytest.fixture
def pool():
    return FakePool({"m1": FakeInfo("m1", "/media/a.mp4"), "m2": FakeInfo("m2", "/media/b.mov")})


@pytest.fixture
def timeline():
    return Timeline.default(TimeBase(30))


def add(timeline, media_id, tl_start, length, *, src_in=0, kind="video", track=None):
    clip = Clip(
        media_id=media_id,
        src_in=src_in,
        src_out=src_in + length,
        tl_start=tl_start,
        src_length=src_in + length + 500,
        kind=kind,
    )
    (track or timeline.lane_for(kind)).insert(clip)
    return clip


def filter_of(args):
    return args[args.index("-filter_complex") + 1]


class TestBuildCommand:
    def test_empty_timeline_is_refused(self, timeline, pool):
        with pytest.raises(RenderError, match="empty"):
            build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))

    def test_missing_media_is_refused(self, timeline):
        add(timeline, "gone", 0, 30)
        with pytest.raises(RenderError, match="media"):
            build_command(timeline, FakePool({}), preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))

    def test_one_input_per_source(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        add(timeline, "m1", 30, 30, src_in=100)   # same file again
        add(timeline, "m2", 60, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert args.count("-i") == 2, "a file used twice is still opened once"

    def test_trim_times_come_from_the_source_window(self, timeline, pool):
        add(timeline, "m1", 0, 30, src_in=60)     # 2s in, 1s long at 30fps
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "trim=start=2.000000:end=3.000000" in graph

    def test_every_segment_is_normalised_before_concat(self, timeline, pool):
        """The reason mixed formats can be cut together: concat demands identical
        inputs, so scale and fps are forced per segment beforehand."""
        add(timeline, "m1", 0, 30)
        add(timeline, "m2", 30, 30)
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert graph.count("scale=1920:1080") == 2
        assert graph.count("fps=30/1") == 2
        assert graph.count("setsar=1") == 2
        assert "concat=n=2:v=1:a=0" in graph

    def test_aspect_is_padded_not_stretched(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "force_original_aspect_ratio=decrease" in graph
        assert "pad=1920:1080" in graph

    def test_gaps_become_black_and_silence(self, timeline, pool):
        add(timeline, "m1", 60, 30)               # starts 2s in, leaving a gap
        add(timeline, "m1", 60, 30, kind="audio")
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "color=c=black:s=1920x1080" in graph
        assert "d=2.000000" in graph
        assert "anullsrc" in graph

    def test_trailing_gap_is_filled(self, timeline, pool):
        """Video ends at 1s but audio runs to 2s; the video track must be padded
        or the concat would produce a stream shorter than the timeline."""
        add(timeline, "m1", 0, 30)
        add(timeline, "m1", 0, 60, kind="audio")
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "color=c=black" in graph
        assert "concat=n=2:v=1:a=0" in graph

    def test_video_only_timeline_disables_audio(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert "-an" in args
        assert "[aout]" not in args

    def test_audio_is_mapped_when_present(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        add(timeline, "m1", 0, 30, kind="audio")
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert "[aout]" in args
        assert "-an" not in args

    def test_progress_is_requested_on_stdout(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert args[args.index("-progress") + 1] == "pipe:1"

    def test_faststart_only_for_mp4(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        mp4 = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        mov = build_command(timeline, pool, preset_by_key("prores_mov"), Path("/tmp/o.mov"))
        assert "+faststart" in mp4
        assert "+faststart" not in mov

    def test_preset_resolution_overrides_the_timeline(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        preset = preset_by_key("h264_mp4").with_size(1280, 720)
        graph = filter_of(build_command(timeline, pool, preset, Path("/tmp/o.mp4")))
        assert "scale=1280:720" in graph
        assert "1920" not in graph

    def test_fractional_rate_is_written_exactly(self, pool):
        timeline = Timeline.default(TimeBase(Fraction(30000, 1001)))
        add(timeline, "m1", 0, 30)
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "fps=30000/1001" in graph, "must not be flattened to 29.97"

    def test_output_path_is_last(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/out.mp4"))
        assert args[-1] == "/tmp/out.mp4"


class TestPresets:
    def test_all_presets_have_unique_keys(self):
        keys = [preset.key for preset in ALL_PRESETS]
        assert len(keys) == len(set(keys))

    def test_available_presets_are_a_subset(self):
        assert set(available_presets()).issubset(set(ALL_PRESETS))

    def test_software_presets_are_always_available(self):
        """Presets with no encoder requirement must never be filtered out."""
        keys = {preset.key for preset in available_presets()}
        assert {"h264_mp4", "hevc_mp4", "prores_mov"} <= keys

    def test_x264_uses_crf(self):
        args = preset_by_key("h264_mp4").with_quality(18).video_args()
        assert args[args.index("-crf") + 1] == "18"

    def test_nvenc_uses_cq_not_crf(self):
        args = preset_by_key("h264_nvenc_mp4").with_quality(25).video_args()
        assert "-crf" not in args, "NVENC has no CRF; it would be silently ignored"
        assert args[args.index("-cq") + 1] == "25"

    def test_intermediates_carry_their_profile(self):
        assert "-profile:v" in preset_by_key("prores_mov").video_args()
        assert "dnxhr_hq" in preset_by_key("dnxhr_mov").video_args()

    def test_pcm_audio_gets_no_bitrate(self):
        args = preset_by_key("prores_mov").audio_args()
        assert "-b:a" not in args, "a bitrate is meaningless for uncompressed PCM"

    def test_lossy_audio_gets_a_bitrate(self):
        assert "-b:a" in preset_by_key("h264_mp4").audio_args()

    def test_filename_uses_the_container(self):
        assert preset_by_key("h264_mp4").filename_for("cut") == "cut.mp4"
        assert preset_by_key("prores_mov").filename_for("cut") == "cut.mov"
