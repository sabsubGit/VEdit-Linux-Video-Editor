"""Media page: the pool on the left, details for the selected item on the right."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.core.project import Project
from vedit.media.pool_view import MediaPoolPanel
from vedit.media.probe import MediaInfo


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DetailsPanel(QWidget):
    """Metadata for the selected media, plus the action that puts it on the timeline."""

    append_requested = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self._info: MediaInfo | None = None

        self.thumb = QLabel()
        self.thumb.setFixedHeight(150)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setFrameShape(QFrame.StyledPanel)
        self.thumb.setStyleSheet(
            f"background: {theme.BG_DARKEST.name()}; border: 1px solid {theme.BORDER.name()};"
        )

        self.title = QLabel("Nothing selected")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-weight: 600; font-size: 14px;")

        self.form = QFormLayout()
        self.form.setLabelAlignment(Qt.AlignRight)
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(6)
        self._fields: dict[str, QLabel] = {}
        for name in ("Path", "Container", "Duration", "Video", "Audio", "Size", "Proxy"):
            value = QLabel("—")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._fields[name] = value
            self.form.addRow(f"{name}:", value)

        self.append_button = QPushButton("Append to Timeline")
        self.append_button.setEnabled(False)
        self.append_button.clicked.connect(self._append)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(self.thumb)
        layout.addWidget(self.title)
        layout.addLayout(self.form)
        layout.addStretch(1)
        layout.addWidget(self.append_button)

        project.proxies.thumb_ready.connect(self._on_thumb)
        project.proxies.status.connect(self._on_status)

    def show_info(self, info: MediaInfo | None) -> None:
        self._info = info
        self.append_button.setEnabled(info is not None)

        if info is None:
            self.title.setText("Nothing selected")
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("")
            for label in self._fields.values():
                label.setText("—")
            return

        timebase = self.project.timebase
        self.title.setText(info.name)
        self._fields["Path"].setText(str(info.path))
        self._fields["Container"].setText(info.container)
        self._fields["Duration"].setText(
            f"{timebase.frames_to_timecode(info.frame_count(timebase))}  "
            f"({float(info.duration):.2f}s)"
        )

        if info.video is not None:
            width, height = info.video.display_size
            rotation = f", rotated {info.video.rotation}°" if info.video.rotation else ""
            self._fields["Video"].setText(
                f"{info.video.codec} · {width}×{height} · "
                f"{float(info.video.fps):g} fps · {info.video.pix_fmt}{rotation}"
            )
        else:
            self._fields["Video"].setText("none")

        if info.audio is not None:
            self._fields["Audio"].setText(
                f"{info.audio.codec} · {info.audio.channels} ch · {info.audio.sample_rate} Hz"
            )
        else:
            self._fields["Audio"].setText("none")

        self._fields["Size"].setText(_human_size(info.size))
        self._update_proxy_field()
        self._load_thumb()

    def _update_proxy_field(self) -> None:
        if self._info is None:
            return
        paths = self.project.proxies.paths_for(self._info.media_id)
        if paths.proxy.exists():
            self._fields["Proxy"].setText("ready")
        elif self._info.video is not None and self._info.video.display_size[1] <= 540:
            self._fields["Proxy"].setText("not needed — source is already small")
        else:
            self._fields["Proxy"].setText("generating…")

    def _load_thumb(self) -> None:
        if self._info is None:
            return
        path = self.project.proxies.thumb_for(self._info.media_id)
        if path is None:
            self.thumb.setPixmap(QPixmap())
            self.thumb.setText("no preview yet")
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        self.thumb.setText("")
        self.thumb.setPixmap(
            pixmap.scaled(
                self.thumb.width() - 8,
                self.thumb.height() - 8,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )

    def _on_thumb(self, media_id: str, _path: str) -> None:
        if self._info is not None and self._info.media_id == media_id:
            self._load_thumb()

    def _on_status(self, media_id: str, _status: str) -> None:
        if self._info is not None and self._info.media_id == media_id:
            self._update_proxy_field()

    def _append(self) -> None:
        if self._info is not None:
            self.append_requested.emit(self._info.media_id)


class MediaPage(QWidget):
    media_activated = Signal(str)
    append_requested = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project

        self.pool_panel = MediaPoolPanel(project.pool, self)
        self.details = DetailsPanel(project, self)

        self.pool_panel.files_dropped.connect(project.import_paths)
        self.pool_panel.remove_requested.connect(project.pool.remove_ids)
        self.pool_panel.selection_changed.connect(self.details.show_info)
        self.pool_panel.media_activated.connect(self.media_activated)
        self.details.append_requested.connect(self.append_requested)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.pool_panel)
        splitter.addWidget(self.details)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([1050, 380])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
