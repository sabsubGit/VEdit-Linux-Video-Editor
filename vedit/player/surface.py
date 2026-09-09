"""The preview surface.

A plain `QWidget` blitting a `QImage` rather than a GL widget with a YUV shader.
At proxy resolution the conversion and blit cost about two milliseconds, which
leaves plenty of headroom at 30 fps, and it avoids an entire class of driver and
context bugs. If 4K preview without proxies ever becomes a requirement this is
the module to replace — nothing else needs to know.

The widget shows the *project frame*, not the decoded image. Those are different
things the moment a 4:3 clip is cut into a 16:9 timeline: the export letterboxes
into the project format, so a viewer that fitted the image's own aspect instead
would show a picture the exported file does not contain. Framing makes the
difference plain — a zoom has to be a zoom into the same rectangle both sides
are talking about — so the frame is what gets fitted, and the picture is drawn
inside it.

The geometry itself is not here. `timeline/framing.py` decides which source
pixels are visible and where they land, precisely so this module and the render
graph cannot drift apart.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from vedit import theme
from vedit.timeline import titles as titles_mod
from vedit.timeline.framing import IDENTITY, Framing, Rect, visible_source

DEFAULT_FRAME = (1920, 1080)


def _qrect(rect: Rect) -> QRectF:
    return QRectF(rect.x, rect.y, rect.width, rect.height)


class VideoSurface(QWidget):
    """Shows one frame of the project, framed the way the export will frame it."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._message = "No media"
        self._frame: tuple[int, int] = DEFAULT_FRAME
        self._framing: Framing = IDENTITY
        self._rotation = 0
        self._flipped = False
        # The outgoing half of a cross dissolve, and how far the incoming one
        # has faded up over it. A mix of 1.0 means there is no transition.
        self._under: tuple[QImage, Framing, int, bool] | None = None
        self._mix = 1.0
        self._titles: tuple = ()
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 180)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    # -- what to show ----------------------------------------------------------

    def set_frame_size(self, width: int, height: int) -> None:
        """The project format. Everything is fitted into this, not into the image."""
        frame = (max(1, int(width)), max(1, int(height)))
        if frame != self._frame:
            self._frame = frame
            self.update()

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def set_picture(
        self, framing: Framing, rotation: int = 0, flipped: bool = False
    ) -> None:
        """How the current clip's picture is framed.

        Called on every tick with the value for the frame on screen, and
        directly by the reframe overlay while a drag is in flight — which is
        why it repaints from the image already in hand and never asks the
        decoder for anything.
        """
        if (framing, rotation, flipped) != (self._framing, self._rotation, self._flipped):
            self._framing = framing
            self._rotation = rotation
            self._flipped = flipped
            self.update()

    def set_dissolve(
        self,
        image: QImage | None,
        framing: Framing = IDENTITY,
        rotation: int = 0,
        flipped: bool = False,
        mix: float = 1.0,
    ) -> None:
        """The shot being dissolved *from*, and how far through the mix we are.

        `mix` is the incoming shot's opacity: 0 at the first frame of the
        transition, 1 once it is over. Passing no image ends the transition.
        """
        under = None if image is None or image.isNull() else (image, framing, rotation, flipped)
        mix = max(0.0, min(1.0, mix))
        if (under is None) != (self._under is None) or mix != self._mix or under is not None:
            self._under = under
            self._mix = 1.0 if under is None else mix
            self.update()

    def set_titles(self, titles) -> None:
        """The titles covering the frame on screen, lowest lane first."""
        titles = tuple(titles)
        if titles != self._titles:
            self._titles = titles
            self.update()

    def title_rects(self, titles=None) -> list:
        """Where each title's words would sit in the widget.

        Reported rather than recomputed by the caller: the overlay lets you pick
        a title up and drag it, and a second copy of this arithmetic is a second
        thing to get wrong. Same reason `frame_rect` is public.

        The titles can be passed in rather than read off the widget, because the
        caller often knows them before the next repaint has put them here — and
        asking for the rects of titles this widget had not been told about yet
        quietly returned nothing.
        """
        frame = self.frame_rect()
        if frame.isEmpty():
            return []
        scale = frame.width() / self._frame[0]
        rects = []
        for title in (self._titles if titles is None else titles):
            if title.is_empty:
                continue
            lines = titles_mod.layout(title, self._frame)
            nudge = titles_mod.horizontal_offset(title, self._frame) * scale
            margin = titles_mod.horizontal_margin(self._frame) * scale
            top = frame.y() + lines[0].top * scale
            bottom = frame.y() + (lines[-1].top + lines[-1].height) * scale
            rects.append(
                QRectF(
                    frame.x() + margin + nudge,
                    top,
                    frame.width() - margin * 2,
                    max(1.0, bottom - top),
                ).toRect()
            )
        return rects

    def clear(self, message: str = "No media") -> None:
        self._image = QImage()
        self._under = None
        self._mix = 1.0
        self._titles = ()
        self._message = message
        self.update()

    @property
    def has_image(self) -> bool:
        return not self._image.isNull()

    @property
    def frame_size(self) -> tuple[int, int]:
        """The project format being shown. The reframe overlay measures against it."""
        return self._frame

    # -- geometry --------------------------------------------------------------

    def frame_rect(self) -> QRect:
        """Where the project frame sits in the widget, letterboxed to fit.

        This is the rectangle the reframe overlay draws its handles around, so
        it is public: the handles have to sit on the picture, not on the widget.
        """
        available = self.rect()
        frame_w, frame_h = self._frame
        scale = min(available.width() / frame_w, available.height() / frame_h)
        width = int(frame_w * scale)
        height = int(frame_h * scale)
        return QRect(
            available.x() + (available.width() - width) // 2,
            available.y() + (available.height() - height) // 2,
            width,
            height,
        )

    def target_rect(self) -> QRect:
        """Backwards-compatible alias: where the picture is drawn."""
        return self.frame_rect()

    # -- painting --------------------------------------------------------------

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.BG_DARKEST)

        if self._image.isNull():
            if self._titles:
                # A title over nothing is a title card, and black is the right
                # thing behind it — the same picture the export produces.
                self._draw_titles(painter)
            else:
                painter.setPen(theme.TEXT_FAINT)
                painter.drawText(self.rect(), Qt.AlignCenter, self._message)
            painter.end()
            return

        # Smooth scaling is worth its cost here: the preview is usually shown
        # below native size, and nearest-neighbour makes fine detail crawl.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)

        # The outgoing shot of a cross dissolve, underneath, at full strength;
        # the incoming one fades up over it. Drawing the mix rather than
        # describing it is the only way the viewer can tell the truth about a
        # transition, and it costs one extra blit for the length of one.
        if self._under is not None and self._mix < 1.0:
            self._draw(painter, *self._under, opacity=1.0)

        self._draw(
            painter,
            self._image,
            self._framing,
            self._rotation,
            self._flipped,
            opacity=self._mix,
        )
        # Words last, and outside `_draw`: a caption sits on the finished frame,
        # so it neither zooms with the shot nor fades with a transition.
        self._draw_titles(painter)
        painter.end()

    def _draw_titles(self, painter: QPainter) -> None:
        """The titles over this frame, laid out by `timeline/titles.py`.

        The same module the export reads, so the words land in the same place.
        Glyphs will never match two rasterisers exactly; position, size and line
        spacing will, and those are what anyone notices.
        """
        if not self._titles:
            return
        frame = self.frame_rect()
        if frame.isEmpty():
            return
        scale = frame.width() / self._frame[0]

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        for title in self._titles:
            if title.is_empty:
                continue
            margin = titles_mod.horizontal_margin(self._frame) * scale
            nudge = titles_mod.horizontal_offset(title, self._frame) * scale
            colour = QColor(title.colour)
            for line in titles_mod.layout(title, self._frame):
                if not line.text.strip():
                    continue
                font = QFont(titles_mod.FONT_FAMILY)
                # Pixel size rather than points: the export measures in pixels
                # of the project frame, and points would drift with the DPI of
                # whatever screen this happens to be on.
                font.setPixelSize(max(1, int(round(line.font_px * scale))))
                painter.setFont(font)

                box = QRectF(
                    frame.x() + margin + nudge,
                    frame.y() + line.top * scale,
                    frame.width() - margin * 2,
                    line.height * scale,
                )
                align = {
                    "left": Qt.AlignLeft,
                    "right": Qt.AlignRight,
                    "centre": Qt.AlignHCenter,
                }[title.align] | Qt.AlignVCenter

                if title.shadow:
                    # Drawn as an outlined path rather than text-behind-text:
                    # it is the same dark edge `drawtext`'s borderw produces,
                    # and it survives over a bright sky the way a one-sided
                    # drop shadow does not.
                    metrics = painter.fontMetrics()
                    bounds = metrics.boundingRect(
                        box.toRect(), int(align), line.text
                    )
                    path = QPainterPath()
                    path.addText(
                        bounds.x(),
                        bounds.y() + metrics.ascent(),
                        font,
                        line.text,
                    )
                    pen = QPen(QColor(0, 0, 0, 166))
                    pen.setWidthF(max(1.0, line.font_px * scale * 0.11))
                    pen.setJoinStyle(Qt.RoundJoin)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    painter.drawPath(path)

                painter.setPen(colour)
                painter.drawText(box, int(align), line.text)
        painter.restore()

    def _draw(
        self,
        painter: QPainter,
        image: QImage,
        framing: Framing,
        rotation: int,
        flipped: bool,
        *,
        opacity: float,
    ) -> None:
        """One picture, framed, into the project frame's place in the widget."""
        if image.isNull() or opacity <= 0.0:
            return
        in_source, in_frame = visible_source(
            framing, (image.width(), image.height()), self._frame, rotation, flipped
        )
        if in_source.is_empty:
            # Framed entirely into the letterbox bars: there is no picture to
            # show here, and the black around it is the correct answer.
            return

        # Project-frame coordinates onto widget coordinates. One scale for both
        # axes, because the frame was fitted preserving its aspect.
        frame = self.frame_rect()
        scale = frame.width() / self._frame[0]
        target = QRectF(
            frame.x() + in_frame.x * scale,
            frame.y() + in_frame.y * scale,
            in_frame.width * scale,
            in_frame.height * scale,
        )

        painter.save()
        painter.setOpacity(opacity)
        if rotation or flipped:
            # Turning is done by the painter rather than by rotating the image:
            # a QTransform costs nothing, and copying a 1.5 MB frame every tick
            # to hold it the other way up would.
            painter.translate(target.center())
            if flipped:
                painter.scale(-1.0, 1.0)
            painter.rotate(rotation)
            # After a quarter turn the target's sides have swapped, so the
            # image is drawn into the transposed rectangle.
            swapped = rotation in (90, 270)
            w = target.height() if swapped else target.width()
            h = target.width() if swapped else target.height()
            painter.drawImage(QRectF(-w / 2, -h / 2, w, h), image, _qrect(in_source))
        else:
            painter.drawImage(target, image, _qrect(in_source))
        painter.restore()
