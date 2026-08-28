"""Faders and channel strips.

The fader is custom-painted rather than a styled `QSlider`, and the deciding
argument is alignment: the fader cap and the meter's 0 dB gridline have to sit
at the same height for a strip to read as a strip, which means both must go
through `levels.db_to_fraction`. A `QSlider` groove has style-dependent margins
that make that impossible to guarantee.

The mute and solo buttons *are* Qt buttons. Hand-drawing a two-state button when
`QPushButton(checkable=True)` and two stylesheet rules do the job would be
gratuitous.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.mixer.meter import GRID_DB, LevelMeter, fraction_to_y, scale_span, y_to_fraction
from vedit.timeline import levels
from vedit.timeline.model import Track

CAP_HEIGHT = 13
CAP_WIDTH = 22
TRACK_WIDTH = 5
STRIP_WIDTH = 78


class Fader(QWidget):
    """A vertical dB fader sharing the meter's travel curve.

    Two signals, and the split is the whole reason a fader move does not bury
    the undo stack: `moved` fires continuously and goes straight to the live
    mixer state, `released` fires once and is what gets committed as an edit.
    """

    moved = Signal(float)
    released = Signal(float)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._db = 0.0
        self._dragging = False
        self.setFixedWidth(CAP_WIDTH + 10)
        self.setMinimumHeight(80)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("Drag to set level. Double-click for 0 dB.")

    # -- value -----------------------------------------------------------------

    def db(self) -> float:
        return self._db

    def set_db(self, db: float) -> None:
        db = levels.clamp_gain(db)
        if db != self._db:
            self._db = db
            self.update()

    @property
    def is_dragging(self) -> bool:
        return self._dragging

    # -- geometry --------------------------------------------------------------

    def _travel(self) -> QRectF:
        """The span the cap's centre moves through.

        Exactly the meter's scale, from `meter.scale_span` — not an inset
        version of it. Insetting by half a cap would move 0 dB a few pixels off
        the meter's 0 dB gridline, which is the one thing this widget exists to
        avoid. The cap is instead clamped when it is drawn.
        """
        top, bottom = scale_span(self.height())
        return QRectF(0, top, self.width(), bottom - top)

    def _y_for(self, db: float) -> float:
        return fraction_to_y(levels.db_to_fraction(db), self.height())

    def _db_at(self, y: float) -> float:
        return levels.fraction_to_db(y_to_fraction(y, self.height()))

    # -- interaction -----------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        self._dragging = True
        self._set_from(event.position().y())

    def mouseMoveEvent(self, event) -> None:
        if self._dragging:
            self._set_from(event.position().y())

    def mouseReleaseEvent(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        self.released.emit(self._db)

    def mouseDoubleClickEvent(self, event) -> None:
        """Back to unity. The one value worth a shortcut of its own."""
        self._dragging = False
        self.set_db(0.0)
        self.moved.emit(0.0)
        self.released.emit(0.0)

    def wheelEvent(self, event) -> None:
        step = 0.5 if event.modifiers() & Qt.ShiftModifier else 1.5
        self.set_db(self._db + (step if event.angleDelta().y() > 0 else -step))
        self.moved.emit(self._db)
        self.released.emit(self._db)

    def _set_from(self, y: float) -> None:
        db = self._db_at(y)
        # A detent at unity, for the same reason the clip volume line has one:
        # 0 dB is the value wanted most often and the hardest to hit by eye.
        if abs(db) < 0.6:
            db = 0.0
        self.set_db(db)
        self.moved.emit(self._db)

    # -- painting --------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        travel = self._travel()
        centre = self.width() / 2

        groove = QRectF(centre - TRACK_WIDTH / 2, travel.top(), TRACK_WIDTH, travel.height())
        painter.fillRect(groove, theme.FADER_TRACK)

        painter.setPen(QPen(theme.METER_GRID, 1))
        for db in GRID_DB:
            y = self._y_for(db)
            length = 7 if db == 0.0 else 4
            painter.drawLine(
                QPointF(centre - length, y), QPointF(centre + length, y)
            )

        # The unity mark is drawn last of the marks and in its own colour, so it
        # stays findable at a glance while dragging.
        unity = self._y_for(0.0)
        painter.setPen(QPen(theme.FADER_UNITY, 1))
        painter.drawLine(QPointF(centre - 8, unity), QPointF(centre + 8, unity))

        y = self._y_for(self._db)
        # Clamped so the cap stays whole at the ends of the travel. The scale
        # itself is not inset, so only the last few pixels are affected and the
        # gridlines still mean what they say.
        cap_y = min(max(y, travel.top() + CAP_HEIGHT / 2), travel.bottom() - CAP_HEIGHT / 2)
        cap = QRectF(centre - CAP_WIDTH / 2, cap_y - CAP_HEIGHT / 2, CAP_WIDTH, CAP_HEIGHT)
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme.FADER_CAP)
        painter.drawRoundedRect(cap, 3, 3)
        painter.setPen(QPen(theme.BG_DARKEST, 1))
        painter.drawLine(QPointF(cap.left() + 3, cap_y), QPointF(cap.right() - 3, cap_y))


class ChannelStrip(QWidget):
    """One audio lane: name, fader, meter, mute and solo, and a dB readout."""

    gain_moved = Signal(str, float)       # track_id, dB — live, no undo step
    gain_committed = Signal(str, float)   # track_id, dB — once, on release
    mute_toggled = Signal(str, bool)
    solo_toggled = Signal(str, bool)

    def __init__(self, track: Track, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ChannelStrip")
        self.track_id = track.track_id
        self.setFixedWidth(STRIP_WIDTH)

        self.name = QLabel(track.name)
        self.name.setObjectName("StripName")
        self.name.setAlignment(Qt.AlignCenter)

        self.fader = Fader(self)
        self.meter = LevelMeter(self)
        self.value = QLabel("0.0")
        self.value.setObjectName("StripValue")
        self.value.setAlignment(Qt.AlignCenter)

        self.mute = QPushButton("M")
        self.mute.setObjectName("MuteButton")
        self.mute.setCheckable(True)
        self.mute.setToolTip("Mute this lane")
        self.solo = QPushButton("S")
        self.solo.setObjectName("SoloButton")
        self.solo.setCheckable(True)
        self.solo.setToolTip("Solo. Additive — several lanes can be soloed at once.")
        for button in (self.mute, self.solo):
            button.setFixedHeight(20)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(4)
        controls.addWidget(self.mute, 1)
        controls.addWidget(self.solo, 1)

        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(6)
        middle.addStretch(1)
        middle.addWidget(self.fader)
        middle.addWidget(self.meter)
        middle.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)
        layout.addWidget(self.name)
        layout.addLayout(middle, 1)
        layout.addWidget(self.value)
        layout.addLayout(controls)

        self.fader.moved.connect(lambda db: self._on_moved(db))
        self.fader.released.connect(lambda db: self.gain_committed.emit(self.track_id, db))
        self.mute.toggled.connect(lambda on: self.mute_toggled.emit(self.track_id, on))
        self.solo.toggled.connect(lambda on: self.solo_toggled.emit(self.track_id, on))

        self.refresh(track)

    def _on_moved(self, db: float) -> None:
        self.value.setText(f"{db:.1f}")
        self.gain_moved.emit(self.track_id, db)

    def refresh(self, track: Track) -> None:
        """Take the strip's state from the model.

        Skipped while the fader is held, so an unrelated `timeline_changed`
        cannot yank it out from under the cursor mid-drag.
        """
        self.name.setText(track.name)
        if not self.fader.is_dragging:
            self.fader.set_db(track.gain_db)
            self.value.setText(f"{track.gain_db:.1f}")

        for button, state in ((self.mute, track.muted), (self.solo, track.solo)):
            if button.isChecked() != state:
                button.blockSignals(True)
                button.setChecked(state)
                button.blockSignals(False)


class MasterStrip(QWidget):
    """The summed output: fader, meter and a dB readout. No mute, no solo.

    Muting the master would be indistinguishable from pulling it down, and
    soloing the only output means nothing — so neither is offered. The button
    row instead clears every clip indicator on the page, which is the thing you
    actually want after a loud passage.
    """

    gain_moved = Signal(float)
    gain_committed = Signal(float)
    clips_cleared = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("MasterStrip")
        self.setFixedWidth(STRIP_WIDTH + 6)

        self.name = QLabel("MASTER")
        self.name.setObjectName("StripName")
        self.name.setAlignment(Qt.AlignCenter)

        self.fader = Fader(self)
        self.meter = LevelMeter(self, width=14)
        self.value = QLabel("0.0")
        self.value.setObjectName("StripValue")
        self.value.setAlignment(Qt.AlignCenter)

        self.reset_clips = QPushButton("CLIP")
        self.reset_clips.setObjectName("MuteButton")
        self.reset_clips.setFixedHeight(20)
        self.reset_clips.setToolTip("Clear every clip indicator")

        middle = QHBoxLayout()
        middle.setContentsMargins(0, 0, 0, 0)
        middle.setSpacing(6)
        middle.addStretch(1)
        middle.addWidget(self.fader)
        middle.addWidget(self.meter)
        middle.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(7, 7, 7, 7)
        layout.setSpacing(5)
        layout.addWidget(self.name)
        layout.addLayout(middle, 1)
        layout.addWidget(self.value)
        layout.addWidget(self.reset_clips)

        self.fader.moved.connect(self._on_moved)
        self.fader.released.connect(self.gain_committed)
        self.reset_clips.clicked.connect(self.clips_cleared)

    def _on_moved(self, db: float) -> None:
        self.value.setText(f"{db:.1f}")
        self.gain_moved.emit(db)

    def refresh(self, gain_db: float) -> None:
        if not self.fader.is_dragging:
            self.fader.set_db(gain_db)
            self.value.setText(f"{gain_db:.1f}")
