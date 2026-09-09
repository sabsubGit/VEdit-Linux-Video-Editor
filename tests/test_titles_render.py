"""Titles, through a real export.

Two things here can only be caught by running ffmpeg. Escaping is one: a stray
apostrophe does not produce wrong text, it produces a filter graph that will not
parse, and a graph-string test would happily assert the broken string. The other
is that a caption really does end up *over* the picture rather than instead of
it, which is the whole point of the feature.

Marked slow; skip with `-m "not slow"`.
"""

from __future__ import annotations

import subprocess

import pytest

from vedit.core.ffmpeg import has_filter
from vedit.core.timebase import TimeBase
from vedit.media.probe import probe
from vedit.render.graph import build_command
from vedit.render.presets import preset_by_key
from vedit.timeline import ops
from vedit.timeline.model import Timeline
from vedit.timeline.titles import Title

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not has_filter("drawtext"),
        reason="this ffmpeg has no drawtext (built without libfreetype)",
    ),
]


class Pool:
    def __init__(self, infos):
        self._by_id = {info.media_id: info for info in infos}

    def info_for(self, media_id):
        return self._by_id.get(media_id)


@pytest.fixture(scope="module")
def source(tmp_path_factory):
    """A solid green clip, so any non-green pixel is text."""
    folder = tmp_path_factory.mktemp("titles")
    path = folder / "green.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=green:s=640x360:d=3:r=30",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return probe(path)


@pytest.fixture
def timeline(source):
    timeline = Timeline.default(TimeBase(30), width=640, height=360)
    ops.append_media(timeline, source)
    return timeline


def render(timeline, source, output):
    preset = preset_by_key("h264_mp4").with_quality(18).with_size(640, 360)
    args = build_command(timeline, Pool([source]), preset, output,
                         progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2500:]}"
    return output


def band_brightness(path, seconds, top_fraction, height_fraction=0.2):
    """The *brightest* pixel in a horizontal band.

    Brightest rather than average: white text on green covers only a few per
    cent of a band, so a mean barely moves and a threshold on it would be
    indistinguishable from noise. A peak is unambiguous — either there are white
    glyphs in that band or there are not.
    """
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
            "-frames:v", "1",
            "-vf", f"crop=iw:ih*{height_fraction}:0:ih*{top_fraction},format=gray",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return max(raw) if raw else 0


class TestTitlesReachTheFile:
    def test_a_caption_leaves_the_picture_underneath(self, timeline, source, tmp_path):
        """The whole point: a title on an upper lane is words *over* the shot,
        not a card instead of it."""
        ops.add_title(timeline, 0, title=Title(text="Caption", position="lower"))
        output = render(timeline, source, tmp_path / "caption.mp4")

        # The top of frame has no text on it and must still be the green shot.
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", "0.5", "-i", str(output),
             "-frames:v", "1", "-vf", "crop=iw:ih*0.2:0:0,scale=1:1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True,
        ).stdout
        red, green, blue = raw[0], raw[1], raw[2]
        assert green > red and green > blue, "the shot should still be there"

    def test_the_words_actually_appear(self, timeline, source, tmp_path):
        plain = render(timeline, source, tmp_path / "plain.mp4")
        before = band_brightness(plain, 0.5, 0.68)

        ops.add_title(timeline, 0, title=Title(text="WORDS HERE", position="lower"))
        titled = render(timeline, source, tmp_path / "titled.mp4")
        after = band_brightness(titled, 0.5, 0.68)

        assert after > before + 40, f"the band should be brighter: {before} -> {after}"

    def test_position_decides_which_band_lights_up(self, timeline, source, tmp_path):
        ops.add_title(timeline, 0, title=Title(text="AT THE TOP", position="top"))
        output = render(timeline, source, tmp_path / "top.mp4")
        assert band_brightness(output, 0.5, 0.02) > band_brightness(output, 0.5, 0.68)

    def test_a_title_card_needs_no_media_under_it(self, source, tmp_path):
        """A title alone on the timeline exports as words on black rather than
        failing for want of something to draw on."""
        timeline = Timeline.default(TimeBase(30), width=640, height=360)
        ops.add_title(timeline, 0, track=timeline.video_tracks[0],
                      title=Title(text="Title Card"))
        output = render(timeline, source, tmp_path / "card.mp4")
        assert float(probe(output).duration) > 0.5
        assert band_brightness(output, 0.5, 0.4) > 100, "the words should be there"

    def test_a_multi_line_title_draws_every_line(self, timeline, source, tmp_path):
        ops.add_title(timeline, 0, title=Title(text="LINE ONE\nLINE TWO",
                                               position="centre", size="large"))
        output = render(timeline, source, tmp_path / "lines.mp4")
        # The two lines straddle the middle, so both halves of the centre band
        # pick up text.
        assert band_brightness(output, 0.5, 0.34, 0.14) > 150
        assert band_brightness(output, 0.5, 0.50, 0.14) > 150


class TestEscaping:
    """A stray metacharacter breaks the *graph*, not the text — so the failure
    is a render that will not start at all."""

    @pytest.mark.parametrize(
        "text",
        [
            "It's here",
            "Ratio 16:9",
            "100% done",
            "One, two, three",
            "a;b",
            "x=y",
            "[bracketed]",
            "back\\slash",
            "everything: it's 100%, [x]=y; done\\",
        ],
    )
    def test_awkward_text_still_renders(self, timeline, source, tmp_path, text):
        ops.add_title(timeline, 0, title=Title(text=text))
        render(timeline, source, tmp_path / "escaped.mp4")
