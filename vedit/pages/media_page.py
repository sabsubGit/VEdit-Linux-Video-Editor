"""Media page: the pool and details up top, the timeline to drop onto below."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.core.project import Project
from vedit.media import thumbs
from vedit.media.pool_view import MediaPoolPanel
from vedit.media.probe import MediaInfo
from vedit.timeline.view import (
    AUDIO_TRACK_HEIGHT_MINI,
    VIDEO_TRACK_HEIGHT_MINI,
    TimelinePanel,
)


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
        # The thumbnail as it is on disk. Kept because the label is rescaled on
        # every resize, and scaling a scaled copy compounds the softening.
        self._source = None
        self._rendered = QSize()

        self.thumb = QLabel()
        self.thumb.setFixedHeight(150)
        # Ignored horizontally: the pixmap is sized *from* the label's width, so
        # letting the pixmap feed back into the width hint would let the two
        # chase each other across layout passes.
        self.thumb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
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
        self.append_button.setToolTip(
            "Add to the end of the timeline. To choose where it lands, "
            "drag it onto the timeline below."
        )
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
            self._source = None
            self._rendered = QSize()
            self.thumb.clear()
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
        elif self._info.video is None:
            # There is no such thing as an audio proxy, so "generating…" here
            # was a wait that was never going to end.
            self._fields["Proxy"].setText("not needed — no video")
        elif self._info.video.display_size[1] <= 540:
            self._fields["Proxy"].setText("not needed — source is already small")
        else:
            self._fields["Proxy"].setText("generating…")

    # -- the preview image -----------------------------------------------------

    def _load_thumb(self) -> None:
        """Re-read from the cache. Called when the selection or the file changes."""
        if self._info is None:
            return
        path = self.project.proxies.thumb_for(self._info.media_id)
        self._source = QPixmap(str(path)) if path is not None else QPixmap()
        self._rendered = QSize()
        self._paint_thumb()

    def _paint_thumb(self) -> None:
        """Fit the current image to the label.

        Always a full cell, never a bare `setPixmap` of whatever was on disk:
        audio and a still-generating video get their own drawn cells, so the
        panel's shape does not change as media finishes ingesting.
        """
        if self._info is None or self._source is None:
            return
        size = self.thumb.contentsRect().size()
        if size.width() < 8 or size.height() < 8 or size == self._rendered:
            return
        self._rendered = size
        self.thumb.setPixmap(
            thumbs.render(self._source, has_video=self._info.video is not None, size=size)
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._paint_thumb()

    def _on_thumb(self, media_id: str, _path: str) -> None:
        if self._info is not None and self._info.media_id == media_id:
            self._load_thumb()

    def _on_status(self, media_id: str, _status: str) -> None:
        if self._info is not None and self._info.media_id == media_id:
            self._update_proxy_field()

    def _append(self) -> None:
        if self._info is not None:
            self.append_requested.emit(self._info.media_id)


class DropTimeline(QWidget):
    """The project timeline, small, under the pool.

    Dropping media here is the whole point of it. "Append to Timeline" can only
    ever put a clip at the end, and choosing *where* a shot goes is most of what
    importing is for — so the lanes are on the same page as the pool rather than
    a page away. It is the same canvas the Edit page uses, only shorter: a drop
    made here snaps the same way and becomes the same single undo step.
    """

    status_message = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project

        self.panel = TimelinePanel(
            project,
            self,
            track_heights={
                "video": VIDEO_TRACK_HEIGHT_MINI,
                "audio": AUDIO_TRACK_HEIGHT_MINI,
            },
        )
        self.panel.status_message.connect(self.status_message)
        # Frame thumbnails are worth their cost on lanes you edit against; on
        # 46-pixel lanes glanced at while importing they are mostly noise.
        self.panel.canvas.show_filmstrips = False

        title = QLabel("Timeline")
        title.setStyleSheet("font-weight: 600;")
        self.hint = QLabel("Drag media here to place it, or double-click to append")
        self.hint.setObjectName("PlaceholderLabel")
        self.duration = QLabel("00:00:00:00")
        self.duration.setStyleSheet(
            f"font-family: monospace; color: {theme.TEXT_DIM.name()};"
        )

        fit = QPushButton("Fit")
        fit.setToolTip("Zoom the timeline to fit")
        fit.clicked.connect(self.panel.canvas.zoom_to_fit)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 4)
        bar.setSpacing(10)
        bar.addWidget(title)
        bar.addWidget(self.hint)
        bar.addStretch(1)
        bar.addWidget(self.duration)
        bar.addWidget(fit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(bar)
        layout.addWidget(self.panel, 1)

        project.timeline_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        timeline = self.project.timeline
        self.duration.setText(
            self.project.timebase.frames_to_timecode(timeline.duration)
        )
        self.hint.setVisible(timeline.duration == 0)


class MediaPage(QWidget):
    media_activated = Signal(str)
    append_requested = Signal(str)
    status_message = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project

        self.pool_panel = MediaPoolPanel(project.pool, self)
        self.details = DetailsPanel(project, self)
        self.drop_timeline = DropTimeline(project, self)

        self.pool_panel.files_dropped.connect(project.import_paths)
        self.pool_panel.remove_requested.connect(project.pool.remove_ids)
        self.pool_panel.selection_changed.connect(self.details.show_info)
        self.pool_panel.media_activated.connect(self.media_activated)
        self.details.append_requested.connect(self.append_requested)
        self.drop_timeline.status_message.connect(self.status_message)

        browser = QSplitter(Qt.Horizontal)
        browser.addWidget(self.pool_panel)
        browser.addWidget(self.details)
        browser.setStretchFactor(0, 3)
        browser.setStretchFactor(1, 1)
        browser.setSizes([1050, 380])

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(browser)
        splitter.addWidget(self.drop_timeline)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        # Tall enough for the default three-and-three lanes without scrolling.
        # A drop target you have to scroll to find the audio half of is a drop
        # target that will be dropped on wrongly.
        splitter.setSizes([500, 340])
        # The browser is the page; the lanes are a place to aim at. Collapsing
        # the browser away would leave the page with nothing to drag *from*.
        splitter.setCollapsible(0, False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

    def focus_default(self) -> None:
        """The pool is what the keyboard is for on this page — Delete removes
        from it, and its own shortcut is scoped to the table having focus."""
        self.pool_panel.table.setFocus()
