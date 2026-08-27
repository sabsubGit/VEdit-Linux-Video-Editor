"""Waveform drawing for audio clips.

Peak files are generated at ingest (see `media.proxy`) as min/max pairs at a
fixed rate. Drawing from those instead of decoding audio means the timeline
repaints without touching the source files at all.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QPainter

from vedit.core.timebase import TimeBase
from vedit.media.proxy import read_peaks


class WaveformCache:
    """Peak data per media id, loaded once and kept for the session.

    A peak file for an hour of audio is about 1.4 MB, so holding the ones in use
    is cheaper than re-reading them on every repaint.
    """

    def __init__(self) -> None:
        self._peaks: dict[str, tuple[int, list[tuple[int, int]]]] = {}
        self._missing: set[str] = set()

    def get(self, media_id: str, path: Path | None) -> tuple[int, list[tuple[int, int]]] | None:
        if media_id in self._peaks:
            return self._peaks[media_id]
        if media_id in self._missing or path is None or not path.exists():
            return None
        try:
            data = read_peaks(path)
        except (OSError, ValueError):
            # A truncated peak file should cost a plain clip body, not a crash.
            self._missing.add(media_id)
            return None
        self._peaks[media_id] = data
        return data

    def forget(self, media_id: str) -> None:
        self._peaks.pop(media_id, None)
        self._missing.discard(media_id)

    def clear(self) -> None:
        self._peaks.clear()
        self._missing.clear()


def draw_waveform(
    painter: QPainter,
    rect: QRectF,
    peaks: tuple[int, list[tuple[int, int]]],
    *,
    src_in: int,
    src_out: int,
    timebase: TimeBase,
    colour: QColor,
) -> None:
    """Paint the clip's slice of its source waveform into `rect`.

    Draws one vertical line per pixel column, each spanning the loudest excursion
    in that column's slice of the source, which is what makes a waveform readable
    at any zoom without resampling the data.
    """
    per_second, samples = peaks
    if not samples or rect.width() < 1:
        return

    start_second = float(timebase.frames_to_seconds(src_in))
    end_second = float(timebase.frames_to_seconds(src_out))
    if end_second <= start_second:
        return

    centre = rect.center().y()
    half_height = rect.height() / 2 - 1
    if half_height <= 0:
        return

    columns = int(rect.width())
    seconds_per_column = (end_second - start_second) / columns
    buckets_per_column = max(seconds_per_column * per_second, 1e-9)

    painter.save()
    painter.setPen(colour)

    cursor = start_second * per_second
    total = len(samples)
    for column in range(columns):
        low_index = int(cursor)
        high_index = int(cursor + buckets_per_column)
        cursor += buckets_per_column
        if low_index >= total:
            break
        high_index = min(max(high_index, low_index + 1), total)

        window = samples[low_index:high_index]
        if not window:
            continue
        lowest = min(pair[0] for pair in window)
        highest = max(pair[1] for pair in window)

        top = centre - (highest / 32768.0) * half_height
        bottom = centre - (lowest / 32768.0) * half_height
        if bottom - top < 1:
            # Silence still deserves a visible centre line, otherwise a quiet
            # passage looks identical to missing data.
            top, bottom = centre - 0.5, centre + 0.5

        x = rect.left() + column
        painter.drawLine(int(x), int(top), int(x), int(bottom))

    painter.restore()
