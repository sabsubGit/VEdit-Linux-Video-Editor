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
    assert [c.tl_start for c in timeline.video_tracks[0].clips] == [0, 120]

    # -- cut ----------------------------------------------------------------
    undo.apply("razor", lambda t: ops.razor(t, 60))
    assert len(timeline.video_tracks[0].clips) == 3

    tail = timeline.video_tracks[0].clips[1]
    undo.apply("ripple", lambda t: ops.ripple_delete(t, [tail]))
    assert timeline.duration == 150, "removed 2s, closing the gap"
    assert timeline.video_tracks[0].gaps() == [], "ripple left no hole"
    assert timeline.audio_tracks[0].clips[0].tl_end == 60, "audio rippled with video"

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
    assert timeline.video_tracks[0].gaps() == [(0, 60)]

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


def test_speed_change_renders_the_right_length(sources, tmp_path):
    """A 2x clip must export at half its source length, in the file itself."""
    info_a, _ = sources
    timebase = TimeBase(30)
    timeline = Timeline.default(timebase, width=320, height=180)
    pool = Pool([info_a])

    clips = ops.append_media(timeline, info_a)
    assert timeline.duration == 120, "4s at 30fps"

    ops.set_speed(timeline, clips, 2.0)
    assert timeline.duration == 60, "2x halves the time it occupies"

    output = tmp_path / "fast.mp4"
    preset = preset_by_key("h264_mp4").with_quality(30).with_size(320, 180)
    args = build_command(timeline, pool, preset, output, progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"

    data = ffprobe(output)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == 60
    assert float(data["format"]["duration"]) == pytest.approx(2.0, abs=0.1)


def test_upper_video_lane_occludes_lower(sources, tmp_path):
    """Rendering must match the preview's topmost-wins rule, and the covered
    clip must resume at the right source frame afterwards."""
    info_a, info_b = sources
    timebase = TimeBase(30)
    timeline = Timeline.default(timebase, width=320, height=180)
    pool = Pool([info_a, info_b])

    ops.place_media(timeline, info_a, 0)                       # V1: 0..120
    over = ops.make_clips(info_b, timebase)[0]
    over.tl_start = 40
    over.src_out = over.src_in + 20                            # V2: 40..60
    timeline.video_tracks[1].insert(over)

    assert timeline.duration == 120

    output = tmp_path / "layered.mp4"
    preset = preset_by_key("h264_mp4").with_quality(30).with_size(320, 180)
    args = build_command(timeline, pool, preset, output, progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"

    data = ffprobe(output)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == 120, "the overlay covers, it does not extend"
    assert float(data["format"]["duration"]) == pytest.approx(4.0, abs=0.1)


def test_two_audio_lanes_are_mixed(sources, tmp_path):
    info_a, info_b = sources
    timebase = TimeBase(30)
    timeline = Timeline.default(timebase, width=320, height=180)
    pool = Pool([info_a, info_b])

    ops.place_media(timeline, info_a, 0)
    second = ops.make_clips(info_b, timebase)[1]     # audio half of the pair
    second.tl_start = 0
    timeline.audio_tracks[1].insert(second)

    output = tmp_path / "mixed.mp4"
    preset = preset_by_key("h264_mp4").with_quality(30).with_size(320, 180)
    args = build_command(timeline, pool, preset, output, progress_to_stdout=False)
    assert "amix=inputs=2" in args[args.index("-filter_complex") + 1]

    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"

    data = ffprobe(output)
    audio = next(s for s in data["streams"] if s["codec_type"] == "audio")
    assert int(audio["channels"]) == 2
    assert float(data["format"]["duration"]) == pytest.approx(4.0, abs=0.15)


def mean_volume(path, *, start, duration):
    """Mean level of a window of a file, in dBFS.

    `volumedetect` reports at info level, so the usual `-v error` would swallow
    the only line worth reading.
    """
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-ss", str(start), "-t", str(duration),
            "-i", str(path), "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = [line for line in result.stderr.splitlines() if "mean_volume" in line]
    assert lines, f"volumedetect said nothing:\n{result.stderr[-1000:]}"
    return float(lines[0].split("mean_volume:")[1].split()[0])


def render_audio(timeline, pool, output):
    preset = preset_by_key("h264_mp4").with_quality(30).with_size(320, 180)
    args = build_command(timeline, pool, preset, output, progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"
    return args[args.index("-filter_complex") + 1]


def test_a_fade_out_is_audible_in_the_rendered_file(sources, tmp_path):
    """The end-to-end proof that a fade set on the Audio page reaches the export.

    Measured rather than asserted on the filter string, because the string being
    right and the audio being right are two different claims.
    """
    info_a, _ = sources
    timeline = Timeline.default(TimeBase(30), width=320, height=180)
    ops.place_media(timeline, info_a, 0)

    clip = timeline.audio_tracks[0].clips[0]
    ops.set_clip_fade(timeline, clip, "out", 60)      # the last two seconds

    output = tmp_path / "faded.mp4"
    graph = render_audio(timeline, Pool([info_a]), output)
    assert "afade=t=out:st=2.000000:d=2.000000" in graph

    head = mean_volume(output, start=0.3, duration=1.0)
    tail = mean_volume(output, start=3.2, duration=0.7)
    assert tail < head - 6, f"the tail ({tail} dB) is not quieter than the head ({head} dB)"


def test_master_gain_attenuates_the_whole_mix(sources, tmp_path):
    """A control render at unity, so the measurement isolates the fader.

    Comparing against the source file instead would fold in the mono-to-stereo
    conversion, which costs its own 3 dB and has nothing to do with the mixer.
    """
    info_a, _ = sources
    pool = Pool([info_a])

    def render(master_db, name):
        timeline = Timeline.default(TimeBase(30), width=320, height=180)
        ops.place_media(timeline, info_a, 0)
        ops.set_master_gain(timeline, master_db)
        output = tmp_path / name
        render_audio(timeline, pool, output)
        return mean_volume(output, start=0.3, duration=1.0)

    unity = render(0.0, "unity.mp4")
    quieter = render(-6.0, "down.mp4")
    assert quieter - unity == pytest.approx(-6.0, abs=0.35)


def test_a_soloed_lane_is_the_only_one_exported(sources, tmp_path):
    """The one line that keeps solo meaning the same thing in preview and export."""
    info_a, info_b = sources
    timeline = Timeline.default(TimeBase(30), width=320, height=180)

    ops.place_media(timeline, info_a, 0)
    second = ops.make_clips(info_b, timeline.timebase)[1]
    second.tl_start = 0
    timeline.audio_tracks[1].insert(second)
    timeline.audio_tracks[1].solo = True

    output = tmp_path / "soloed.mp4"
    graph = render_audio(timeline, Pool([info_a, info_b]), output)
    assert "amix" not in graph, "the un-soloed lane is still in the mix"

    data = ffprobe(output)
    assert any(s["codec_type"] == "audio" for s in data["streams"])
