"""Undo/redo.

This is snapshot-based rather than inverse-operation-based: each edit records the
whole track list before and after. Timelines are small — a few hundred clips of a
few dozen bytes — so a snapshot costs far less than the class of bugs you get from
hand-written `undo()` methods that drift out of step with their `do()`.

The important guarantee is that an edit is atomic: if a mutation raises halfway
through, the timeline is rolled back and nothing is recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TypeVar

from vedit.timeline.model import Timeline, TimelineState

T = TypeVar("T")


@dataclass(slots=True)
class Entry:
    label: str
    before: TimelineState
    after: TimelineState


class UndoStack:
    """Records edits against one `Timeline`, which it mutates in place."""

    def __init__(self, timeline: Timeline, limit: int = 200) -> None:
        self.timeline = timeline
        self.limit = limit
        self._done: list[Entry] = []
        self._undone: list[Entry] = []
        self._listeners: list[Callable[[], None]] = []
        self._depth = 0
        self._clean_at = 0

    # -- notification ----------------------------------------------------------

    def on_change(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def _notify(self) -> None:
        for callback in self._listeners:
            callback()

    # -- applying edits --------------------------------------------------------

    def apply(self, label: str, mutate: Callable[[Timeline], T]) -> T:
        """Run `mutate` against the timeline as one undoable step.

        Nested calls collapse into the outermost step, so a compound edit built
        from smaller ops still undoes as a single action.
        """
        if self._depth > 0:
            return mutate(self.timeline)

        before = self.timeline.snapshot()
        self._depth = 1
        try:
            result = mutate(self.timeline)
            self.timeline.validate()
        except Exception:
            self.timeline.restore(before)
            raise
        finally:
            self._depth = 0

        after = self.timeline.snapshot()
        self._done.append(Entry(label, before, after))
        if len(self._done) > self.limit:
            dropped = len(self._done) - self.limit
            del self._done[:dropped]
            self._clean_at -= dropped
        self._undone.clear()
        self._notify()
        return result

    # -- history ---------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._done)

    @property
    def can_redo(self) -> bool:
        return bool(self._undone)

    @property
    def undo_label(self) -> str | None:
        return self._done[-1].label if self._done else None

    @property
    def redo_label(self) -> str | None:
        return self._undone[-1].label if self._undone else None

    def undo(self) -> str | None:
        if not self._done:
            return None
        entry = self._done.pop()
        self.timeline.restore(entry.before)
        self._undone.append(entry)
        self._notify()
        return entry.label

    def redo(self) -> str | None:
        if not self._undone:
            return None
        entry = self._undone.pop()
        self.timeline.restore(entry.after)
        self._done.append(entry)
        self._notify()
        return entry.label

    def clear(self) -> None:
        self._done.clear()
        self._undone.clear()
        self._clean_at = 0
        self._notify()

    # -- unsaved-changes tracking ---------------------------------------------

    def mark_clean(self) -> None:
        """Call after saving; `is_dirty` then reports against this point."""
        self._clean_at = len(self._done)

    @property
    def is_dirty(self) -> bool:
        return len(self._done) != self._clean_at
