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
from dataclasses import replace
from typing import Iterable, Literal, Sequence

from vedit.core.timebase import TimeBase
from vedit.media.probe import MediaInfo
from vedit.timeline.framing import ROTATIONS, Framing, Region
from vedit.timeline.titles import Title
from vedit.timeline.model import (
    MAX_GAIN_DB,
    MAX_SPEED,
    MIN_GAIN_DB,
    MIN_SPEED,
    Clip,
    Timeline,
    TimelineError,
    Track,
    new_id,
)

Edge = Literal["in", "out"]

# Spare source a title pretends to have at each end, in frames. A title is drawn
# rather than read, so the true answer is "as much as you like"; a finite number
# keeps the trim arithmetic in integers, and an hour at 60 fps is longer than
# anyone will drag a caption.
TITLE_ROOM = 216_000


# -- building clips from media ------------------------------------------------


def make_clips(info: MediaInfo, timebase: TimeBase, *, link: bool = True) -> list[Clip]:
    """Build the clip(s) for one source file.

    A file with both streams yields two clips sharing a `link_id` — one for the
    video lane, one for the audio lane. That pairing is created at import time
    because retrofitting linkage onto existing edits is far harder.
    """
    length = timebase.source_frames(info.duration)
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
            # With a speed change the timeline offset and the source offset are
            # different distances; splitting on the raw offset would move the cut.
            source_offset = clip.source_span_for(frame - clip.tl_start)
            right_link = None
            if clip.link_id is not None:
                right_link = rebound.setdefault(clip.link_id, new_id("l"))
            # A reversed clip reads its window from the top down, so the piece
            # that plays *second* is the lower part of the source, not the upper.
            # Cutting on the raw offset would swap the two halves around.
            if clip.reversed:
                boundary = clip.src_out - source_offset
                right_window = (clip.src_in, boundary)
            else:
                boundary = clip.src_in + source_offset
                right_window = (boundary, clip.src_out)
            # A moving framing is *divided* at the cut rather than copied to
            # both halves: each half keeps the part of the move it covered, so
            # cutting a push in leaves the picture doing exactly what it did
            # before. Measured before the window is narrowed below, because
            # that changes the clip's duration and so its progress.
            at_cut = clip.framing_at(frame)
            left_end = clip.framing_end if clip.moves else None
            right_start = at_cut if clip.moves else clip.framing
            # A zoom region is divided the same way, and for the same reason: a
            # punch-in that spans the cut has to go on doing exactly what it was
            # doing. Each half keeps its own share, ramps included, and a region
            # falling wholly on one side goes only to that side.
            left_zoom, right_zoom = _split_region(clip.zoom, frame - clip.tl_start)

            # Everything else about the clip except its window carries across,
            # so a cut is only ever a cut. Fades are the deliberate exception —
            # they belong to the clip's own edges, and a right-hand half that
            # restarted its fade in would be a fade in the middle of a shot.
            right = Clip(
                media_id=clip.media_id,
                src_in=right_window[0],
                src_out=right_window[1],
                tl_start=frame,
                src_length=clip.src_length,
                kind=clip.kind,
                speed=clip.speed,
                link_id=right_link,
                name=clip.name,
                enabled=clip.enabled,
                reversed=clip.reversed,
                muted=clip.muted,
                gain_db=clip.gain_db,
                title=clip.title,
                framing=right_start,
                framing_end=left_end,
                zoom=right_zoom,
                # Not the dissolve: it belongs to this clip's head, and the
                # right-hand half's head is the cut, which is not a transition.
                rotation=clip.rotation,
                flipped=clip.flipped,
            )
            if left_end is not None:
                clip.framing_end = at_cut
            clip.zoom = left_zoom
            if clip.reversed:
                clip.src_in = boundary
            else:
                clip.src_out = boundary
            track.clips.append(right)
            created.append(right)
        track.sort()

    return created


def _split_region(region: Region | None, at: int) -> tuple[Region | None, Region | None]:
    """One zoom region divided by a cut `at` frames into the clip.

    The ramps go with the edges they belong to: the half holding the region's
    start keeps the ramp in, the half holding its end keeps the ramp out, and
    the raw edge each half gains at the cut has no ramp — which is right,
    because at that frame the zoom is already part-way in and must not restart.
    """
    if region is None:
        return None, None
    if at <= region.start:
        return None, replace(region, start=region.start - at, end=region.end - at)
    if at >= region.end:
        return region, None

    left = Region(
        region.framing,
        start=region.start,
        end=at,
        ramp_in=min(region.ramp_in, at - region.start),
        ramp_out=0,
    )
    right = Region(
        region.framing,
        start=0,
        end=region.end - at,
        ramp_in=0,
        ramp_out=min(region.ramp_out, region.end - at),
    )
    return left, right


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

    # Headroom is measured in source frames; a delta is in timeline frames. At
    # 2x, 100 spare source frames only buy 50 frames of timeline.
    #
    # `head_room`/`tail_room` are properties of the *source* window. A reversed
    # clip plays that window backwards, so the material sitting before its first
    # played frame is the source's tail, and the two swap.
    speed = clip.speed or 1.0
    head, tail = clip.head_room, clip.tail_room
    if clip.reversed:
        head, tail = tail, head
    if clip.is_title:
        # A title is generated, not read: there is always more of it, so its
        # length is whatever it is dragged to. Without this its source window —
        # which `add_title` sizes to exactly the default length — reads as a
        # clip with no spare footage, and the handles refuse to lengthen it.
        head = tail = TITLE_ROOM
    head_room_tl = int(head / speed)
    tail_room_tl = int(tail / speed)

    if edge == "in":
        # Negative delta drags the in-point earlier, which needs head material.
        from_source = -head_room_tl
        from_neighbour = (before.tl_end if before else 0) - clip.tl_start
        low = max(from_source, from_neighbour)
        high = clip.duration - 1
    else:
        low = -(clip.duration - 1)
        from_neighbour = (after.tl_start - clip.tl_end) if after else math.inf
        high = int(min(tail_room_tl, from_neighbour))
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
        source_delta = member.source_span_for(delta)
        if member.is_title:
            # The window is synthetic, so it is resized to the new length rather
            # than consumed. Written in terms of duration rather than by moving
            # an edge of the window: a title that has been cut in two carries a
            # window starting partway in, and re-anchoring that at zero without
            # accounting for the offset would lengthen the clip by it.
            length = member.duration + (-delta if edge == "in" else delta)
            member.src_in = 0
            member.src_out = member.src_length = member.source_span_for(length)
            if edge == "in":
                member.tl_start += delta
            member.clamp_to_length()
            continue
        if member.reversed:
            # Reading backwards, the timeline's in-edge is anchored to `src_out`
            # and its out-edge to `src_in`, and dragging an edge later consumes
            # *less* source, so the sign flips with it.
            if edge == "in":
                member.src_out = min(member.src_length, member.src_out - source_delta)
                member.tl_start += delta
            else:
                member.src_in = max(0, member.src_in - source_delta)
        elif edge == "in":
            member.src_in = max(0, member.src_in + source_delta)
            member.tl_start += delta
        else:
            member.src_out = min(member.src_length, member.src_out + source_delta)
        # A trim can shorten a clip past its own fade, or out from under its
        # own zoom region. Left alone the fade would be longer than the clip it
        # lives on and the playback envelope would run off the end of its own
        # ramp; the region would ask the export for frames that are not there.
        member.clamp_to_length()

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


# -- speed --------------------------------------------------------------------


def set_speed(timeline: Timeline, clips: Sequence[Clip], speed: float) -> None:
    """Change clip playback speed, keeping the in-point fixed.

    The clip's source window is unchanged — only how long it takes to play
    through it. So the clip's head stays where it is and its tail moves, which
    is what you want when retiming something already positioned on the timeline.

    Applied to the whole link group, so picture and sound retime together.
    Later clips are *not* rippled: a retime leaves a gap or an overlap for the
    editor to resolve, rather than silently rearranging the rest of the edit.
    """
    if speed <= 0:
        raise TimelineError("speed must be greater than zero")
    speed = max(MIN_SPEED, min(MAX_SPEED, float(speed)))

    group = expand_links(timeline, clips)
    if not group:
        return
    _assert_unlocked(timeline, group)

    for clip in group:
        track = timeline.track_of(clip)
        _, after = track.neighbours(clip)
        previous_speed = clip.speed
        clip.speed = speed

        # Slowing a clip makes it longer, which can run it into its neighbour.
        # Pull the out-point in rather than refusing the retime outright.
        if after is not None and clip.tl_end > after.tl_start:
            available = after.tl_start - clip.tl_start
            if available < 1:
                clip.speed = previous_speed
                raise TimelineError(
                    f"no room to slow {clip.name or 'clip'} down here — "
                    f"move the next clip first"
                )
            clip.src_out = min(
                clip.src_length, clip.src_in + max(1, clip.source_span_for(available))
            )
        # A retime changes how long the clip plays for without touching its
        # source window, so anything measured in timeline frames — the fades,
        # the zoom region — has to be pulled back inside the new length.
        clip.clamp_to_length()


def clip_speed(timeline: Timeline, clip: Clip) -> float:
    return clip.speed


# -- direction ----------------------------------------------------------------


def set_reversed(timeline: Timeline, clips: Sequence[Clip], backwards: bool) -> list[Clip]:
    """Play the clips' source windows back to front, or forwards again.

    Applied to the whole link group, like a retime: picture and sound have to
    turn round together or the take falls apart.

    Nothing about the clip's *timeline* geometry changes — same start, same
    length, same neighbours — because reversing only changes the order the same
    source frames are read in. That is what makes it safe to toggle on a clip
    sitting between two others.
    """
    group = expand_links(timeline, clips)
    if not group:
        return []
    _assert_unlocked(timeline, group)

    for clip in group:
        clip.reversed = bool(backwards)
    return group


def toggle_reversed(timeline: Timeline, clips: Sequence[Clip]) -> list[Clip]:
    """Flip direction, taking the first clip's state as the one to invert."""
    group = list(clips)
    if not group:
        return []
    return set_reversed(timeline, group, not group[0].reversed)


# -- levels -------------------------------------------------------------------


def _audio_only(clips: Sequence[Clip]) -> list[Clip]:
    """The audio members of a selection.

    Gain and fades deliberately do *not* expand link groups the way a move does:
    a video clip has no level, so applying a gain to a linked pair would either
    do nothing to half of it or silently invent a meaning for it.
    """
    return [clip for clip in clips if clip.kind == "audio"]


def set_clip_gain(timeline: Timeline, clips: Sequence[Clip], gain_db: float) -> list[Clip]:
    """Set the level trim on the audio clips of a selection."""
    targets = _audio_only(clips)
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(gain_db)))
    for clip in targets:
        clip.gain_db = gain_db
    return targets


def set_clip_muted(timeline: Timeline, clips: Sequence[Clip], muted: bool) -> list[Clip]:
    """Silence the audio clips of a selection without removing them.

    Unlike gain and fades, this *does* expand the link group first, and then
    keeps only the audio members. That is the difference between an operation
    describing a level — which a video clip has no meaning for — and one
    describing whether a cut is heard: right-clicking the picture half of a
    linked pair on the Edit page and asking to mute it can only sensibly mean
    "mute the sound that belongs to this", so it does.
    """
    targets = _audio_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.muted = bool(muted)
    return targets


def toggle_clip_mute(timeline: Timeline, clips: Sequence[Clip]) -> list[Clip]:
    """Flip mute on a selection, taking its first audio clip as the state to
    invert so a mixed selection ends up uniform rather than checkerboarded."""
    targets = _audio_only(expand_links(timeline, clips))
    if not targets:
        return []
    return set_clip_muted(timeline, targets, not targets[0].muted)


def normalise_clips(timeline: Timeline, clips: Sequence[Clip], gains: dict[str, float]) -> list[Clip]:
    """Apply a precomputed per-clip gain, keyed by clip id.

    The gains are measured by the caller rather than here: measuring needs the
    peak files, which are a `ProxyManager` concern, and this module has no
    business knowing where media lives.
    """
    targets = [clip for clip in _audio_only(clips) if clip.clip_id in gains]
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(gains[clip.clip_id])))
    return targets


def set_clip_fade(timeline: Timeline, clip: Clip, edge: Edge, frames: int) -> int:
    """Set one fade on one clip. Returns the length actually set.

    Clamps rather than raising, by the dragging convention: the fade handle stops
    against the other fade instead of throwing when they meet.
    """
    if clip.kind != "audio":
        return 0
    _assert_unlocked(timeline, [clip])

    other = clip.fade_out if edge == "in" else clip.fade_in
    frames = max(0, min(int(frames), clip.duration - other))
    if edge == "in":
        clip.fade_in = frames
    else:
        clip.fade_out = frames
    return frames


def clear_clip_fades(timeline: Timeline, clips: Sequence[Clip]) -> list[Clip]:
    targets = _audio_only(clips)
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.fade_in = 0
        clip.fade_out = 0
    return targets


def set_track_gain(timeline: Timeline, track: Track, gain_db: float) -> float:
    """Set a lane's fader position. Not blocked by a lock: locking a track
    protects its edit, not its monitoring level."""
    track.gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(gain_db)))
    return track.gain_db


def set_track_muted(timeline: Timeline, track: Track, muted: bool) -> None:
    track.muted = bool(muted)


def set_track_locked(timeline: Timeline, track: Track, locked: bool) -> None:
    track.locked = bool(locked)


def set_track_solo(timeline: Timeline, track: Track, solo: bool) -> None:
    track.solo = bool(solo)


def clear_solos(timeline: Timeline) -> None:
    """Drop every solo — what Alt-clicking a solo button does."""
    for track in timeline.audio_tracks:
        track.solo = False


def set_master_gain(timeline: Timeline, gain_db: float) -> float:
    timeline.master_gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, float(gain_db)))
    return timeline.master_gain_db


# -- tracks -------------------------------------------------------------------


def add_track(timeline: Timeline, kind: str) -> Track:
    return timeline.add_track(kind)


def remove_track(timeline: Timeline, track: Track) -> None:
    """Remove a lane and its clips, then renumber what is left."""
    timeline.remove_track(track)
    timeline.renumber_tracks()


def clear_track(timeline: Timeline, track: Track) -> None:
    """Empty a lane without removing it."""
    if track.locked:
        raise TimelineError(f"track {track.name} is locked")
    # Linked partners on other lanes are left alone: clearing V2 should not
    # silently delete audio the user can still see on A1.
    track.clips.clear()


# -- the picture ---------------------------------------------------------------


def _video_only(clips: Sequence[Clip]) -> list[Clip]:
    """The picture members of a selection, links expanded first.

    The mirror of `_audio_only`, but it expands the way mute does rather than
    the way gain does. Reframing is aimed at a shot, and a shot on the timeline
    is usually a linked pair — so grabbing either half and reframing should
    reach the picture. Gain cannot work that way because a video clip has no
    level to set; a framing has exactly one place to land.
    """
    return [clip for clip in clips if clip.kind == "video"]


def set_framing(
    timeline: Timeline, clips: Sequence[Clip], framing: Framing
) -> list[Clip]:
    """Set which part of the frame the picture fills."""
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.framing = framing
    return targets


def set_rotation(timeline: Timeline, clips: Sequence[Clip], rotation: int) -> list[Clip]:
    """Turn the picture by a quarter turn multiple, clockwise."""
    rotation = int(rotation) % 360
    if rotation not in ROTATIONS:
        raise TimelineError(f"{rotation}° is not a quarter turn")

    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.rotation = rotation
    return targets


def rotate_clips(timeline: Timeline, clips: Sequence[Clip], quarters: int) -> list[Clip]:
    """Turn by `quarters` steps from wherever the clips already are.

    Led by the first clip, the way `toggle_reversed` is: a mixed selection
    should end up agreeing rather than each member keeping its own offset.
    """
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    current = targets[0].rotation
    return set_rotation(timeline, targets, (current + 90 * int(quarters)) % 360)


def set_flipped(timeline: Timeline, clips: Sequence[Clip], flipped: bool) -> list[Clip]:
    """Mirror the picture left to right."""
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.flipped = bool(flipped)
    return targets


def toggle_flipped(timeline: Timeline, clips: Sequence[Clip]) -> list[Clip]:
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    return set_flipped(timeline, targets, not targets[0].flipped)


def reset_framing(timeline: Timeline, clips: Sequence[Clip]) -> list[Clip]:
    """Put the picture back to untouched — framing, rotation and flip together.

    One action rather than three, because "put it back how it was" is one
    thought, and a Reset that left the clip still upside down would be a
    surprise.
    """
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.framing = Framing()
        clip.framing_end = None
        clip.zoom = None
        clip.rotation = 0
        clip.flipped = False
    return targets


def set_zoom_region(
    timeline: Timeline,
    clips: Sequence[Clip],
    region: Region | None,
) -> list[Clip]:
    """Punch in for part of a clip, or take the punch-in away.

    Clip-local frames, so the region stays put when the clip is dragged along
    the timeline, and clamped to the clip so a region set before a trim cannot
    outlive the frames it was drawn over.
    """
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.zoom = region
        clip.clamp_zoom()
    return targets


def zoom_region_for(
    clip: Clip, at_frame: int, framing: Framing, *, frames: int
) -> Region:
    """A region of `frames` centred on a timeline frame, fitted to the clip.

    Centred rather than started there because the moment being zoomed into is
    the one under the playhead — starting the punch-in at it would show the
    approach and miss the event.
    """
    local = at_frame - clip.tl_start
    half = max(1, frames) // 2
    start = max(0, min(local - half, max(0, clip.duration - 1)))
    end = min(clip.duration, max(start + 1, start + max(1, frames)))
    return Region(framing, start, end).clamped_to(clip.duration)


def set_zoom_ramp(
    timeline: Timeline, clips: Sequence[Clip], edge: Edge, frames: int
) -> list[Clip]:
    """How long the zoom takes to arrive at one end. Zero is a cut.

    Clamped rather than rejected for the same reason a fade is: it is set by
    dragging a corner, and the mouse goes where it likes.
    """
    targets = [clip for clip in _video_only(expand_links(timeline, clips)) if clip.zoom]
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        region = clip.zoom
        other = region.ramp_out if edge == "in" else region.ramp_in
        room = max(0, region.length - other)
        wanted = max(0, min(int(frames), room))
        clip.zoom = replace(
            region, **{"ramp_in" if edge == "in" else "ramp_out": wanted}
        )
    return targets


def set_framing_move(
    timeline: Timeline, clips: Sequence[Clip], end: Framing | None
) -> list[Clip]:
    """Give the framing somewhere to travel to, or take the travel away.

    `None` removes the move and leaves the clip on its starting framing, which
    is what "Remove Move" means: stop moving, stay where you began.
    """
    targets = _video_only(expand_links(timeline, clips))
    if not targets:
        return []
    _assert_unlocked(timeline, targets)

    for clip in targets:
        clip.framing_end = end
    return targets


# -- transitions ---------------------------------------------------------------


def set_dissolve(timeline: Timeline, clip: Clip, frames: int) -> int:
    """Cross-fade into `clip` from the shot before it. Returns the length set.

    Stored on the incoming clip and paid for out of the outgoing one's unused
    source, so the two still abut and neither moves: adding a transition never
    changes where anything is or how long the edit runs.

    The picture dissolves; the sound cross-fades to match, by putting a fade out
    on the outgoing audio and a fade in on the incoming one over the same span.
    That is what a dissolve sounds like, and it needs nothing the mixer does not
    already do.
    """
    if clip.kind != "video":
        raise TimelineError("a dissolve is a picture transition")

    track = timeline.track_of(clip)
    _assert_unlocked(timeline, [clip])

    wanted = max(0, int(frames))
    if wanted == 0:
        clip.dissolve_in = 0
        _mirror_dissolve_in_audio(timeline, track, clip, 0)
        return 0

    index = track.index_of(clip)
    if index == 0:
        raise TimelineError("nothing before this clip to dissolve from")
    outgoing = track.clips[index - 1]
    if outgoing.tl_end != clip.tl_start:
        raise TimelineError("close the gap before the clip to dissolve into it")
    _assert_unlocked(timeline, [outgoing])

    clip.dissolve_in = wanted
    usable = track.dissolve_before(clip)
    if usable is None:
        clip.dissolve_in = 0
        raise TimelineError(
            f"{outgoing.name or 'the clip before'} has no unused footage left to "
            "dissolve from — trim its end back to make room"
        )

    clip.dissolve_in = usable[1]
    _mirror_dissolve_in_audio(timeline, track, clip, usable[1])
    return usable[1]


def _mirror_dissolve_in_audio(
    timeline: Timeline, track: Track, clip: Clip, frames: int
) -> None:
    """Match a picture dissolve with a cross-fade on the linked sound.

    Audio clips abut just as tightly as the picture does, so there is no overlap
    to mix across — but a fade out of one against a fade in of the next over the
    same span is the same thing to the ear, and rides on the fades the mixer and
    the exporter already understand.
    """
    for partner in timeline.linked_group(clip):
        if partner.kind == "audio":
            partner.fade_in = min(frames, partner.duration)
            partner.clamp_fades()

    index = track.index_of(clip)
    if index == 0:
        return
    for partner in timeline.linked_group(track.clips[index - 1]):
        if partner.kind == "audio":
            partner.fade_out = min(frames, max(0, partner.duration - partner.fade_in))
            partner.clamp_fades()


def clear_dissolves(timeline: Timeline, clips: Sequence[Clip]) -> list[Clip]:
    touched = []
    for clip in clips:
        if clip.kind == "video" and clip.dissolve_in:
            set_dissolve(timeline, clip, 0)
            touched.append(clip)
    return touched


# Long enough to register as a punch-in rather than a glitch, short enough to
# read as "for a moment". Dragged from there.
DEFAULT_ZOOM_SECONDS = 2.0


# -- titles --------------------------------------------------------------------

# Long enough to read, short enough that it is obviously meant to be adjusted.
DEFAULT_TITLE_SECONDS = 3.0


def add_title(
    timeline: Timeline,
    frame: int,
    *,
    title: Title | None = None,
    track: Track | None = None,
    frames: int | None = None,
) -> Clip:
    """Put a title on the timeline at `frame`.

    It goes on the *highest* free video lane by default rather than the first
    one, because a title is nearly always meant to sit over a shot rather than
    replace it — and a lane that already has picture on it is the one place it
    must not land.
    """
    if frame < 0:
        raise TimelineError("cannot place a title before the start of the timeline")

    length = frames if frames is not None else max(
        1, int(round(DEFAULT_TITLE_SECONDS * float(timeline.timebase.fps)))
    )
    if track is None:
        track = _free_video_lane(timeline, frame, length)

    clip = Clip(
        media_id="",
        src_in=0,
        src_out=length,
        tl_start=frame,
        src_length=length,
        kind="video",
        name=(title or Title()).text.split("\n")[0][:40] or "Title",
        title=title or Title(),
    )
    if track.locked:
        raise TimelineError(f"track {track.name} is locked")
    track.insert(clip)
    return clip


def _free_video_lane(timeline: Timeline, frame: int, length: int) -> Track:
    """A lane above the picture with room for a title, adding one if need be.

    Titles go on their own lane and a new one is made rather than squeezing in
    beside a shot, because a title you can drag along and stretch is only
    draggable if there is empty lane either side of it. Reusing a half-full lane
    gives you a title wedged between two clips, which is the one shape that
    cannot be adjusted.
    """
    lanes = timeline.video_tracks
    for track in lanes[1:]:
        if not track.locked and track.would_overlap(frame, frame + length) is None:
            return track
    return timeline.add_track("video")


def set_title(timeline: Timeline, clip: Clip, title: Title) -> Clip:
    """Rewrite a title's words and look."""
    if not clip.is_title:
        raise TimelineError("that clip is not a title")
    _assert_unlocked(timeline, [clip])
    clip.title = title
    clip.name = title.text.split("\n")[0][:40] or "Title"
    return clip
