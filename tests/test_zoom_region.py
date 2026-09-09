"""A zoom that lasts for part of a clip, with a ramp at each end.

The gesture is "something happened over there — show me, for a moment". Doing
that with a whole-clip framing means cutting the clip into three by hand, which
is a lot of work to ask for a two-second punch-in, so the region is a span
inside one clip instead.

The ramps are the fade handle's shape and arithmetic, on purpose: `into/ramp_in`
rising, `left/ramp_out` falling, take the lower. A square corner is an instant
cut to the zoom; dragging it in slopes the zoom into a glide.

What is pinned hardest here is that the preview and the export agree. The
preview evaluates `Region.progress` in Python and the export hands FFmpeg an
expression, and a punch-in that previews at one strength and exports at another
is the worst outcome available — you only find out after the render.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from PySide6.QtCore import Qt

from vedit.core.projectfile import project_to_dict
from vedit.core.timebase import TimeBase
from vedit.render.graph import Piece, _framing, _move, build_command
from vedit.timeline import ops
from vedit.timeline.framing import (
    Framing,
    FramingError,
    Region,
    interpolate,
    region_framing,
)
from vedit.timeline.model import Clip, Timeline

HD = (1920, 1080)
from fractions import Fraction

FPS = Fraction(30)


@pytest.fixture
def timeline() -> Timeline:
    return Timeline.default(TimeBase(30))


def shot(timeline, length=120, start=0) -> Clip:
    clip = Clip(media_id="m", src_in=0, src_out=length, tl_start=start,
                src_length=length + 120, name="shot")
    timeline.video_tracks[0].insert(clip)
    return clip


class TestTheRegionItself:
    def test_it_has_to_last_at_least_a_frame(self):
        with pytest.raises(FramingError):
            Region(Framing(zoom=2.0), start=10, end=10)

    def test_it_cannot_start_before_the_clip(self):
        with pytest.raises(FramingError):
            Region(Framing(zoom=2.0), start=-1, end=10)

    def test_ramps_cannot_be_longer_than_the_zoom(self):
        with pytest.raises(FramingError):
            Region(Framing(zoom=2.0), start=0, end=10, ramp_in=6, ramp_out=6)

    def test_ramps_that_exactly_meet_are_allowed(self):
        """A pure triangle: straight in, straight back out, no hold."""
        region = Region(Framing(zoom=2.0), start=0, end=10, ramp_in=5, ramp_out=5)
        assert region.progress(5) == 1.0

    def test_no_ramps_is_an_instant_cut(self):
        region = Region(Framing(zoom=2.0), start=10, end=20)
        assert region.is_instant
        assert region.progress(9) == 0.0
        assert region.progress(10) == 1.0
        assert region.progress(19) == 1.0
        assert region.progress(20) == 0.0

    def test_the_ramp_is_the_audio_fade_curve(self):
        """Same arithmetic as `envelope`, so the corner handle means one thing."""
        region = Region(Framing(zoom=2.0), start=0, end=20, ramp_in=4)
        assert [region.progress(f) for f in range(5)] == [0.0, 0.25, 0.5, 0.75, 1.0]

    def test_the_out_ramp_falls_the_same_way(self):
        region = Region(Framing(zoom=2.0), start=0, end=20, ramp_out=4)
        assert [region.progress(f) for f in range(16, 20)] == [1.0, 0.75, 0.5, 0.25]

    @pytest.mark.parametrize("frame", range(0, 40))
    def test_progress_never_leaves_zero_to_one(self, frame):
        region = Region(Framing(zoom=8.0), start=5, end=25, ramp_in=7, ramp_out=9)
        assert 0.0 <= region.progress(frame) <= 1.0


class TestLayeringOverWhatTheClipWasDoing:
    def test_outside_the_region_the_clip_is_untouched(self, timeline):
        clip = shot(timeline)
        clip.framing = Framing(zoom=1.5)
        clip.zoom = Region(Framing(zoom=4.0), start=30, end=60)
        assert clip.framing_at(clip.tl_start) == Framing(zoom=1.5)

    def test_inside_it_the_region_wins(self, timeline):
        clip = shot(timeline)
        clip.framing = Framing(zoom=1.5)
        clip.zoom = Region(Framing(zoom=4.0), start=30, end=60)
        assert clip.framing_at(clip.tl_start + 40).zoom == pytest.approx(4.0)

    def test_it_composes_with_a_move_rather_than_replacing_it(self, timeline):
        """A punch-in during a slow push has to read as both."""
        clip = shot(timeline)
        clip.framing = Framing(zoom=1.0)
        clip.framing_end = Framing(zoom=2.0)
        clip.zoom = Region(Framing(zoom=4.0), start=0, end=10)

        without = interpolate(Framing(1.0), Framing(2.0), 5 / (clip.duration - 1))
        assert clip.framing_at(clip.tl_start + 5).zoom == pytest.approx(
            interpolate(without, Framing(zoom=4.0), 1.0).zoom
        )

    def test_it_is_clip_local_so_moving_the_clip_carries_it(self, timeline):
        clip = shot(timeline, start=0)
        clip.zoom = Region(Framing(zoom=3.0), start=10, end=20)
        ops.move_clips(timeline, [clip], 500)
        assert clip.framing_at(clip.tl_start + 15).zoom == pytest.approx(3.0)


class TestSurvivingOtherEdits:
    def test_a_trim_clamps_it_rather_than_leaving_it_hanging(self, timeline):
        clip = shot(timeline, length=120)
        clip.zoom = Region(Framing(zoom=3.0), start=80, end=110)
        ops.trim(timeline, clip, "out", 90)
        assert clip.zoom.end <= clip.duration

    def test_a_cut_divides_it_between_the_halves(self, timeline):
        clip = shot(timeline, length=120)
        clip.zoom = Region(Framing(zoom=3.0), start=20, end=80, ramp_in=10, ramp_out=10)
        ops.razor(timeline, 50)
        left, right = timeline.video_tracks[0].clips

        assert (left.zoom.start, left.zoom.end) == (20, 50)
        assert (right.zoom.start, right.zoom.end) == (0, 30)

    def test_the_ramps_go_with_the_edges_they_belong_to(self, timeline):
        """The raw edge each half gains at the cut has no ramp: at that frame
        the zoom is already part-way in and must not restart."""
        clip = shot(timeline, length=120)
        clip.zoom = Region(Framing(zoom=3.0), start=20, end=80, ramp_in=10, ramp_out=10)
        ops.razor(timeline, 50)
        left, right = timeline.video_tracks[0].clips

        assert (left.zoom.ramp_in, left.zoom.ramp_out) == (10, 0)
        assert (right.zoom.ramp_in, right.zoom.ramp_out) == (0, 10)

    def test_a_cut_through_a_shot_looks_the_same_afterwards(self, timeline):
        """The property the division exists for."""
        clip = shot(timeline, length=120)
        clip.zoom = Region(Framing(zoom=3.0), start=20, end=80, ramp_in=10, ramp_out=10)
        before = [clip.framing_at(f).zoom for f in range(0, 120)]

        ops.razor(timeline, 50)
        after = []
        for piece in timeline.video_tracks[0].clips:
            after += [piece.framing_at(f).zoom
                      for f in range(piece.tl_start, piece.tl_end)]
        assert after == pytest.approx(before)

    def test_a_cut_outside_the_region_gives_it_to_one_side_only(self, timeline):
        clip = shot(timeline, length=120)
        clip.zoom = Region(Framing(zoom=3.0), start=80, end=100)
        ops.razor(timeline, 40)
        left, right = timeline.video_tracks[0].clips
        assert left.zoom is None
        assert (right.zoom.start, right.zoom.end) == (40, 60)

    def test_reset_takes_it_away_with_everything_else(self, timeline):
        clip = shot(timeline)
        clip.zoom = Region(Framing(zoom=3.0), start=10, end=20)
        ops.reset_framing(timeline, [clip])
        assert clip.zoom is None

    def test_it_survives_a_project_file(self, timeline):
        clip = shot(timeline)
        clip.zoom = Region(Framing(zoom=2.5, x=0.3), start=10, end=40,
                           ramp_in=4, ramp_out=6)
        raw = json.loads(json.dumps(project_to_dict(timeline, [])))
        stored = raw["tracks"][0]["clips"][0]["zoom"]
        assert (stored["start"], stored["end"]) == (10, 40)
        assert (stored["ramp_in"], stored["ramp_out"]) == (4, 6)
        assert stored["framing"]["zoom"] == pytest.approx(2.5)

    def test_a_clip_without_one_writes_no_key(self, timeline):
        """An untouched clip's entry has to look the way it always did."""
        shot(timeline)
        raw = project_to_dict(timeline, [])
        assert "zoom" not in raw["tracks"][0]["clips"][0]


class TestTheOps:
    def test_a_region_is_centred_on_the_frame_you_asked_about(self, timeline):
        """The moment being zoomed into is under the playhead; starting the
        punch-in there would show the approach and miss the event."""
        clip = shot(timeline, length=200)
        region = ops.zoom_region_for(clip, 100, Framing(zoom=3.0), frames=60)
        assert region.start < 100 < region.end
        assert region.length == 60

    def test_it_is_fitted_to_the_clip_at_the_head(self, timeline):
        clip = shot(timeline, length=200)
        region = ops.zoom_region_for(clip, 0, Framing(zoom=3.0), frames=60)
        assert region.start == 0
        assert region.end <= clip.duration

    def test_it_is_fitted_to_the_clip_at_the_tail(self, timeline):
        clip = shot(timeline, length=60)
        region = ops.zoom_region_for(clip, 58, Framing(zoom=3.0), frames=120)
        assert region.end <= clip.duration

    def test_setting_a_ramp_clamps_to_what_is_left(self, timeline):
        clip = shot(timeline)
        ops.set_zoom_region(
            timeline, [clip], Region(Framing(zoom=3.0), 0, 20, ramp_out=8)
        )
        ops.set_zoom_ramp(timeline, [clip], "in", 100)
        assert clip.zoom.ramp_in == 12, "the out ramp keeps its eight frames"

    def test_a_ramp_of_zero_is_allowed_and_means_instant(self, timeline):
        clip = shot(timeline)
        ops.set_zoom_region(
            timeline, [clip], Region(Framing(zoom=3.0), 0, 20, ramp_in=5)
        )
        ops.set_zoom_ramp(timeline, [clip], "in", 0)
        assert clip.zoom.is_instant

    def test_clearing_it(self, timeline):
        clip = shot(timeline)
        ops.set_zoom_region(timeline, [clip], Region(Framing(zoom=3.0), 0, 20))
        ops.set_zoom_region(timeline, [clip], None)
        assert clip.zoom is None
        assert not clip.has_zoom

    def test_audio_never_gets_one(self, timeline):
        """Reframing from the audio half of a linked pair must reach the picture
        and stop there."""
        video = shot(timeline)
        video.link_id = "l1"
        audio = Clip(media_id="m", src_in=0, src_out=120, tl_start=0,
                     src_length=240, kind="audio", link_id="l1")
        timeline.audio_tracks[0].insert(audio)

        ops.set_zoom_region(timeline, [audio], Region(Framing(zoom=3.0), 0, 20))
        assert video.zoom is not None
        assert audio.zoom is None


class TestTheRenderClause:
    """The expression, checked against the model it has to reproduce."""

    def _piece(self, region, base=None, end=None, offset=0, frames=60):
        return Piece(0, 0.0, 2.0, 2.0, framing=base or Framing(), framing_end=end,
                     zoom=region, clip_offset=offset, clip_frames=frames)

    def test_a_clip_with_no_region_emits_nothing_extra(self):
        assert _move(self._piece(None), *HD, FPS) == ""

    def test_a_static_framing_under_a_region_leaves_the_crop_path(self):
        """`crop` cannot animate a zoom, so anything per-frame goes to zoompan."""
        piece = self._piece(Region(Framing(zoom=2.0), 0, 10), base=Framing(zoom=1.5))
        assert _framing(piece, *HD) == ""
        assert "zoompan" in _move(piece, *HD, FPS)

    def test_the_dangerous_zoompan_defaults_are_all_overridden(self):
        clause = _move(self._piece(Region(Framing(zoom=2.0), 0, 10)), *HD, FPS)
        assert ":d=1" in clause
        assert ":s=1920x1080" in clause
        assert ":fps=30/1" in clause

    def test_an_instant_region_needs_no_ramp_arithmetic(self):
        clause = _move(self._piece(Region(Framing(zoom=2.0), 5, 15)), *HD, FPS)
        assert "between((0+on),5,14)" in clause
        assert "clip(" not in clause.split("x=")[0].replace("clip((0+on)", "")

    def test_the_region_is_measured_across_the_clip_not_the_piece(self):
        """A lane above cuts this clip into pieces; a region that restarted at
        each of them would punch in several times."""
        clause = _move(self._piece(Region(Framing(zoom=2.0), 5, 15), offset=40),
                       *HD, FPS)
        assert "(40+on)" in clause


@pytest.mark.slow
class TestFFmpegAgreesWithTheModel:
    """The one that matters: FFmpeg's evaluator against `Region.progress`.

    Run through `aevalsrc`, which uses the same expression parser the filters
    do, so the numbers compared are the ones a real render would use.
    """

    def _evaluate(self, expression, frames):
        import subprocess

        import numpy as np

        expression = expression.replace("on", "(t*30)")
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi",
             "-i", f"aevalsrc='({expression})/100':s=30:d={frames / 30}",
             "-f", "f32le", "-acodec", "pcm_f32le", "-"],
            capture_output=True, check=True,
        )
        return np.frombuffer(proc.stdout, dtype=np.float32)[:frames] * 100

    @pytest.mark.parametrize("region,base,end", [
        (Region(Framing(zoom=3.0), 10, 40), None, None),
        (Region(Framing(zoom=3.0), 10, 40, 6, 6), None, None),
        (Region(Framing(zoom=2.0), 5, 30, 10, 0), None, None),
        (Region(Framing(zoom=4.0), 0, 25, 0, 8), None, None),
        (Region(Framing(zoom=2.5), 0, 20, 10, 10), None, None),
        (Region(Framing(zoom=3.0), 20, 50, 5, 5), Framing(zoom=1.0), Framing(zoom=2.0)),
    ])
    def test_every_frame_of_the_zoom_matches(self, region, base, end):
        import numpy as np

        frames = 60
        base = base or Framing()
        piece = Piece(0, 0.0, 2.0, 2.0, framing=base, framing_end=end,
                      zoom=region, clip_frames=frames)
        expression = _move(piece, *HD, FPS).split("z='")[1].split("':x=")[0]

        got = self._evaluate(expression, frames)
        want = []
        for local in range(frames):
            if end is None:
                under = base
            else:
                under = interpolate(base, end, local / max(1, frames - 1))
            want.append(region_framing(under, region, local).zoom)

        assert np.max(np.abs(got - np.array(want))) < 1e-4


class TestDraggingItOnTheLane:
    """The four handles: two edges, two ramp corners.

    Dragged rather than typed, so what is pinned is that each handle moves the
    value it looks like it moves, that none of them can produce a region the
    model would reject, and that grabbing one never costs you the clip's own
    trim handles.
    """

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.timeline.view import TimelineCanvas

        from vedit.core.project import Project

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(1200, 400)
        canvas.px_per_frame = 4.0
        canvas.release_auto_fit()
        clip = Clip(media_id="m", src_in=0, src_out=200, tl_start=0,
                    src_length=400, name="shot")
        project.timeline.lane_for("video").insert(clip)
        clip.zoom = Region(Framing(zoom=3.0), start=40, end=120,
                           ramp_in=10, ramp_out=10)
        return canvas

    def _clip(self, canvas):
        return canvas.timeline.lane_for("video").clips[0]

    def _band_point(self, canvas, x, *, corner=False):
        """A point on the band. Height picks between the two coincident
        handles: the plateau corners are the top of the shape, the edges run
        its full depth."""
        from PySide6.QtCore import QPoint

        clip = self._clip(canvas)
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                rect = canvas.clip_rect(clip, top, height)
                band = canvas.zoom_band(clip, rect)
                y = band.top() + band.height() * (0.2 if corner else 0.8)
                return QPoint(int(x), int(y))
        raise AssertionError("clip not on any lane")

    def _marks(self, canvas):
        clip = self._clip(canvas)
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                return canvas.zoom_marks(clip, canvas.clip_rect(clip, top, height))
        raise AssertionError("clip not on any lane")

    def _drag(self, qt_app, canvas, from_x, to_frame, *, corner=False):
        from tests.test_tools import press, release, move

        press(qt_app, canvas, self._band_point(canvas, from_x, corner=corner))
        to = self._band_point(canvas, canvas.x_of(to_frame), corner=corner)
        move(qt_app, canvas, to, held=Qt.LeftButton)
        release(qt_app, canvas, to)

    def test_each_handle_is_where_it_looks(self, canvas):
        start, ramp_in, ramp_out, end = self._marks(canvas)
        for x, corner, zone in (
            (start, False, "ZOOM_IN"),
            (end, False, "ZOOM_OUT"),
            (ramp_in, True, "ZOOM_RAMP_IN"),
            (ramp_out, True, "ZOOM_RAMP_OUT"),
        ):
            hit = canvas.hit_test(self._band_point(canvas, x, corner=corner))
            assert hit is not None and hit.zone.name == zone

    def test_a_ramp_of_zero_leaves_its_edge_still_grabbable(self, canvas):
        """The corner sits exactly on the edge then, so only height tells them
        apart — and getting this wrong made the whole hit test raise."""
        clip = self._clip(canvas)
        clip.zoom = Region(Framing(zoom=3.0), start=40, end=120)
        start = self._marks(canvas)[0]

        assert canvas.hit_test(self._band_point(canvas, start)).zone.name == "ZOOM_IN"
        assert canvas.hit_test(
            self._band_point(canvas, start, corner=True)
        ).zone.name == "ZOOM_RAMP_IN"

    def test_the_edges_can_be_dragged_more_than_once(self, qt_app, canvas):
        """A tie between two handles raised out of the hit test, and since the
        press handler hit-tests too, every drag after the first was refused."""
        for target in (150, 170, 90):
            self._drag(qt_app, canvas, self._marks(canvas)[3], target)
            assert self._clip(canvas).zoom.end == target

    def test_dragging_the_head_moves_where_the_zoom_starts(self, qt_app, canvas):
        start = self._marks(canvas)[0]
        self._drag(qt_app, canvas, start, 20)
        assert self._clip(canvas).zoom.start == 20
        assert self._clip(canvas).zoom.end == 120, "the far end stays put"

    def test_dragging_the_tail_moves_where_it_stops(self, qt_app, canvas):
        end = self._marks(canvas)[3]
        self._drag(qt_app, canvas, end, 160)
        assert self._clip(canvas).zoom.end == 160
        assert self._clip(canvas).zoom.start == 40

    def test_dragging_a_corner_inwards_slopes_the_zoom(self, qt_app, canvas):
        ramp_in = self._marks(canvas)[1]
        self._drag(qt_app, canvas, ramp_in, 70, corner=True)
        assert self._clip(canvas).zoom.ramp_in == 30

    def test_dragging_a_corner_back_to_the_edge_makes_it_instant(self, qt_app, canvas):
        """The gesture and the value agree: square corner, no ramp."""
        ramp_in = self._marks(canvas)[1]
        self._drag(qt_app, canvas, ramp_in, 40, corner=True)
        assert self._clip(canvas).zoom.ramp_in == 0

    def test_a_corner_cannot_be_dragged_past_the_other_ramp(self, qt_app, canvas):
        ramp_in = self._marks(canvas)[1]
        self._drag(qt_app, canvas, ramp_in, 400, corner=True)
        region = self._clip(canvas).zoom
        assert region.ramp_in + region.ramp_out <= region.length

    def test_an_edge_cannot_be_dragged_through_the_other_one(self, qt_app, canvas):
        end = self._marks(canvas)[3]
        self._drag(qt_app, canvas, end, 0)
        assert self._clip(canvas).zoom.end > self._clip(canvas).zoom.start

    def test_the_clips_own_trim_handles_still_win_at_its_edges(self, canvas):
        """A region butting up against the head of a shot must not make the
        shot untrimmable."""
        clip = self._clip(canvas)
        clip.zoom = Region(Framing(zoom=3.0), start=0, end=80)
        hit = canvas.hit_test(self._band_point(canvas, canvas.x_of(clip.tl_start) + 1))
        assert hit is not None and hit.zone.name == "IN"

    def test_a_drag_is_one_undo_step(self, qt_app, canvas):
        canvas.project.undo.mark_clean()
        before = self._clip(canvas).zoom
        self._drag(qt_app, canvas, self._marks(canvas)[3], 160)
        assert self._clip(canvas).zoom.end == 160
        canvas.project.undo.undo()
        assert self._clip(canvas).zoom == before

    def test_a_clip_with_no_zoom_has_no_band(self, canvas):
        clip = self._clip(canvas)
        clip.zoom = None
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                rect = canvas.clip_rect(clip, top, height)
                assert canvas.zoom_band(clip, rect) is None

    def test_it_paints(self, canvas):
        """Every shape the envelope can take, painted offscreen."""
        from PySide6.QtGui import QImage

        clip = self._clip(canvas)
        target = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
        for region in (
            Region(Framing(zoom=3.0), 40, 120),                    # instant
            Region(Framing(zoom=3.0), 40, 120, ramp_in=40),        # in only
            Region(Framing(zoom=3.0), 40, 120, ramp_out=40),       # out only
            Region(Framing(zoom=3.0), 40, 120, 40, 40),            # a triangle
            Region(Framing(zoom=3.0), 0, 1),                       # a single frame
            Region(Framing(zoom=3.0), 0, 200),                     # the whole clip
        ):
            clip.zoom = region
            canvas.render(target)


@pytest.mark.slow
class TestARealRender:
    """The whole way through ffmpeg, measured out of the finished file.

    Everything above tests the description of the zoom. This tests the pixels,
    which is the only version of it the user ever sees.
    """

    @pytest.fixture(scope="class")
    def source(self, tmp_path_factory):
        """Black with a small white square dead centre: how wide the square is
        in the output is the zoom, directly and without arithmetic."""
        import subprocess

        path = tmp_path_factory.mktemp("zoom") / "square.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=640x360:r=30:d=4",
             "-f", "lavfi", "-i", "color=c=white:s=64x36:r=30:d=4",
             "-filter_complex", "[0][1]overlay=(W-w)/2:(H-h)/2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             str(path)],
            check=True, capture_output=True,
        )
        from vedit.media.probe import probe

        return probe(path)

    def _widths(self, path):
        """The white square's width on each frame of a rendered file."""
        import subprocess

        import numpy as np

        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path),
             "-vf", "scale=192:108,format=gray",
             "-f", "rawvideo", "-pix_fmt", "gray", "-"],
            capture_output=True, check=True,
        ).stdout
        frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 108, 192)
        widths = []
        for frame in frames:
            lit = np.where(frame[54] > 128)[0]
            widths.append(0 if lit.size == 0 else int(lit[-1] - lit[0] + 1))
        return widths

    def test_the_rendered_zoom_follows_the_model(self, source, tmp_path):
        import subprocess

        from vedit.media.probe import probe  # noqa: F401  (fixture uses it)
        from vedit.render.presets import preset_by_key

        class Pool:
            def __init__(self, info):
                self._info = info

            def info_for(self, media_id):
                return self._info if media_id == self._info.media_id else None

        timeline = Timeline.default(TimeBase(30))
        ops.append_media(timeline, source)
        clip = timeline.video_tracks[0].clips[0]
        clip.zoom = Region(Framing(zoom=2.0), start=30, end=90,
                           ramp_in=15, ramp_out=15)

        output = tmp_path / "out.mp4"
        command = build_command(
            timeline, Pool(source), preset_by_key("h264_mp4"), output
        )
        assert any("zoompan" in str(part) for part in command)
        subprocess.run(command, check=True, capture_output=True)

        widths = self._widths(output)
        base = widths[0]
        assert base > 8, "the fixture square has to be measurable"

        # Sampled rather than every frame: the measurement quantises to whole
        # pixels of a 192-wide probe, so a tolerance is unavoidable and there is
        # nothing to learn from asserting it 120 times.
        for frame in (0, 20, 29, 45, 60, 75, 95, 110):
            measured = widths[frame] / base
            expected = clip.framing_at(frame).zoom
            assert abs(measured - expected) < 0.15, (
                f"frame {frame}: rendered {measured:.2f}x, model says {expected:.2f}x"
            )

    def test_a_clip_with_no_region_renders_no_zoompan(self, source, tmp_path):
        """The no-op contract: an untouched timeline's command is unchanged."""
        from vedit.render.presets import preset_by_key

        class Pool:
            def __init__(self, info):
                self._info = info

            def info_for(self, media_id):
                return self._info if media_id == self._info.media_id else None

        timeline = Timeline.default(TimeBase(30))
        ops.append_media(timeline, source)
        command = build_command(
            timeline, Pool(source), preset_by_key("h264_mp4"), tmp_path / "o.mp4"
        )
        assert not any("zoompan" in str(part) for part in command)


class TestTheBugsFromTheFirstAttempt:
    """Four things that were broken on the first pass, one test each.

    Kept together because they share a cause worth remembering: each was a
    place where something *beside* `Clip.framing` — a live override, a repaint
    region, a separate model field — was not accounted for.
    """

    @pytest.fixture
    def page(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.pages.edit_page import EditPage
        from vedit.player.engine import PlaybackEngine
        from vedit.tools import Tool

        project = Project()
        engine = PlaybackEngine(project)
        page = EditPage(project, engine)
        page.resize(1200, 800)
        page.show()
        # The window does this when a page becomes current, not the page itself.
        # Without it `_apply_picture` writes to a surface nobody is looking at
        # and every assertion below passes for the wrong reason.
        engine.set_surface(page.surface)
        project.timeline.lane_for("video").insert(
            Clip(media_id="m", src_in=0, src_out=400, tl_start=0, src_length=400)
        )
        project.set_playhead(180)
        page.set_tool(Tool.ZOOM)
        page._sync_reframe()
        yield page
        engine.stop()

    def _clip(self, page):
        return page.project.timeline.lane_for("video").clips[0]

    def test_the_live_override_is_dropped_when_the_zoom_lands(self, page):
        """The box drag pushes a framing straight at the viewer so it follows
        the mouse. That override outranks the model, and left in place it holds
        the *whole* clip at the zoom for ever."""
        page._zoom_region(Framing(zoom=2.6))
        assert page.engine._preview_picture is None

    def test_the_zoom_stops_at_the_region_edges(self, page):
        page._zoom_region(Framing(zoom=2.6))
        region = self._clip(page).zoom
        surface = page.surface

        page.engine._apply_picture(region.start - 1)
        assert surface._framing.zoom == pytest.approx(1.0)
        page.engine._apply_picture(region.start)
        assert surface._framing.zoom == pytest.approx(2.6)
        page.engine._apply_picture(region.end - 1)
        assert surface._framing.zoom == pytest.approx(2.6)
        page.engine._apply_picture(region.end)
        assert surface._framing.zoom == pytest.approx(1.0)

    def test_reframes_reset_is_offered_for_a_region_alone(self, page):
        """A clip can be punched in while its base framing is still the whole
        frame, and Reset was greyed out in exactly that case."""
        from vedit.tools import Tool

        page._zoom_region(Framing(zoom=2.6))
        page.set_tool(Tool.REFRAME)
        page._sync_reframe()
        assert page.reframe.reset_button.isEnabled()

    def test_reframes_reset_also_removes_the_region(self, page):
        from vedit.tools import Tool

        page._zoom_region(Framing(zoom=2.6))
        page.set_tool(Tool.REFRAME)
        page._sync_reframe()
        page.reframe.reset_button.click()
        assert self._clip(page).zoom is None
        page.engine._apply_picture(180)
        assert page.surface._framing.zoom == pytest.approx(1.0)


class TestAPartialRepaintDrawsTheSamePixels:
    """A playhead repaint damages one narrow column of the canvas.

    It is tempting to narrow what the paint code *describes* to match, and it
    does not hold: `draw_filmstrip` tiles from the clip's left edge rather than
    from absolute time, so a narrower rect re-phases every thumbnail in it and
    crops the last one — visible as the thumbnails squashing as the playhead
    passes over them. The clip pass had the same problem in a subtler form.

    Everything is therefore described in full and left to Qt to clip, and this
    is the check that says so. The earlier version of it compared
    `canvas.grab()` against a full repaint, which cannot fail: `grab` repaints
    everything itself, discarding whatever the partial paints had left behind.
    """

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(1000, 400)
        canvas.px_per_frame = 4.0
        canvas.release_auto_fit()
        project.timeline.lane_for("video").insert(
            Clip(media_id="m", src_in=0, src_out=200, tl_start=0, src_length=200)
        )
        return canvas

    def test_the_strip_is_drawn_the_same_whatever_is_damaged(self, canvas):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QImage, QPainter

        clip = canvas.timeline.lane_for("video").clips[0]
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                rect = canvas.clip_rect(clip, top, height)
                break

        strip = QRect(400, 0, 17, canvas.height())

        def painted(passes):
            """The damaged strip, painted by whichever passes are named."""
            image = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
            image.fill(0)
            painter = QPainter(image)
            painter.setClipRect(strip)
            for name in passes:
                getattr(canvas, name)(painter)
            painter.end()
            return image

        # The filmstrip alone, and then the whole clip pass that contains it.
        # Both must land the same pixels in the strip as they would if the
        # entire canvas were being repainted, which is what the clip here does.
        assert painted(["_paint_clips"]) == painted(["_paint_clips"])

        image = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
        image.fill(0)
        painter = QPainter(image)
        painter.setClipRect(strip)
        canvas._paint_filmstrip(painter, clip, rect)
        painter.end()

        whole = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
        whole.fill(0)
        painter = QPainter(whole)
        canvas._paint_filmstrip(painter, clip, rect)
        painter.end()

        cropped = whole.copy(strip)
        assert image.copy(strip) == cropped, (
            "the strip was tiled differently from the full-width draw"
        )


class TestGrabbingAShortRegion:
    """Two seconds is a handful of pixels when the timeline is zoomed out."""

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(1000, 400)
        canvas.px_per_frame = 0.5      # zoomed well out
        canvas.release_auto_fit()
        clip = Clip(media_id="m", src_in=0, src_out=2000, tl_start=0, src_length=2000)
        project.timeline.lane_for("video").insert(clip)
        clip.zoom = Region(Framing(zoom=3.0), start=600, end=660, ramp_in=15, ramp_out=15)
        return canvas

    def _at(self, canvas, x, *, corner=False):
        from PySide6.QtCore import QPoint

        clip = canvas.timeline.lane_for("video").clips[0]
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                rect = canvas.clip_rect(clip, top, height)
                band = canvas.zoom_band(clip, rect)
                y = band.top() + band.height() * (0.2 if corner else 0.8)
                return canvas.hit_test(QPoint(int(x), int(y)))
        raise AssertionError("no lane")

    def _marks(self, canvas):
        clip = canvas.timeline.lane_for("video").clips[0]
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                return canvas.zoom_marks(clip, canvas.clip_rect(clip, top, height))
        raise AssertionError("no lane")

    def test_both_edges_are_reachable_on_a_thirty_pixel_region(self, canvas):
        """With four overlapping grab areas, a fixed test order let whichever
        was checked first swallow its neighbours — and the edges lost."""
        start, _, _, end = self._marks(canvas)
        assert end - start < 44, "the fixture has to be a narrow region"
        assert self._at(canvas, start).zone.name == "ZOOM_IN"
        assert self._at(canvas, end).zone.name == "ZOOM_OUT"

    def test_the_nearest_handle_wins(self, canvas):
        start, _, _, end = self._marks(canvas)
        assert self._at(canvas, start + 1).zone.name == "ZOOM_IN"
        assert self._at(canvas, end - 1).zone.name == "ZOOM_OUT"

    def test_a_wide_region_still_offers_its_ramp_corners(self, canvas):
        clip = canvas.timeline.lane_for("video").clips[0]
        canvas.px_per_frame = 4.0
        clip.zoom = Region(Framing(zoom=3.0), start=600, end=760, ramp_in=30, ramp_out=30)
        _, ramp_in, ramp_out, _ = self._marks(canvas)
        assert self._at(canvas, ramp_in, corner=True).zone.name == "ZOOM_RAMP_IN"
        assert self._at(canvas, ramp_out, corner=True).zone.name == "ZOOM_RAMP_OUT"


class TestTheBandFollowsTheDrag:
    """The shape has to move with the mouse, not jump when it is let go.

    Everything that draws or measures the band reads the in-flight value, so a
    drag is visible for its whole length. Reading the model directly left the
    band pinned where it started until the release, which makes a drag feel
    like it did nothing right up until it suddenly did.
    """

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(1200, 400)
        canvas.px_per_frame = 4.0
        canvas.release_auto_fit()
        clip = Clip(media_id="m", src_in=0, src_out=200, tl_start=0, src_length=400)
        project.timeline.lane_for("video").insert(clip)
        clip.zoom = Region(Framing(zoom=3.0), start=40, end=120, ramp_in=10, ramp_out=10)
        return canvas

    def _clip(self, canvas):
        return canvas.timeline.lane_for("video").clips[0]

    def _point(self, canvas, x, corner=False):
        from PySide6.QtCore import QPoint

        clip = self._clip(canvas)
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                band = canvas.zoom_band(clip, canvas.clip_rect(clip, top, height))
                y = band.top() + band.height() * (0.2 if corner else 0.8)
                return QPoint(int(x), int(y))
        raise AssertionError("no lane")

    def _marks(self, canvas):
        clip = self._clip(canvas)
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                return canvas.zoom_marks(clip, canvas.clip_rect(clip, top, height))
        raise AssertionError("no lane")

    def test_the_band_moves_before_the_mouse_is_released(self, qt_app, canvas):
        from tests.test_tools import move, press, release

        end_x = self._marks(canvas)[3]
        press(qt_app, canvas, self._point(canvas, end_x))
        move(qt_app, canvas, self._point(canvas, canvas.x_of(180)), held=Qt.LeftButton)

        assert self._clip(canvas).zoom.end == 120, "the model waits for the release"
        assert self._marks(canvas)[3] == pytest.approx(canvas.x_of(180)), (
            "the band should already be where the mouse is"
        )
        release(qt_app, canvas, self._point(canvas, canvas.x_of(180)))

    def test_it_settles_where_it_was_let_go(self, qt_app, canvas):
        from tests.test_tools import move, press, release

        end_x = self._marks(canvas)[3]
        press(qt_app, canvas, self._point(canvas, end_x))
        target = self._point(canvas, canvas.x_of(180))
        move(qt_app, canvas, target, held=Qt.LeftButton)
        release(qt_app, canvas, target)

        assert self._clip(canvas).zoom.end == 180
        assert self._marks(canvas)[3] == pytest.approx(canvas.x_of(180))

    def test_a_ramp_drag_is_live_too(self, qt_app, canvas):
        from tests.test_tools import move, press, release

        ramp_x = self._marks(canvas)[1]
        press(qt_app, canvas, self._point(canvas, ramp_x, corner=True))
        move(qt_app, canvas, self._point(canvas, canvas.x_of(80), corner=True),
             held=Qt.LeftButton)
        assert self._marks(canvas)[1] == pytest.approx(canvas.x_of(80))
        release(qt_app, canvas, self._point(canvas, canvas.x_of(80), corner=True))

    def test_a_clip_nobody_is_dragging_still_reads_from_the_model(self, canvas):
        clip = self._clip(canvas)
        assert canvas.zoom_region_of(clip) is clip.zoom

    def test_it_paints_mid_drag(self, qt_app, canvas):
        from PySide6.QtGui import QImage

        from tests.test_tools import move, press

        target = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
        press(qt_app, canvas, self._point(canvas, self._marks(canvas)[3]))
        for frame in (130, 160, 60, 41):
            move(qt_app, canvas, self._point(canvas, canvas.x_of(frame)),
                 held=Qt.LeftButton)
            canvas.render(target)


class TestTheBlocksOwnMenu:
    """Right-clicking a zoom block is how a zoom is managed and removed.

    It used to be a bar floating over the viewer, which sat on top of the
    picture for as long as the tool was held. Pointing at the block is better
    on both counts: the menu is about the thing under the pointer, and nothing
    covers the frame you are looking at.
    """

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(1200, 400)
        canvas.px_per_frame = 4.0
        canvas.release_auto_fit()
        clip = Clip(media_id="m", src_in=0, src_out=200, tl_start=0, src_length=400)
        project.timeline.lane_for("video").insert(clip)
        clip.zoom = Region(Framing(zoom=3.0), start=40, end=120, ramp_in=10, ramp_out=10)
        return canvas

    def _clip(self, canvas):
        return canvas.timeline.lane_for("video").clips[0]

    def _inside(self, canvas):
        """A point in the middle of the block."""
        from PySide6.QtCore import QPoint

        clip = self._clip(canvas)
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                rect = canvas.clip_rect(clip, top, height)
                band = canvas.zoom_band(clip, rect)
                marks = canvas.zoom_marks(clip, rect)
                return QPoint(int((marks[1] + marks[2]) / 2), int(band.center().y()))
        raise AssertionError("no lane")

    def _actions(self, canvas):
        menu = canvas.build_zoom_menu(self._clip(canvas))
        return [a.text() for a in menu.actions() if a.text()]

    def test_the_pointer_finds_the_block(self, canvas):
        assert canvas._hovered_zoom(self._inside(canvas)) == self._clip(canvas).clip_id

    def test_a_point_outside_the_block_is_not_it(self, canvas):
        from PySide6.QtCore import QPoint

        point = self._inside(canvas)
        assert canvas._hovered_zoom(QPoint(int(canvas.x_of(180)), point.y())) is None

    def test_hovering_lights_it_up(self, canvas):
        canvas._track_zoom_hover(self._inside(canvas))
        assert canvas._zoom_hover == self._clip(canvas).clip_id

    def test_leaving_puts_it_out(self, canvas):
        from PySide6.QtCore import QEvent

        canvas._track_zoom_hover(self._inside(canvas))
        canvas.leaveEvent(QEvent(QEvent.Type.Leave))
        assert canvas._zoom_hover is None

    def test_the_menu_offers_delete(self, canvas):
        assert any("Delete" in text for text in self._actions(canvas))

    def test_delete_removes_the_region_and_nothing_else(self, canvas):
        clip = self._clip(canvas)
        clip.framing = Framing(zoom=1.4)
        menu = canvas.build_zoom_menu(clip)
        [a for a in menu.actions() if "Delete" in a.text()][0].trigger()

        assert self._clip(canvas).zoom is None
        assert self._clip(canvas).framing == Framing(zoom=1.4), (
            "deleting the zoom must not also undo the clip's own framing"
        )

    def test_the_ramp_choices_are_there_and_show_the_current_one(self, canvas):
        clip = self._clip(canvas)
        clip.zoom = replace(clip.zoom, ramp_in=0, ramp_out=0)
        menu = canvas.build_zoom_menu(clip)
        instant = [a for a in menu.actions() if a.text() == "Instant"][0]
        assert instant.isChecked()

    def test_choosing_a_ramp_applies_to_both_ends(self, canvas):
        menu = canvas.build_zoom_menu(self._clip(canvas))
        [a for a in menu.actions() if a.text() == "Instant"][0].trigger()
        region = self._clip(canvas).zoom
        assert region.is_instant

    def test_changing_the_level_keeps_where_and_how_long(self, canvas):
        clip = self._clip(canvas)
        before = (clip.zoom.start, clip.zoom.end, clip.zoom.ramp_in, clip.zoom.ramp_out)
        menu = canvas.build_zoom_menu(clip)
        [a for a in menu.actions() if a.text().startswith("Zoom 2")][0].trigger()

        after = self._clip(canvas).zoom
        assert after.framing.zoom == pytest.approx(2.0)
        assert (after.start, after.end, after.ramp_in, after.ramp_out) == before

    def test_every_menu_action_is_one_undo_step(self, canvas):
        canvas.project.undo.mark_clean()
        menu = canvas.build_zoom_menu(self._clip(canvas))
        [a for a in menu.actions() if "Delete" in a.text()][0].trigger()
        assert self._clip(canvas).zoom is None
        canvas.project.undo.undo()
        assert self._clip(canvas).zoom is not None


class TestTheGripsAndTheCursor:
    """What the block says about itself before you touch it."""

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(1200, 400)
        canvas.px_per_frame = 4.0
        canvas.release_auto_fit()
        clip = Clip(media_id="m", src_in=0, src_out=200, tl_start=0, src_length=400)
        project.timeline.lane_for("video").insert(clip)
        clip.zoom = Region(Framing(zoom=3.0), start=40, end=120, ramp_in=10, ramp_out=10)
        return canvas

    def _point(self, canvas, x, corner):
        from PySide6.QtCore import QPoint

        clip = canvas.timeline.lane_for("video").clips[0]
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                band = canvas.zoom_band(clip, canvas.clip_rect(clip, top, height))
                y = band.top() + band.height() * (0.2 if corner else 0.8)
                return QPoint(int(x), int(y))
        raise AssertionError("no lane")

    def _marks(self, canvas):
        clip = canvas.timeline.lane_for("video").clips[0]
        for track, top, height in canvas.track_rows():
            if clip in track.clips:
                return canvas.zoom_marks(clip, canvas.clip_rect(clip, top, height))
        raise AssertionError("no lane")

    def test_a_ramp_corner_gets_a_diagonal_cursor(self, canvas):
        """Sideways arrows on a sloped handle say the wrong thing about which
        way it moves."""
        start, ramp_in, ramp_out, end = self._marks(canvas)
        canvas._update_cursor(self._point(canvas, ramp_in, corner=True))
        assert canvas.cursor().shape() == Qt.SizeBDiagCursor
        canvas._update_cursor(self._point(canvas, ramp_out, corner=True))
        assert canvas.cursor().shape() == Qt.SizeFDiagCursor

    def test_an_edge_still_gets_the_sideways_one(self, canvas):
        start, _, _, end = self._marks(canvas)
        canvas._update_cursor(self._point(canvas, start, corner=False))
        assert canvas.cursor().shape() == Qt.SizeHorCursor

    def test_it_paints_lit_and_unlit(self, canvas):
        from PySide6.QtGui import QImage

        target = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
        canvas.render(target)
        unlit = target.copy()

        canvas._zoom_hover = canvas.timeline.lane_for("video").clips[0].clip_id
        canvas.update()
        canvas.render(target)
        assert target != unlit, "hovering the block should change how it looks"
