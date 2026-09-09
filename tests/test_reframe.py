"""Reframing: the model, the ops, and the two consumers agreeing.

The framing geometry itself is pinned in `test_framing.py`. This is about
everything that carries it — a cut that must not lose it, a project file that
must round-trip it, and above all the preview and the export describing the same
rectangle. A framing that survives everywhere except the export is worse than no
framing at all, because you only find out after the render.
"""

from __future__ import annotations

import json

import pytest

from fractions import Fraction

from vedit.core.projectfile import project_to_dict
from vedit.core.timebase import TimeBase
from vedit.render.graph import Piece, _framing, _orientation
from vedit.timeline import ops
from vedit.timeline.framing import Framing, window
from vedit.timeline.model import Clip, Timeline, TimelineError
from vedit.tools import Tool

HD = (1920, 1080)
FPS = Fraction(30)


def make_clip(**kwargs) -> Clip:
    base = dict(
        media_id="m1", src_in=0, src_out=60, tl_start=0, src_length=120, kind="video"
    )
    return Clip(**{**base, **kwargs})


@pytest.fixture
def timeline() -> Timeline:
    return Timeline.default(TimeBase(30))


@pytest.fixture
def linked(timeline: Timeline) -> Timeline:
    """A linked A/V pair, the shape most real footage lands as."""
    video = make_clip(link_id="l1", name="shot")
    audio = make_clip(kind="audio", link_id="l1", name="shot")
    timeline.lane_for("video").insert(video)
    timeline.lane_for("audio").insert(audio)
    return timeline


class TestDefaults:
    def test_a_new_clip_is_untouched(self):
        clip = make_clip()
        assert clip.framing.is_identity
        assert clip.rotation == 0 and clip.flipped is False
        assert not clip.has_framing

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"framing": Framing(zoom=1.5)},
            {"rotation": 90},
            {"flipped": True},
        ],
    )
    def test_any_picture_change_shows_in_has_framing(self, kwargs):
        """The menu and the lane badge both gate on this one question."""
        assert make_clip(**kwargs).has_framing

    def test_a_rotation_that_is_not_a_quarter_turn_is_rejected(self):
        with pytest.raises(TimelineError, match="45"):
            make_clip(rotation=45)

    def test_framing_must_be_a_framing(self):
        with pytest.raises(TimelineError, match="must be a Framing"):
            make_clip(framing=1.5)

    def test_a_copy_keeps_the_picture(self):
        """Undo restores through `copy`, so a field it drops is a field that
        silently resets on every undo."""
        clip = make_clip(framing=Framing(zoom=2.0, x=0.5), rotation=270, flipped=True)
        copy = clip.copy()
        assert copy.framing == clip.framing
        assert copy.rotation == 270 and copy.flipped is True


class TestOps:
    def test_reframing_from_the_audio_half_reaches_the_picture(self, linked):
        """A shot on the timeline is usually a linked pair, and grabbing the
        wrong half of it should not silently do nothing."""
        audio = linked.lane_for("audio").clips[0]
        changed = ops.set_framing(linked, [audio], Framing(zoom=2.0))
        assert [clip.kind for clip in changed] == ["video"]
        assert linked.lane_for("video").clips[0].framing.zoom == 2.0

    def test_audio_clips_never_get_a_framing(self, linked):
        ops.set_framing(linked, list(linked.all_clips()), Framing(zoom=2.0))
        assert linked.lane_for("audio").clips[0].framing.is_identity

    def test_a_locked_track_refuses(self, timeline):
        clip = make_clip()
        track = timeline.lane_for("video")
        track.insert(clip)
        track.locked = True
        with pytest.raises(TimelineError, match="locked"):
            ops.set_framing(timeline, [clip], Framing(zoom=2.0))

    def test_rotating_steps_from_where_the_clip_already_is(self, timeline):
        clip = make_clip(rotation=90)
        timeline.lane_for("video").insert(clip)
        ops.rotate_clips(timeline, [clip], 1)
        assert clip.rotation == 180

    def test_rotating_wraps_the_short_way_round(self, timeline):
        clip = make_clip(rotation=0)
        timeline.lane_for("video").insert(clip)
        ops.rotate_clips(timeline, [clip], -1)
        assert clip.rotation == 270

    def test_reset_clears_the_whole_picture_not_just_the_zoom(self, timeline):
        """"Put it back" is one thought; a reset that left the clip upside down
        would be a surprise."""
        clip = make_clip(framing=Framing(zoom=2.0), rotation=90, flipped=True)
        timeline.lane_for("video").insert(clip)
        ops.reset_framing(timeline, [clip])
        assert not clip.has_framing

    def test_a_selection_with_no_picture_in_it_does_nothing(self, timeline):
        audio = make_clip(kind="audio")
        timeline.lane_for("audio").insert(audio)
        assert ops.set_framing(timeline, [audio], Framing(zoom=2.0)) == []


class TestSurvivesOtherEdits:
    def test_a_cut_carries_the_picture_to_both_halves(self, timeline):
        """Razoring is a cut, not a reframe. The right-hand half kept its
        window but used to come back unzoomed and the wrong way up."""
        clip = make_clip(framing=Framing(zoom=2.0, x=0.4), rotation=90, flipped=True)
        timeline.lane_for("video").insert(clip)
        ops.razor(timeline, 30)

        halves = timeline.lane_for("video").clips
        assert len(halves) == 2
        for half in halves:
            assert half.framing == Framing(zoom=2.0, x=0.4)
            assert half.rotation == 90 and half.flipped is True

    def test_a_cut_carries_the_clip_gain_too(self, timeline):
        """Not framing, but the same bug in the same place: a cut must not
        change how loud a clip is."""
        clip = make_clip(kind="audio", gain_db=-6.0)
        timeline.lane_for("audio").insert(clip)
        ops.razor(timeline, 30)
        assert [c.gain_db for c in timeline.lane_for("audio").clips] == [-6.0, -6.0]

    def test_trimming_leaves_the_picture_alone(self, timeline):
        clip = make_clip(framing=Framing(zoom=1.8))
        timeline.lane_for("video").insert(clip)
        ops.trim(timeline, clip, "out", 40)
        assert clip.framing.zoom == 1.8

    def test_a_speed_change_leaves_the_picture_alone(self, timeline):
        clip = make_clip(framing=Framing(zoom=1.8))
        timeline.lane_for("video").insert(clip)
        ops.set_speed(timeline, [clip], 2.0)
        assert clip.framing.zoom == 1.8

    def test_undo_restores_the_previous_framing(self, timeline):
        from vedit.core.commands import UndoStack

        clip = make_clip()
        timeline.lane_for("video").insert(clip)
        undo = UndoStack(timeline)
        undo.apply("Reframe", lambda t: ops.set_framing(t, [t.lane_for("video").clips[0]], Framing(zoom=3.0)))
        assert timeline.lane_for("video").clips[0].framing.zoom == 3.0
        undo.undo()
        assert timeline.lane_for("video").clips[0].framing.is_identity


class TestProjectFile:
    def test_the_picture_is_written_out(self, timeline):
        clip = make_clip(framing=Framing(zoom=2.0, x=-0.25), rotation=180, flipped=True)
        timeline.lane_for("video").insert(clip)
        raw = project_to_dict(timeline, [])
        stored = raw["tracks"][0]["clips"][0]
        assert stored["framing"] == {"zoom": 2.0, "x": -0.25, "y": 0.0}
        assert stored["rotation"] == 180 and stored["flipped"] is True

    def test_it_survives_json(self, timeline):
        """The one that matters — the file is written as text, not as objects."""
        clip = make_clip(framing=Framing(zoom=1.75, x=0.5, y=-0.5))
        timeline.lane_for("video").insert(clip)
        raw = json.loads(json.dumps(project_to_dict(timeline, [])))
        assert raw["tracks"][0]["clips"][0]["framing"]["zoom"] == 1.75


class TestVideoClipAt:
    def test_the_topmost_lane_wins(self, timeline):
        lower = make_clip(name="under")
        upper = make_clip(name="over")
        timeline.video_tracks[0].insert(lower)
        timeline.video_tracks[1].insert(upper)
        assert timeline.video_clip_at(10).name == "over"

    def test_a_disabled_clip_does_not_count(self, timeline):
        clip = make_clip(enabled=False)
        timeline.video_tracks[0].insert(clip)
        assert timeline.video_clip_at(10) is None

    def test_a_muted_lane_does_not_count(self, timeline):
        lower = make_clip(name="under")
        upper = make_clip(name="over")
        timeline.video_tracks[0].insert(lower)
        timeline.video_tracks[1].insert(upper)
        timeline.video_tracks[1].muted = True
        assert timeline.video_clip_at(10).name == "under"

    def test_a_gap_has_no_clip(self, timeline):
        timeline.video_tracks[0].insert(make_clip(tl_start=100))
        assert timeline.video_clip_at(10) is None


class TestRenderClauses:
    """The export side, in isolation. `test_render.py` covers the whole graph."""

    def test_an_untouched_clip_emits_nothing(self):
        """The house contract: a no-op adds no filter, so an unedited timeline
        builds exactly the command it always did."""
        piece = Piece(0, 0.0, 1.0, 1.0)
        assert _framing(piece, *HD) == ""
        assert _orientation(piece) == ""

    def test_a_zoom_crops_then_fills_again(self):
        piece = Piece(0, 0.0, 1.0, 1.0, framing=Framing(zoom=2.0))
        assert _framing(piece, *HD) == "crop=960:540:480:270,scale=1920:1080,"

    def test_panning_to_an_edge_lands_on_zero(self):
        """The offset genuinely is zero there; nudging it to keep the number
        even would leave a sliver of the wrong picture."""
        piece = Piece(0, 0.0, 1.0, 1.0, framing=Framing(zoom=2.0, x=-1.0, y=-1.0))
        assert "crop=960:540:0:0," in _framing(piece, *HD)

    @pytest.mark.parametrize("zoom", [1.01, 1.37, 2.5, 3.333, 8.0])
    def test_every_crop_is_even_and_inside_the_frame(self, zoom):
        """Odd numbers shift the chroma planes against the luma in yuv420p."""
        piece = Piece(0, 0.0, 1.0, 1.0, framing=Framing(zoom=zoom, x=1.0, y=1.0))
        clause = _framing(piece, *HD)
        crop = clause.split("crop=")[1].split(",")[0]
        w, h, x, y = (int(part) for part in crop.split(":"))
        assert w % 2 == 0 and h % 2 == 0 and x % 2 == 0 and y % 2 == 0
        assert x + w <= HD[0] and y + h <= HD[1]

    @pytest.mark.parametrize(
        "rotation, expected",
        [(90, "transpose=1,"), (180, "hflip,vflip,"), (270, "transpose=2,")],
    )
    def test_rotation_uses_the_right_transpose(self, rotation, expected):
        assert _orientation(Piece(0, 0.0, 1.0, 1.0, rotation=rotation)) == expected

    def test_a_flip_comes_after_the_turn(self):
        """So "flipped" mirrors what the viewer shows, not what the file stored."""
        piece = Piece(0, 0.0, 1.0, 1.0, rotation=90, flipped=True)
        assert _orientation(piece) == "transpose=1,hflip,"

    def test_the_crop_matches_the_geometry_module(self):
        """The whole point of the shared module: the export must not do its own
        arithmetic. Within a pixel of rounding, the crop *is* the window."""
        framing = Framing(zoom=1.6, x=0.3, y=-0.7)
        view = window(framing, HD)
        clause = _framing(Piece(0, 0.0, 1.0, 1.0, framing=framing), *HD)
        crop = clause.split("crop=")[1].split(",")[0]
        w, h, x, y = (int(part) for part in crop.split(":"))
        assert abs(w - view.width) < 2 and abs(h - view.height) < 2
        assert abs(x - view.x) < 2 and abs(y - view.y) < 2

    def test_the_preset_resolution_is_used_not_the_timelines(self):
        piece = Piece(0, 0.0, 1.0, 1.0, framing=Framing(zoom=2.0))
        assert _framing(piece, 1280, 720) == "crop=640:360:320:180,scale=1280:720,"


class TestPreviewMatchesRender:
    """The two consumers, checked against each other.

    Everything above proves the pieces work. This proves they agree — which is
    the only property a user can actually feel, because a preview that lies is
    discovered after the export, not before it.
    """

    def surface(self, qt_app, image_size, frame=HD):
        from PySide6.QtGui import QImage
        from vedit.player.surface import VideoSurface

        surface = VideoSurface()
        surface.resize(frame[0] // 2, frame[1] // 2)
        surface.set_frame_size(*frame)
        surface.set_image(QImage(*image_size, QImage.Format_RGB888))
        return surface

    def test_the_surface_fits_the_project_frame_not_the_image(self, qt_app):
        """A 4:3 clip on a 16:9 timeline exports letterboxed, so the viewer has
        to letterbox it too — fitting the image's own aspect would show a
        picture the exported file does not contain."""
        surface = self.surface(qt_app, (640, 480))
        rect = surface.frame_rect()
        assert rect.width() / rect.height() == pytest.approx(16 / 9, abs=0.02)

    @pytest.mark.parametrize(
        "source", [(1920, 1080), (640, 480), (608, 1080), (1920, 816)]
    )
    @pytest.mark.parametrize("zoom", [1.0, 1.5, 3.0])
    def test_both_sides_read_the_same_part_of_the_source(self, qt_app, source, zoom):
        """The crop the export applies, converted back into source pixels, must
        be the rectangle the preview reads out of the decoded image."""
        from vedit.timeline.framing import source_box, visible_source

        framing = Framing(zoom=zoom, x=0.25)
        in_source, _ = visible_source(framing, source, HD)

        clause = _framing(Piece(0, 0.0, 1.0, 1.0, framing=framing), *HD)
        if not clause:
            # Identity: the export crops nothing, so the preview must be
            # reading the whole picture.
            box = source_box(source, HD)
            assert in_source.width == pytest.approx(source[0], abs=1)
            assert box.width > 0
            return

        crop = clause.split("crop=")[1].split(",")[0]
        w, h, x, y = (float(part) for part in crop.split(":"))

        # ffmpeg crops the *fitted* frame; convert that window back into the
        # source's own pixels the way the preview does.
        box = source_box(source, HD)
        per_x, per_y = source[0] / box.width, source[1] / box.height
        expected_x = max(0.0, (x - box.x)) * per_x
        expected_w = min(w, box.right - max(x, box.x)) * per_x

        assert in_source.x == pytest.approx(expected_x, abs=2.0)
        assert in_source.width == pytest.approx(expected_w, abs=2.0)

    def test_a_rotated_source_is_fitted_by_its_turned_shape(self, qt_app):
        """Rotation runs before the scale in the export, so the preview has to
        measure the turned shape too or the two letterbox differently."""
        from vedit.timeline.framing import source_box

        upright = source_box((1920, 1080), HD, rotation=90)
        assert upright.height == pytest.approx(1080)
        assert upright.width < upright.height

    def test_the_viewer_paints_without_falling_over(self, qt_app):
        """Offscreen paint of every picture state — the geometry can be right
        and still produce an invalid QRectF that Qt refuses to draw."""
        from PySide6.QtGui import QImage

        surface = self.surface(qt_app, (608, 1080))
        target = QImage(960, 540, QImage.Format_RGB888)
        for framing, rotation, flipped in (
            (Framing(), 0, False),
            (Framing(zoom=2.0, x=1.0, y=-1.0), 0, False),
            (Framing(zoom=8.0), 90, True),
            (Framing(zoom=1.2), 180, False),
            (Framing(zoom=3.0, x=-1.0), 270, True),
        ):
            surface.set_picture(framing, rotation, flipped)
            surface.render(target)


class TestTheMove:
    """A framing that travels from one value to another across the clip.

    Two values rather than a keyframe list, so the things worth pinning are the
    ends landing exactly, the progress being measured across the *clip* rather
    than whatever fragment is on screen, and a cut dividing the move instead of
    duplicating it.
    """

    def test_a_clip_does_not_move_by_default(self):
        clip = make_clip()
        assert not clip.has_move and not clip.moves
        assert clip.framing_at(0) == clip.framing

    def test_the_ends_land_exactly(self):
        """Not one interpolation step short of the end, which is what measuring
        across `duration` rather than `duration - 1` would give."""
        clip = make_clip(src_out=61, framing_end=Framing(zoom=3.0))
        assert clip.framing_at(clip.tl_start) == Framing()
        assert clip.framing_at(clip.tl_end - 1) == Framing(zoom=3.0)

    def test_the_middle_is_halfway(self):
        clip = make_clip(src_out=61, framing_end=Framing(zoom=3.0))
        assert clip.framing_at(30).zoom == pytest.approx(2.0)

    def test_a_frame_past_the_end_does_not_extrapolate(self):
        """The engine hands back a frame slightly ahead of the playhead while
        parked, and a zoom of nine would be rejected by `Framing` itself."""
        clip = make_clip(src_out=61, framing_end=Framing(zoom=3.0))
        assert clip.framing_at(clip.tl_end + 5).zoom == 3.0

    def test_a_move_that_goes_nowhere_is_not_a_move(self):
        """The editor still shows its controls, but the export must not pay for
        an animated filter to produce a still picture."""
        clip = make_clip(framing=Framing(zoom=2.0), framing_end=Framing(zoom=2.0))
        assert clip.has_move, "the editor sees a move to adjust"
        assert not clip.moves, "the export sees nothing worth animating"

    def test_adding_a_move_is_visible_before_either_end_is_framed(self):
        """Otherwise the controls for setting the end vanish the moment you
        need them."""
        clip = make_clip(framing_end=Framing())
        assert clip.has_move and clip.has_framing

    def test_a_cut_divides_the_move_rather_than_copying_it(self, timeline):
        """Cutting a push in leaves the picture doing exactly what it did — the
        halves meet at the framing that was showing at the cut."""
        clip = make_clip(src_out=41, framing_end=Framing(zoom=3.0))
        timeline.lane_for("video").insert(clip)
        ops.razor(timeline, 20)

        left, right = timeline.lane_for("video").clips
        assert left.framing.zoom == pytest.approx(1.0)
        assert left.framing_end.zoom == pytest.approx(right.framing.zoom)
        assert right.framing_end.zoom == pytest.approx(3.0)

    def test_a_cut_through_a_static_clip_leaves_it_static(self, timeline):
        clip = make_clip(src_out=41, framing=Framing(zoom=2.0))
        timeline.lane_for("video").insert(clip)
        ops.razor(timeline, 20)
        for half in timeline.lane_for("video").clips:
            assert not half.has_move
            assert half.framing.zoom == 2.0

    def test_removing_a_move_leaves_the_clip_where_it_started(self, timeline):
        clip = make_clip(framing=Framing(zoom=2.0), framing_end=Framing(zoom=4.0))
        timeline.lane_for("video").insert(clip)
        ops.set_framing_move(timeline, [clip], None)
        assert not clip.has_move
        assert clip.framing.zoom == 2.0

    def test_reset_clears_the_move_too(self, timeline):
        clip = make_clip(framing=Framing(zoom=2.0), framing_end=Framing(zoom=4.0))
        timeline.lane_for("video").insert(clip)
        ops.reset_framing(timeline, [clip])
        assert not clip.has_framing

    def test_it_survives_a_project_file(self, timeline):
        clip = make_clip(framing_end=Framing(zoom=2.5, x=0.4))
        timeline.lane_for("video").insert(clip)
        stored = project_to_dict(timeline, [])["tracks"][0]["clips"][0]
        assert stored["framing_end"] == {"zoom": 2.5, "x": 0.4, "y": 0.0}

    def test_a_static_clip_writes_no_move_at_all(self, timeline):
        """So a project that uses none of this looks the way it always did."""
        timeline.lane_for("video").insert(make_clip())
        assert "framing_end" not in project_to_dict(timeline, [])["tracks"][0]["clips"][0]


class TestMoveRenderClauses:
    def test_a_static_clip_emits_no_zoompan(self):
        from vedit.render.graph import _move

        piece = Piece(0, 0.0, 1.0, 1.0, framing=Framing(zoom=2.0))
        assert _move(piece, *HD, FPS) == ""

    def test_a_moving_clip_emits_no_static_crop(self):
        """Exactly one of the two handles a piece, or the crop would be applied
        and then animated on top of itself."""
        piece = Piece(
            0, 0.0, 1.0, 1.0,
            framing=Framing(zoom=1.0), framing_end=Framing(zoom=2.0), clip_frames=30,
        )
        assert _framing(piece, *HD) == ""

    def test_the_dangerous_zoompan_defaults_are_all_overridden(self):
        """`d=90` holds each frame for three seconds, `fps=25` retimes the
        segment and breaks concat, `s=hd720` resizes it. None can be left out."""
        from vedit.render.graph import _move

        piece = Piece(
            0, 0.0, 1.0, 1.0,
            framing=Framing(), framing_end=Framing(zoom=2.0), clip_frames=30,
        )
        clause = _move(piece, *HD, FPS)
        assert ":d=1:" in clause
        assert ":s=1920x1080:" in clause
        assert ":fps=30/1," in clause

    def test_the_zoom_is_spelled_out_inside_the_pan(self):
        """Referring to it as `z` inside x or y would mean the *previous*
        frame's zoom — a one-frame lag that reads as a wobble."""
        from vedit.render.graph import _move

        piece = Piece(
            0, 0.0, 1.0, 1.0,
            framing=Framing(), framing_end=Framing(zoom=2.0), clip_frames=30,
        )
        x_expr = _move(piece, *HD, FPS).split("x='")[1].split("'")[0]
        assert "on" in x_expr, "the pan must recompute the zoom itself"

    def test_progress_is_measured_across_the_clip_not_the_piece(self):
        """A lane above cuts this clip into pieces; a move that restarted at
        each of them would stutter."""
        from vedit.render.graph import _move

        piece = Piece(
            0, 0.0, 1.0, 1.0,
            framing=Framing(), framing_end=Framing(zoom=2.0),
            clip_offset=45, clip_frames=91,
        )
        assert "(45+on)/90" in _move(piece, *HD, FPS)


class TestTheOverlayPaints:
    """Every state the overlay can be painted in, painted.

    `QWidget.render()` re-raises what a paint override throws, so this catches
    the class of bug that no amount of geometry testing can: a name that is
    only looked up on a branch nobody's test has ever taken. A missing import
    inside the zoom marquee's shading survived a green suite and a real edit
    session before it was found by hand.
    """

    def overlay(self, qt_app, tool):
        from PySide6.QtGui import QImage
        from vedit.player.reframe import ReframeOverlay
        from vedit.player.surface import VideoSurface

        surface = VideoSurface()
        surface.resize(*[n // 2 for n in HD])
        surface.set_frame_size(*HD)
        surface.set_image(QImage(*HD, QImage.Format_RGB888))
        overlay = ReframeOverlay(surface)
        overlay.setGeometry(surface.rect())
        overlay.set_tool(tool)
        overlay.set_target(Framing())
        return overlay

    def drag(self, overlay, start, end):
        """The real gesture, not a poke at a private attribute."""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent
        from PySide6.QtCore import QEvent

        def press(kind, pos):
            return QMouseEvent(
                kind, QPointF(*pos), QPointF(*pos),
                Qt.LeftButton, Qt.LeftButton, Qt.NoModifier,
            )

        overlay.mousePressEvent(press(QEvent.MouseButtonPress, start))
        overlay.mouseMoveEvent(press(QEvent.MouseMove, end))

    def target(self):
        from PySide6.QtGui import QImage

        return QImage(HD[0] // 2, HD[1] // 2, QImage.Format_RGB888)

    @pytest.mark.parametrize("tool", [Tool.REFRAME, Tool.ZOOM])
    def test_it_paints_at_rest(self, qt_app, tool):
        self.overlay(qt_app, tool).render(self.target())

    def test_it_paints_the_zoom_marquee_mid_drag(self, qt_app):
        """The branch that shipped broken: dimming outside the box."""
        overlay = self.overlay(qt_app, Tool.ZOOM)
        self.drag(overlay, (120, 90), (480, 360))
        assert overlay._marquee is not None, "the drag did not start a marquee"
        overlay.render(self.target())

    def test_it_paints_a_marquee_dragged_backwards(self, qt_app):
        """Up and to the left, where the rect needs normalising first."""
        overlay = self.overlay(qt_app, Tool.ZOOM)
        self.drag(overlay, (480, 360), (120, 90))
        overlay.render(self.target())

    def test_it_paints_mid_reframe(self, qt_app):
        overlay = self.overlay(qt_app, Tool.REFRAME)
        self.drag(overlay, (400, 300), (460, 340))
        overlay.render(self.target())

    def test_it_paints_over_a_gap(self, qt_app):
        """Parked where there is no picture: armed, but nothing to grab."""
        overlay = self.overlay(qt_app, Tool.REFRAME)
        overlay.set_target(None)
        overlay.render(self.target())

    def test_it_paints_a_title_it_can_drag(self, qt_app):
        from PySide6.QtCore import QRect

        overlay = self.overlay(qt_app, Tool.POINTER)
        overlay.set_titles([(make_clip(kind="title"), QRect(200, 300, 400, 60))])
        overlay.render(self.target())
