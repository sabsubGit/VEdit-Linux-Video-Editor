"""Turning a timeline into an ffmpeg command.

Renders always read the **original** media, never the proxies — proxy quality has
no bearing on output.

The whole edit becomes a single `filter_complex`: each clip is trimmed, its
timestamps rebased to zero, normalised to the project format, and the results are
concatenated. Normalising scale and frame rate *per segment before* the concat is
what lets clips of different sizes and rates cut together, which is a common
failure elsewhere — `concat` requires every input to agree, and silently produces
garbage timing if they do not.

Levels are applied at the same three points as the preview mixer, in the same
order: clip gain and fades per piece, lane gain after the lane concat, master
gain after the mix. Every clause is emitted only when it is not a no-op, so a
timeline nobody has mixed produces exactly the command it did before any of this
existed.

One known divergence, documented rather than fixed: the preview clips at int16
after the master fader, and ffmpeg's float pipeline does not clip until encode,
so a mix that distorts in preview renders slightly cleaner. An `alimiter` would
close the gap by making the export *quieter* than what was monitored, which is
the worse surprise of the two.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from vedit.core.ffmpeg import ffmpeg_path
from vedit.media.pool import MediaPool
from vedit.render.presets import Preset
from vedit.timeline.framing import IDENTITY, Framing, Region, window
from vedit.timeline import titles as titles_mod
from vedit.timeline.levels import from_db
from vedit.timeline.model import Timeline, Track


class RenderError(Exception):
    """The timeline cannot be rendered as configured."""


@dataclass(frozen=True, slots=True)
class Piece:
    """One trimmed span of one input file, or a gap."""

    input_index: int | None      # None for a gap
    start: float                 # seconds into the source
    end: float
    duration: float              # length on the timeline, after any speed change
    speed: float = 1.0
    reverse: bool = False        # play the trimmed span back to front
    gain: float = 1.0            # linear, from the clip's gain_db
    fade_in: float = 0.0         # seconds
    fade_out: float = 0.0
    # Picture settings, video only. Defaulted and last in the list because
    # `_gap` and `_piece_for` construct a Piece positionally.
    framing: Framing = IDENTITY
    framing_end: Framing | None = None   # None: the framing does not travel
    rotation: int = 0
    flipped: bool = False
    # Where this piece sits inside its clip, in project frames. A clip can be
    # cut into several pieces by a lane above it, and a move has to be measured
    # across the whole clip or it would restart at every one of them.
    clip_offset: int = 0
    clip_frames: int = 0
    # A punch-in covering part of the clip, clip-local like the offset above and
    # for the same reason.
    zoom: Region | None = None
    # The shot this one dissolves in from, as a whole Piece of its own: the
    # outgoing clip carries its own speed, direction and picture into the
    # transition, so nothing less than a full piece will describe it.
    under: Piece | None = None
    # Titles drawn over this piece, lowest lane first. Attached to the piece
    # below rather than being pieces of their own, because a title is not a
    # shot — it is words on top of one, and the timeline's "highest lane wins"
    # rule would otherwise make a caption hide the thing it captions.
    titles: tuple = ()


def _gap(timeline: Timeline, from_frame: int, to_frame: int) -> Piece:
    return Piece(None, 0.0, 0.0, timeline.seconds(to_frame) - timeline.seconds(from_frame))


def _piece_for(clip, timeline: Timeline, inputs: dict[str, int], start: int, end: int) -> Piece:
    """One clip span as a render piece, in source seconds.

    Fades are carried only when the piece covers the clip's own edge. A razored
    clip's right-hand half must not restart its fade in, and video pieces never
    carry levels at all.
    """
    # A reversed clip maps the span's *end* to the lower source edge, and `trim`
    # only understands ascending times — the reversal itself is a filter, not a
    # backwards trim.
    first, last = clip.source_window_for(start, end)
    source_start = timeline.seconds(first)
    source_end = timeline.seconds(last)
    played = timeline.seconds(end) - timeline.seconds(start)

    framing, rotation, flipped = IDENTITY, 0, False
    framing_end, clip_offset, clip_frames = None, 0, 0
    zoom = None
    if clip.kind == "video":
        framing, rotation, flipped = clip.framing, clip.rotation, clip.flipped
        framing_end = clip.framing_end if clip.moves else None
        clip_offset = start - clip.tl_start
        clip_frames = clip.duration
        zoom = clip.zoom

    gain, fade_in, fade_out = 1.0, 0.0, 0.0
    if clip.kind == "audio":
        gain = from_db(clip.gain_db)
        if clip.fade_in and start <= clip.tl_start:
            fade_in = min(timeline.seconds(clip.fade_in), played)
        if clip.fade_out and end >= clip.tl_end:
            fade_out = min(timeline.seconds(clip.fade_out), played)

    return Piece(
        inputs[clip.media_id],
        source_start,
        source_end,
        played,
        clip.speed,
        clip.reversed,
        gain,
        fade_in,
        fade_out,
        framing,
        framing_end,
        rotation,
        flipped,
        clip_offset,
        clip_frames,
        zoom,
    )


def _dissolve_under(
    track, clip, timeline: Timeline, inputs: dict[str, int], pool: MediaPool
) -> Piece | None:
    """The outgoing shot, played on past its own out-point, for a dissolve.

    It is the same clip carried forward into borrowed source, so it is built by
    asking the ordinary piece builder for a span that runs off the end of the
    clip. `source_window_for` maps that to the handle without knowing it is
    doing anything unusual.
    """
    found = track.dissolve_before(clip)
    if found is None:
        return None
    outgoing, frames = found
    if pool.info_for(outgoing.media_id) is None:
        return None
    return _piece_for(
        outgoing, timeline, inputs, outgoing.tl_end, outgoing.tl_end + frames
    )


def _video_pieces(timeline: Timeline, inputs: dict[str, int], pool: MediaPool) -> list[Piece]:
    """Flatten every video lane, topmost clip winning.

    Must agree with the preview's `build_video_playlist`, or the exported file
    would not be what the editor watched.
    """
    duration = timeline.duration
    lanes = [t for t in timeline.video_tracks if not t.muted]

    edges = {0, duration}
    for track in lanes:
        for clip in track.clips:
            edges.add(max(0, min(clip.tl_start, duration)))
            edges.add(max(0, min(clip.tl_end, duration)))
            # A dissolve ends partway into its clip, and that is a boundary
            # like any other: what is on screen changes there.
            found = track.dissolve_before(clip)
            if found is not None:
                edges.add(max(0, min(clip.tl_start + found[1], duration)))

    pieces: list[Piece] = []
    boundaries = sorted(edges)
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        winner = None
        winning_track = None
        for track in lanes:
            found = track.clip_at(start)
            if found is None or not found.enabled or found.is_title:
                continue
            if pool.info_for(found.media_id) is not None:
                winner, winning_track = found, track

        overlay = tuple(clip.title for clip in timeline.titles_at(start))
        if winner is None:
            # Nothing but words: they go over black, which is what a title card
            # is. Still a gap piece, so it costs no decoding.
            pieces.append(replace(_gap(timeline, start, end), titles=overlay))
            continue

        piece = _piece_for(winner, timeline, inputs, start, end)
        if start == winner.tl_start:
            under = _dissolve_under(winning_track, winner, timeline, inputs, pool)
            if under is not None:
                piece = replace(piece, under=under)
        pieces.append(replace(piece, titles=overlay))
    return pieces


def _even(value: float, *, minimum: int) -> int:
    """Round down to an even whole number, at or above `minimum`.

    `yuv420p` subsamples chroma two pixels at a time, so an odd crop size or an
    odd offset shifts the colour planes against the luma. Rounding both *down*
    keeps the crop inside the frame it was measured against.

    Sizes floor at 2 because a zero-width crop is not a picture; offsets floor
    at 0, because panning all the way to an edge genuinely means zero and
    nudging it to 2 would leave a two-pixel sliver of the wrong thing.
    """
    return max(minimum, int(value) // 2 * 2)


def _orientation(piece: Piece) -> str:
    """Rotation and flip, as clauses that go *before* the scale.

    Before, because a quarter turn swaps the picture's width and height, and it
    is the turned shape that has to be fitted into the project frame — rotating
    afterwards would fit the picture on its side and then tip the letterbox
    bars over with it.

    Empty when there is nothing to do, so an untouched timeline builds exactly
    the command it built before this existed.
    """
    clauses: list[str] = []
    if piece.rotation == 90:
        clauses.append("transpose=1")
    elif piece.rotation == 180:
        # Two transposes would work; a pair of flips is the same picture for
        # less work, since neither has to move a pixel to a new row length.
        clauses += ["hflip", "vflip"]
    elif piece.rotation == 270:
        clauses.append("transpose=2")
    if piece.flipped:
        # After the turn, so "flipped" mirrors what the viewer shows rather
        # than what the file happened to store.
        clauses.append("hflip")
    return "".join(f"{clause}," for clause in clauses)


def _move(piece: Piece, width: int, height: int, fps: Fraction) -> str:
    """A framing that travels, as a `zoompan` that goes *after* the rate change.

    `crop` cannot do this. Its width and height expressions are evaluated once
    when the filter is configured, so it can animate a pan but never a zoom;
    `zoompan` is the only filter in FFmpeg that re-evaluates the size per frame.

    Three of its defaults are actively dangerous here and all three are passed
    explicitly: `d` is 90, which would hold every frame for three seconds; `fps`
    is 25, which would silently retime the segment and break the frame-rate
    agreement `concat` depends on; and `s` is hd720, which would resize it. It
    goes after the `fps` filter so that `on` counts project frames, and the
    progress below is therefore in the same unit as the clip's duration.

    The zoom expression is substituted into the pan expressions rather than
    referred to as `z`, because inside `x` and `y` that name means *the previous
    frame's* zoom — a one-frame lag that reads as a wobble on a slow push.
    """
    moves = piece.framing_end is not None
    region = piece.zoom
    if (not moves and region is None) or piece.clip_frames <= 0:
        return ""
    start = piece.framing
    end = piece.framing_end if moves else start

    # Clip-local frame number. `on` counts frames into this *piece*, and a lane
    # above can cut one clip into several, so the offset is what keeps both the
    # move and the region measured across the clip they belong to.
    local = f"({piece.clip_offset}+on)"

    # Progress across the whole clip. One frame short of the duration so the
    # last frame lands on the end framing exactly, matching `Clip.framing_at`.
    span = max(1, piece.clip_frames - 1)
    progress = f"clip({local}/{span},0,1)"

    def travel(first: float, last: float) -> str:
        if first == last:
            return f"{first:.6f}"
        return f"({first:.6f}+({last - first:.6f})*{progress})"

    def punch(base: str, target: float) -> str:
        """`base` pulled toward the region's value by however much of it applies.

        This is `region_framing` — an interpolate between the clip's own value
        and the region's — with the weight computed per frame instead of in
        Python. Both sides must agree exactly or a punch-in previews at one
        strength and exports at another.
        """
        if region is None:
            return base
        return f"({base}+({target:.6f}-({base}))*{_ramp(region, local)})"

    zoom = punch(travel(start.zoom, end.zoom), region.framing.zoom if region else 0.0)
    pan_x = punch(travel(start.x, end.x), region.framing.x if region else 0.0)
    pan_y = punch(travel(start.y, end.y), region.framing.y if region else 0.0)
    # The window, exactly as `framing.window` computes it, in filter syntax.
    x = f"({width}-{width}/{zoom})/2*(1+{pan_x})"
    y = f"({height}-{height}/{zoom})/2*(1+{pan_y})"
    return (
        f"zoompan=z='{zoom}':x='{x}':y='{y}'"
        f":d=1:s={width}x{height}:fps={fps.numerator}/{fps.denominator},"
    )


def _ramp(region: Region, local: str) -> str:
    """`Region.progress` as an FFmpeg expression, frame for frame.

    Zero outside the region; inside, `into/ramp_in` rising and `left/ramp_out`
    falling, whichever is lower — the same two ratios `Region.progress` takes
    the minimum of, and the same linear shape as an audio fade. A ramp of zero
    frames contributes nothing to the minimum, which is what makes a square
    corner an instant cut to the zoom rather than a division by zero.
    """
    into = f"({local}-{region.start})"
    left = f"({region.end}-{local})"
    parts = []
    if region.ramp_in > 0:
        parts.append(f"{into}/{region.ramp_in}")
    if region.ramp_out > 0:
        parts.append(f"{left}/{region.ramp_out}")

    if not parts:
        strength = "1"
    elif len(parts) == 1:
        strength = f"clip({parts[0]},0,1)"
    else:
        strength = f"clip(min({parts[0]},{parts[1]}),0,1)"
    # `between` is inclusive at both ends and the region's end is exclusive, so
    # the last frame inside it is one before.
    return f"if(between({local},{region.start},{region.end - 1}),{strength},0)"


def _titles(piece: Piece, width: int, height: int) -> str:
    """Every title over this piece, as `drawtext` clauses.

    One clause per *line* rather than one per title, so the line spacing is the
    spacing this app decided on rather than whatever drawtext would do with an
    embedded newline. It also means the viewer can place lines by the same
    arithmetic and land on the same pixels.

    Applied at the very end of the chain, after the framing and any move, so a
    caption stays put while the shot behind it pushes in — which is what a
    caption is for.
    """
    clauses: list[str] = []
    for title in piece.titles:
        if title.is_empty:
            continue
        margin = titles_mod.horizontal_margin((width, height))
        for line in titles_mod.layout(title, (width, height)):
            if not line.text.strip():
                continue
            nudge = titles_mod.horizontal_offset(title, (width, height))
            if title.align == "left":
                x = f"{margin + nudge:.1f}"
            elif title.align == "right":
                x = f"{width - margin + nudge:.1f}-text_w"
            else:
                x = f"(w-text_w)/2{nudge:+.1f}"
            # Vertically centred in its own line box, so a descender does not
            # push the line off its place.
            y = f"{line.top:.1f}+({line.height:.1f}-text_h)/2"
            options = [
                # `expansion=none` first, and that ordering is load-bearing:
                # drawtext expands the text as it parses the `text` option, so
                # setting it afterwards is too late and a title containing a
                # per cent sign fails the whole render with "Stray %".
                "expansion=none",
                f"text={titles_mod.quote(line.text)}",
                f"font={titles_mod.quote(titles_mod.FONT_FAMILY)}",
                f"fontsize={line.font_px:.1f}",
                f"fontcolor={title.colour}",
                f"x={x}",
                f"y={y}",
            ]
            if title.shadow:
                # A dark edge rather than a drop shadow: it works over a bright
                # sky and a dark interior alike, which a one-sided shadow does
                # not.
                options.append("borderw=%d" % max(1, round(line.font_px * 0.055)))
                options.append("bordercolor=black@0.65")
            clauses.append("drawtext=" + ":".join(options))
    return "".join(f"{clause}," for clause in clauses)


def _framing(piece: Piece, width: int, height: int) -> str:
    """Zoom and pan, as clauses that go *after* the scale and pad.

    Framing is defined on the project frame — the letterboxed picture the editor
    shows — so the crop has to happen once the source has been fitted into that
    frame, not before. `vedit/timeline/framing.py` works out which rectangle;
    this only turns it into filter syntax, which is what keeps the exported
    pixels the same ones the preview drew.
    """
    if piece.framing_end is not None or piece.zoom is not None:
        return ""   # it changes per frame; `_move` handles it after the rate change
    if piece.framing.is_identity:
        return ""
    view = window(piece.framing, (width, height))
    crop_w = _even(view.width, minimum=2)
    crop_h = _even(view.height, minimum=2)
    crop_x = _even(view.x, minimum=0)
    crop_y = _even(view.y, minimum=0)
    return f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale={width}:{height},"


def _shaping(piece: Piece) -> str:
    """The clip's own level clauses, appended to its filter chain.

    `afade`'s default curve is `tri`, which is a linear ramp — the same shape the
    preview's `envelope()` applies, so the two agree. `st` is relative to the
    piece's own zero, which the preceding `asetpts=PTS-STARTPTS` guarantees.

    Empty when there is nothing to apply, so an unmixed timeline's command stays
    byte-identical to what it was before mixing existed.
    """
    clauses = ""
    if piece.gain != 1.0:
        clauses += f",volume={piece.gain:.6f}"
    if piece.fade_in > 0.0:
        clauses += f",afade=t=in:st=0:d={piece.fade_in:.6f}"
    if piece.fade_out > 0.0:
        start = max(0.0, piece.duration - piece.fade_out)
        clauses += f",afade=t=out:st={start:.6f}:d={piece.fade_out:.6f}"
    return clauses


def _audio_pieces(track: Track, timeline: Timeline, inputs: dict[str, int], pool: MediaPool) -> list[Piece]:
    """Flatten one audio lane into an ordered piece list, gaps made explicit."""
    pieces: list[Piece] = []
    cursor = 0
    duration = timeline.duration

    for clip in track.clips:
        if clip.tl_start > cursor:
            pieces.append(_gap(timeline, cursor, clip.tl_start))
        info = pool.info_for(clip.media_id)
        if info is None or not clip.audible:
            # A muted clip exports as silence of exactly its own length, so the
            # lane's timing is identical to the one that was monitored.
            pieces.append(_gap(timeline, clip.tl_start, clip.tl_end))
        else:
            pieces.append(_piece_for(clip, timeline, inputs, clip.tl_start, clip.tl_end))
        cursor = clip.tl_end

    if cursor < duration:
        pieces.append(_gap(timeline, cursor, duration))
    return pieces


def build_command(
    timeline: Timeline,
    pool: MediaPool,
    preset: Preset,
    output: Path,
    *,
    progress_to_stdout: bool = True,
) -> list[str]:
    """Build the full ffmpeg argument list for rendering `timeline`."""
    if timeline.duration <= 0:
        raise RenderError("the timeline is empty")

    # One -i per distinct source, reused by every clip that references it.
    inputs: dict[str, int] = {}
    input_args: list[str] = []
    for media_id in timeline.media_ids():
        info = pool.info_for(media_id)
        if info is None:
            continue
        inputs[media_id] = len(inputs)
        input_args += ["-i", str(info.path)]

    if not inputs and not any(clip.is_title for clip in timeline.all_clips()):
        # Titles are the exception: they are the one thing on the timeline that
        # renders without a file behind it, so a card on its own is a real edit
        # rather than an empty one.
        raise RenderError("none of the clips on the timeline have media attached")

    width = preset.width or timeline.width
    height = preset.height or timeline.height
    fps: Fraction = timeline.timebase.fps

    video_pieces = _video_pieces(timeline, inputs, pool)
    # `audible_audio_tracks` rather than a local mute test: it is the one
    # definition of what is heard, so solo means the same thing in the exported
    # file as it does on the Audio page.
    audio_lanes = [
        (track, _audio_pieces(track, timeline, inputs, pool))
        for track in timeline.audible_audio_tracks()
        if track.clips
    ]

    filters: list[str] = []
    video_labels: list[str] = []

    # A single black/silent source each, reused by every gap.
    gap_video = f"color=c=black:s={width}x{height}:r={fps.numerator}/{fps.denominator}"
    gap_audio = f"anullsrc=r={preset.sample_rate}:cl=stereo"

    def video_chain(piece: Piece, label: str) -> str:
        """One normalised video segment, ending on `[label]`.

        Every branch that reaches `concat` goes through here, which is what
        guarantees they agree about size, pixel format, aspect and rate — the
        four things concat refuses to join without. A dissolve builds two of
        them and mixes the result, so both halves of a transition are normalised
        exactly alike.
        """
        if piece.input_index is None:
            return (
                f"{gap_video}:d={piece.duration:.6f},"
                f"{_titles(piece, width, height)}"
                f"format=yuv420p,setpts=PTS-STARTPTS[{label}]"
            )
        # setpts scales the timestamps: at 2x, PTS is halved so the same
        # source frames occupy half the time. The fps filter afterwards
        # resamples to the project rate, dropping or repeating as needed.
        retime = "" if piece.speed == 1.0 else f"/{piece.speed:.6f}"
        # `reverse` buffers the whole span it is given, which is why it goes
        # after the trim and never sees more than one clip's worth of frames.
        # The setpts that follows rebases the timestamps it hands back, so
        # the piece still starts at zero for the concat.
        backwards = "reverse,setpts=PTS-STARTPTS," if piece.reverse else ""
        return (
            f"[{piece.input_index}:v]"
            f"trim=start={piece.start:.6f}:end={piece.end:.6f},"
            f"setpts=(PTS-STARTPTS){retime},"
            f"{backwards}"
            # Turning the picture comes first: it decides what shape is
            # being fitted.
            f"{_orientation(piece)}"
            # force_original_aspect_ratio + pad letterboxes mixed aspect
            # ratios instead of stretching them.
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,"
            # ...and framing crops into the frame that produced, then fills
            # it again.
            f"{_framing(piece, width, height)}"
            f"fps={fps.numerator}/{fps.denominator},"
            # A framing that travels goes after the rate change, so its
            # frame counter counts project frames.
            f"{_move(piece, width, height, fps)}"
            # Words go on last: a caption should stay put while the shot behind
            # it moves.
            f"{_titles(piece, width, height)}"
            f"format=yuv420p"
            f"[{label}]"
        )

    for index, piece in enumerate(video_pieces):
        label = f"v{index}"
        if piece.under is None:
            filters.append(video_chain(piece, label))
        else:
            # A dissolve: the outgoing shot playing on into its unused source,
            # mixed into the incoming one. `offset=0` because the two branches
            # cover exactly the transition and nothing either side of it, so
            # xfade's output is the same length as each input rather than their
            # sum — which is what keeps the timeline's arithmetic intact.
            # Neither branch draws the titles: they would be mixed against
            # each other and fade in over themselves. They go on the result.
            filters.append(video_chain(replace(piece.under, titles=()), f"{label}u"))
            filters.append(video_chain(replace(piece, titles=()), f"{label}o"))
            filters.append(
                f"[{label}u][{label}o]"
                f"xfade=transition=fade:duration={piece.duration:.6f}:offset=0,"
                f"{_titles(piece, width, height)}"
                f"format=yuv420p[{label}]"
            )
        video_labels.append(f"[{label}]")

    lane_outputs: list[str] = []
    for lane_index, (track, pieces) in enumerate(audio_lanes):
        labels: list[str] = []
        for index, piece in enumerate(pieces):
            label = f"a{lane_index}_{index}"
            if piece.input_index is None:
                filters.append(
                    f"{gap_audio}:d={piece.duration:.6f},asetpts=PTS-STARTPTS[{label}]"
                )
            else:
                # asetrate retimes by resampling, so pitch rises and falls with
                # speed. The preview resamples the same way; using atempo here
                # would preserve pitch and no longer match what was previewed.
                retime = (
                    ""
                    if piece.speed == 1.0
                    else f"asetrate={int(preset.sample_rate * piece.speed)},"
                )
                # areverse before the shaping clauses, never after: a fade-in
                # belongs to the start of the clip as it is *heard*, and applying
                # it before the reversal would put it at the end.
                backwards = "areverse,asetpts=PTS-STARTPTS," if piece.reverse else ""
                filters.append(
                    f"[{piece.input_index}:a]"
                    f"atrim=start={piece.start:.6f}:end={piece.end:.6f},"
                    f"asetpts=PTS-STARTPTS,"
                    f"aresample={preset.sample_rate}:first_pts=0,"
                    f"{retime}"
                    f"aresample={preset.sample_rate},"
                    f"{backwards}"
                    f"aformat=sample_fmts=fltp:channel_layouts=stereo"
                    f"{_shaping(piece)}"
                    f"[{label}]"
                )
            labels.append(f"[{label}]")

        lane_label = f"alane{lane_index}"
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[{lane_label}]")

        lane_gain = from_db(track.gain_db)
        if lane_gain != 1.0:
            filters.append(f"[{lane_label}]volume={lane_gain:.6f}[{lane_label}g]")
            lane_label = f"{lane_label}g"
        lane_outputs.append(f"[{lane_label}]")

    # concat needs at least one segment on each stream it is told to produce.
    if not video_labels:
        raise RenderError("there is no video to render")

    # A timeline with no audio anywhere exports without an audio stream. Gaps
    # between real audio clips still become silence — but a wholly silent track
    # would just be dead weight in the file.
    has_audio = any(
        piece.input_index is not None for _, pieces in audio_lanes for piece in pieces
    )

    filters.append(
        "".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vout]"
    )
    if has_audio:
        master = from_db(timeline.master_gain_db)
        # The mix lands on [aout] directly unless there is a master fader to
        # apply, so a timeline at unity produces the command it always did.
        mix_label = "aout_pre" if master != 1.0 else "aout"
        if len(lane_outputs) == 1:
            filters.append(f"{lane_outputs[0]}anull[{mix_label}]")
        else:
            # dropout_transition=0 stops amix from ducking the mix when one lane
            # falls silent, which would audibly pump the others.
            filters.append(
                "".join(lane_outputs)
                + f"amix=inputs={len(lane_outputs)}:duration=longest"
                f":dropout_transition=0:normalize=0[{mix_label}]"
            )
        if master != 1.0:
            # After the mix, exactly where the preview applies it — so what was
            # heard on the Audio page is what lands in the file.
            filters.append(f"[{mix_label}]volume={master:.6f}[aout]")

    args = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y"]
    args += input_args
    args += ["-filter_complex", ";".join(filters)]
    args += ["-map", "[vout]"]
    if has_audio:
        args += ["-map", "[aout]"]

    args += preset.video_args()
    if has_audio:
        args += preset.audio_args()
    else:
        args += ["-an"]

    if preset.container == "mp4":
        args += ["-movflags", "+faststart"]
    if progress_to_stdout:
        # Machine-readable progress; parsed by render.job.
        args += ["-progress", "pipe:1", "-nostats"]

    args.append(str(output))
    return args
