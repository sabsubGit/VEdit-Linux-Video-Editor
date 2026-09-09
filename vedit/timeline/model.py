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
from vedit.timeline.framing import (
    IDENTITY,
    ROTATIONS,
    Framing,
    Region,
    interpolate,
    region_framing,
)
from vedit.timeline.titles import Title

TrackKind = Literal["video", "audio"]

# Beyond this range playback stops being useful and the decoder spends all its
# time seeking; the UI offers a much narrower set of presets than this.
MIN_SPEED = 0.1
MAX_SPEED = 10.0

# Gain range shared by clips, tracks and the master. -60 dB is inaudible against
# any real programme material, so it is where the fader bottoms out rather than a
# separate "off"; +12 dB is enough to rescue a quiet recording and not enough to
# destroy one.
MIN_GAIN_DB = -60.0
MAX_GAIN_DB = 12.0

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
    speed: float = 1.0
    link_id: str | None = None
    clip_id: str = field(default_factory=lambda: new_id("c"))
    name: str = ""
    enabled: bool = True
    reversed: bool = False # plays its source window back to front
    muted: bool = False    # audio only: occupies the lane but makes no sound
    gain_db: float = 0.0   # clip trim, applied before the track fader
    fade_in: int = 0       # timeline frames from tl_start
    fade_out: int = 0      # timeline frames ending at tl_end
    # Picture only. `framing` is which part of the project frame the picture
    # fills; rotation and flip are applied before it, because turning a clip
    # upright changes the shape the framing is choosing from. See
    # `timeline/framing.py` — the geometry lives there so the preview and the
    # export cannot drift apart.
    framing: Framing = IDENTITY
    # Where the framing ends up. None means it does not move — which is the
    # common case, and the reason this is a second value rather than a list of
    # keyframes: a push in only needs somewhere to start and somewhere to stop,
    # and a curve editor is the single biggest thing standing between a normal
    # person and a slow zoom.
    framing_end: Framing | None = None
    rotation: int = 0      # quarter turns clockwise: 0, 90, 180, 270
    flipped: bool = False  # mirrored left to right
    # Cross dissolve at this clip's head, in timeline frames. Stored on the
    # *incoming* clip because that is the one the transition belongs to, and
    # expressed as a length rather than an overlap: the clips still abut, so
    # nothing moves and no two clips ever occupy the same frame. What makes it
    # work is the outgoing clip's unused source — see `Track.dissolve_before`.
    dissolve_in: int = 0
    # Words drawn over whatever is underneath. A clip carrying one is a *title*:
    # it has no media behind it, so `media_id` is empty and nothing looks it up
    # in the pool. Titles are the first thing on the timeline not backed by a
    # file, and making them a kind of clip rather than a new sort of object is
    # what lets them be trimmed, moved, rippled and undone by everything that
    # already knows how to do those things.
    title: Title | None = None
    # A zoom that lasts for part of the clip rather than all of it, with a ramp
    # at each end saying how fast it arrives. Held clip-local so it travels with
    # the clip, and separate from `framing` so the two compose: the region is a
    # punch-in *on top of* whatever the shot was already doing. See
    # `framing.Region`.
    zoom: Region | None = None

    def __post_init__(self) -> None:
        if self.title is not None and self.kind != "video":
            raise TimelineError(
                f"clip {self.name or self.clip_id}: a title belongs on a video track"
            )
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
        if not MIN_SPEED <= self.speed <= MAX_SPEED:
            raise TimelineError(
                f"clip {self.name or self.clip_id}: speed {self.speed} is outside "
                f"{MIN_SPEED}x to {MAX_SPEED}x"
            )
        if not MIN_GAIN_DB <= self.gain_db <= MAX_GAIN_DB:
            raise TimelineError(
                f"clip {self.name or self.clip_id}: gain {self.gain_db} dB is outside "
                f"{MIN_GAIN_DB} to {MAX_GAIN_DB}"
            )
        if self.rotation not in ROTATIONS:
            raise TimelineError(
                f"clip {self.name or self.clip_id}: rotation {self.rotation}° is not "
                f"one of {', '.join(f'{r}°' for r in ROTATIONS)}"
            )
        # `Framing` validates itself on construction, so there is nothing to
        # re-check here — only that it is one at all, which a hand-built clip
        # or a hand-edited project file could get wrong.
        for value in (self.framing, self.framing_end):
            if value is not None and not isinstance(value, Framing):
                raise TimelineError(
                    f"clip {self.name or self.clip_id}: framing must be a Framing, "
                    f"not {type(value).__name__}"
                )
        if self.zoom is not None and not isinstance(self.zoom, Region):
            raise TimelineError(
                f"clip {self.name or self.clip_id}: zoom must be a Region, "
                f"not {type(self.zoom).__name__}"
            )
        self.clamp_to_length()

    # -- geometry --------------------------------------------------------------

    @property
    def source_span(self) -> int:
        """Source frames the clip consumes. Independent of how long it plays for."""
        return self.src_out - self.src_in

    @property
    def duration(self) -> int:
        """Timeline frames the clip occupies.

        At 2x a clip eats two source frames per timeline frame, so it occupies
        half the space. Deriving this from the source window rather than storing
        it keeps the two from ever disagreeing; at the default 1.0 speed it is
        exactly the source span, so the common path is unchanged.
        """
        if self.speed == 1.0:
            return self.source_span
        return max(1, round(self.source_span / self.speed))

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
        """Which source frame is showing at a given timeline position.

        A reversed clip walks its window backwards from the out-point, so the
        mapping is anchored at `src_out` and counts down. Note that at the very
        first frame that is `src_out` itself, one past the last frame of the
        window: the value is a *boundary* — everything downstream turns it into
        seconds to seek or trim with — and reversing the window has to include
        that far edge or the first frame back would be missing.
        """
        offset = timeline_frame - self.tl_start
        consumed = offset if self.speed == 1.0 else int(round(offset * self.speed))
        if self.reversed:
            return self.src_out - consumed
        return self.src_in + consumed

    def source_window_for(self, start: int, end: int) -> tuple[int, int]:
        """The source span a timeline span reads, always low edge first.

        Reversing swaps which end of the window the span starts at, and a trim
        or a seek wants the pair in ascending order regardless.
        """
        first = self.source_frame_at(start)
        last = self.source_frame_at(end)
        return (last, first) if self.reversed else (first, last)

    def source_span_for(self, timeline_frames: int) -> int:
        """Source frames consumed by `timeline_frames` of playback at this speed."""
        if self.speed == 1.0:
            return timeline_frames
        return int(round(timeline_frames * self.speed))

    # -- fades -----------------------------------------------------------------

    def clamp_fades(self) -> None:
        """Keep both fades inside the clip.

        Clamped rather than rejected, because a fade is set by dragging and a
        later trim can legitimately shorten the clip out from under one. When
        there is not room for both, the fade in wins — an arbitrary choice, but
        one that has to be made somewhere and stay put.
        """
        span = self.duration
        self.fade_in = max(0, min(self.fade_in, span))
        self.fade_out = max(0, min(self.fade_out, span - self.fade_in))

    @property
    def has_fades(self) -> bool:
        return self.fade_in > 0 or self.fade_out > 0

    @property
    def is_title(self) -> bool:
        """Whether this clip draws text instead of reading a file."""
        return self.title is not None

    @property
    def has_framing(self) -> bool:
        """Whether anything has been done to this clip's picture.

        The menu and the lane badge both gate on this, and the export uses the
        same question to decide whether to emit any filter at all.
        """
        return (
            not self.framing.is_identity
            or self.has_move
            or self.has_zoom
            or self.rotation != 0
            or self.flipped
        )

    def clamp_to_length(self) -> None:
        """Pull everything measured in frames back inside the clip.

        Fades and the zoom region are both spans laid on the clip's own
        timeline, so both are invalidated by the same events — a trim, a speed
        change, anything that alters `duration`. One call rather than two,
        because remembering to make the second one is exactly the mistake that
        left a region hanging off the end of a trimmed clip.
        """
        self.clamp_fades()
        self.clamp_zoom()

    def clamp_zoom(self) -> None:
        """Keep the zoom region inside the clip.

        The same problem `clamp_fades` solves and for the same reason: a trim
        can shorten a clip out from under a region that was set when it was
        longer, and a region hanging off the end would ask the export for frames
        the clip does not have.
        """
        if self.zoom is not None:
            self.zoom = self.zoom.clamped_to(self.duration)

    @property
    def has_zoom(self) -> bool:
        """Whether part of this clip is punched in. Gates the lane's badge."""
        return self.zoom is not None

    @property
    def has_move(self) -> bool:
        """Whether this clip has been given a second framing to travel to.

        The question the *editor* asks. A move exists the moment it is added,
        before either end has been framed — otherwise the controls for setting
        the end would vanish the instant you needed them.
        """
        return self.framing_end is not None

    @property
    def moves(self) -> bool:
        """Whether the framing actually travels.

        The question the *export* asks. An end that matches the start is not a
        move, and rendering it as one would cost a far more expensive filter to
        produce exactly the static picture.
        """
        return self.framing_end is not None and self.framing_end != self.framing

    def framing_at(self, timeline_frame: int) -> Framing:
        """The framing showing at a timeline frame.

        Measured across the clip, not across whatever fragment of it is being
        rendered: an upper lane cutting a hole in this one splits it into
        several pieces, and a move that restarted at each of them would stutter.

        The span is one frame short of the duration so the *last* frame is the
        end framing exactly, rather than one step before it.
        """
        local = timeline_frame - self.tl_start
        if self.framing_end is None:
            base = self.framing
        else:
            span = max(1, self.duration - 1)
            base = interpolate(self.framing, self.framing_end, local / span)
        # The region is laid over whatever the clip was already doing, so a
        # punch-in during a slow push reads as both, not as one replacing the
        # other.
        return region_framing(base, self.zoom, local)

    @property
    def audible(self) -> bool:
        """Whether this clip should make any sound at all.

        Mute is deliberately separate from `enabled`: disabling a clip takes it
        out of the edit entirely — and does so for the whole link group, picture
        included — whereas muting silences one audio block while its linked
        picture keeps playing. That is the whole point of muting a single cut.
        """
        return self.kind == "audio" and self.enabled and not self.muted

    # -- headroom for trimming -------------------------------------------------

    @property
    def spare_room(self) -> int:
        """Unused source beyond the end this clip plays *towards*.

        For a normal clip that is the tail; for a reversed one it is the head,
        because playing on past its out-point means counting further down. This
        is what a dissolve spends: the outgoing clip has to keep showing
        something after its own last frame.
        """
        return self.head_room if self.reversed else self.tail_room

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
    gain_db: float = 0.0
    solo: bool = False

    def __iter__(self) -> Iterator[Clip]:
        return iter(self.clips)

    def __len__(self) -> int:
        return len(self.clips)

    @property
    def duration(self) -> int:
        return self.clips[-1].tl_end if self.clips else 0

    def sort(self) -> None:
        self.clips.sort(key=lambda clip: clip.tl_start)

    def dissolve_before(self, clip: Clip) -> tuple[Clip, int] | None:
        """The outgoing clip and the dissolve length that is actually usable.

        Computed on demand rather than clamped into the model, because what
        limits it keeps moving: trimming either clip changes both how much
        spare source there is and how long the shot lasts. Asking the question
        at the moment it matters means no edit can leave a dissolve in a state
        that has to be repaired afterwards.

        Returns None when there is no transition to draw — no request, no
        neighbour, a gap between them, or nothing left to dissolve from.
        """
        if clip.dissolve_in <= 0:
            return None
        index = self.index_of(clip)
        if index == 0:
            return None
        outgoing = self.clips[index - 1]
        if outgoing.tl_end != clip.tl_start:
            return None   # a gap between them: there is nothing to mix with

        # Bounded by three things: the request, how much of each shot exists to
        # spend, and how much unused source the outgoing clip can keep playing.
        # The last one is in source frames, so a retimed clip spends them faster
        # than it spends timeline frames.
        spare = outgoing.spare_room
        affordable = spare if outgoing.speed == 1.0 else int(spare / outgoing.speed)
        wanted = min(clip.dissolve_in, clip.duration, outgoing.duration, affordable)
        return (outgoing, wanted) if wanted > 0 else None

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
        # `replace` rather than a hand-written constructor call: the field list
        # was already once a place where a new field could be silently dropped,
        # and every undo would then quietly reset it.
        return replace(self, clips=[clip.copy() for clip in self.clips])


@dataclass(slots=True)
class TimelineState:
    """Everything the undo stack has to restore.

    The snapshot used to be a bare list of tracks, which meant any state living
    on the `Timeline` itself was invisible to undo. Master gain is the first such
    field, so the snapshot grew a shape rather than the field being left out.
    """

    tracks: list[Track]
    master_gain_db: float = 0.0


@dataclass(slots=True)
class Timeline:
    """Tracks plus the project format that everything is conformed to."""

    timebase: TimeBase = field(default_factory=lambda: TimeBase(30))
    width: int = 1920
    height: int = 1080
    sample_rate: int = 48000
    tracks: list[Track] = field(default_factory=list)
    master_gain_db: float = 0.0

    @classmethod
    def default(
        cls,
        timebase: TimeBase | None = None,
        width: int = 1920,
        height: int = 1080,
        *,
        video_tracks: int = 3,
        audio_tracks: int = 3,
    ) -> Timeline:
        """A fresh timeline with empty lanes ready to drop onto.

        More than one of each by default: having somewhere to put a cutaway or a
        music bed without first hunting for an "add track" command is what makes
        a timeline feel usable straight away.
        """
        tracks = [Track(kind="video", name=f"V{n + 1}") for n in range(max(1, video_tracks))]
        tracks += [Track(kind="audio", name=f"A{n + 1}") for n in range(max(1, audio_tracks))]
        return cls(
            timebase=timebase or TimeBase(30),
            width=width,
            height=height,
            tracks=tracks,
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

    def audible_audio_tracks(self) -> list[Track]:
        """Audio lanes that should be heard, honouring both mute and solo.

        The single definition of audibility: preview, render and the mixer all
        call this and nothing else, because a solo that means one thing on the
        Audio page and another in the exported file is worse than no solo.

        Solo is additive — several lanes can be soloed at once — and it does not
        override mute, so a lane that is both soloed and muted stays silent and a
        stale solo cannot resurrect something deliberately killed.
        """
        lanes = [track for track in self.audio_tracks if not track.muted]
        soloed = [track for track in lanes if track.solo]
        return soloed or lanes

    def video_clip_at(self, frame: int) -> Clip | None:
        """The picture showing at a frame: topmost unmuted lane, enabled clip.

        v1 has no opacity, so overlapping video is pure occlusion and "what is
        on top here?" is the whole compositing model. The preview's viewer asks
        this to find the framing to draw with — going to the model rather than
        to the flattened playlist, because framing does not affect decoding and
        must not be able to drag the decoder into a reseek.
        """
        winner: Clip | None = None
        for track in self.video_tracks:
            if track.muted:
                continue
            # Later lanes are higher, so the last match wins. A title is not a
            # picture — it is drawn over one — so it never occludes the lane
            # below it.
            found = track.clip_at(frame)
            if found is not None and found.enabled and not found.is_title:
                winner = found
        return winner

    def titles_at(self, frame: int) -> list[Clip]:
        """Every title covering a frame, lowest lane first.

        Lowest first because that is the order they are drawn in, and a title on
        V3 should land on top of one on V2 the same way pictures do.
        """
        found: list[Clip] = []
        for track in self.video_tracks:
            if track.muted:
                continue
            clip = track.clip_at(frame)
            if clip is not None and clip.enabled and clip.is_title:
                found.append(clip)
        return found

    def lane_for(self, kind: TrackKind) -> Track:
        """The first lane of a kind — where media lands unless told otherwise."""
        lanes = self.video_tracks if kind == "video" else self.audio_tracks
        if not lanes:
            raise TimelineError(f"the timeline has no {kind} track")
        return lanes[0]

    def add_track(self, kind: TrackKind, *, index: int | None = None) -> Track:
        """Add a lane, named after how many of its kind already exist."""
        existing = self.video_tracks if kind == "video" else self.audio_tracks
        track = Track(kind=kind, name=f"{'V' if kind == 'video' else 'A'}{len(existing) + 1}")
        if index is None:
            # Keep video lanes grouped above audio lanes.
            position = len(self.video_tracks) if kind == "video" else len(self.tracks)
            self.tracks.insert(position, track)
        else:
            self.tracks.insert(index, track)
        return track

    def remove_track(self, track: Track) -> None:
        """Remove a lane. The last lane of a kind stays, so there is always
        somewhere for imported media to land."""
        siblings = self.video_tracks if track.kind == "video" else self.audio_tracks
        if len(siblings) <= 1:
            raise TimelineError(f"cannot remove the last {track.kind} track")
        self.tracks = [t for t in self.tracks if t.track_id != track.track_id]

    def renumber_tracks(self) -> None:
        """Rename lanes to be contiguous after one is removed."""
        for index, track in enumerate(self.video_tracks):
            track.name = f"V{index + 1}"
        for index, track in enumerate(self.audio_tracks):
            track.name = f"A{index + 1}"

    def video_priority(self, track: Track) -> int:
        """Higher wins when lanes overlap: V2 covers V1."""
        return self.video_tracks.index(track)

    def media_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for clip in self.all_clips():
            seen.setdefault(clip.media_id, None)
        return list(seen)

    # -- whole-timeline state --------------------------------------------------

    def validate(self) -> None:
        for track in self.tracks:
            track.validate()

    def snapshot(self) -> TimelineState:
        """Deep copy of the undoable state, used by the undo stack."""
        return TimelineState(
            tracks=[track.copy() for track in self.tracks],
            master_gain_db=self.master_gain_db,
        )

    def restore(self, snapshot: TimelineState | Iterable[Track]) -> None:
        """Replace contents in place, so UI holding this object stays valid.

        A bare iterable of tracks is still accepted: tests and any caller that
        only cares about the lanes should not have to build a state object.
        """
        if isinstance(snapshot, TimelineState):
            self.tracks = [track.copy() for track in snapshot.tracks]
            self.master_gain_db = snapshot.master_gain_db
        else:
            self.tracks = [track.copy() for track in snapshot]

    def seconds(self, frames: int) -> float:
        """Frames to seconds — only for handing across the FFmpeg boundary."""
        return float(self.timebase.frames_to_seconds(frames))
