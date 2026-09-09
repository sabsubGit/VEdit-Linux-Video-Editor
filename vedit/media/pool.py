"""The media pool: everything imported into the project.

Import is deliberately permissive — anything ffprobe can open is accepted. The
extension list below is used *only* to decide what to try when scanning a folder,
so a stray README doesn't produce an error dialog. Files dropped or picked
explicitly are always probed, whatever they are called.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap

from vedit.core.timebase import TimeBase
from vedit.media import thumbs
from vedit.media.probe import MediaInfo, UnsupportedMedia, probe
from vedit.media.proxy import ProxyManager, Status

# Only consulted when walking a directory.
SCANNABLE_SUFFIXES = frozenset(
    {
        # video containers
        ".mp4", ".mov", ".mkv", ".webm", ".avi", ".mxf", ".m4v", ".mpg", ".mpeg",
        ".ts", ".m2ts", ".mts", ".wmv", ".flv", ".ogv", ".3gp", ".vob", ".braw",
        # audio
        ".wav", ".aiff", ".aif", ".mp3", ".m4a", ".aac", ".flac", ".opus", ".ogg",
        ".wma", ".ac3", ".eac3", ".caf",
    }
)

MEDIA_ID_ROLE = Qt.UserRole + 1
INFO_ROLE = Qt.UserRole + 2

# Carries media ids when dragging from the pool onto the timeline.
MEDIA_MIME = "application/x-vedit-media-ids"

COLUMNS = ("Name", "Duration", "Format", "Status")


class MediaPool(QAbstractTableModel):
    """Table model over the imported `MediaInfo`s."""

    media_added = Signal(list)      # list[str] of media ids
    import_failed = Signal(str, str)  # path, reason

    def __init__(self, proxies: ProxyManager, timebase: TimeBase, parent=None) -> None:
        super().__init__(parent)
        self._proxies = proxies
        self._timebase = timebase
        self._items: list[MediaInfo] = []
        self._index_of: dict[str, int] = {}
        self._status: dict[str, Status] = {}
        self._thumbs: dict[str, QIcon] = {}

        proxies.status.connect(self._on_status)
        proxies.thumb_ready.connect(self._on_thumb)

    # -- Qt model ---------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        info = self._items[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            if column == 0:
                return info.name
            if column == 1:
                return self._timebase.frames_to_timecode(info.frame_count(self._timebase))
            if column == 2:
                return info.describe()
            if column == 3:
                return self._status.get(info.media_id, Status.MISSING).value
        elif role == Qt.DecorationRole and column == 0:
            return self._icon_for(info)
        elif role == Qt.ToolTipRole:
            return f"{info.path}\n{info.describe()}"
        elif role == MEDIA_ID_ROLE:
            return info.media_id
        elif role == INFO_ROLE:
            return info
        elif role == Qt.TextAlignmentRole and column in (1, 3):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if index.isValid():
            return base | Qt.ItemIsDragEnabled
        return base

    def _icon_for(self, info: MediaInfo) -> QIcon:
        """The row's icon, made once and kept.

        Every row has one, including audio and including video whose thumbnail
        is still generating — all three states are the same size, so the icon
        landing later never shifts the name beside it.

        Built here rather than at import so a project opened against a warm
        cache shows its frames immediately, without waiting for the ingest
        workers to walk the whole pool first.
        """
        icon = self._thumbs.get(info.media_id)
        if icon is None:
            icon = QIcon(
                thumbs.thumbnail(
                    self._proxies.thumb_for(info.media_id),
                    has_video=info.video is not None,
                )
            )
            self._thumbs[info.media_id] = icon
        return icon

    # -- dragging out to the timeline -------------------------------------------

    def mimeTypes(self) -> list[str]:
        return [MEDIA_MIME]

    def mimeData(self, indexes) -> QMimeData:
        """Pack the selected media ids, de-duplicated across columns.

        A table selection yields one index per column, so the same row arrives
        several times; dropping duplicates keeps a single row from placing the
        same clip four times.
        """
        ordered: list[str] = []
        for index in indexes:
            if not index.isValid():
                continue
            media_id = self._items[index.row()].media_id
            if media_id not in ordered:
                ordered.append(media_id)

        data = QMimeData()
        data.setData(MEDIA_MIME, "\n".join(ordered).encode())
        return data

    @staticmethod
    def ids_from_mime(data: QMimeData) -> list[str]:
        if not data.hasFormat(MEDIA_MIME):
            return []
        raw = bytes(data.data(MEDIA_MIME)).decode(errors="replace")
        return [line for line in raw.split("\n") if line]

    # -- lookups ----------------------------------------------------------------

    def info_at(self, row: int) -> MediaInfo | None:
        return self._items[row] if 0 <= row < len(self._items) else None

    def info_for(self, media_id: str) -> MediaInfo | None:
        row = self._index_of.get(media_id)
        return self._items[row] if row is not None else None

    def all_media(self) -> list[MediaInfo]:
        return list(self._items)

    def __len__(self) -> int:
        return len(self._items)

    # -- import -----------------------------------------------------------------

    def add_paths(self, paths: list[str | Path]) -> list[MediaInfo]:
        """Import files and/or folders, skipping anything already in the pool."""
        candidates: list[Path] = []
        for entry in paths:
            path = Path(entry).expanduser()
            if path.is_dir():
                candidates.extend(self._scan(path))
            elif path.is_file():
                candidates.append(path)

        added: list[MediaInfo] = []
        for path in candidates:
            try:
                info = probe(path)
            except UnsupportedMedia as exc:
                self.import_failed.emit(str(path), str(exc))
                continue
            if info.media_id in self._index_of:
                continue
            added.append(info)

        if not added:
            return []

        first = len(self._items)
        self.beginInsertRows(QModelIndex(), first, first + len(added) - 1)
        for info in added:
            self._index_of[info.media_id] = len(self._items)
            self._items.append(info)
            self._status[info.media_id] = self._proxies.status_of(info)
        self.endInsertRows()

        for info in added:
            self._proxies.request(info)

        self.media_added.emit([info.media_id for info in added])
        return added

    @staticmethod
    def _scan(folder: Path) -> list[Path]:
        """Recursively collect plausible media, sorted so import order is stable."""
        found = [
            path
            for path in folder.rglob("*")
            if path.is_file() and path.suffix.lower() in SCANNABLE_SUFFIXES
        ]
        return sorted(found)

    def remove_ids(self, media_ids: list[str]) -> None:
        doomed = set(media_ids)
        if not doomed:
            return
        self.beginResetModel()
        self._items = [info for info in self._items if info.media_id not in doomed]
        self._index_of = {info.media_id: row for row, info in enumerate(self._items)}
        for media_id in doomed:
            self._status.pop(media_id, None)
            self._thumbs.pop(media_id, None)
        self.endResetModel()

    # -- background updates -----------------------------------------------------

    def _row_changed(self, media_id: str, column: int) -> None:
        row = self._index_of.get(media_id)
        if row is None:
            return
        index = self.index(row, column)
        self.dataChanged.emit(index, index)

    def _on_status(self, media_id: str, status: str) -> None:
        try:
            self._status[media_id] = Status(status)
        except ValueError:
            return
        self._row_changed(media_id, 3)

    def _on_thumb(self, media_id: str, path: str) -> None:
        pixmap = QPixmap(path)
        if pixmap.isNull():
            # Leave the placeholder in place: a half-written or unreadable file
            # is a reason to keep showing a cell, not to blank the row.
            return
        self._thumbs[media_id] = QIcon(thumbs.letterbox(pixmap, thumbs.POOL_CELL))
        self._row_changed(media_id, 0)
