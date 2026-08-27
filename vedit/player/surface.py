"""The preview surface.

A plain `QWidget` blitting a `QImage` rather than a GL widget with a YUV shader.
At proxy resolution the conversion and blit cost about two milliseconds, which
leaves plenty of headroom at 30 fps, and it avoids an entire class of driver and
context bugs. If 4K preview without proxies ever becomes a requirement this is
the module to replace — nothing else needs to know.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QSizePolicy, QWidget

from vedit import theme


class VideoSurface(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._image = QImage()
        self._message = "No media"
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 180)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def set_image(self, image: QImage) -> None:
        self._image = image
        self.update()

    def clear(self, message: str = "No media") -> None:
        self._image = QImage()
        self._message = message
        self.update()

    def target_rect(self) -> QRect:
        """Letterboxed destination preserving the image's aspect ratio."""
        if self._image.isNull():
            return QRect()
        available = self.rect()
        scaled = self._image.size().scaled(available.size(), Qt.KeepAspectRatio)
        return QRect(
            available.x() + (available.width() - scaled.width()) // 2,
            available.y() + (available.height() - scaled.height()) // 2,
            scaled.width(),
            scaled.height(),
        )

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.BG_DARKEST)

        if self._image.isNull():
            painter.setPen(theme.TEXT_FAINT)
            painter.drawText(self.rect(), Qt.AlignCenter, self._message)
            painter.end()
            return

        # Smooth scaling is worth its cost here: the preview is usually shown
        # below native size, and nearest-neighbour makes fine detail crawl.
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(self.target_rect(), self._image)
        painter.end()
