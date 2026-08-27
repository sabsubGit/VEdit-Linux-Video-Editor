"""The project: everything one editing session holds.

A single object owns the media pool, the timeline, the undo stack and the ingest
workers, so pages don't reach into each other — they all talk to this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from vedit.core.commands import UndoStack
from vedit.core.projectfile import LoadResult, load_project, save_project
from vedit.core.timebase import TimeBase
from vedit.media.pool import MediaPool
from vedit.media.probe import MediaInfo
from vedit.media.proxy import ProxyManager
from vedit.timeline.model import Timeline


class Project(QObject):
    timeline_changed = Signal()
    playhead_changed = Signal(int)
    selection_changed = Signal()

    def __init__(self, timebase: TimeBase | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.timebase = timebase or TimeBase(30)
        self.timeline = Timeline.default(self.timebase)
        self.proxies = ProxyManager(self)
        self.pool = MediaPool(self.proxies, self.timebase, self)
        self.undo = UndoStack(self.timeline)
        self.path: str | None = None

        self._playhead = 0
        self._selected: list[str] = []

        self.undo.on_change(self.timeline_changed.emit)

    # -- edits -----------------------------------------------------------------

    def edit(self, label: str, mutate: Callable[[Timeline], object]) -> object:
        """Run an undoable edit and notify the UI."""
        result = self.undo.apply(label, mutate)
        self.timeline_changed.emit()
        return result

    def undo_edit(self) -> str | None:
        label = self.undo.undo()
        if label is not None:
            self._prune_selection()
            self.timeline_changed.emit()
        return label

    def redo_edit(self) -> str | None:
        label = self.undo.redo()
        if label is not None:
            self._prune_selection()
            self.timeline_changed.emit()
        return label

    # -- playhead --------------------------------------------------------------

    @property
    def playhead(self) -> int:
        return self._playhead

    def set_playhead(self, frame: int) -> None:
        frame = max(0, int(frame))
        if frame != self._playhead:
            self._playhead = frame
            self.playhead_changed.emit(frame)

    # -- selection -------------------------------------------------------------

    @property
    def selected_ids(self) -> list[str]:
        return list(self._selected)

    def selected_clips(self):
        clips = []
        for clip_id in self._selected:
            try:
                _, clip = self.timeline.find(clip_id)
            except Exception:
                continue
            clips.append(clip)
        return clips

    def set_selection(self, clip_ids: list[str]) -> None:
        if clip_ids != self._selected:
            self._selected = list(clip_ids)
            self.selection_changed.emit()

    def _prune_selection(self) -> None:
        """After an undo, a selected clip may no longer exist."""
        alive = {clip.clip_id for clip in self.timeline.all_clips()}
        kept = [clip_id for clip_id in self._selected if clip_id in alive]
        if kept != self._selected:
            self._selected = kept
            self.selection_changed.emit()

    # -- media -----------------------------------------------------------------

    def media_for(self, media_id: str) -> MediaInfo | None:
        return self.pool.info_for(media_id)

    def import_paths(self, paths: list) -> list[MediaInfo]:
        return self.pool.add_paths(paths)

    # -- files -----------------------------------------------------------------

    @property
    def is_dirty(self) -> bool:
        return self.undo.is_dirty

    @property
    def display_name(self) -> str:
        return Path(self.path).stem if self.path else "Untitled"

    def save_to(self, path: str | Path) -> Path:
        """Write the project, storing only media the timeline actually uses."""
        used = {clip.media_id for clip in self.timeline.all_clips()}
        media = [info for info in self.pool.all_media() if info.media_id in used]
        written = save_project(Path(path), self.timeline, media, self.playhead)
        self.path = str(written)
        self.undo.mark_clean()
        return written

    def load_from(self, path: str | Path) -> LoadResult:
        """Replace this project's contents with the file's. Returns the result so
        the caller can report any media that could not be relinked."""
        result = load_project(Path(path))

        self.timebase = result.timeline.timebase
        self.timeline.timebase = result.timeline.timebase
        self.timeline.width = result.timeline.width
        self.timeline.height = result.timeline.height
        self.timeline.sample_rate = result.timeline.sample_rate
        self.timeline.tracks = result.timeline.tracks

        self.pool.remove_ids([info.media_id for info in self.pool.all_media()])
        self.pool.add_paths([info.path for info in result.media])

        self.undo.clear()
        self.undo.mark_clean()
        self.path = str(path)
        self._selected = []
        self.set_playhead(result.playhead)
        self.timeline_changed.emit()
        self.selection_changed.emit()
        return result

    def reset(self) -> None:
        """Start a new, empty project in place."""
        self.timeline.tracks = Timeline.default(self.timebase).tracks
        self.pool.remove_ids([info.media_id for info in self.pool.all_media()])
        self.undo.clear()
        self.undo.mark_clean()
        self.path = None
        self._selected = []
        self.set_playhead(0)
        self.timeline_changed.emit()
        self.selection_changed.emit()

    def shutdown(self) -> None:
        self.proxies.shutdown()
