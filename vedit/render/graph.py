"""Turning a timeline into an ffmpeg command.

Renders always read the **original** media, never the proxies — proxy quality has
no bearing on output.

The whole edit becomes a single `filter_complex`: each clip is trimmed, its
timestamps rebased to zero, normalised to the project format, and the results are
concatenated. Normalising scale and frame rate *per segment before* the concat is
what lets clips of different sizes and rates cut together, which is a common
failure elsewhere — `concat` requires every input to agree, and silently produces
garbage timing if they do not.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vedit.core.ffmpeg import ffmpeg_path
from vedit.media.pool import MediaPool
from vedit.render.presets import Preset
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


def _gap(timeline: Timeline, from_frame: int, to_frame: int) -> Piece:
    return Piece(None, 0.0, 0.0, timeline.seconds(to_frame) - timeline.seconds(from_frame))


def _piece_for(clip, timeline: Timeline, inputs: dict[str, int], start: int, end: int) -> Piece:
    """One clip span as a render piece, in source seconds."""
    source_start = timeline.seconds(clip.source_frame_at(start))
    source_end = timeline.seconds(clip.source_frame_at(end))
    played = timeline.seconds(end) - timeline.seconds(start)
    return Piece(inputs[clip.media_id], source_start, source_end, played, clip.speed)


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
    audio_lanes = [
        _audio_pieces(track, timeline, inputs, pool)
        for track in timeline.audio_tracks
        if not track.muted and track.clips
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
    for lane_index, pieces in enumerate(audio_lanes):
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
                    f"[{label}]"
                )
            labels.append(f"[{label}]")

        lane_label = f"alane{lane_index}"
        filters.append("".join(labels) + f"concat=n={len(labels)}:v=0:a=1[{lane_label}]")
        lane_outputs.append(f"[{lane_label}]")

    # concat needs at least one segment on each stream it is told to produce.
    if not video_labels:
        raise RenderError("there is no video to render")

    # A timeline with no audio anywhere exports without an audio stream. Gaps
    # between real audio clips still become silence — but a wholly silent track
    # would just be dead weight in the file.
    has_audio = any(
        piece.input_index is not None for pieces in audio_lanes for piece in pieces
    )

    filters.append(
        "".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vout]"
    )
    if has_audio:
        if len(lane_outputs) == 1:
            filters.append(f"{lane_outputs[0]}anull[aout]")
        else:
            # dropout_transition=0 stops amix from ducking the mix when one lane
            # falls silent, which would audibly pump the others.
            filters.append(
                "".join(lane_outputs)
                + f"amix=inputs={len(lane_outputs)}:duration=longest"
                f":dropout_transition=0:normalize=0[aout]"
            )

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
