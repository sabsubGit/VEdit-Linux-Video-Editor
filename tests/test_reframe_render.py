"""Reframing, through a real export.

Everything else about framing is tested against numbers. This runs ffmpeg over a
source whose four quadrants are four different colours, and asks what colour
came out — which is the one way to catch a sign error in the pan, a crop
measured from the wrong corner, or a rotation that turns the picture the way
nobody expected. All three are mistakes the arithmetic tests would happily
agree with themselves about.

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
from vedit.timeline.framing import Framing
from vedit.timeline.model import Timeline

pytestmark = pytest.mark.slow

# Deliberately far apart in RGB so a soft edge or a colour-space round trip
# cannot turn one into another.
QUADRANTS = {
    "top left": (255, 0, 0),
    "top right": (0, 255, 0),
    "bottom left": (0, 0, 255),
    "bottom right": (255, 255, 0),
}


class Pool:
    """Minimal stand-in for MediaPool — the render graph only calls info_for."""

    def __init__(self, infos):
        self._by_id = {info.media_id: info for info in infos}

    def info_for(self, media_id):
        return self._by_id.get(media_id)


@pytest.fixture(scope="module")
def quad_source(tmp_path_factory):
    """A 1920x1080 clip: red, green, blue and yellow, one per quarter."""
    folder = tmp_path_factory.mktemp("reframe")
    path = folder / "quads.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "color=c=red:s=960x540:d=2:r=30",
            "-f", "lavfi", "-i", "color=c=lime:s=960x540:d=2:r=30",
            "-f", "lavfi", "-i", "color=c=blue:s=960x540:d=2:r=30",
            "-f", "lavfi", "-i", "color=c=yellow:s=960x540:d=2:r=30",
            "-filter_complex",
            "[0][1]hstack[top];[2][3]hstack[bottom];[top][bottom]vstack",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return probe(path)


def render(timeline, info, output):
    preset = preset_by_key("h264_mp4").with_quality(20)
    args = build_command(timeline, Pool([info]), preset, output, progress_to_stdout=False)
    result = subprocess.run(args, capture_output=True, text=True)
    assert result.returncode == 0, f"ffmpeg failed:\n{result.stderr[-2000:]}"
    return output


def frame_at(path, seconds):
    """One frame of a rendered file, extracted so it can be sampled on its own."""
    still = path.with_name(f"{path.stem}-{seconds:.2f}.png")
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-ss", f"{seconds:.3f}", "-i", str(path),
            "-frames:v", "1", str(still),
        ],
        check=True,
        capture_output=True,
    )
    return still


def sample(path, x_fraction, y_fraction):
    """The colour at a relative position in the first frame, as (r, g, b)."""
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-vf", f"crop=40:40:iw*{x_fraction}-20:ih*{y_fraction}-20,scale=1:1",
            "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        check=True,
    )
    return tuple(result.stdout[:3])


# A turned or letterboxed picture leaves black around it, and "which quadrant is
# black nearest to" has no answer — it is equidistant from three of them. So the
# bars are a fifth thing a sample can be, which also means the two sides get
# checked on putting them in the same place.
REGIONS = {**QUADRANTS, "bars": (0, 0, 0)}


def nearest(colour, palette):
    return min(
        palette, key=lambda name: sum((a - b) ** 2 for a, b in zip(colour, palette[name]))
    )


def nearest_quadrant(colour):
    """Which of the four the sampled colour is closest to."""
    return nearest(colour, QUADRANTS)


def nearest_region(colour):
    """Which quadrant, or the letterbox bars."""
    return nearest(colour, REGIONS)


@pytest.fixture
def timeline(quad_source):
    timeline = Timeline.default(TimeBase(30), width=1920, height=1080)
    ops.append_media(timeline, quad_source)
    return timeline


class TestPanDirection:
    """Zoomed all the way into one corner, that corner is the whole picture.

    This is what pins the sign of the pan. Get it backwards and every one of
    these comes back with the diagonally opposite colour.
    """

    @pytest.mark.parametrize(
        "x, y, expected",
        [
            (-1.0, -1.0, "top left"),
            (1.0, -1.0, "top right"),
            (-1.0, 1.0, "bottom left"),
            (1.0, 1.0, "bottom right"),
        ],
    )
    def test_a_corner_fills_the_frame(self, timeline, quad_source, tmp_path, x, y, expected):
        clip = timeline.video_tracks[0].clips[0]
        ops.set_framing(timeline, [clip], Framing(zoom=2.0, x=x, y=y))
        output = render(timeline, quad_source, tmp_path / f"{expected.replace(' ', '-')}.mp4")

        for at_x, at_y in ((0.25, 0.25), (0.75, 0.75)):
            assert nearest_quadrant(sample(output, at_x, at_y)) == expected


class TestUntouched:
    def test_without_framing_all_four_quadrants_survive(self, timeline, quad_source, tmp_path):
        """The control. If this ever fails, the crop is being applied when it
        should not be."""
        output = render(timeline, quad_source, tmp_path / "plain.mp4")
        seen = {
            nearest_quadrant(sample(output, x, y))
            for x, y in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
        }
        assert seen == set(QUADRANTS)


class TestRotation:
    def test_a_quarter_turn_right_moves_every_corner_clockwise(
        self, timeline, quad_source, tmp_path
    ):
        """Turned clockwise, the top left corner ends up at the top right, and
        the rest follow it round.

        A turned 16:9 picture is a tall sliver in a 16:9 frame — it spans
        roughly the middle third — so the samples sit either side of the centre
        line rather than out at the corners, which would land in the bars. Not
        *on* the centre line either: that is the seam between two quadrants.
        """
        clip = timeline.video_tracks[0].clips[0]
        ops.set_rotation(timeline, [clip], 90)
        output = render(timeline, quad_source, tmp_path / "rot90.mp4")

        assert nearest_quadrant(sample(output, 0.42, 0.25)) == "bottom left"
        assert nearest_quadrant(sample(output, 0.58, 0.25)) == "top left"
        assert nearest_quadrant(sample(output, 0.42, 0.75)) == "bottom right"
        assert nearest_quadrant(sample(output, 0.58, 0.75)) == "top right"


class TestFlip:
    def test_flipping_swaps_left_for_right(self, timeline, quad_source, tmp_path):
        clip = timeline.video_tracks[0].clips[0]
        ops.set_flipped(timeline, [clip], True)
        output = render(timeline, quad_source, tmp_path / "flip.mp4")
        assert nearest_quadrant(sample(output, 0.25, 0.25)) == "top right"
        assert nearest_quadrant(sample(output, 0.75, 0.25)) == "top left"


def first_frame_image(path):
    """The source's first frame as a QImage at its native size.

    Decoded with ffmpeg rather than PyAV so this test does not depend on the
    preview's own decoder being right — it is the *geometry* being compared,
    and using the same decoder both sides would hide a fault in it.
    """
    from PySide6.QtGui import QImage

    info = probe(path)
    width, height = info.video.display_size
    raw = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1",
            "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    # Copied, because QImage does not take ownership of the buffer.
    return QImage(raw, width, height, 3 * width, QImage.Format_RGB888).copy()


def paint_preview(image, framing, rotation=0, flipped=False, size=(960, 540)):
    """What the viewer puts on screen for this framing."""
    from PySide6.QtGui import QImage
    from vedit.player.surface import VideoSurface

    surface = VideoSurface()
    surface.resize(*size)
    surface.set_frame_size(1920, 1080)
    surface.set_image(image)
    surface.set_picture(framing, rotation, flipped)

    target = QImage(*size, QImage.Format_RGB888)
    surface.render(target)
    return target


def preview_region(painted, x_fraction, y_fraction):
    colour = painted.pixelColor(
        int(painted.width() * x_fraction), int(painted.height() * y_fraction)
    )
    return nearest_region((colour.red(), colour.green(), colour.blue()))


class TestPreviewShowsWhatIsExported:
    """The claim the whole feature rests on.

    Both sides are driven from one framing, and both are asked what colour is
    at the same place. If they ever disagree, the editor is lying about what it
    is going to produce — which is the failure users cannot work around, because
    they only see it after the export.
    """

    @pytest.mark.parametrize(
        "framing, rotation, flipped",
        [
            (Framing(), 0, False),
            (Framing(zoom=2.0, x=-1.0, y=-1.0), 0, False),
            (Framing(zoom=2.0, x=1.0, y=-1.0), 0, False),
            (Framing(zoom=2.0, x=-1.0, y=1.0), 0, False),
            (Framing(zoom=2.0, x=1.0, y=1.0), 0, False),
            (Framing(zoom=1.5, x=0.5), 0, False),
            (Framing(), 0, True),
            (Framing(zoom=1.3), 0, True),
            (Framing(), 90, False),
            (Framing(), 180, False),
            (Framing(), 270, False),
            (Framing(zoom=1.4), 90, False),
            (Framing(), 90, True),
            (Framing(zoom=2.0, x=1.0), 0, True),
            (Framing(zoom=2.0, x=-1.0, y=1.0), 90, False),
            (Framing(zoom=1.6, x=0.6, y=-0.4), 270, True),
        ],
    )
    def test_the_same_colour_lands_in_the_same_place(
        self, qt_app, timeline, quad_source, tmp_path, framing, rotation, flipped
    ):
        clip = timeline.video_tracks[0].clips[0]
        ops.set_framing(timeline, [clip], framing)
        ops.set_rotation(timeline, [clip], rotation)
        ops.set_flipped(timeline, [clip], flipped)

        exported = render(timeline, quad_source, tmp_path / "compare.mp4")
        painted = paint_preview(
            first_frame_image(quad_source.path), framing, rotation, flipped
        )

        # Two rings of sample points. The outer four sit where the bars fall on
        # a turned picture; the inner four sit inside the sliver a turned
        # picture becomes. Neither ring alone covers both cases, and none of
        # them sit on x=0.5, which is the seam between two quadrants.
        points = (
            (0.20, 0.20), (0.80, 0.20), (0.20, 0.80), (0.80, 0.80),
            (0.42, 0.30), (0.58, 0.30), (0.42, 0.70), (0.58, 0.70),
        )
        seen = []
        for at_x, at_y in points:
            shown = preview_region(painted, at_x, at_y)
            seen.append(shown)
            assert shown == nearest_region(
                sample(exported, at_x, at_y)
            ), f"preview and export disagree at ({at_x}, {at_y})"

        # Without this the rotated cases would pass by agreeing that every
        # sample is black, which proves nothing about where the picture went.
        assert any(region != "bars" for region in seen), "sampled only letterbox"


class TestTheMove:
    """A framing that travels across the clip.

    `zoompan` is fiddly enough that these are worth running against real ffmpeg
    rather than trusting the expression to be well formed: three of its defaults
    would quietly break the export, and a filter graph that merely *parses* is
    not evidence that it does the right thing.
    """

    def test_a_push_in_starts_wide_and_ends_tight(
        self, timeline, quad_source, tmp_path
    ):
        """Framed on the top-left quadrant at the end, the first frame still
        shows all four and the last shows only red."""
        clip = timeline.video_tracks[0].clips[0]
        ops.set_framing(timeline, [clip], Framing())
        ops.set_framing_move(timeline, [clip], Framing(zoom=2.0, x=-1.0, y=-1.0))
        output = render(timeline, quad_source, tmp_path / "push.mp4")

        first = frame_at(output, 0.0)
        assert {
            nearest_quadrant(sample(first, x, y))
            for x, y in ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
        } == set(QUADRANTS), "the move should begin on the whole picture"

        last = frame_at(output, 1.9)
        for x, y in ((0.25, 0.25), (0.75, 0.75)):
            assert nearest_quadrant(sample(last, x, y)) == "top left"

    def test_the_output_keeps_its_frame_rate_and_length(
        self, timeline, quad_source, tmp_path
    ):
        """`zoompan` defaults to 25 fps and holds each frame for 90 of them.
        Both would sail through a graph-string test and wreck the export."""
        clip = timeline.video_tracks[0].clips[0]
        ops.set_framing_move(timeline, [clip], Framing(zoom=2.0))
        output = render(timeline, quad_source, tmp_path / "rate.mp4")

        info = probe(output)
        assert float(info.video.fps) == pytest.approx(30.0)
        assert float(info.duration) == pytest.approx(2.0, abs=0.1)

    def test_the_export_tracks_the_preview_all_the_way_through(
        self, qt_app, timeline, quad_source, tmp_path
    ):
        """Sampled at five points along the move, both sides must agree — which
        is what pins the progress calculation, not just the two ends."""
        clip = timeline.video_tracks[0].clips[0]
        start, end = Framing(zoom=2.0, x=-1.0, y=-1.0), Framing(zoom=2.0, x=1.0, y=1.0)
        ops.set_framing(timeline, [clip], start)
        ops.set_framing_move(timeline, [clip], end)

        output = render(timeline, quad_source, tmp_path / "track.mp4")
        image = first_frame_image(quad_source.path)

        for seconds in (0.0, 0.5, 1.0, 1.5, 1.9):
            frame = int(round(seconds * 30))
            painted = paint_preview(image, clip.framing_at(frame))
            exported = frame_at(output, seconds)
            assert preview_region(painted, 0.5, 0.5) == nearest_region(
                sample(exported, 0.5, 0.5)
            ), f"disagree {seconds}s into the move"
