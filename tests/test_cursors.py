"""Drawings used as mouse cursors, which is not the same as using them as icons.

A cursor may be reduced to a one-bit mask by the platform — X11 without ARGB
cursors, and anything reaching the screen through XWayland. These glyphs are
thin antialiased strokes: measured, only about a fifth of the scissors is fully
opaque. Threshold that and you keep a scattered handful of pixels and lose the
rest, which is exactly how it looked — "a shotgun blast of white dots".

So a cursor pixmap has one hard requirement, pinned below: every pixel is
either fully opaque or fully transparent, so there is nothing for a threshold
to disagree about.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from vedit import icons
from vedit.tools import TOOLS


def alpha_histogram(pixmap):
    image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
    solid = clear = partial = 0
    for y in range(image.height()):
        for x in range(image.width()):
            alpha = image.pixelColor(x, y).alpha()
            if alpha >= 255:
                solid += 1
            elif alpha == 0:
                clear += 1
            else:
                partial += 1
    return solid, partial, clear


class TestACursorSurvivesAOneBitMask:
    def test_the_scissors_has_no_half_transparent_pixels(self, qt_app):
        solid, partial, _ = alpha_histogram(icons.cursor("cut"))
        assert solid > 0, "there has to be a cursor there at all"
        assert partial == 0, (
            f"{partial} antialiased pixels would be thresholded away by a "
            "platform that only takes a one-bit cursor mask"
        )

    @pytest.mark.parametrize("name", [spec.icon for spec in TOOLS])
    def test_every_tool_drawing_could_be_used_as_one(self, qt_app, name):
        """None of them are cursors today except the scissors, but the next one
        that becomes one should not have to rediscover this."""
        solid, partial, _ = alpha_histogram(icons.cursor(name))
        assert solid > 0
        assert partial == 0

    def test_it_keeps_far_more_than_the_plain_icon_would(self, qt_app):
        """The measurement the fix came from, kept as the reason for it."""
        def survives(pixmap):
            image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
            return sum(
                1
                for y in range(image.height())
                for x in range(image.width())
                if image.pixelColor(x, y).alpha() >= 128
            )

        from vedit import theme

        plain = survives(icons.pixmap("cut", theme.TEXT, 26))
        made_for_it = survives(icons.cursor("cut"))
        assert made_for_it > plain * 2


class TestItIsLegibleAnywhere:
    """A cursor has no background it can count on."""

    def test_it_is_white_with_a_dark_outline(self, qt_app):
        image = icons.cursor("cut").toImage().convertToFormat(QImage.Format_ARGB32)
        whites = darks = 0
        for y in range(image.height()):
            for x in range(image.width()):
                colour = image.pixelColor(x, y)
                if not colour.alpha():
                    continue
                if colour.red() > 200:
                    whites += 1
                elif colour.red() < 60:
                    darks += 1
        assert whites > 0, "the body of the cursor"
        assert darks > 0, "and an outline, or it vanishes on a pale waveform"

    def test_the_outline_encloses_the_body(self, qt_app):
        """Every white pixel has ink all round it — no white touching the edge
        of the shape, which is where it would bleed into a light background."""
        image = icons.cursor("cut").toImage().convertToFormat(QImage.Format_ARGB32)
        for y in range(1, image.height() - 1):
            for x in range(1, image.width() - 1):
                if image.pixelColor(x, y).red() <= 200:
                    continue
                if not image.pixelColor(x, y).alpha():
                    continue
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    assert image.pixelColor(x + dx, y + dy).alpha() > 0, (
                        f"white at ({x}, {y}) has a bare edge"
                    )


class TestTheCanvasDrawsItItself:
    """The scissors are painted by the timeline under a hidden pointer.

    Handing the system a cursor image looked right in every screenshot and
    arrived on a real screen (Hyprland on NVIDIA) garbled: the compositor draws
    client cursor images on a hardware plane that no capture includes. What the
    widget paints arrives as the widget does.
    """

    def _canvas(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        canvas = TimelineCanvas(Project())
        canvas.resize(900, 300)
        return canvas

    def test_holding_the_cut_tool_hides_the_system_pointer(self, qt_app, monkeypatch, tmp_path):
        from PySide6.QtCore import Qt

        from vedit.tools import Tool

        canvas = self._canvas(monkeypatch, tmp_path)
        canvas.set_tool(Tool.CUT)
        assert canvas.cursor().shape() == Qt.BlankCursor
        assert canvas.cursor().shape() != Qt.BitmapCursor, "no cursor image is handed over"

    def test_the_scissors_are_painted_where_the_mouse_is(self, qt_app, monkeypatch, tmp_path):
        from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        from vedit.tools import Tool

        canvas = self._canvas(monkeypatch, tmp_path)
        canvas.set_tool(Tool.CUT)
        without = canvas.grab().toImage()

        at = QPoint(500, 150)
        qt_app.sendEvent(
            canvas,
            QMouseEvent(
                QEvent.MouseMove, QPointF(at), QPointF(canvas.mapToGlobal(at)),
                Qt.NoButton, Qt.NoButton, Qt.NoModifier,
            ),
        )
        shown = canvas.grab().toImage()

        def white(image):
            return {
                (x, y)
                for x in range(at.x() - 20, at.x() + 20)
                for y in range(at.y() - 20, at.y() + 20)
                if image.pixelColor(x, y).name() == "#ffffff"
            }

        drawn = white(shown) - white(without)
        assert len(drawn) > 60, "the scissors should be there"
        xs = [x for x, _ in drawn]
        assert abs((min(xs) + max(xs)) / 2 - at.x()) <= 1.5, "and centred on the mouse"

        # Leaving the timeline takes them away with the pointer.
        qt_app.sendEvent(canvas, QEvent(QEvent.Leave))
        assert not (white(canvas.grab().toImage()) - white(without))

    def test_putting_the_tool_down_restores_the_arrow(self, qt_app, monkeypatch, tmp_path):
        from PySide6.QtCore import Qt

        from vedit.tools import Tool

        canvas = self._canvas(monkeypatch, tmp_path)
        canvas.set_tool(Tool.CUT)
        canvas.set_tool(Tool.POINTER)
        assert canvas.cursor().shape() not in (Qt.BlankCursor, Qt.BitmapCursor)
