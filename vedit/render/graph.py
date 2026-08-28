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

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vedit.core.ffmpeg import ffmpeg_path
from vedit.media.pool import MediaPool
from vedit.render.presets import Preset
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
    gain: float = 1.0            # linear, from the clip's gain_db
    fade_in: float = 0.0         # seconds
    fade_out: float = 0.0


def _gap(timeline: Timeline, from_frame: int, to_frame: int) -> Piece:
    return Piece(None, 0.0, 0.0, timeline.seconds(to_frame) - timeline.seconds(from_frame))


def _piece_for(clip, timeline: Timeline, inputs: dict[str, int], start: int, end: int) -> Piece:
    """One clip span as a render piece, in source seconds.

    Fades are carried only when the piece covers the clip's own edge. A razored
    clip's right-hand half must not restart its fade in, and video pieces never
    carry levels at all.
    """
    source_start = timeline.seconds(clip.source_frame_at(start))
    source_end = timeline.seconds(clip.source_frame_at(end))
    played = timeline.seconds(end) - timeline.seconds(start)

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
        gain,
        fade_in,
        fade_out,
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

    pieces: list[Piece] = []
    boundaries = sorted(edges)
    for start, end in zip(boundaries, boundaries[1:]):
        if end <= start:
            continue
        winner = None
        for track in lanes:
            found = track.clip_at(start)
            if found is not None and found.enabled and pool.info_for(found.media_id) is not None:
                winner = found
        if winner is None:
            pieces.append(_gap(timeline, start, end))
        else:
            pieces.append(_piece_for(winner, timeline, inputs, start, end))
    return pieces


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
        if info is None or not clip.enabled:
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

    if not inputs:
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

    for index, piece in enumerate(video_pieces):
        label = f"v{index}"
        if piece.input_index is None:
            filters.append(
                f"{gap_video}:d={piece.duration:.6f},format=yuv420p,setpts=PTS-STARTPTS[{label}]"
            )
        else:
            # setpts scales the timestamps: at 2x, PTS is halved so the same
            # source frames occupy half the time. The fps filter afterwards
            # resamples to the project rate, dropping or repeating as needed.
            retime = "" if piece.speed == 1.0 else f"/{piece.speed:.6f}"
            filters.append(
                f"[{piece.input_index}:v]"
                f"trim=start={piece.start:.6f}:end={piece.end:.6f},"
                f"setpts=(PTS-STARTPTS){retime},"
                # force_original_aspect_ratio + pad letterboxes mixed aspect
                # ratios instead of stretching them.
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={fps.numerator}/{fps.denominator},format=yuv420p"
                f"[{label}]"
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
                filters.append(
                    f"[{piece.input_index}:a]"
                    f"atrim=start={piece.start:.6f}:end={piece.end:.6f},"
                    f"asetpts=PTS-STARTPTS,"
                    f"aresample={preset.sample_rate}:first_pts=0,"
                    f"{retime}"
                    f"aresample={preset.sample_rate},"
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
