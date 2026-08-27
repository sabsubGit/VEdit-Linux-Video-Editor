"""Flattening the timeline into a playlist the decoder can walk.

The decoder should not have to understand tracks, links or selection — it only
needs "at timeline frame N, read this file from this offset". Producing that list
here keeps the threaded code, which is the part that is hard to debug, as simple
as possible.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from pathlib import Path

from vedit.core.timebase import TimeBase
from vedit.media.pool import MediaPool
from vedit.media.proxy import ProxyManager
from vedit.timeline.model import Timeline, Track


@dataclass(frozen=True, slots=True)
class Segment:
    """One continuous read from one file, covering [tl_start, tl_end)."""

    tl_start: int
    tl_end: int
    path: Path | None      # None means a gap: black video / silence
    src_start: int         # offset into the source, in timeline frames
    media_id: str = ""

    @property
    def duration(self) -> int:
        return self.tl_end - self.tl_start

    @property
    def is_gap(self) -> bool:
        return self.path is None

    def source_seconds(self, timeline_frame: int, timebase: TimeBase) -> float:
        """Where to seek in the source for a given timeline position."""
        offset = self.src_start + (timeline_frame - self.tl_start)
        return float(timebase.frames_to_seconds(offset))


class Playlist:
    """An ordered, gap-filled segment list with a fast lookup by frame."""

    def __init__(self, segments: list[Segment], duration: int) -> None:
        self.segments = segments
        self.duration = duration
        self._starts = [segment.tl_start for segment in segments]

    def __len__(self) -> int:
        return len(self.segments)

    def __bool__(self) -> bool:
        return bool(self.segments)

    def index_at(self, frame: int) -> int:
        """Index of the segment covering `frame`, or -1 past the end."""
        if not self.segments or frame < 0 or frame >= self.duration:
            return -1
        position = bisect.bisect_right(self._starts, frame) - 1
        return position if position >= 0 else -1

    def at(self, frame: int) -> Segment | None:
        index = self.index_at(frame)
        return self.segments[index] if index >= 0 else None

    def after(self, index: int) -> Segment | None:
        return self.segments[index + 1] if 0 <= index + 1 < len(self.segments) else None


def build_playlist(
    timeline: Timeline,
    track: Track | None,
    pool: MediaPool,
    proxies: ProxyManager,
    *,
    use_proxies: bool = True,
) -> Playlist:
    """Turn one track into a playlist, inserting explicit gap segments.

    Gaps are represented rather than skipped so the player renders black and
    silence for them instead of jumping — a gap is part of the edit, not an
    absence of one.
    """
    duration = timeline.duration
    if track is None or duration <= 0:
        return Playlist([], duration)

    segments: list[Segment] = []
    cursor = 0

    for clip in track.clips:
        if clip.tl_start > cursor:
            segments.append(Segment(cursor, clip.tl_start, None, 0))

        info = pool.info_for(clip.media_id)
        path = None
        if info is not None and clip.enabled and not track.muted:
            path = proxies.playback_path(info) if use_proxies else info.path

        segments.append(
            Segment(
                tl_start=clip.tl_start,
                tl_end=clip.tl_end,
                path=path,
                src_start=clip.src_in,
                media_id=clip.media_id,
            )
        )
        cursor = clip.tl_end

    if cursor < duration:
        segments.append(Segment(cursor, duration, None, 0))

    return Playlist(segments, duration)
