"""Pool thumbnails, normalised to one shape.

Sources arrive in every aspect ratio a camera can produce, and a cached
thumbnail may predate whatever the current generator writes, so the image on
disk is never a size the UI can count on. Nothing displays one directly:
everything goes through `render`, which composites onto a fixed cell. A 4:3
clip, a phone video shot upright and a music bed then occupy exactly the same
rectangle, which is what lets the pool read as a column rather than a ragged
edge.

That also covers the two states where there is no frame to show at all. Both
get a drawn cell of the same size rather than nothing, so the Name column never
shifts sideways when an ingest finishes.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPen, QPixmap

from vedit import theme

# Matches the table's icon size; a row is 58 tall, so 54 leaves a hair of gap.
POOL_CELL = QSize(96, 54)

# A fixed pattern rather than anything derived from the file: the placeholder
# stands for "audio", not for this particular audio, and a shape that changed
# per row would read as information that is not there.
_AUDIO_BARS = (0.30, 0.62, 0.44, 0.92, 0.70, 1.00, 0.55, 0.78, 0.38, 0.58, 0.26)


def _blank(size: QSize) -> QPixmap:
    canvas = QPixmap(size)
    canvas.fill(theme.BG_DARKEST)
    return canvas


def letterbox(pixmap: QPixmap, size: QSize) -> QPixmap:
    """Centre `pixmap` on a cell of exactly `size`, keeping its aspect ratio."""
    canvas = _blank(size)
    if pixmap.isNull():
        return canvas
    scaled = pixmap.scaled(size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    painter = QPainter(canvas)
    painter.drawPixmap(
        (size.width() - scaled.width()) // 2,
        (size.height() - scaled.height()) // 2,
        scaled,
    )
    painter.end()
    return canvas


def audio_cell(size: QSize) -> QPixmap:
    """A waveform glyph, for media that has no picture to take a frame from."""
    canvas = _blank(size)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)

    middle = size.height() / 2
    span = size.width() * 0.62
    left = (size.width() - span) / 2
    step = span / len(_AUDIO_BARS)
    width = max(1.0, step * 0.5)

    colour = theme.WAVEFORM.darker(140)
    painter.setPen(Qt.NoPen)
    painter.setBrush(colour)
    for index, height in enumerate(_AUDIO_BARS):
        half = height * size.height() * 0.32
        painter.drawRoundedRect(
            QRectF(left + index * step, middle - half, width, half * 2),
            width / 2,
            width / 2,
        )
    painter.end()
    return canvas


def pending_cell(size: QSize) -> QPixmap:
    """A film frame, for video whose thumbnail has not been generated yet.

    Drawn rather than left empty so the row keeps its shape: the icon appearing
    later must not push the name across.
    """
    canvas = _blank(size)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.Antialiasing)

    pen = QPen(theme.TEXT_FAINT)
    pen.setWidthF(1.0)
    painter.setPen(pen)
    frame = QRectF(0, 0, size.width(), size.height()).adjusted(
        size.width() * 0.18, size.height() * 0.16,
        -size.width() * 0.18, -size.height() * 0.16,
    )
    painter.drawRect(frame)

    # Sprocket holes down both sides.
    hole = QRectF(0, 0, max(2.0, size.width() * 0.045), max(2.0, size.height() * 0.09))
    painter.setPen(Qt.NoPen)
    painter.setBrush(theme.TEXT_FAINT)
    for row in range(3):
        y = frame.top() + frame.height() * (0.2 + 0.3 * row) - hole.height() / 2
        for x in (frame.left() - hole.width() * 2.2, frame.right() + hole.width() * 1.2):
            painter.drawRect(QRectF(x, y, hole.width(), hole.height()))
    painter.end()
    return canvas


def render(source: QPixmap, *, has_video: bool, size: QSize = POOL_CELL) -> QPixmap:
    """The cell to show for one media item, whatever state it is in."""
    if not source.isNull():
        return letterbox(source, size)
    return pending_cell(size) if has_video else audio_cell(size)


def thumbnail(path: Path | None, *, has_video: bool, size: QSize = POOL_CELL) -> QPixmap:
    """`render` for a thumbnail on disk, tolerating a missing or unreadable file."""
    source = QPixmap(str(path)) if path is not None else QPixmap()
    return render(source, has_video=has_video, size=size)
