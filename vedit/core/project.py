"""The project: everything one editing session holds.

A single object owns the media pool, the timeline, the undo stack and the ingest
workers, so pages don't reach into each other — they all talk to this.
"""

from __future__ import annotations

import logging
import os
from fractions import Fraction
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, Signal

from vedit.core import recovery
from vedit.core.commands import UndoStack
from vedit.core.projectfile import LoadResult, OfflineMedia, load_project, save_project
from vedit.core.timebase import STANDARD_RATES, TimeBase
from vedit.media.pool import MediaPool
from vedit.media.probe import MediaInfo, UnsupportedMedia, probe
from vedit.media.proxy import ProxyManager
from vedit.timeline.model import Timeline

log = logging.getLogger("vedit.project")

DEFAULT_SIZE = (1920, 1080)
DEFAULT_RATE = Fraction(30)
# A project never follows a clip above this. Phone slow motion is shot at 120
# or 240 fps to be played back slowly, not to set the rate of the whole edit.
MAX_MATCHED_RATE = Fraction(60)
# How far a relinked file may fall short of what its clips use before it is
# taken to be the wrong file rather than a re-export of the right one.
RELINK_SLACK_SECONDS = 0.5
# Folders are searched by file name when relinking; this stops a careless
# choice of "/" from walking the whole disk.
RELINK_SCAN_LIMIT = 50_000


class FormatError(Exception):
    """The project format cannot be changed that way."""


class RelinkError(Exception):
    """The chosen file cannot stand in for the missing one."""


def matched_rate(rate: Fraction) -> Fraction:
    """The standard rate a project should run at to suit a clip of `rate`."""
    allowed = [standard for standard in STANDARD_RATES if standard <= MAX_MATCHED_RATE]
    return min(allowed, key=lambda standard: abs(float(standard) - float(rate)))


def describe_format(width: int, height: int, fps: Fraction) -> str:
    return f"{width}×{height} at {float(fps):.5g} fps"


class Project(QObject):
    timeline_changed = Signal()
    playhead_changed = Signal(int)
    selection_changed = Signal()
    # Size or frame rate changed. Separate from `timeline_changed` because a few
    # things cache numbers derived from the rate and need to be told outright.
    format_changed = Signal()
    # A file came back online. The timeline is unchanged, but what its clips
    # can show is not, and a parked viewer has no other reason to redraw.
    media_relinked = Signal()

    def __init__(self, timebase: TimeBase | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.timebase = timebase or TimeBase(30)
        self.timeline = Timeline.default(self.timebase)
        self.proxies = ProxyManager(self)
        self.pool = MediaPool(self.proxies, self.timebase, self)
        self.undo = UndoStack(self.timeline)
        self.path: str | None = None
        # Media the project refers to that could not be found, by the id its
        # clips still carry. See `projectfile.OfflineMedia`.
        self.offline: dict[str, OfflineMedia] = {}

        self._playhead = 0
        self._selected: list[str] = []
        # True once someone has set the format by hand, after which the first
        # clip dropped no longer gets to decide it.
        self._format_chosen = False
        # Bumped on anything worth saving; the autosave compares against it so
        # an idle editor writes nothing.
        self._revision = 0
        self._autosaved_revision = 0

        self.undo.on_change(self.timeline_changed.emit)
        self.undo.on_change(self._touch)
        self.format_changed.connect(self.pool.refresh_durations)

    def _touch(self) -> None:
        self._revision += 1

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

    # -- format ----------------------------------------------------------------

    @property
    def format(self) -> tuple[int, int, Fraction]:
        return self.timeline.width, self.timeline.height, self.timebase.fps

    @property
    def can_change_rate(self) -> bool:
        """Whether the frame rate is still free to change.

        Every position on the timeline is a whole number of frames at the
        project rate, so changing the rate under existing clips would move
        every cut. An empty timeline has nothing to move.
        """
        return not any(True for _ in self.timeline.all_clips())

    def _apply_format(self, width: int, height: int, fps: Fraction) -> bool:
        """Set the format. Returns whether anything changed."""
        width, height = max(2, width - width % 2), max(2, height - height % 2)
        fps = Fraction(fps)
        if fps <= 0:
            raise FormatError("the frame rate must be positive")
        if (width, height, fps) == self.format:
            return False
        if fps != self.timebase.fps:
            if not self.can_change_rate:
                raise FormatError(
                    "The frame rate can only be changed while the timeline is empty."
                )
            # The history may still hold clips counted at the old rate even
            # though the timeline is empty; undoing into them would be wrong.
            self.undo.clear()
            # In place: the engine, the decoders and the pool all hold this
            # object, and replacing it would leave them on the old rate.
            self.timebase.fps = fps
            self.set_playhead(0)
        self.timeline.width, self.timeline.height = width, height
        return True

    def set_format(self, width: int, height: int, fps: Fraction) -> bool:
        """Change the project format by hand. Raises `FormatError` if the frame
        rate is asked to change under existing clips."""
        self._format_chosen = True
        if not self._apply_format(width, height, fps):
            return False
        self.undo.mark_dirty()
        self.format_changed.emit()
        self.timeline_changed.emit()
        return True

    def match_format(self, info: MediaInfo) -> str | None:
        """Let the first clip decide the project format.

        Called before media is placed. Does something only on an empty timeline
        whose format nobody has chosen, and only for media with a picture —
        which is the moment a person means "make a video like this one". Returns
        a description of the new format when it changed.
        """
        if self._format_chosen or info.video is None or not self.can_change_rate:
            return None
        width, height = info.video.display_size
        try:
            changed = self._apply_format(width, height, matched_rate(info.video.fps))
        except FormatError:
            return None
        if not changed:
            return None
        self.format_changed.emit()
        self.timeline_changed.emit()
        return describe_format(*self.format)

    # -- offline media ---------------------------------------------------------

    def is_offline(self, media_id: str) -> bool:
        return bool(media_id) and media_id in self.offline

    def offline_media(self) -> list[OfflineMedia]:
        """Missing files that clips on the timeline still depend on."""
        used = {clip.media_id for clip in self.timeline.all_clips()}
        return [item for item in self.offline.values() if item.media_id in used]

    def offline_clip_count(self) -> int:
        return sum(1 for clip in self.timeline.all_clips() if clip.media_id in self.offline)

    def relink(self, media_id: str, path: str | Path) -> MediaInfo:
        """Bring offline media back by pointing it at a file.

        Raises `RelinkError` when the file cannot stand in for the original: it
        is not media, it lacks the picture or sound the clips need, or it is too
        short to contain them.
        """
        if media_id not in self.offline:
            raise RelinkError("that media is not offline")
        try:
            info = probe(path)
        except UnsupportedMedia as exc:
            raise RelinkError(str(exc)) from exc

        clips = [clip for clip in self.timeline.all_clips() if clip.media_id == media_id]
        if any(clip.kind == "video" for clip in clips) and info.video is None:
            raise RelinkError(f"{info.name} has no picture, and the clips using it do")
        if any(clip.kind == "audio" for clip in clips) and info.audio is None:
            raise RelinkError(f"{info.name} has no sound, and the clips using it do")
        needed = max((clip.src_out for clip in clips), default=0)
        slack = int(RELINK_SLACK_SECONDS * float(self.timebase.fps))
        if info.frame_count(self.timebase) + slack < needed:
            raise RelinkError(
                f"{info.name} is shorter than the clips that use it "
                f"({self.timebase.frames_to_timecode(info.frame_count(self.timebase))} "
                f"against {self.timebase.frames_to_timecode(needed)})"
            )

        self.pool.add_paths([info.path])
        for clip in clips:
            clip.media_id = info.media_id
        self.undo.remap_media(media_id, info.media_id)
        del self.offline[media_id]
        self.undo.mark_dirty()
        self.timeline_changed.emit()
        self.media_relinked.emit()
        return info

    def locate(self, media_id: str, path: str | Path) -> list[str]:
        """Relink one file by hand, then look beside it for the rest.

        Footage that went missing together usually moved together, so finding
        one file is normally enough to find all of them. Returns every name
        relinked; raises `RelinkError` if the chosen file itself will not do.
        """
        info = self.relink(media_id, path)
        return [info.name] + self.relink_from_folder(Path(info.path).parent)

    def relink_from_folder(self, folder: str | Path) -> list[str]:
        """Relink everything offline that has a file of the same name under
        `folder`. Returns the names that were found."""
        # Every missing file, not only those with clips on the timeline right
        # now: a clip deleted a moment ago can be undone back into existence,
        # and should not come back still offline.
        wanted = {item.name: item for item in self.offline.values()}
        if not wanted:
            return []

        found: list[str] = []
        scanned = 0
        for root, _dirs, files in os.walk(Path(folder).expanduser()):
            for name in files:
                scanned += 1
                item = wanted.get(name)
                if item is None:
                    continue
                try:
                    self.relink(item.media_id, Path(root) / name)
                except RelinkError as exc:
                    log.info("Not relinking %s: %s", name, exc)
                    continue
                found.append(name)
                del wanted[name]
            if not wanted or scanned > RELINK_SCAN_LIMIT:
                break
        return found

    # -- files -----------------------------------------------------------------

    @property
    def is_dirty(self) -> bool:
        return self.undo.is_dirty

    @property
    def display_name(self) -> str:
        return Path(self.path).stem if self.path else "Untitled"

    def _used_media(self) -> list[MediaInfo]:
        used = {clip.media_id for clip in self.timeline.all_clips()}
        return [info for info in self.pool.all_media() if info.media_id in used]

    def save_to(self, path: str | Path) -> Path:
        """Write the project, storing only media the timeline actually uses."""
        written = save_project(
            Path(path),
            self.timeline,
            self._used_media(),
            self.playhead,
            offline=self.offline_media(),
        )
        self.path = str(written)
        self.undo.mark_clean()
        self.clear_autosave()
        return written

    def load_from(self, path: str | Path) -> LoadResult:
        """Replace this project's contents with the file's. Returns the result so
        the caller can report any media that is offline."""
        result = load_project(Path(path))

        # In place, for the reason given in `_apply_format`.
        self.timebase.fps = result.timeline.timebase.fps
        self.timeline.timebase = self.timebase
        self.timeline.width = result.timeline.width
        self.timeline.height = result.timeline.height
        self.timeline.sample_rate = result.timeline.sample_rate
        self.timeline.tracks = result.timeline.tracks
        self.timeline.master_gain_db = result.timeline.master_gain_db
        self.offline = {item.media_id: item for item in result.offline}
        # A saved project has a format somebody settled on.
        self._format_chosen = True

        self.pool.remove_ids([info.media_id for info in self.pool.all_media()])
        self.pool.add_paths([info.path for info in result.media])

        self.undo.clear()
        self.undo.mark_clean()
        self.clear_autosave()
        self.path = str(path)
        self._selected = []
        self._playhead = -1          # force the signal: the rate may have changed
        self.set_playhead(result.playhead)
        self.format_changed.emit()
        self.timeline_changed.emit()
        self.selection_changed.emit()
        return result

    def recover_from(self, autosave: recovery.Recovery) -> LoadResult:
        """Open an autosave as the unsaved state of the project it came from."""
        result = self.load_from(autosave.path)
        self.path = autosave.original
        self.undo.mark_dirty()
        # Written under this process's name before the old file goes, so there
        # is no moment at which the work exists nowhere on disk.
        self.autosave()
        recovery.discard(autosave.path)
        return result

    def reset(self) -> None:
        """Start a new, empty project in place."""
        self.timebase.fps = DEFAULT_RATE
        self.timeline.width, self.timeline.height = DEFAULT_SIZE
        self.timeline.master_gain_db = 0.0
        self.timeline.tracks = Timeline.default(self.timebase).tracks
        self.offline = {}
        self._format_chosen = False
        self.pool.remove_ids([info.media_id for info in self.pool.all_media()])
        self.undo.clear()
        self.undo.mark_clean()
        self.clear_autosave()
        self.path = None
        self._selected = []
        self.set_playhead(0)
        self.format_changed.emit()
        self.timeline_changed.emit()
        self.selection_changed.emit()

    # -- autosave --------------------------------------------------------------

    def autosave(self) -> Path | None:
        """Write unsaved work to this session's recovery file.

        Does nothing when there is nothing unsaved or nothing new since the last
        one. Never touches the project's own file, and never raises: a full disk
        must not interrupt an edit to announce that the safety net has a hole.
        """
        if not self.is_dirty:
            self.clear_autosave()
            return None
        if self._revision == self._autosaved_revision:
            return None
        target = recovery.session_file()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            save_project(
                target,
                self.timeline,
                self._used_media(),
                self.playhead,
                offline=self.offline_media(),
                extra=recovery.note(self.path),
            )
        except OSError as exc:
            log.warning("Autosave failed: %s", exc)
            return None
        self._autosaved_revision = self._revision
        return target

    def clear_autosave(self) -> None:
        """Forget this session's recovery file: the work is saved, or was
        deliberately thrown away."""
        self._autosaved_revision = self._revision
        try:
            recovery.discard(recovery.session_file())
        except OSError:
            pass

    def shutdown(self) -> None:
        # Reached only by closing the window, which has already asked about
        # unsaved changes — so whatever is left was discarded on purpose.
        self.clear_autosave()
        self.proxies.shutdown()
