"""The timeline data model.

Plain Python: this module imports nothing from Qt and nothing from FFmpeg. That
is on purpose — it is the part worth testing exhaustively, and every UI bug later
gets blamed on it, so it needs to be inspectable without a running application.

**All times are integer frames in the project timebase.** A clip's `src_in` is an
offset into its source measured in *timeline* frames, not source frames. Cutting a
25 fps source into a 30 fps timeline therefore needs no rescaling anywhere: the
source is addressed by time (`src_in / project_fps` seconds), which is exactly the
unit FFmpeg's `trim` filter and PyAV's `seek` both want.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Literal

from vedit.core.timebase import TimeBase

TrackKind = Literal["video", "audio"]

_ids = itertools.count(1)


def new_id(prefix: str) -> str:
    return f"{prefix}{next(_ids)}"


class TimelineError(Exception):
    """An edit was rejected. The message is written to be shown to a user."""


@dataclass(slots=True)
class Clip:
    """One piece of source media placed on a track.

    `src_in`/`src_out` bound the visible window into the source; `src_length` is
    how much source exists in total, which is what stops a trim from extending a
    clip past the end of its own media.
    """

    media_id: str
    src_in: int
    src_out: int          # exclusive
    tl_start: int
    src_length: int
    kind: TrackKind = "video"
    link_id: str | None = None
    clip_id: str = field(default_factory=lambda: new_id("c"))
    name: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        if self.src_in < 0:
            raise TimelineError(f"clip {self.name or self.clip_id}: src_in must not be negative")
        if self.src_out <= self.src_in:
            raise TimelineError(f"clip {self.name or self.clip_id}: must be at least one frame long")
        if self.src_out > self.src_length:
            raise TimelineError(
                f"clip {self.name or self.clip_id}: src_out {self.src_out} exceeds "
                f"source length {self.src_length}"
            )
        if self.tl_start < 0:
            raise TimelineError(f"clip {self.name or self.clip_id}: cannot start before zero")

    # -- geometry --------------------------------------------------------------

    @property
    def duration(self) -> int:
        return self.src_out - self.src_in

    @property
    def tl_end(self) -> int:
        """Exclusive: a clip at 0 lasting 10 frames occupies 0..9 and ends at 10."""
        return self.tl_start + self.duration

    def covers(self, frame: int) -> bool:
        return self.tl_start <= frame < self.tl_end

    def crosses(self, frame: int) -> bool:
        """True if `frame` falls strictly inside — cutting at an edge does nothing."""
        return self.tl_start < frame < self.tl_end

    def overlaps(self, start: int, end: int) -> bool:
        return self.tl_start < end and start < self.tl_end

    def source_frame_at(self, timeline_frame: int) -> int:
        """Which source frame is showing at a given timeline position."""
        return self.src_in + (timeline_frame - self.tl_start)

    # -- headroom for trimming -------------------------------------------------

    @property
    def head_room(self) -> int:
        """Frames of unused source before the in-point."""
        return self.src_in

    @property
    def tail_room(self) -> int:
        """Frames of unused source after the out-point."""
        return self.src_length - self.src_out

    def copy(self) -> Clip:
        return replace(self)

    def with_new_id(self) -> Clip:
        return replace(self, clip_id=new_id("c"))


@dataclass(slots=True)
class Track:
    """An ordered, non-overlapping lane of clips.

    v1 has no transitions, so overlap is invalid state rather than a thing to
    render. `insert` enforces that, which keeps every consumer simpler.
    """

    kind: TrackKind
    name: str
    clips: list[Clip] = field(default_factory=list)
    muted: bool = False
    locked: bool = False
    track_id: str = field(default_factory=lambda: new_id("t"))

    def __iter__(self) -> Iterator[Clip]:
        return iter(self.clips)

    def __len__(self) -> int:
        return len(self.clips)

    @property
    def duration(self) -> int:
        return self.clips[-1].tl_end if self.clips else 0

    def sort(self) -> None:
        self.clips.sort(key=lambda clip: clip.tl_start)

    def clip_at(self, frame: int) -> Clip | None:
        for clip in self.clips:
            if clip.covers(frame):
                return clip
        return None

    def clip_by_id(self, clip_id: str) -> Clip | None:
        for clip in self.clips:
            if clip.clip_id == clip_id:
                return clip
        return None

    def index_of(self, clip: Clip) -> int:
        for index, candidate in enumerate(self.clips):
            if candidate.clip_id == clip.clip_id:
                return index
        raise TimelineError(f"clip {clip.clip_id} is not on track {self.name}")

    def clips_in_range(self, start: int, end: int) -> list[Clip]:
        return [clip for clip in self.clips if clip.overlaps(start, end)]

    def neighbours(self, clip: Clip) -> tuple[Clip | None, Clip | None]:
        index = self.index_of(clip)
        before = self.clips[index - 1] if index > 0 else None
        after = self.clips[index + 1] if index + 1 < len(self.clips) else None
        return before, after

    def would_overlap(self, start: int, end: int, *, ignoring: str | None = None) -> Clip | None:
        """The first clip that would collide with the span, if any."""
        for clip in self.clips:
            if ignoring is not None and clip.clip_id == ignoring:
                continue
            if clip.overlaps(start, end):
                return clip
        return None

    def insert(self, clip: Clip) -> None:
        collision = self.would_overlap(clip.tl_start, clip.tl_end)
        if collision is not None:
            raise TimelineError(
                f"cannot place {clip.name or 'clip'} at frame {clip.tl_start}: "
                f"it would overlap {collision.name or collision.clip_id}"
            )
        self.clips.append(clip)
        self.sort()

    def remove(self, clip: Clip) -> int:
        index = self.index_of(clip)
        self.clips.pop(index)
        return index

    def append_at_end(self, clip: Clip) -> None:
        """Place at the current tail — what dropping media on a track does."""
        clip.tl_start = self.duration
        self.clips.append(clip)

    def gaps(self, until: int | None = None) -> list[tuple[int, int]]:
        """Empty spans as (start, end) pairs. The renderer fills these with black
        or silence, so it needs them explicitly."""
        spans: list[tuple[int, int]] = []
        cursor = 0
        for clip in self.clips:
            if clip.tl_start > cursor:
                spans.append((cursor, clip.tl_start))
            cursor = max(cursor, clip.tl_end)
        if until is not None and cursor < until:
            spans.append((cursor, until))
        return spans

    def validate(self) -> None:
        previous: Clip | None = None
        for clip in self.clips:
            if clip.kind != self.kind:
                raise TimelineError(
                    f"{clip.kind} clip {clip.clip_id} is on {self.kind} track {self.name}"
                )
            if previous is not None and clip.tl_start < previous.tl_end:
                raise TimelineError(
                    f"clips {previous.clip_id} and {clip.clip_id} overlap on {self.name}"
                )
            previous = clip

    def copy(self) -> Track:
        return Track(
            kind=self.kind,
            name=self.name,
            clips=[clip.copy() for clip in self.clips],
            muted=self.muted,
            locked=self.locked,
            track_id=self.track_id,
        )


@dataclass(slots=True)
class Timeline:
    """Tracks plus the project format that everything is conformed to."""

    timebase: TimeBase = field(default_factory=lambda: TimeBase(30))
    width: int = 1920
    height: int = 1080
    sample_rate: int = 48000
    tracks: list[Track] = field(default_factory=list)

    @classmethod
    def default(cls, timebase: TimeBase | None = None, width: int = 1920, height: int = 1080) -> Timeline:
        """A fresh timeline with one video and one audio lane."""
        return cls(
            timebase=timebase or TimeBase(30),
            width=width,
            height=height,
            tracks=[Track(kind="video", name="V1"), Track(kind="audio", name="A1")],
        )

    # -- lookups ---------------------------------------------------------------

    @property
    def video_tracks(self) -> list[Track]:
        return [track for track in self.tracks if track.kind == "video"]

    @property
    def audio_tracks(self) -> list[Track]:
        return [track for track in self.tracks if track.kind == "audio"]

    @property
    def duration(self) -> int:
        return max((track.duration for track in self.tracks), default=0)

    def track_by_id(self, track_id: str) -> Track:
        for track in self.tracks:
            if track.track_id == track_id:
                return track
        raise TimelineError(f"no track with id {track_id}")

    def track_of(self, clip: Clip | str) -> Track:
        clip_id = clip if isinstance(clip, str) else clip.clip_id
        for track in self.tracks:
            if track.clip_by_id(clip_id) is not None:
                return track
        raise TimelineError(f"clip {clip_id} is not on any track")

    def find(self, clip_id: str) -> tuple[Track, Clip]:
        for track in self.tracks:
            clip = track.clip_by_id(clip_id)
            if clip is not None:
                return track, clip
        raise TimelineError(f"no clip with id {clip_id}")

    def all_clips(self) -> Iterator[Clip]:
        for track in self.tracks:
            yield from track.clips

    def linked_group(self, clip: Clip) -> list[Clip]:
        """A clip and its linked partners.

        This is how "separate audio and video tracks" behaves: importing a file
        with both streams makes two clips sharing a `link_id`, and edits move them
        together until the user unlinks.
        """
        if clip.link_id is None:
            return [clip]
        return [other for other in self.all_clips() if other.link_id == clip.link_id]

    def editable_tracks(self) -> list[Track]:
        return [track for track in self.tracks if not track.locked]

    def media_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for clip in self.all_clips():
            seen.setdefault(clip.media_id, None)
        return list(seen)

    # -- whole-timeline state --------------------------------------------------

    def validate(self) -> None:
        for track in self.tracks:
            track.validate()

    def snapshot(self) -> list[Track]:
        """Deep copy of the track list, used by the undo stack."""
        return [track.copy() for track in self.tracks]

    def restore(self, snapshot: Iterable[Track]) -> None:
        """Replace contents in place, so UI holding this object stays valid."""
        self.tracks = [track.copy() for track in snapshot]

    def seconds(self, frames: int) -> float:
        """Frames to seconds — only for handing across the FFmpeg boundary."""
        return float(self.timebase.frames_to_seconds(frames))
