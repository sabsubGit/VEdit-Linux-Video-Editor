"""Reframing a clip by dragging its picture in the viewer.

The gesture is the feature. Punching in on a shot, cropping rubbish out of the
edge of frame, and rescuing a video shot upright on a phone are the same action
at different amounts, so they are one tool rather than three menu items — and
the way to say "this bit, bigger" is to point at it, not to type a number.

It lives in the viewer because the timeline has no room left: a clip rectangle
already spends both top corners on fades, both ends on trim, and a band across
the middle on the volume line. It is also simply the better place. You reframe
by looking at the picture.

A separate widget stacked over `VideoSurface` rather than drawing into it, so
the surface stays the plain blitter its own docstring promises to keep being.

Two signals, matching the mixer's faders: `changed` fires on every mouse move
and goes straight to the viewer, so the picture tracks the mouse without the
model or the decoder being touched at all; `committed` fires once on release and
is what becomes an undo step. Sixty edits per drag would thrash both.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from vedit import theme
from vedit.timeline import framing as F
from vedit.timeline.framing import Framing, Rect
from vedit.tools import Tool

HANDLE = 13          # side of the square corner grips, in pixels
EDGE_GRAB = 18       # how far from a corner still counts as grabbing it
WHEEL_STEP = 1.12    # zoom per wheel notch


class ReframeOverlay(QWidget):
    """Corner grips and a drag area over the viewer's picture."""

    changed = Signal(object)     # Framing, live during a drag
    committed = Signal(object)   # Framing, once, on release
    status_message = Signal(str)
    seek_requested = Signal(str)  # "start" or "end" of the clip being framed
    move_requested = Signal(bool)  # add a move / take it away
    reset_requested = Signal()     # put the picture back to the whole frame
    zoomed = Signal(object)        # Framing, from a box dragged with the Zoom tool
    title_moved = Signal(object, object)     # clip, Title — live during a drag
    title_committed = Signal(object, object)  # clip, Title — once, on release

    def __init__(self, surface, parent=None) -> None:
        super().__init__(parent or surface)
        self.surface = surface
        self._framing = Framing()
        self._active = False
        self._enabled = False

        self._dragging = False
        self._corner: str | None = None
        self._press = QPoint()
        self._press_framing = Framing()
        self._moves = False
        self._editing = "start"
        self._resettable = False
        self._tool = Tool.REFRAME
        self._marquee: QRect | None = None
        # Titles under the playhead, so they can be picked up and dragged.
        # Held as (clip, rect-in-widget) so the hit test is a containment check
        # rather than a second copy of the layout arithmetic.
        self._titles: list = []
        self._title_clip = None
        self._title_start = None
        # Where the drag has got to. The model is not touched until release, so
        # without this the commit would compare the clip against itself and
        # decide nothing had changed.
        self._title_now = None

        self._build_bar()
        self.setMouseTracking(True)
        self.setVisible(False)
        # Cover the surface and keep covering it. An event filter rather than a
        # hook inside `VideoSurface`, so the surface needs to know nothing about
        # this widget existing.
        self.surface.installEventFilter(self)
        self.setGeometry(self.surface.rect())

    def eventFilter(self, watched, event) -> bool:
        if watched is self.surface and event.type() == QEvent.Resize:
            self.setGeometry(self.surface.rect())
            self._place_bar()
        return False

    # -- state -----------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Arm or disarm the tool. Disarmed, the viewer is a viewer again."""
        self._enabled = enabled
        self._update_visibility()

    def set_tool(self, tool: Tool) -> None:
        """Which gesture the viewer is offering.

        Reframe and Zoom set the same thing by different means — one is "move
        what I can see", the other is "show me this bit" — so they share the
        overlay and differ only in what the mouse does. Anything else puts the
        overlay away.
        """
        self._tool = tool
        self._marquee = None
        self._enabled = tool in (Tool.REFRAME, Tool.ZOOM)
        # Reframe only. Zoom had it too for a while, so that a zoom could be
        # undone from where it was made — but a zoom now lives as a block on the
        # timeline with its own menu, and a bar floating over the picture for as
        # long as the tool is held is in the way of the thing you are trying to
        # look at.
        self.bar.setVisible(tool is Tool.REFRAME)
        self.setCursor(Qt.CrossCursor if tool is Tool.ZOOM else Qt.ArrowCursor)
        self._refresh_bar()
        self._update_visibility()
        self.update()

    def set_target(
        self,
        framing: Framing | None,
        *,
        moves: bool = False,
        editing: str = "start",
        resettable: bool = False,
    ) -> None:
        """The framing of the clip under the playhead, or None if there is none.

        None disables the handles without disarming the tool: parked over a gap
        there is nothing to reframe, and offering grips would be a lie.
        """
        self._active = framing is not None
        self._framing = framing or Framing()
        self._moves = moves
        self._editing = editing
        # Asked of the clip rather than worked out from `framing`, which is only
        # the clip's *base* value. A zoom region sits beside it, so a clip can be
        # punched in while its base framing is still the whole frame — and Reset
        # was greyed out in exactly that case, which is the one case where the
        # user most wants it.
        self._resettable = resettable
        self._update_visibility()
        self._refresh_bar()
        self.update()

    def set_titles(self, titles: list) -> None:
        """The titles on screen, as (clip, rect) in widget coordinates.

        Dragging one is the obvious way to put a caption where you want it, and
        it is only obvious if you can grab the words themselves — so the overlay
        has to know where they landed.
        """
        self._titles = list(titles)
        self._update_visibility()
        self.update()

    def _title_at(self, pos: QPoint):
        # Topmost first: a title on a higher lane draws over a lower one, so it
        # is the one you meant to grab.
        for clip, rect in reversed(self._titles):
            if rect.adjusted(-6, -6, 6, 6).contains(pos):
                return clip, rect
        return None

    @property
    def editing(self) -> str:
        """Which end of a move the mouse is currently adjusting."""
        return self._editing

    def _build_bar(self) -> None:
        """The Start / End controls, as real buttons rather than painted ones.

        Picking an end *seeks to it* rather than switching some hidden mode.
        That way the viewer is never showing one framing while you are editing
        another — what is on screen is always the thing under the mouse, which
        removes the whole idea of a selected keyframe from a tool aimed at
        people who have never met one.
        """
        self.bar = QWidget(self)
        self.start_button = QPushButton("Start", self.bar)
        self.end_button = QPushButton("End", self.bar)
        for button in (self.start_button, self.end_button):
            button.setCheckable(True)
            # No fixed width: the stylesheet's padding makes 58px clip the
            # label, and "Framing" is wider than "Start" anyway.
            button.setMinimumWidth(56)
        self.start_button.setToolTip("Go to the start of the shot and frame it")
        self.end_button.setToolTip("Go to the end of the shot and frame it")
        self.start_button.clicked.connect(lambda: self.seek_requested.emit("start"))
        self.end_button.clicked.connect(lambda: self.seek_requested.emit("end"))

        self.readout = QLabel("1×", self.bar)
        self.readout.setStyleSheet("font-family: monospace;")
        self.readout.setMinimumWidth(52)
        self.readout.setAlignment(Qt.AlignCenter)

        self.move_button = QPushButton("Add Move", self.bar)
        self.move_button.setToolTip(
            "Let the framing travel across the shot — a slow push in or drift"
        )
        self.move_button.clicked.connect(lambda: self.move_requested.emit(not self._moves))

        # The way back. Double-clicking the picture already resets it, but that
        # is a gesture you have to be told about, and the thing people need
        # after trying a zoom is to see plainly that it can be undone — hours
        # later, not only as the next action while Ctrl+Z still reaches it.
        self.reset_button = QPushButton("Reset", self.bar)
        self.reset_button.setToolTip("Put the picture back to the whole frame")
        self.reset_button.clicked.connect(self.reset_requested)

        layout = QHBoxLayout(self.bar)
        layout.setContentsMargins(8, 5, 8, 5)
        layout.setSpacing(6)
        layout.addWidget(self.start_button)
        layout.addWidget(self.end_button)
        layout.addWidget(self.readout)
        layout.addWidget(self.move_button)
        layout.addWidget(self.reset_button)

        # Hidden until the Reframe tool is actually held: the overlay now also
        # appears whenever there is a title to drag, and a Start/End bar
        # floating over the picture at that moment belongs to nothing.
        self.bar.setVisible(False)
        self.bar.setStyleSheet(
            f"background: {theme.BG_PANEL.name()}; border: 1px solid {theme.BORDER.name()};"
            "border-radius: 6px;"
        )

    @staticmethod
    def _zoom_label(framing: Framing) -> str:
        return f"{framing.zoom:.2f}×".replace(".00×", "×")

    def _refresh_bar(self) -> None:
        # Framing a start and an end is the Reframe tool's business. Zoom is one
        # gesture that lands one value, so it shows what that value is and how
        # to be rid of it, and nothing else.
        framing_tool = self._tool is Tool.REFRAME
        self.start_button.setVisible(framing_tool)
        self.end_button.setVisible(framing_tool and self._moves)
        self.move_button.setVisible(framing_tool)
        self.start_button.setText("Start" if self._moves else "Framing")
        self.start_button.setChecked(self._editing == "start")
        self.end_button.setChecked(self._editing == "end")
        self.move_button.setText("Remove Move" if self._moves else "Add Move")
        self.readout.setText(self._zoom_label(self._framing))
        # Nothing to put back when the picture is already the whole frame.
        self.reset_button.setEnabled(
            self._resettable or not self._framing.is_identity or self._moves
        )
        self._place_bar()

    def _place_bar(self) -> None:
        """Centred along the foot of the picture, tucked inside it."""
        rect = self.picture_rect()
        hint = self.bar.sizeHint()
        self.bar.setGeometry(
            rect.center().x() - hint.width() // 2,
            max(0, rect.bottom() - hint.height() - 10),
            hint.width(),
            hint.height(),
        )

    def _update_visibility(self) -> None:
        # Also visible when there is a title to grab: dragging a caption into
        # place should not need a tool armed first, because the words are right
        # there and reaching for them is the obvious move.
        self.setVisible((self._enabled and self._active) or bool(self._titles))

    @property
    def framing(self) -> Framing:
        return self._framing

    # -- geometry --------------------------------------------------------------

    def picture_rect(self) -> QRect:
        """The project frame's rectangle, taken from the surface itself.

        Asking the surface rather than recomputing it is what keeps the handles
        on the picture when the window is resized.
        """
        return self.surface.frame_rect()

    def _corner_rects(self) -> dict[str, QRect]:
        rect = self.picture_rect()
        half = HANDLE // 2
        return {
            "tl": QRect(rect.left() - half, rect.top() - half, HANDLE, HANDLE),
            "tr": QRect(rect.right() - half, rect.top() - half, HANDLE, HANDLE),
            "bl": QRect(rect.left() - half, rect.bottom() - half, HANDLE, HANDLE),
            "br": QRect(rect.right() - half, rect.bottom() - half, HANDLE, HANDLE),
        }

    def _corner_at(self, pos: QPoint) -> str | None:
        for name, rect in self._corner_rects().items():
            if rect.adjusted(-EDGE_GRAB, -EDGE_GRAB, EDGE_GRAB, EDGE_GRAB).contains(pos):
                return name
        return None

    def _frame_size(self) -> tuple[int, int]:
        return self.surface.frame_size

    def _to_frame_pixels(self, dx: float, dy: float) -> tuple[float, float]:
        """A drag in widget pixels, in project-frame pixels."""
        rect = self.picture_rect()
        if rect.width() <= 0:
            return 0.0, 0.0
        scale = self._frame_size()[0] / rect.width()
        return dx * scale, dy * scale

    # -- mouse -----------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            event.ignore()
            return

        # A title takes the click before any framing gesture does: if the words
        # are under the pointer, moving the words is what was meant.
        found = self._title_at(event.position().toPoint())
        if found is not None:
            self._title_clip, _ = found
            self._title_start = self._title_now = self._title_clip.title
            self._press = event.position().toPoint()
            self._dragging = True
            event.accept()
            return

        if not self._active:
            event.ignore()
            return
        if self._tool is Tool.ZOOM:
            self._press = event.position().toPoint()
            self._marquee = QRect(self._press, self._press)
            self._dragging = True
            event.accept()
            return
        self._dragging = True
        self._corner = self._corner_at(event.position().toPoint())
        self._press = event.position().toPoint()
        self._press_framing = self._framing
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        pos = event.position().toPoint()

        if self._title_clip is not None and self._dragging:
            self._drag_title(pos)
            event.accept()
            return
        if not self._dragging and self._title_at(pos) is not None:
            self.setCursor(Qt.SizeAllCursor)
            return

        if self._tool is Tool.ZOOM:
            if self._dragging:
                self._marquee = QRect(self._press, pos).normalized()
                self.update()
                event.accept()
            return
        if not self._dragging:
            corner = self._corner_at(pos)
            self.setCursor(
                Qt.SizeFDiagCursor if corner in ("tl", "br")
                else Qt.SizeBDiagCursor if corner in ("tr", "bl")
                else Qt.OpenHandCursor
            )
            return

        dx = pos.x() - self._press.x()
        dy = pos.y() - self._press.y()

        if self._corner is not None:
            # Dragging a corner inward zooms in. Both axes contribute, and the
            # sign flips per corner so every grip pulls the same way: toward the
            # middle is always more zoom.
            inward = dx if "l" in self._corner else -dx
            inward += dy if "t" in self._corner else -dy
            rect = self.picture_rect()
            reach = max(1, rect.width() + rect.height())
            framing = self._press_framing.nudged(
                zoom=self._press_framing.zoom * (1.0 + 2.0 * inward / reach)
            )
        else:
            frame_dx, frame_dy = self._to_frame_pixels(dx, dy)
            framing = F.pan_for_delta(
                self._press_framing, frame_dx, frame_dy, self._frame_size()
            )

        self._framing = framing
        self.changed.emit(framing)
        self.readout.setText(self._zoom_label(framing))
        self.status_message.emit(f"Reframe: {F.describe(framing)}")
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if self._title_clip is not None:
            clip, start, moved = self._title_clip, self._title_start, self._title_now
            self._title_clip = self._title_start = self._title_now = None
            self._dragging = False
            if moved is not None and moved != start:
                self.title_committed.emit(clip, moved)
            event.accept()
            return
        if not self._dragging:
            return
        if self._tool is Tool.ZOOM:
            self._dragging = False
            box, self._marquee = self._marquee, None
            self.update()
            if box is not None and box.width() > 8 and box.height() > 8:
                self._zoom_into(box)
            else:
                self.status_message.emit("Drag a box round what to zoom into")
            event.accept()
            return
        self._dragging = False
        self._corner = None
        # Only an actual change becomes an undo step: a click that moved nothing
        # should not land on the stack.
        if self._framing != self._press_framing:
            self.committed.emit(self._framing)
        event.accept()

    def _drag_title(self, pos: QPoint) -> None:
        """Move the picked-up title, in fractions of the frame."""
        rect = self.picture_rect()
        if rect.width() <= 0 or self._title_start is None:
            return
        frame_w, frame_h = self._frame_size()
        scale_x = frame_w / rect.width()
        scale_y = frame_h / max(1, rect.height())
        dx = (pos.x() - self._press.x()) * scale_x / frame_w
        dy = (pos.y() - self._press.y()) * scale_y / frame_h

        moved = replace(
            self._title_start,
            offset_x=max(-1.0, min(1.0, self._title_start.offset_x + dx)),
            offset_y=max(-1.0, min(1.0, self._title_start.offset_y + dy)),
        )
        self._title_now = moved
        self.title_moved.emit(self._title_clip, moved)
        self.update()

    def _zoom_into(self, box: QRect) -> None:
        """Commit the framing that fills the frame with the dragged box."""
        rect = self.picture_rect()
        if rect.width() <= 0:
            return
        scale = self._frame_size()[0] / rect.width()
        in_frame = Rect(
            (box.x() - rect.x()) * scale,
            (box.y() - rect.y()) * scale,
            box.width() * scale,
            box.height() * scale,
        )
        framing = F.framing_for_box(in_frame, self._frame_size())
        self._framing = framing
        self.changed.emit(framing)
        # Its own signal rather than `committed`: a box dragged with the Zoom
        # tool means "show me this, here", and lands a region covering a few
        # seconds round the playhead. Reframe's own gesture still sets the whole
        # shot, which is the difference between the two tools.
        self.zoomed.emit(framing)
        self.update()

    def wheelEvent(self, event) -> None:
        if not self._active:
            event.ignore()
            return
        notches = event.angleDelta().y() / 120
        framing = self._framing.nudged(zoom=self._framing.zoom * WHEEL_STEP**notches)
        if framing != self._framing:
            self._framing = framing
            self.changed.emit(framing)
            self.committed.emit(framing)
            self.status_message.emit(f"Reframe: {F.describe(framing)}")
            self.update()
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:
        """Back to the whole frame — the way out of a zoom that went too far."""
        if not self._active:
            return
        if not self._framing.is_identity:
            self._framing = Framing()
            self.changed.emit(self._framing)
            self.committed.emit(self._framing)
            self.status_message.emit("Reframe: reset")
            self.update()

    # -- painting --------------------------------------------------------------

    def paintEvent(self, event) -> None:
        rect = self.picture_rect()
        if rect.isEmpty():
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # A thirds grid, faint: it is what people actually reframe against, and
        # it doubles as the signal that the tool is armed.
        grid = QColor(theme.TEXT)
        grid.setAlpha(48)
        painter.setPen(QPen(grid, 1))
        for step in (1, 2):
            x = rect.left() + rect.width() * step / 3
            y = rect.top() + rect.height() * step / 3
            painter.drawLine(int(x), rect.top(), int(x), rect.bottom())
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        border = QColor(theme.ACCENT)
        border.setAlpha(200)
        painter.setPen(QPen(border, 1))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        if self._tool is Tool.REFRAME:
            painter.setPen(QPen(theme.BG_DARKEST, 1))
            painter.setBrush(theme.ACCENT)
            for handle in self._corner_rects().values():
                painter.drawRect(handle)

        if self._marquee is not None and not self._marquee.isEmpty():
            # Dim everything outside the box, so the marquee shows what you are
            # about to get rather than merely where the mouse has been.
            shade = QColor(0, 0, 0, 110)
            outside = QPainterPath()
            outside.addRect(QRectF(rect))
            inner = QPainterPath()
            inner.addRect(QRectF(self._marquee))
            painter.fillPath(outside.subtracted(inner), QBrush(shade))
            painter.setBrush(Qt.NoBrush)
            pen = QPen(theme.ACCENT)
            pen.setWidthF(1.4)
            painter.setPen(pen)
            painter.drawRect(self._marquee)

        painter.end()
