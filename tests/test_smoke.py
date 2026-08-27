"""End-to-end: import, cut, undo, render, verify.

Generates its own media so it needs no assets, and runs a real ffmpeg render,
then probes the result. The mixed 30/25 fps pair is the point of the test — it is
what proves the per-segment normalisation before `concat` actually works.

Marked slow; skip with `-m "not slow"`.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from vedit.core.commands import UndoStack
from vedit.core.timebase import TimeBase
from vedit.media.probe import probe
from vedit.render.graph import build_command
from vedit.render.presets import preset_by_key
from vedit.timeline import ops
from vedit.timeline.model import Timeline

pytestmark = pytest.mark.slow


def make_clip(path, *, size, rate, seconds, tone):
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
            "-f", "lavfi", "-i", f"sine=frequency={tone}",
            "-t", str(seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class Pool:
    """Minimal stand-in for MediaPool — the render graph only calls info_for."""

    def __init__(self, infos):
        self._by_id = {info.media_id: info for info in infos}

    def info_for(self, media_id):
        return self._by_id.get(media_id)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    folder = tmp_path_factory.mktemp("smoke")
    a = make_clip(folder / "a.mp4", size="640x360", rate=30, seconds=4, tone=440)
    b = make_clip(folder / "b.mp4", size="480x270", rate=25, seconds=3, tone=880)
    return probe(a), probe(b)


def ffprobe(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def test_import_cut_undo_render(sources, tmp_path):
    info_a, info_b = sources
    timebase = TimeBase(30)
    timeline = Timeline.default(timebase, width=640, height=360)
    pool = Pool([info_a, info_b])
    undo = UndoStack(timeline)

    # -- import -------------------------------------------------------------
    undo.apply("append a", lambda t: ops.append_media(t, info_a))
    undo.apply("append b", lambda t: ops.append_media(t, info_b))

    # 4s at 30fps = 120 frames; 3s at 25fps conformed to 30fps = 90 frames.
    assert timeline.duration == 210
    assert [c.tl_start for c in timeline.tracks[0].clips] == [0, 120]

    # -- cut ----------------------------------------------------------------
    undo.apply("razor", lambda t: ops.razor(t, 60))
    assert len(timeline.tracks[0].clips) == 3

    tail = timeline.tracks[0].clips[1]
    undo.apply("ripple", lambda t: ops.ripple_delete(t, [tail]))
    assert timeline.duration == 150, "removed 2s, closing the gap"
    assert timeline.tracks[0].gaps() == [], "ripple left no hole"
    assert timeline.tracks[1].clips[0].tl_end == 60, "audio rippled with video"

    # -- undo / redo --------------------------------------------------------
    snapshot = [(c.clip_id, c.tl_start, c.src_in, c.src_out) for c in timeline.all_clips()]
    undo.undo()
    assert timeline.duration == 210
    undo.redo()
    assert [(c.clip_id, c.tl_start, c.src_in, c.src_out) for c in timeline.all_clips()] == snapshot

    # -- render -------------------------------------------------------------
    output = tmp_path / "out.mp4"
    preset = preset_by_key("h264_mp4").with_quality(28).with_size(640, 360)
    args = build_command(timeline, pool, preset, output, progress_to_stdout=False)

    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"
    assert output.exists()

    # -- verify -------------------------------------------------------------
    data = ffprobe(output)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")

    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"]) == (640, 360)
    assert video["r_frame_rate"] == "30/1"
    assert audio["codec_name"] == "aac"
    assert int(audio["channels"]) == 2

    # The exported file must be exactly as long as the timeline claims.
    expected = float(timebase.frames_to_seconds(timeline.duration))
    assert float(data["format"]["duration"]) == pytest.approx(expected, abs=0.05)
    assert int(video["nb_frames"]) == timeline.duration, (
        "frame count must match the timeline exactly — a mismatch here means the "
        "timeline is claiming frames the sources cannot supply"
    )


def test_render_with_a_gap(sources, tmp_path):
    """A gap must become real black and silence of the right length, not be
    skipped over."""
    info_a, _ = sources
    timebase = TimeBase(30)
    timeline = Timeline.default(timebase, width=320, height=180)
    pool = Pool([info_a])

    ops.place_media(timeline, info_a, 60)      # 2s of nothing, then the clip
    assert timeline.tracks[0].gaps() == [(0, 60)]

    output = tmp_path / "gap.mp4"
    preset = preset_by_key("h264_mp4").with_quality(30).with_size(320, 180)
    args = build_command(timeline, pool, preset, output, progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"

    data = ffprobe(output)
    expected = float(timebase.frames_to_seconds(timeline.duration))
    assert float(data["format"]["duration"]) == pytest.approx(expected, abs=0.05)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == timeline.duration
