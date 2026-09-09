"""Cross dissolves, through a real export.

Two solid-colour clips: if the transition works, the middle of it is neither
colour but a mix of the two. Nothing short of running ffmpeg proves that — a
graph string can be perfectly well formed and still hard-cut.

Marked slow; skip with `-m "not slow"`.
"""

from __future__ import annotations

import subprocess

import pytest

from vedit.core.timebase import TimeBase
from vedit.media.probe import probe
from vedit.render.graph import build_command
from vedit.render.presets import preset_by_key
from vedit.timeline import ops
from vedit.timeline.model import Clip, Timeline

pytestmark = pytest.mark.slow


class Pool:
    def __init__(self, infos):
        self._by_id = {info.media_id: info for info in infos}

    def info_for(self, media_id):
        return self._by_id.get(media_id)


def solid(folder, name, colour, seconds=4):
    path = folder / f"{name}.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"color=c={colour}:s=640x360:d={seconds}:r=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return probe(path)


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    folder = tmp_path_factory.mktemp("dissolve")
    # Red into blue: the mix is unmistakably neither.
    return solid(folder, "red", "red"), solid(folder, "blue", "blue")


def colour_at(path, seconds):
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", "scale=1:1", "-f", "rawvideo",
            "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return tuple(raw[:3])


@pytest.fixture
def timeline(sources):
    """Two 30-frame shots, each with a second of unused source to dissolve from."""
    red, blue = sources
    timeline = Timeline.default(TimeBase(30), width=640, height=360)
    track = timeline.lane_for("video")
    track.insert(Clip(media_id=red.media_id, src_in=0, src_out=30,
                      tl_start=0, src_length=120, name="red"))
    track.insert(Clip(media_id=blue.media_id, src_in=0, src_out=30,
                      tl_start=30, src_length=120, name="blue"))
    return timeline


def render(timeline, sources, output):
    preset = preset_by_key("h264_mp4").with_quality(18).with_size(640, 360)
    args = build_command(timeline, Pool(list(sources)), preset, output,
                         progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"
    return output


class TestItActuallyMixes:
    def test_without_a_dissolve_the_cut_is_hard(self, timeline, sources, tmp_path):
        """The control: every frame is one colour or the other."""
        output = render(timeline, sources, tmp_path / "cut.mp4")
        red, blue = colour_at(output, 0.5), colour_at(output, 1.5)
        assert red[0] > 150 and red[2] < 90
        assert blue[2] > 150 and blue[0] < 90

    def test_the_middle_of_a_dissolve_is_neither_shot(self, timeline, sources, tmp_path):
        """Halfway through, red is on its way down and blue on its way up, so
        the frame is a colour neither clip contains."""
        blue_clip = timeline.lane_for("video").clips[1]
        ops.set_dissolve(timeline, blue_clip, 20)
        output = render(timeline, sources, tmp_path / "mixed.mp4")

        # The dissolve runs from frame 30 to 50, so frame 40 is 20/30 s in.
        middle = colour_at(output, 40 / 30)
        assert 40 < middle[0] < 215, f"red should be partway down, got {middle}"
        assert 40 < middle[2] < 215, f"blue should be partway up, got {middle}"

    def test_it_ramps_rather_than_jumping(self, timeline, sources, tmp_path):
        """Sampled across the transition, red falls and blue rises the whole
        way — which is what tells a dissolve from a two-step fade."""
        blue_clip = timeline.lane_for("video").clips[1]
        ops.set_dissolve(timeline, blue_clip, 20)
        output = render(timeline, sources, tmp_path / "ramp.mp4")

        reds = [colour_at(output, frame / 30)[0] for frame in (31, 36, 41, 46)]
        blues = [colour_at(output, frame / 30)[2] for frame in (31, 36, 41, 46)]
        assert reds == sorted(reds, reverse=True), f"red should only fall: {reds}"
        assert blues == sorted(blues), f"blue should only rise: {blues}"

    def test_the_edit_keeps_its_length(self, timeline, sources, tmp_path):
        """A dissolve borrows unused source; it does not shorten the programme.
        `xfade` normally outputs the *sum* of its inputs minus the overlap,
        which would make the file longer if the branches were built wrong."""
        blue_clip = timeline.lane_for("video").clips[1]
        ops.set_dissolve(timeline, blue_clip, 20)
        output = render(timeline, sources, tmp_path / "length.mp4")
        assert float(probe(output).duration) == pytest.approx(2.0, abs=0.08)

    def test_either_side_of_the_transition_is_untouched(self, timeline, sources, tmp_path):
        blue_clip = timeline.lane_for("video").clips[1]
        ops.set_dissolve(timeline, blue_clip, 20)
        output = render(timeline, sources, tmp_path / "edges.mp4")

        before, after = colour_at(output, 0.3), colour_at(output, 1.9)
        assert before[0] > 150 and before[2] < 90, "still pure red before it starts"
        assert after[2] > 150 and after[0] < 90, "pure blue once it is over"
