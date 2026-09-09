"""Tool icons, drawn rather than shipped.

Hand-drawn `QPainterPath` work for the same reason the marks on the clips are:
an icon font or an emoji renders differently on every machine, and at eighteen
pixels the difference between legible and mush is a pixel or two. Drawing them
means they are the same everywhere, they take their colour from the theme, and
there are no image files to lose.

Each icon comes back as a `QIcon` carrying two pixmaps — dim for off, accent for
on — so a checkable button shows its state without a stylesheet rule per button.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

from vedit import theme

SIZE = 20          # the canvas each icon is drawn on
INSET = 2.0        # breathing room inside it


def _canvas(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    return pixmap


def _pen(painter: QPainter, colour: QColor, width: float = 1.6) -> QPen:
    pen = QPen(colour)
    pen.setWidthF(width)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    return pen


# -- the drawings --------------------------------------------------------------


def _draw_pointer(painter: QPainter, colour: QColor, size: int) -> None:
    """The ordinary arrow: select, move, trim — the tool you are in by default."""
    arrow = QPainterPath()
    arrow.moveTo(size * 0.30, size * 0.14)
    arrow.lineTo(size * 0.30, size * 0.74)
    arrow.lineTo(size * 0.44, size * 0.60)
    arrow.lineTo(size * 0.54, size * 0.84)
    arrow.lineTo(size * 0.66, size * 0.78)
    arrow.lineTo(size * 0.56, size * 0.55)
    arrow.lineTo(size * 0.74, size * 0.53)
    arrow.closeSubpath()
    painter.setPen(Qt.NoPen)
    painter.fillPath(arrow, QBrush(colour))


def _draw_zoom(painter: QPainter, colour: QColor, size: int) -> None:
    """A magnifier over a marquee: drag a box round what should fill the frame."""
    _pen(painter, colour, 1.3)
    dashed = QPen(colour)
    dashed.setWidthF(1.0)
    dashed.setStyle(Qt.DotLine)
    painter.setPen(dashed)
    painter.drawRect(QRectF(INSET, INSET, size - INSET * 2, size - INSET * 2))

    _pen(painter, colour, 1.7)
    centre = QPointF(size * 0.44, size * 0.44)
    radius = size * 0.20
    painter.drawEllipse(centre, radius, radius)
    painter.drawLine(
        QPointF(centre.x() + radius * 0.72, centre.y() + radius * 0.72),
        QPointF(size * 0.80, size * 0.80),
    )
    # The plus inside, so it reads as zoom *in* rather than "search".
    painter.drawLine(
        QPointF(centre.x() - radius * 0.45, centre.y()),
        QPointF(centre.x() + radius * 0.45, centre.y()),
    )
    painter.drawLine(
        QPointF(centre.x(), centre.y() - radius * 0.45),
        QPointF(centre.x(), centre.y() + radius * 0.45),
    )


def _draw_snap(painter: QPainter, colour: QColor, size: int) -> None:
    """Two arrows closing on a line: things meeting exactly."""
    middle = size / 2
    _pen(painter, colour)
    painter.drawLine(QPointF(middle, INSET), QPointF(middle, size - INSET))
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(colour))
    for direction in (-1, 1):
        tip = middle + direction * 1.5
        base = middle + direction * 6.0
        arrow = QPainterPath()
        arrow.moveTo(tip, middle)
        arrow.lineTo(base, middle - 3.5)
        arrow.lineTo(base, middle + 3.5)
        arrow.closeSubpath()
        painter.fillPath(arrow, QBrush(colour))


def _draw_thumbs(painter: QPainter, colour: QColor, size: int) -> None:
    """A strip of frames."""
    _pen(painter, colour, 1.3)
    top, height = size * 0.28, size * 0.44
    width = (size - INSET * 2) / 3
    for index in range(3):
        painter.drawRect(QRectF(INSET + index * width, top, width - 1.0, height))


def _draw_reframe(painter: QPainter, colour: QColor, size: int) -> None:
    """Corner brackets — crop marks, which is what reframing is."""
    _pen(painter, colour, 1.7)
    reach = size * 0.26
    left, top = INSET, INSET
    right, bottom = size - INSET, size - INSET
    for x, y, dx, dy in (
        (left, top, 1, 1), (right, top, -1, 1),
        (left, bottom, 1, -1), (right, bottom, -1, -1),
    ):
        painter.drawLine(QPointF(x, y), QPointF(x + dx * reach, y))
        painter.drawLine(QPointF(x, y), QPointF(x, y + dy * reach))
    painter.setBrush(QBrush(colour))
    painter.setPen(Qt.NoPen)
    painter.drawRect(QRectF(size * 0.40, size * 0.40, size * 0.20, size * 0.20))


def _draw_title(painter: QPainter, colour: QColor, size: int) -> None:
    """A capital T sitting on a baseline."""
    _pen(painter, colour, 1.9)
    top = size * 0.26
    painter.drawLine(QPointF(size * 0.26, top), QPointF(size * 0.74, top))
    painter.drawLine(QPointF(size / 2, top), QPointF(size / 2, size * 0.68))
    pen = QPen(colour)
    pen.setWidthF(1.2)
    painter.setPen(pen)
    painter.drawLine(QPointF(INSET, size - INSET - 1), QPointF(size - INSET, size - INSET - 1))


def _draw_dissolve(painter: QPainter, colour: QColor, size: int) -> None:
    """One frame handing over to the next, split on the diagonal.

    An earlier version drew two overlapping rectangles with a ramp between
    them. It was legible at four times this size and mud at this one; a single
    box divided corner to corner survives being small, which is the only test
    that matters for a toolbar.
    """
    box = QRectF(INSET, size * 0.24, size - INSET * 2, size * 0.52)

    filled = QPainterPath()
    filled.moveTo(box.left(), box.top())
    filled.lineTo(box.left(), box.bottom())
    filled.lineTo(box.right(), box.bottom())
    filled.closeSubpath()
    painter.setPen(Qt.NoPen)
    painter.fillPath(filled, QBrush(colour))

    _pen(painter, colour, 1.4)
    painter.drawRect(box)


def _draw_cut(painter: QPainter, colour: QColor, size: int) -> None:
    """Scissors rather than the razor blade an NLE would use.

    Every editor in the world draws a razor here and nobody outside that world
    recognises it. Scissors mean "cut" to anyone.
    """
    ring = size * 0.115
    left_handle = QPointF(size * 0.30, size - INSET - ring)
    right_handle = QPointF(size * 0.70, size - INSET - ring)
    _pen(painter, colour, 1.5)
    painter.drawLine(left_handle, QPointF(size * 0.66, INSET + 1.0))
    painter.drawLine(right_handle, QPointF(size * 0.34, INSET + 1.0))
    painter.drawEllipse(left_handle, ring, ring)
    painter.drawEllipse(right_handle, ring, ring)


def _draw_fit(painter: QPainter, colour: QColor, size: int) -> None:
    """Arrows pushing out to two walls: fill the width."""
    middle = size / 2
    _pen(painter, colour, 1.5)
    painter.drawLine(QPointF(INSET, size * 0.24), QPointF(INSET, size * 0.76))
    painter.drawLine(
        QPointF(size - INSET, size * 0.24), QPointF(size - INSET, size * 0.76)
    )
    painter.drawLine(QPointF(size * 0.28, middle), QPointF(size * 0.72, middle))
    painter.setPen(Qt.NoPen)
    for direction, edge in ((-1, size * 0.28), (1, size * 0.72)):
        arrow = QPainterPath()
        arrow.moveTo(edge + direction * 4.0, middle)
        arrow.lineTo(edge, middle - 3.0)
        arrow.lineTo(edge, middle + 3.0)
        arrow.closeSubpath()
        painter.fillPath(arrow, QBrush(colour))


DRAWINGS = {
    "pointer": _draw_pointer,
    "cut": _draw_cut,
    "zoom": _draw_zoom,
    "snap": _draw_snap,
    "thumbs": _draw_thumbs,
    "reframe": _draw_reframe,
    "title": _draw_title,
    "dissolve": _draw_dissolve,
    "fit": _draw_fit,
}


# -- building the icons --------------------------------------------------------


def pixmap(name: str, colour: QColor, size: int = SIZE) -> QPixmap:
    canvas = _canvas(size)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing, True)
    DRAWINGS[name](painter, colour, size)
    painter.end()
    return canvas


# How much ink a pixel needs before a cursor keeps it at full strength. Low,
# because the point is to rescue the antialiased edge rather than to trim it.
_CURSOR_INK = 60


def cursor(name: str, size: int = 30) -> QPixmap:
    """One of these drawings as a mouse cursor.

    Not the same thing as the toolbar pixmap, for two reasons.

    A cursor may be reduced to a **one-bit mask** by the platform — X11 without
    ARGB cursors, and anything going through XWayland. These glyphs are thin
    antialiased strokes, only about a fifth of which is fully opaque, so
    thresholding that alpha keeps a scattered handful of pixels and throws the
    rest away: the scissors arrived on screen as a spray of white dots. Forcing
    every inked pixel to full opacity means the shape survives the reduction.

    And a cursor has no background it can rely on. It travels over dark lanes,
    pale waveforms and bright thumbnails alike, so it is drawn white with a dark
    outline — which is what every system cursor does, for the same reason.
    """
    glyph = pixmap(name, QColor(255, 255, 255), size).toImage()
    glyph = glyph.convertToFormat(QImage.Format_ARGB32)

    # Harden: an inked pixel becomes fully opaque, everything else vanishes.
    for y in range(glyph.height()):
        for x in range(glyph.width()):
            alpha = glyph.pixelColor(x, y).alpha()
            glyph.setPixelColor(
                x, y,
                QColor(255, 255, 255, 255) if alpha >= _CURSOR_INK
                else QColor(0, 0, 0, 0),
            )

    # The outline is the hardened shape stamped around itself in black, which
    # follows the drawing exactly however it is shaped.
    out = QPixmap(size + 2, size + 2)
    out.fill(Qt.transparent)
    painter = QPainter(out)
    shadow = QImage(glyph)
    for y in range(shadow.height()):
        for x in range(shadow.width()):
            if shadow.pixelColor(x, y).alpha():
                shadow.setPixelColor(x, y, QColor(0, 0, 0, 255))
    for dx in (0, 1, 2):
        for dy in (0, 1, 2):
            painter.drawImage(dx, dy, shadow)
    painter.drawImage(1, 1, glyph)
    painter.end()
    return out


def icon(name: str, size: int = SIZE) -> QIcon:
    """A two-state icon: dim when off, accent when on.

    Carrying both states on the icon rather than restyling the button means a
    checkable tool shows whether it is armed with no extra wiring, and an
    ordinary button simply never uses the second one.
    """
    built = QIcon()
    built.addPixmap(pixmap(name, theme.TEXT_DIM, size), QIcon.Normal, QIcon.Off)
    built.addPixmap(pixmap(name, theme.TEXT, size), QIcon.Active, QIcon.Off)
    built.addPixmap(pixmap(name, theme.ACCENT, size), QIcon.Normal, QIcon.On)
    built.addPixmap(pixmap(name, theme.ACCENT, size), QIcon.Active, QIcon.On)
    built.addPixmap(pixmap(name, theme.TEXT_FAINT, size), QIcon.Disabled, QIcon.Off)
    return built
