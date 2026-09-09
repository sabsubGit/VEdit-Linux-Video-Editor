"""Flattening the timeline into a playlist the decoder can walk.

The decoder should not have to understand tracks, links or selection — it only
needs "at timeline frame N, read this file from this offset". Producing that list
here keeps the threaded code, which is the part that is hard to debug, as simple
as possible.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, replace
from pathlib import Path

from vedit.core.timebase import TimeBase
from vedit.media.pool import MediaPool
from vedit.media.proxy import ProxyManager
from vedit.timeline.levels import from_db
from vedit.timeline.model import Clip, Timeline, Track


@dataclass(frozen=True, slots=True)
class Segment:
    """One continuous read from one file, covering [tl_start, tl_end)."""

    tl_start: int
    tl_end: int
    path: Path | None      # None means a gap: black video / silence
    src_start: int         # offset into the source, in timeline frames
    media_id: str = ""
    speed: float = 1.0
    clip_id: str = ""
    # Read the source backwards. `src_start` still means "the source position
    # playing at tl_start" — for a reversed segment that is the *far* edge of the
    # window, and time walks down from it rather than up.
    reversed: bool = False
    # Level shaping, carried here so the audio thread never has to look a clip
    # up. Gain is linear rather than dB: the mixer applies it once per block and
    # has no business doing a pow() in the hot path.
    gain: float = 1.0
    fade_in: int = 0       # timeline frames from tl_start
    fade_out: int = 0      # timeline frames ending at tl_end
    # How far into a cross dissolve this segment sits, so the viewer can mix
    # the outgoing shot under it. Zero for everything that is not a transition.
    dissolve: int = 0
    dissolve_from: int = 0   # timeline frame the dissolve began at

    @property
    def duration(self) -> int:
        return self.tl_end - self.tl_start

    @property
    def is_gap(self) -> bool:
        return self.path is None

    def source_seconds(self, timeline_frame: int, timebase: TimeBase) -> float:
        """Where to seek in the source for a given timeline position."""
        played = timeline_frame - self.tl_start
        if self.speed != 1.0:
            played = int(round(played * self.speed))
        if self.reversed:
            played = -played
        return float(timebase.frames_to_seconds(self.src_start + played))

    def timeline_frame_for(self, source_seconds: float, timebase: TimeBase) -> int:
        """Inverse of `source_seconds`: which timeline frame a decoded frame is for."""
        source_frame = timebase.seconds_to_frames(source_seconds) - self.src_start
        if self.reversed:
            source_frame = -source_frame
        if self.speed != 1.0:
            source_frame = source_frame / self.speed
        return self.tl_start + int(round(source_frame))


class Playlist:
    """An ordered, gap-filled segment list with a fast lookup by frame."""

    def __init__(self, segments: list[Segment], duration: int, track_id: str = "") -> None:
        self.segments = segments
        self.duration = duration
        # Which lane this came from. The mixer keys live fader positions and
        # meter readings by it, so a playlist that has lost its lane identity is
        # a strip that cannot be found.
        self.track_id = track_id
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
        return Playlist([], duration, track.track_id if track is not None else "")

    segments: list[Segment] = []
    cursor = 0

    for clip in track.clips:
        if clip.tl_start > cursor:
            segments.append(Segment(cursor, clip.tl_start, None, 0))

        info = pool.info_for(clip.media_id)
        path = None
        # A muted audio clip is turned into a gap rather than decoded and
        # multiplied by zero: the segment still occupies its span, so everything
        # downstream stays aligned, and nothing is read from disk for it.
        playable = clip.audible if clip.kind == "audio" else clip.enabled
        if info is not None and playable and not track.muted:
            path = proxies.playback_path(info) if use_proxies else info.path

        segments.append(
            Segment(
                tl_start=clip.tl_start,
                tl_end=clip.tl_end,
                path=path,
                src_start=clip.source_frame_at(clip.tl_start),
                media_id=clip.media_id,
                speed=clip.speed,
                clip_id=clip.clip_id,
                reversed=clip.reversed,
                gain=from_db(clip.gain_db) if clip.kind == "audio" else 1.0,
                fade_in=clip.fade_in if clip.kind == "audio" else 0,
                fade_out=clip.fade_out if clip.kind == "audio" else 0,
            )
        )
        cursor = clip.tl_end

    if cursor < duration:
        segments.append(Segment(cursor, duration, None, 0))

    return Playlist(segments, duration, track.track_id)


def build_video_playlist(
    timeline: Timeline,
    pool: MediaPool,
    proxies: ProxyManager,
    *,
    use_proxies: bool = True,
) -> Playlist:
    """Flatten every video lane into one playlist, topmost lane winning.

    v1 has no opacity or transitions, so overlapping video is pure occlusion:
    whatever is on the highest lane at a given frame is what you see. Splitting
    the timeline at every clip edge across all lanes and then asking "who is on
    top here?" gives exactly that, and collapses to the single-track case for
    free when only V1 is used.
    """
    duration = timeline.duration
    lanes = [t for t in timeline.video_tracks if not t.muted]
    if not lanes or duration <= 0:
        return Playlist([Segment(0, duration, None, 0)] if duration > 0 else [], duration)

    # Every clip edge is a point where the winning lane can change.
    edges = {0, duration}
    for track in lanes:
        for clip in track.clips:
            edges.add(max(0, min(clip.tl_start, duration)))
            edges.add(max(0, min(clip.tl_end, duration)))
            found = track.dissolve_before(clip)
            if found is not None:
                edges.add(max(0, min(clip.tl_start + found[1], duration)))
    boundaries = sorted(edges)

    segments: list[Segment] = []
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        winner: Clip | None = None
        winning_track: Track | None = None
        # Later lanes are higher, so the last match wins. A title is skipped:
        # it is words drawn over a picture, not a picture, so it must not
        # occlude the lane below the way a shot would.
        for track in lanes:
            found = track.clip_at(start)
            if found is not None and found.enabled and not found.is_title:
                winner, winning_track = found, track

        if winner is None:
            segments.append(Segment(start, end, None, 0))
            continue

        info = pool.info_for(winner.media_id)
        path = None
        if info is not None:
            path = proxies.playback_path(info) if use_proxies else info.path

        dissolve = 0
        found = winning_track.dissolve_before(winner) if winning_track else None
        if found is not None and start < winner.tl_start + found[1]:
            dissolve = found[1]

        segments.append(
            Segment(
                tl_start=start,
                tl_end=end,
                path=path,
                # The visible span may start partway into the clip, so the source
                # offset has to be measured from where this segment begins.
                src_start=winner.source_frame_at(start),
                media_id=winner.media_id,
                speed=winner.speed,
                clip_id=winner.clip_id,
                reversed=winner.reversed,
                dissolve=dissolve,
                dissolve_from=winner.tl_start,
            )
        )

    return Playlist(_merge_adjacent(segments), duration)


def _consumed(segment: Segment) -> int:
    """Signed source frames a segment gets through — negative when reversed."""
    span = int(round((segment.tl_end - segment.tl_start) * segment.speed))
    return -span if segment.reversed else span


def _merge_adjacent(segments: list[Segment]) -> list[Segment]:
    """Join segments that are really one continuous read.

    Splitting at every edge across every lane produces neighbouring pieces of
    the same clip; merging them back keeps the decoder from re-seeking a file it
    is already positioned in.
    """
    merged: list[Segment] = []
    for segment in segments:
        if merged:
            last = merged[-1]
            continuous = (
                last.tl_end == segment.tl_start
                and last.path == segment.path
                and last.media_id == segment.media_id
                and last.speed == segment.speed
                and last.reversed == segment.reversed
                # Same clip, not merely the same file: two pieces of one clip can
                # be rejoined, two different clips of the same source cannot,
                # because they may carry different gain or fades.
                and last.clip_id == segment.clip_id
                # A transition looks different from the rest of its own clip,
                # so the span it covers has to stay its own segment.
                and last.dissolve == segment.dissolve
                and (
                    segment.is_gap
                    or last.src_start + _consumed(last) == segment.src_start
                )
            )
            if continuous:
                merged[-1] = replace(last, tl_end=segment.tl_end)
                continue
        merged.append(segment)
    return merged


def audio_playlists(
    timeline: Timeline,
    pool: MediaPool,
    proxies: ProxyManager,
    *,
    use_proxies: bool = True,
) -> list[Playlist]:
    """One playlist per audible audio lane, for the mixer to sum.

    `audible_audio_tracks` rather than a local mute test, so solo means the same
    thing here as it does in the render graph.
    """
    return [
        build_playlist(timeline, track, pool, proxies, use_proxies=use_proxies)
        for track in timeline.audible_audio_tracks()
        if track.clips
    ]


def build_dissolve_playlist(
    timeline: Timeline,
    pool: MediaPool,
    proxies: ProxyManager,
    *,
    use_proxies: bool = True,
) -> Playlist:
    """The *outgoing* half of every cross dissolve, and gaps everywhere else.

    A second playlist rather than something folded into the first, because a
    dissolve is the one moment two shots are on screen at once and the decoder
    reads one file at a time. Fed to its own decoder, this one sits idle on gaps
    for the whole timeline except during transitions — so the cost is paid only
    where there is actually a transition to show.

    Each segment reads the outgoing clip *past its own out-point*, into the
    unused source the dissolve is spending. `source_frame_at` maps that without
    needing to know it is being asked for something beyond the clip's end.
    """
    duration = timeline.duration
    segments: list[Segment] = []
    cursor = 0

    spans: list[tuple[int, int, Clip]] = []
    for track in timeline.video_tracks:
        if track.muted:
            continue
        for clip in track.clips:
            if not clip.enabled:
                continue
            found = track.dissolve_before(clip)
            if found is None:
                continue
            outgoing, frames = found
            if outgoing.enabled:
                spans.append((clip.tl_start, clip.tl_start + frames, outgoing))
    spans.sort()

    for start, end, outgoing in spans:
        if start < cursor:
            # Two dissolves cannot overlap on one lane, but two lanes can each
            # have one at the same moment. Only the first is shown, matching
            # the render, which mixes into whichever clip is on top.
            continue
        if start > cursor:
            segments.append(Segment(cursor, start, None, 0))
        info = pool.info_for(outgoing.media_id)
        path = None
        if info is not None:
            path = proxies.playback_path(info) if use_proxies else info.path
        segments.append(
            Segment(
                tl_start=start,
                tl_end=end,
                path=path,
                src_start=outgoing.source_frame_at(outgoing.tl_end),
                media_id=outgoing.media_id,
                speed=outgoing.speed,
                clip_id=outgoing.clip_id,
                reversed=outgoing.reversed,
            )
        )
        cursor = end

    if cursor < duration:
        segments.append(Segment(cursor, duration, None, 0))
    return Playlist(segments, duration)
