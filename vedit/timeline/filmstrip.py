"""Drawing frame thumbnails along video clips.

The sheets themselves are made at ingest (see `media.proxy`) as one tiled sprite
image per source. Keeping them in a single pixmap matters because the timeline
repaints on every frame during playback: blitting cells out of one already-loaded
pixmap costs far less than opening files or decoding video while painting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QPainter, QPixmap


@dataclass(frozen=True, slots=True)
class Filmstrip:
    """A sprite sheet of periodic frames, plus how to index it."""

    pixmap: QPixmap
    count: int
    columns: int
    cell_width: int
    cell_height: int
    interval: float          # seconds of source between adjacent cells

    @property
    def aspect(self) -> float:
        return self.cell_width / self.cell_height if self.cell_height else 1.0

    def index_for(self, source_seconds: float) -> int:
        if self.interval <= 0:
            return 0
        index = int(source_seconds / self.interval)
        return max(0, min(index, self.count - 1))

    def cell(self, index: int) -> QRect:
        index = max(0, min(index, self.count - 1))
        row, column = divmod(index, self.columns)
        return QRect(
            column * self.cell_width,
            row * self.cell_height,
            self.cell_width,
            self.cell_height,
        )


class FilmstripCache:
    """Loaded sheets, kept for the session.

    A sheet is a few hundred kilobytes decoded, and there is one per source
    rather than one per clip, so holding the ones in use costs little.
    """

    def __init__(self) -> None:
        self._strips: dict[str, Filmstrip] = {}
        self._missing: set[str] = set()

    def get(self, media_id: str, located: tuple[Path, dict] | None) -> Filmstrip | None:
        if media_id in self._strips:
            return self._strips[media_id]
        if media_id in self._missing or located is None:
            return None

        path, meta = located
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            # A half-written or corrupt sheet should cost a plain clip body,
            # not a broken timeline.
            self._missing.add(media_id)
            return None

        strip = Filmstrip(
            pixmap=pixmap,
            count=int(meta.get("count", 0)),
            columns=max(1, int(meta.get("columns", 1))),
            cell_width=max(1, int(meta.get("cell_width", 1))),
            cell_height=max(1, int(meta.get("cell_height", 1))),
            interval=float(meta.get("interval", 1.0)),
        )
        if strip.count <= 0:
            self._missing.add(media_id)
            return None

        self._strips[media_id] = strip
        return strip

    def forget(self, media_id: str) -> None:
        self._strips.pop(media_id, None)
        self._missing.discard(media_id)

    def clear(self) -> None:
        self._strips.clear()
        self._missing.clear()


def draw_filmstrip(
    painter: QPainter,
    rect: QRectF,
    strip: Filmstrip,
    *,
    start_seconds: float,
    seconds_per_pixel: float,
) -> None:
    """Tile thumbnails across `rect`.

    Each thumbnail shows the frame at its own left edge, so the strip stays
    honest about where you are in the clip however it is zoomed or trimmed —
    scaling one image to fit would misrepresent every position but the ends.

    `seconds_per_pixel` already accounts for clip speed, so a retimed clip shows
    the frames it actually plays.
    """
    if strip.count <= 0 or rect.width() < 2 or rect.height() < 2:
        return

    # Thumbnails keep their aspect ratio at whatever height the lane allows.
    height = rect.height()
    width = max(2.0, height * strip.aspect)

    painter.save()
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

    offset = 0.0
    while offset < rect.width():
        cell_width = min(width, rect.width() - offset)
        source = start_seconds + (offset * seconds_per_pixel)
        cell = strip.cell(strip.index_for(source))

        if cell_width < width:
            # The last thumbnail is cropped rather than squashed, so the frames
            # stay the same size right up to the edge of the clip.
            cell = QRect(
                cell.x(), cell.y(),
                max(1, int(cell.width() * (cell_width / width))), cell.height(),
            )

        painter.drawPixmap(
            QRectF(rect.left() + offset, rect.top(), cell_width, height),
            strip.pixmap,
            QRectF(cell),
        )
        offset += width

    painter.restore()
