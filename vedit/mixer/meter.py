"""The level meter.

Custom-painted for the same reason the timeline is: the shape wanted here — a
dB-scaled bar in three colour zones, a held-peak line above it and a clip flag
that latches — is not any Qt widget. `QProgressBar` is linear, one colour, and
has no notion of a peak hold; styling one into this costs more code than drawing
it, and still cannot guarantee the thing that matters most, which is that the
0 dB gridline lands at the same height as the fader's 0 dB mark.

The ballistics live in `timeline.levels` rather than here, so how the bar moves
is testable without a widget.
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from vedit import theme
from vedit.timeline import levels

CLIP_FLAG_HEIGHT = 7      # the latching block across the top
BAR_INSET = 1.0
GRID_DB = (0.0, -6.0, -12.0, -24.0, -48.0)
REPAINT_THRESHOLD_DB = 0.2   # below this the bar has not visibly moved


def scale_span(height: float) -> tuple[float, float]:
    """Top and bottom y of the dB scale for a widget of this height.

    Shared by the meter and the fader — not merely similar, the same function —
    so a fader sitting at 0 dB and the meter's 0 dB gridline land on the same
    pixel row. That alignment is the entire reason both are custom-painted
    instead of being a styled QSlider next to a QProgressBar.
    """
    top = CLIP_FLAG_HEIGHT + 2.0
    return top, max(top + 1.0, height - 1.0)


def fraction_to_y(fraction: float, height: float) -> float:
    top, bottom = scale_span(height)
    return bottom - fraction * (bottom - top)


def y_to_fraction(y: float, height: float) -> float:
    top, bottom = scale_span(height)
    return (bottom - y) / max(1.0, bottom - top)


class LevelMeter(QWidget):
    """A post-fader peak meter with a held peak and a latching clip flag."""

    def __init__(self, parent=None, *, width: int = 12) -> None:
        super().__init__(parent)
        self.ballistics = levels.MeterBallistics()
        self.setFixedWidth(width)
        self.setMinimumHeight(80)
        self.setToolTip("Peak level. Click to clear the clip indicator.")
        self._painted_db = self.ballistics.level_db

    # -- state -----------------------------------------------------------------

    def feed(self, peak: float | None, now: float, *, clipped: bool = False) -> None:
        """Advance the bar. Repaints only when it has visibly moved.

        Twelve strips repainting thirty times a second for a bar that has not
        changed is real work for nothing, and the meter competes with the
        timeline for the same paint budget.
        """
        was_clipped = self.ballistics.clipped
        self.ballistics.feed(peak, now, clipped=clipped)
        moved = abs(self.ballistics.level_db - self._painted_db) >= REPAINT_THRESHOLD_DB
        if moved or self.ballistics.clipped != was_clipped:
            self._painted_db = self.ballistics.level_db
            self.update()

    def reset(self) -> None:
        """Playback stopped: drop to the floor. The clip latch survives."""
        self.ballistics.reset()
        self._painted_db = self.ballistics.level_db
        self.update()

    def clear_clip(self) -> None:
        self.ballistics.clear_clip()
        self.update()

    def mousePressEvent(self, event) -> None:
        self.clear_clip()

    # -- painting --------------------------------------------------------------

    def _bar_area(self) -> QRectF:
        top, bottom = scale_span(self.height())
        return QRectF(BAR_INSET, top, self.width() - BAR_INSET * 2, bottom - top)

    def y_for(self, db: float) -> float:
        """Screen y of a level — shared with the fader so the two line up."""
        return fraction_to_y(levels.db_to_fraction(db), self.height())

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.METER_BG)
        area = self._bar_area()

        # Gridlines first, so the bar covers them where it reaches.
        painter.setPen(QPen(theme.METER_GRID, 1))
        for db in GRID_DB:
            y = self.y_for(db)
            painter.drawLine(int(area.left()), int(y), int(area.right()), int(y))

        self._paint_bar(painter, area)

        hold = self.ballistics.hold_db
        if hold > levels.METER_FLOOR_DB:
            painter.setPen(QPen(theme.METER_PEAK, 2))
            y = self.y_for(hold)
            painter.drawLine(int(area.left()), int(y), int(area.right()), int(y))

        flag = QRectF(0, 0, self.width(), CLIP_FLAG_HEIGHT)
        painter.fillRect(
            flag, theme.METER_CLIP if self.ballistics.clipped else theme.METER_GRID
        )

    def _paint_bar(self, painter: QPainter, area: QRectF) -> None:
        """Three stacked zones, each clipped to how far the bar has reached.

        Drawn as bands rather than a single colour that changes with level: the
        bands stay put, so the colour of the *top* of the bar tells you where it
        is without the whole thing flashing red on one transient.
        """
        level = self.ballistics.level_db
        if level <= levels.METER_FLOOR_DB:
            return

        top = self.y_for(level)
        for low, high, colour in (
            (levels.METER_FLOOR_DB, -12.0, theme.METER_LOW),
            (-12.0, -3.0, theme.METER_MID),
            (-3.0, 12.0, theme.METER_HIGH),
        ):
            if level <= low:
                break
            band_top = max(top, self.y_for(min(level, high)))
            band_bottom = self.y_for(low)
            if band_bottom - band_top < 0.5:
                continue
            painter.fillRect(
                QRectF(area.left(), band_top, area.width(), band_bottom - band_top),
                QColor(colour),
            )
