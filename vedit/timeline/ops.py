"""Edit operations.

Every function here mutates a `Timeline` in place and is meant to be run through
`UndoStack.apply`, which snapshots around it and rolls back if one raises.

Two conventions hold throughout:

* **Link groups move together.** An operation given one clip of a linked A/V pair
  applies to the whole pair, which is what keeps sound attached to picture.
* **Dragging clamps, placing rejects.** Trims clamp to the nearest legal value so
  a drag stops at a limit instead of throwing; explicit placements raise
  `TimelineError` so a bad move can be refused outright.
"""

from __future__ import annotations

import math
from typing import Iterable, Literal, Sequence

from vedit.core.timebase import TimeBase
from vedit.media.probe import MediaInfo
from vedit.timeline.model import Clip, Timeline, TimelineError, Track, new_id

Edge = Literal["in", "out"]


# -- building clips from media ------------------------------------------------


def make_clips(info: MediaInfo, timebase: TimeBase, *, link: bool = True) -> list[Clip]:
    """Build the clip(s) for one source file.

    A file with both streams yields two clips sharing a `link_id` — one for the
    video lane, one for the audio lane. That pairing is created at import time
    because retrofitting linkage onto existing edits is far harder.
    """
    length = timebase.seconds_to_frames_ceil(info.duration)
    if length < 1:
        raise TimelineError(f"{info.name} is shorter than one frame at the project rate")

    link_id = new_id("l") if (link and info.has_video and info.has_audio) else None
    clips: list[Clip] = []
    if info.has_video:
        clips.append(
            Clip(
                media_id=info.media_id,
                src_in=0,
                src_out=length,
                tl_start=0,
                src_length=length,
                kind="video",
                link_id=link_id,
                name=info.name,
            )
        )
    if info.has_audio:
        clips.append(
            Clip(
                media_id=info.media_id,
                src_in=0,
                src_out=length,
                tl_start=0,
                src_length=length,
                kind="audio",
                link_id=link_id,
                name=info.name,
            )
        )
    return clips


def _default_track(timeline: Timeline, kind: str) -> Track:
    for track in timeline.tracks:
        if track.kind == kind and not track.locked:
            return track
    raise TimelineError(f"there is no unlocked {kind} track to place this on")


def append_media(timeline: Timeline, info: MediaInfo) -> list[Clip]:
    """Place media at the end of the timeline.

    A linked pair must start on the same frame, so both lanes start at whichever
    is currently longer — otherwise the audio would slide out of sync the moment
    the two tracks had different lengths.
    """
    clips = make_clips(info, timeline.timebase)
    kinds = {clip.kind for clip in clips}
    tracks = {kind: _default_track(timeline, kind) for kind in kinds}
    start = max(track.duration for track in tracks.values())

    for clip in clips:
        clip.tl_start = start
        tracks[clip.kind].insert(clip)
    return clips


def place_media(timeline: Timeline, info: MediaInfo, frame: int, *, track: Track | None = None) -> list[Clip]:
    """Place media at an explicit frame, refusing an overlap."""
    if frame < 0:
        raise TimelineError("cannot place media before the start of the timeline")

    clips = make_clips(info, timeline.timebase)
    if track is not None:
        # An explicit drop target only decides the lane for its own kind; the
        # linked partner still goes to its default lane.
        targets = {track.kind: track}
    else:
        targets = {}
    for clip in clips:
        targets.setdefault(clip.kind, _default_track(timeline, clip.kind))

    for clip in clips:
        clip.tl_start = frame
        targets[clip.kind].insert(clip)
    return clips


# -- selection helpers --------------------------------------------------------


def expand_links(timeline: Timeline, clips: Iterable[Clip]) -> list[Clip]:
    """Grow a selection to include linked partners, without duplicates."""
    seen: dict[str, Clip] = {}
    for clip in clips:
        for member in timeline.linked_group(clip):
            seen.setdefault(member.clip_id, member)
    return list(seen.values())


def _assert_unlocked(timeline: Timeline, clips: Iterable[Clip]) -> None:
    for clip in clips:
        track = timeline.track_of(clip)
        if track.locked:
            raise TimelineError(f"track {track.name} is locked")


# -- razor --------------------------------------------------------------------


def razor(timeline: Timeline, frame: int, tracks: Sequence[Track] | None = None) -> list[Clip]:
    """Split every clip crossing `frame`, returning the new right-hand pieces.

    Cutting across all tracks at once is what keeps a linked pair in step: both
    halves of the pair get the same cut without any special-casing. Cutting
    exactly on a clip boundary is a no-op rather than an error.

    The right-hand pieces of a split link group are re-linked to *each other*
    under a fresh id. Letting them keep the original id would leave the left and
    right halves of a cut joined, so dragging one would drag the other and the
    cut would be pointless.
    """
    targets = list(tracks) if tracks is not None else timeline.editable_tracks()
    created: list[Clip] = []
    rebound: dict[str, str] = {}

    for track in targets:
        if track.locked:
            continue
        for clip in list(track.clips):
            if not clip.crosses(frame):
                continue
            offset = frame - clip.tl_start
            right_link = None
            if clip.link_id is not None:
                right_link = rebound.setdefault(clip.link_id, new_id("l"))
            right = Clip(
                media_id=clip.media_id,
                src_in=clip.src_in + offset,
                src_out=clip.src_out,
                tl_start=frame,
                src_length=clip.src_length,
                kind=clip.kind,
                link_id=right_link,
                name=clip.name,
                enabled=clip.enabled,
            )
            clip.src_out = clip.src_in + offset
            track.clips.append(right)
            created.append(right)
        track.sort()

    return created


# -- deletion -----------------------------------------------------------------


def lift(timeline: Timeline, clips: Iterable[Clip]) -> None:
    """Remove clips, leaving a gap. Everything else keeps its position."""
    group = expand_links(timeline, clips)
    _assert_unlocked(timeline, group)
    for clip in group:
        timeline.track_of(clip).remove(clip)


def ripple_delete(timeline: Timeline, clips: Iterable[Clip]) -> None:
    """Remove clips and close the gap, shifting later clips on *every* track.

    Shifting all tracks rather than just the affected one is the whole point: a
    ripple that moved only the video lane would slide the audio out of sync.
    """
    group = expand_links(timeline, clips)
    if not group:
        return
    _assert_unlocked(timeline, group)

    span_start = min(clip.tl_start for clip in group)
    span_end = max(clip.tl_end for clip in group)
    length = span_end - span_start

    for clip in group:
        timeline.track_of(clip).remove(clip)

    for track in timeline.tracks:
        for clip in track.clips:
            if clip.tl_start >= span_end:
                clip.tl_start -= length
        track.sort()


def ripple_delete_range(timeline: Timeline, start: int, end: int) -> None:
    """Cut at both ends of a span, delete what is inside, and close the gap."""
    if end <= start:
        return
    razor(timeline, start)
    razor(timeline, end)

    doomed = [
        clip
        for track in timeline.editable_tracks()
        for clip in track.clips
        if clip.tl_start >= start and clip.tl_end <= end
    ]
    if not doomed:
        return

    for clip in doomed:
        timeline.track_of(clip).remove(clip)

    length = end - start
    for track in timeline.tracks:
        for clip in track.clips:
            if clip.tl_start >= end:
                clip.tl_start -= length
        track.sort()


# -- moving -------------------------------------------------------------------


def move_clips(
    timeline: Timeline,
    clips: Iterable[Clip],
    delta: int,
    *,
    target_track: Track | None = None,
) -> None:
    """Shift clips along their tracks by `delta` frames.

    Raises rather than clamping: a drop onto occupied space should be refused so
    the clip snaps back, not silently land somewhere the user did not point at.
    """
    group = expand_links(timeline, clips)
    if not group:
        return
    _assert_unlocked(timeline, group)

    earliest = min(clip.tl_start for clip in group)
    if earliest + delta < 0:
        # Clamp against the start of the timeline; dragging left into the void is
        # a normal gesture and should stop at zero rather than fail.
        delta = -earliest

    moving_ids = {clip.clip_id for clip in group}
    moves: list[tuple[Clip, Track, Track, int]] = []

    for clip in group:
        source_track = timeline.track_of(clip)
        destination = source_track
        if target_track is not None and target_track.kind == clip.kind:
            destination = target_track
        if destination.locked:
            raise TimelineError(f"track {destination.name} is locked")

        start = clip.tl_start + delta
        end = start + clip.duration
        for existing in destination.clips:
            if existing.clip_id in moving_ids:
                continue
            if existing.overlaps(start, end):
                raise TimelineError(
                    f"cannot move {clip.name or 'clip'} there: "
                    f"{existing.name or existing.clip_id} is in the way"
                )
        moves.append((clip, source_track, destination, start))

    for clip, source_track, destination, start in moves:
        if source_track is not destination:
            source_track.remove(clip)
        clip.tl_start = start

    for _, source_track, destination, _ in moves:
        source_track.sort()
        destination.sort()

    for clip, source_track, destination, _ in moves:
        if destination.clip_by_id(clip.clip_id) is None:
            destination.clips.append(clip)
            destination.sort()


def move_clips_to(timeline: Timeline, clips: Iterable[Clip], frame: int, **kwargs) -> None:
    """Move a selection so its earliest clip lands on `frame`."""
    group = list(clips)
    if not group:
        return
    earliest = min(clip.tl_start for clip in group)
    move_clips(timeline, group, frame - earliest, **kwargs)


# -- trimming -----------------------------------------------------------------


def _delta_bounds(timeline: Timeline, clip: Clip, edge: Edge) -> tuple[int, int]:
    """Legal range of movement for one edge of one clip.

    Bounded by three things at once: how much unused source exists beyond the
    edge, the neighbouring clip on the same track, and the one-frame minimum
    length.
    """
    track = timeline.track_of(clip)
    before, after = track.neighbours(clip)

    if edge == "in":
        # Negative delta drags the in-point earlier, which needs head material.
        from_source = -clip.head_room
        from_neighbour = (before.tl_end if before else 0) - clip.tl_start
        low = max(from_source, from_neighbour)
        high = clip.duration - 1
    else:
        low = -(clip.duration - 1)
        from_source = clip.tail_room
        from_neighbour = (after.tl_start - clip.tl_end) if after else math.inf
        high = int(min(from_source, from_neighbour))
    return low, high


def trim(timeline: Timeline, clip: Clip, edge: Edge, new_frame: int) -> int:
    """Move one edge of a clip to `new_frame`, clamped to what is legal.

    Returns the frame the edge actually landed on. Clamping rather than raising
    is what makes dragging a trim handle feel right — it stops against the limit.
    """
    group = expand_links(timeline, [clip])
    _assert_unlocked(timeline, group)

    anchor = clip.tl_start if edge == "in" else clip.tl_end
    desired = new_frame - anchor

    low, high = -math.inf, math.inf
    for member in group:
        member_low, member_high = _delta_bounds(timeline, member, edge)
        low = max(low, member_low)
        high = min(high, member_high)

    if low > high:
        return anchor
    delta = int(max(low, min(high, desired)))

    for member in group:
        if edge == "in":
            member.src_in += delta
            member.tl_start += delta
        else:
            member.src_out += delta

    for track in timeline.tracks:
        track.sort()
    return anchor + delta


def trim_bounds(timeline: Timeline, clip: Clip, edge: Edge) -> tuple[int, int]:
    """Frame range this edge can be dragged to — for showing limits in the UI."""
    group = expand_links(timeline, [clip])
    low, high = -math.inf, math.inf
    for member in group:
        member_low, member_high = _delta_bounds(timeline, member, edge)
        low = max(low, member_low)
        high = min(high, member_high)

    anchor = clip.tl_start if edge == "in" else clip.tl_end
    if low > high:
        return anchor, anchor
    return anchor + int(low), anchor + int(high)


# -- linking ------------------------------------------------------------------


def unlink(timeline: Timeline, clip: Clip) -> list[Clip]:
    """Break a link group so its members can be edited apart."""
    group = timeline.linked_group(clip)
    for member in group:
        member.link_id = None
    return group


def link(timeline: Timeline, clips: Sequence[Clip]) -> str | None:
    """Join clips into one link group."""
    if len(clips) < 2:
        return None
    link_id = new_id("l")
    for clip in clips:
        clip.link_id = link_id
    return link_id


# -- gaps ---------------------------------------------------------------------


def close_gap(timeline: Timeline, track: Track, frame: int) -> bool:
    """Pull everything after the gap at `frame` back to close it.

    Ripples all tracks, for the same sync reason as `ripple_delete`.
    """
    for start, end in track.gaps():
        if start <= frame < end:
            length = end - start
            for other in timeline.tracks:
                for clip in other.clips:
                    if clip.tl_start >= end:
                        clip.tl_start -= length
                other.sort()
            return True
    return False
