"""The media pool widget: import buttons, drop target, and the table itself."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.media.pool import INFO_ROLE, MEDIA_ID_ROLE, MediaPool
from vedit.media.probe import MediaInfo


class MediaTable(QTableView):
    """Table of imported media. Accepts file drops; drags media ids back out."""

    files_dropped = Signal(list)
    media_activated = Signal(str)

    def __init__(self, model: MediaPool, parent=None) -> None:
        super().__init__(parent)
        self.setModel(model)

        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setDropIndicatorShown(False)

        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.setIconSize(QSize(96, 54))
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(58)
        self.horizontalHeader().setHighlightSections(False)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)

        self.doubleClicked.connect(self._on_activated)
        self._drag_hover = False

    # -- drops in --------------------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            self._drag_hover = True
            self.viewport().update()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:
        self._drag_hover = False
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        self._drag_hover = False
        self.viewport().update()
        if not mime.hasUrls():
            super().dropEvent(event)
            return
        # Local files only — a dropped http URL is not something we can decode.
        paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()

    # -- painting --------------------------------------------------------------

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if self.model() is not None and self.model().rowCount() > 0 and not self._drag_hover:
            return

        painter = QPainter(self.viewport())
        rect = self.viewport().rect().adjusted(14, 14, -14, -14)
        pen = QPen(theme.ACCENT if self._drag_hover else theme.BORDER)
        pen.setWidth(2)
        pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 7, 7)
        painter.setPen(theme.TEXT_DIM if self._drag_hover else theme.TEXT_FAINT)
        painter.drawText(
            rect,
            Qt.AlignCenter,
            "Drop video or audio files here\nor use Import Files / Import Folder",
        )
        painter.end()

    # -- selection -------------------------------------------------------------

    def selected_ids(self) -> list[str]:
        model = self.model()
        return [
            model.data(index, MEDIA_ID_ROLE)
            for index in self.selectionModel().selectedRows()
        ]

    def selected_info(self) -> MediaInfo | None:
        rows = self.selectionModel().selectedRows()
        return self.model().data(rows[0], INFO_ROLE) if rows else None

    def _on_activated(self, index) -> None:
        media_id = self.model().data(index, MEDIA_ID_ROLE)
        if media_id:
            self.media_activated.emit(media_id)


class MediaPoolPanel(QWidget):
    """Import controls above the media table."""

    files_dropped = Signal(list)
    media_activated = Signal(str)
    selection_changed = Signal(object)   # MediaInfo | None
    remove_requested = Signal(list)

    def __init__(self, model: MediaPool, parent=None) -> None:
        super().__init__(parent)
        self.model = model

        self.table = MediaTable(model, self)
        self.table.files_dropped.connect(self.files_dropped)
        self.table.media_activated.connect(self.media_activated)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self.selection_changed.emit(self.table.selected_info())
        )
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)

        import_files = QPushButton("Import Files…")
        import_files.clicked.connect(self.choose_files)
        import_folder = QPushButton("Import Folder…")
        import_folder.clicked.connect(self.choose_folder)

        self.count_label = QLabel("")
        self.count_label.setObjectName("PlaceholderLabel")

        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(import_files)
        bar.addWidget(import_folder)
        bar.addStretch(1)
        bar.addWidget(self.count_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        layout.addLayout(bar)
        layout.addWidget(self.table, 1)

        remove = QAction("Remove", self)
        remove.setShortcut(QKeySequence.Delete)
        remove.setShortcutContext(Qt.WidgetWithChildrenShortcut)
        remove.triggered.connect(self._remove_selected)
        self.addAction(remove)

        model.modelReset.connect(self._update_count)
        model.rowsInserted.connect(self._update_count)
        model.rowsRemoved.connect(self._update_count)
        self._update_count()

    def _update_count(self, *_) -> None:
        count = self.model.rowCount()
        self.count_label.setText("" if count == 0 else f"{count} item{'s' if count != 1 else ''}")

    def choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Import media",
            "",
            # A single permissive filter: the app accepts whatever ffprobe opens,
            # so a narrow filter would hide files that actually work.
            "Media files (*.mp4 *.mov *.mkv *.webm *.avi *.mxf *.m4v *.mpg *.mpeg "
            "*.ts *.m2ts *.mts *.wmv *.flv *.wav *.aiff *.mp3 *.m4a *.aac *.flac "
            "*.opus *.ogg *.ac3);;All files (*)",
        )
        if paths:
            self.files_dropped.emit(paths)

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Import folder")
        if folder:
            self.files_dropped.emit([folder])

    def _context_menu(self, point) -> None:
        if not self.table.selected_ids():
            return
        menu = QMenu(self)
        menu.addAction("Remove from pool", self._remove_selected)
        menu.exec(self.table.viewport().mapToGlobal(point))

    def _remove_selected(self) -> None:
        ids = self.table.selected_ids()
        if ids:
            self.remove_requested.emit(ids)
