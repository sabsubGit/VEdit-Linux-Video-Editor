"""The framing geometry.

This module is the reason the preview and the export can be trusted to show the
same picture — both read their numbers from it and neither does any geometry of
its own. So it is worth testing far past what the feature obviously needs: every
bug here becomes a discrepancy nobody notices until an export comes back wrong.
"""

from __future__ import annotations

import pytest

from vedit.timeline import framing as F
from vedit.timeline.framing import Framing, FramingError, Rect

HD = (1920, 1080)


class TestValidation:
    def test_the_default_is_identity(self):
        assert Framing().is_identity

    def test_a_zoom_of_one_with_pan_is_still_identity_in_effect(self):
        """There is no slack at 1.0, so a pan cannot move anything."""
        assert F.window(Framing(zoom=1.0, x=1.0), HD) == F.window(Framing(), HD)

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"zoom": 0.5}, "outside"),
            ({"zoom": 0.0}, "outside"),
            ({"zoom": 99.0}, "outside"),
            ({"x": 1.5}, "pan x"),
            ({"y": -2.0}, "pan y"),
        ],
    )
    def test_out_of_range_values_are_rejected(self, kwargs, message):
        with pytest.raises(FramingError, match=message):
            Framing(**kwargs)

    def test_a_drag_clamps_instead_of_raising(self):
        """`nudged` is what the mouse calls; running past the edge must stop at
        the edge, not raise at the user mid-gesture."""
        framing = Framing(zoom=2.0, x=0.9)
        assert framing.nudged(dx=5.0).x == 1.0
        assert framing.nudged(zoom=100.0).zoom == F.MAX_ZOOM
        assert framing.nudged(zoom=0.01).zoom == F.MIN_ZOOM


class TestWindow:
    def test_identity_selects_the_whole_frame(self):
        assert F.window(Framing(), HD) == Rect(0.0, 0.0, 1920.0, 1080.0)

    def test_zoom_halves_the_window_and_stays_centred(self):
        assert F.window(Framing(zoom=2.0), HD) == Rect(480.0, 270.0, 960.0, 540.0)

    @pytest.mark.parametrize("zoom", [1.0, 1.3, 2.0, 4.0, 8.0])
    @pytest.mark.parametrize("x", [-1.0, -0.4, 0.0, 0.7, 1.0])
    @pytest.mark.parametrize("y", [-1.0, 0.0, 1.0])
    def test_the_window_never_escapes_the_frame(self, zoom, x, y):
        """The property the whole pan-as-slack design exists to guarantee: no
        combination of legal values can describe a window hanging off an edge,
        so nothing downstream ever has to clamp."""
        view = F.window(Framing(zoom=zoom, x=x, y=y), HD)
        assert view.x >= -1e-9
        assert view.y >= -1e-9
        assert view.right <= HD[0] + 1e-9
        assert view.bottom <= HD[1] + 1e-9

    def test_full_pan_reaches_exactly_the_edge(self):
        assert F.window(Framing(zoom=2.0, x=1.0), HD).right == pytest.approx(1920.0)
        assert F.window(Framing(zoom=2.0, x=-1.0), HD).x == pytest.approx(0.0)

    def test_zoom_does_not_invalidate_an_existing_pan(self):
        """Changing zoom re-scales the slack, so a pan set at one zoom stays
        meaningful at another. This is why there is no clamp method."""
        for zoom in (1.5, 3.0, 8.0):
            view = F.window(Framing(zoom=zoom, x=1.0, y=1.0), HD)
            assert view.right == pytest.approx(1920.0)
            assert view.bottom == pytest.approx(1080.0)


class TestSourceBox:
    def test_a_matching_aspect_fills_the_frame(self):
        assert F.source_box((1280, 720), HD) == Rect(0.0, 0.0, 1920.0, 1080.0)

    def test_a_four_by_three_source_gets_bars_at_the_sides(self):
        box = F.source_box((640, 480), HD)
        assert box.height == 1080.0
        assert box.width == pytest.approx(1440.0)
        assert box.x == pytest.approx(240.0)

    def test_a_vertical_source_gets_bars_at_the_sides_too(self):
        box = F.source_box((608, 1080), HD)
        assert box.height == 1080.0
        assert box.width == pytest.approx(608.0)

    def test_a_quarter_turn_swaps_the_aspect(self):
        upright = F.source_box((1920, 1080), HD, rotation=90)
        assert upright.width < upright.height
        assert upright.height == 1080.0

    def test_a_degenerate_source_does_not_divide_by_zero(self):
        assert F.source_box((0, 0), HD) == Rect(0.0, 0.0, 1920.0, 1080.0)


class TestFillZoom:
    @pytest.mark.parametrize("source", [(640, 480), (608, 1080), (1920, 816)])
    def test_filling_leaves_no_bars(self, source):
        """The one-click fix for phone footage: after this zoom the picture
        covers the frame in both directions."""
        zoom = F.fill_zoom(source, HD)
        box = F.source_box(source, HD)
        view = F.window(Framing(zoom=zoom), HD)
        # Containment rather than equality: the two rects are computed by
        # different routes and agree only to floating-point precision.
        assert box.x <= view.x + 1e-6 and box.y <= view.y + 1e-6
        assert box.right >= view.right - 1e-6 and box.bottom >= view.bottom - 1e-6

    def test_a_matching_aspect_needs_no_zoom(self):
        assert F.fill_zoom((1280, 720), HD) == pytest.approx(1.0)

    def test_an_extreme_aspect_asks_for_what_it_can_have(self):
        """A menu action must not raise; it takes the most zoom that is legal."""
        assert F.fill_zoom((16, 4000), HD) == F.MAX_ZOOM


class TestVisibleSource:
    def test_identity_shows_the_whole_source_across_the_whole_frame(self):
        in_source, in_frame = F.visible_source(Framing(), (1920, 1080), HD)
        assert in_source == Rect(0.0, 0.0, 1920.0, 1080.0)
        assert in_frame == Rect(0.0, 0.0, 1920.0, 1080.0)

    def test_zooming_reads_less_of_the_source_but_still_fills_the_frame(self):
        in_source, in_frame = F.visible_source(Framing(zoom=2.0), (1920, 1080), HD)
        assert in_source == Rect(480.0, 270.0, 960.0, 540.0)
        assert in_frame == Rect(0.0, 0.0, 1920.0, 1080.0)

    def test_a_letterboxed_source_only_offers_its_own_pixels(self):
        """At identity a 4:3 source occupies the middle of the frame, and the
        bars either side are not something the source can supply."""
        in_source, in_frame = F.visible_source(Framing(), (640, 480), HD)
        assert in_source == Rect(0.0, 0.0, 640.0, 480.0)
        assert in_frame.x == pytest.approx(240.0)
        assert in_frame.width == pytest.approx(1440.0)

    def test_panning_into_the_bars_shows_only_what_exists(self):
        """Panning a vertical clip sideways runs out of picture before it runs
        out of frame; the caller gets a smaller target and paints background
        around it rather than stretching to fill."""
        framing = Framing(zoom=1.5, x=-1.0)
        in_source, in_frame = F.visible_source(framing, (608, 1080), HD)
        assert not in_source.is_empty
        assert in_frame.width < 1920.0

    def test_a_window_entirely_in_the_bars_shows_nothing(self):
        framing = Framing(zoom=8.0, x=-1.0)
        in_source, in_frame = F.visible_source(framing, (200, 1080), HD)
        assert in_source.is_empty and in_frame.is_empty

    def test_source_pixels_are_measured_in_source_space(self):
        """The decoded image is at the source's own size, so the first rect has
        to be in its pixels — not the project's."""
        in_source, _ = F.visible_source(Framing(zoom=2.0), (640, 480), HD)
        assert in_source.right <= 640.0
        assert in_source.bottom <= 480.0

    def test_a_rotated_source_comes_back_in_the_stored_pixels(self):
        """Framing is measured against the turned picture, but the decoder
        hands back the image the file holds — so the rect must come back in
        *its* coordinates, not the turned ones. Getting this backwards reads a
        quarter-turned clip with its width and height swapped, which looks
        plausible on a centred shot and is wrong everywhere else."""
        in_source, _ = F.visible_source(Framing(), (1920, 1080), HD, rotation=90)
        assert in_source.width == pytest.approx(1920.0)
        assert in_source.height == pytest.approx(1080.0)


class TestUnorient:
    """Mapping a rectangle on the turned picture back onto the stored pixels."""

    def test_no_turn_changes_nothing(self):
        rect = Rect(10.0, 20.0, 30.0, 40.0)
        assert F.unorient(rect, (1920, 1080)) == rect

    def test_a_quarter_turn_swaps_the_sides(self):
        turned = F.unorient(Rect(0.0, 0.0, 100.0, 200.0), (1920, 1080), rotation=90)
        assert (turned.width, turned.height) == (200.0, 100.0)

    @pytest.mark.parametrize("rotation", [0, 90, 180, 270])
    @pytest.mark.parametrize("flipped", [False, True])
    def test_the_result_always_lands_inside_the_image(self, rotation, flipped):
        source = (1920, 1080)
        turned_w, turned_h = F.rotated_size(*source, rotation)
        rect = Rect(turned_w * 0.1, turned_h * 0.2, turned_w * 0.5, turned_h * 0.6)
        mapped = F.unorient(rect, source, rotation, flipped)
        assert mapped.x >= -1e-9 and mapped.y >= -1e-9
        assert mapped.right <= source[0] + 1e-9
        assert mapped.bottom <= source[1] + 1e-9

    def test_mirroring_reflects_across_the_middle(self):
        """A window hard against the left edge of a mirrored picture is reading
        the right edge of the file."""
        mapped = F.unorient(Rect(0.0, 0.0, 400.0, 1080.0), (1920, 1080), flipped=True)
        assert mapped.x == pytest.approx(1520.0)


class TestInterpolate:
    def test_the_ends_are_exact(self):
        start, end = Framing(zoom=1.0), Framing(zoom=2.0, x=0.5)
        assert F.interpolate(start, end, 0.0) == start
        assert F.interpolate(start, end, 1.0) == end

    def test_the_middle_is_halfway(self):
        middle = F.interpolate(Framing(zoom=1.0), Framing(zoom=3.0, x=1.0), 0.5)
        assert middle.zoom == pytest.approx(2.0)
        assert middle.x == pytest.approx(0.5)

    @pytest.mark.parametrize("progress", [-5.0, -0.1, 1.1, 99.0])
    def test_progress_outside_the_clip_clamps_rather_than_extrapolating(self, progress):
        """A frame one past the end must give the end framing, not a zoom of
        nine that `Framing` would then reject."""
        result = F.interpolate(Framing(), Framing(zoom=2.0), progress)
        assert F.MIN_ZOOM <= result.zoom <= F.MAX_ZOOM


class TestPanForDelta:
    def test_dragging_right_reveals_what_was_off_to_the_left(self):
        """Dragging moves the picture, so the window moves the other way."""
        panned = F.pan_for_delta(Framing(zoom=2.0), 100.0, 0.0, HD)
        assert panned.x < 0.0

    def test_a_drag_at_identity_does_nothing(self):
        """No slack, nothing to pan, and no division by zero."""
        assert F.pan_for_delta(Framing(), 500.0, 500.0, HD) == Framing()

    def test_dragging_the_full_slack_reaches_the_edge(self):
        slack = (1920 - 960) / 2
        panned = F.pan_for_delta(Framing(zoom=2.0), -slack, 0.0, HD)
        assert panned.x == pytest.approx(1.0)


class TestRect:
    def test_disjoint_rectangles_intersect_to_nothing(self):
        assert Rect(0, 0, 10, 10).intersected(Rect(50, 50, 10, 10)).is_empty

    def test_touching_edges_do_not_count_as_overlap(self):
        assert Rect(0, 0, 10, 10).intersected(Rect(10, 0, 10, 10)).is_empty


class TestDescribe:
    def test_an_untouched_clip_says_so(self):
        assert F.describe(Framing()) == "none"

    def test_a_whole_number_zoom_loses_its_decimals(self):
        assert F.describe(Framing(zoom=2.0)) == "2×"

    def test_rotation_and_flip_are_listed_with_the_zoom(self):
        assert F.describe(Framing(zoom=1.5), rotation=90, flipped=True) == (
            "1.50× · 90° · flipped"
        )


class TestFramingForBox:
    """The zoom tool's gesture: drag a box, get the framing that shows it."""

    def test_a_box_the_size_of_the_frame_is_identity(self):
        assert F.framing_for_box(Rect(0, 0, 1920, 1080), HD) == Framing()

    def test_the_centre_quarter_doubles_the_zoom(self):
        framing = F.framing_for_box(Rect(480, 270, 960, 540), HD)
        assert framing.zoom == pytest.approx(2.0)
        assert framing.x == pytest.approx(0.0)
        assert framing.y == pytest.approx(0.0)

    def test_a_corner_box_pans_to_that_corner(self):
        framing = F.framing_for_box(Rect(0, 0, 960, 540), HD)
        assert framing.x == pytest.approx(-1.0)
        assert framing.y == pytest.approx(-1.0)
        assert F.window(framing, HD) == Rect(0.0, 0.0, 960.0, 540.0)

    def test_the_box_always_ends_up_fully_visible(self):
        """The zoom takes the smaller of the two fits, so a marquee of the wrong
        shape gives you more than you asked for rather than cropping it."""
        box = Rect(600, 200, 300, 700)     # far taller than 16:9
        view = F.window(F.framing_for_box(box, HD), HD)
        assert view.x <= box.x + 1e-6 and view.y <= box.y + 1e-6
        assert view.right >= box.right - 1e-6
        assert view.bottom >= box.bottom - 1e-6

    def test_a_tiny_box_is_capped_at_the_maximum_zoom(self):
        assert F.framing_for_box(Rect(950, 530, 20, 20), HD).zoom == F.MAX_ZOOM

    def test_a_box_with_no_area_gives_up_rather_than_dividing_by_zero(self):
        assert F.framing_for_box(Rect(100, 100, 0, 0), HD) == Framing()

    @pytest.mark.parametrize(
        "box",
        [
            Rect(0, 0, 100, 100), Rect(1820, 980, 100, 100),
            Rect(-50, -50, 400, 400), Rect(1700, 900, 400, 400),
        ],
    )
    def test_the_result_is_always_a_legal_framing(self, box):
        """Including boxes dragged off the edge of the picture, which is what a
        marquee started outside the frame produces."""
        framing = F.framing_for_box(box, HD)
        assert F.MIN_ZOOM <= framing.zoom <= F.MAX_ZOOM
        assert -1.0 <= framing.x <= 1.0 and -1.0 <= framing.y <= 1.0
