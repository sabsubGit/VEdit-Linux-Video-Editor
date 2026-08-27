"""The project: everything one editing session holds.

A single object owns the media pool, the timeline, the undo stack and the ingest
workers, so pages don't reach into each other — they all talk to this.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal

from vedit.core.commands import UndoStack
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

    def shutdown(self) -> None:
        self.proxies.shutdown()
