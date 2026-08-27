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
    duration: float


def _pieces_for(track: Track | None, timeline: Timeline, inputs: dict[str, int], pool: MediaPool) -> list[Piece]:
    """Flatten a track into an ordered piece list, with gaps made explicit."""
    pieces: list[Piece] = []
    cursor = 0
    duration = timeline.duration

    def gap(from_frame: int, to_frame: int) -> None:
        if to_frame > from_frame:
            pieces.append(
                Piece(None, 0.0, 0.0, timeline.seconds(to_frame) - timeline.seconds(from_frame))
            )

    if track is not None:
        for clip in track.clips:
            gap(cursor, clip.tl_start)
            info = pool.info_for(clip.media_id)
            if info is None or not clip.enabled:
                gap(clip.tl_start, clip.tl_end)
            else:
                start = timeline.seconds(clip.src_in)
                end = timeline.seconds(clip.src_out)
                pieces.append(Piece(inputs[clip.media_id], start, end, end - start))
            cursor = clip.tl_end

    gap(cursor, duration)
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

    video_track = timeline.video_tracks[0] if timeline.video_tracks else None
    audio_track = timeline.audio_tracks[0] if timeline.audio_tracks else None

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

    video_pieces = _pieces_for(video_track, timeline, inputs, pool)
    audio_pieces = _pieces_for(audio_track, timeline, inputs, pool)

    filters: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []

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
            filters.append(
                f"[{piece.input_index}:v]"
                f"trim=start={piece.start:.6f}:end={piece.end:.6f},"
                f"setpts=PTS-STARTPTS,"
                # force_original_aspect_ratio + pad letterboxes mixed aspect
                # ratios instead of stretching them.
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={fps.numerator}/{fps.denominator},format=yuv420p"
                f"[{label}]"
            )
        video_labels.append(f"[{label}]")

    for index, piece in enumerate(audio_pieces):
        label = f"a{index}"
        if piece.input_index is None:
            filters.append(
                f"{gap_audio}:d={piece.duration:.6f},asetpts=PTS-STARTPTS[{label}]"
            )
        else:
            filters.append(
                f"[{piece.input_index}:a]"
                f"atrim=start={piece.start:.6f}:end={piece.end:.6f},"
                f"asetpts=PTS-STARTPTS,"
                f"aresample={preset.sample_rate}:first_pts=0,"
                f"aformat=sample_fmts=fltp:channel_layouts=stereo"
                f"[{label}]"
            )
        audio_labels.append(f"[{label}]")

    # concat needs at least one segment on each stream it is told to produce.
    if not video_labels:
        raise RenderError("there is no video to render")

    # A timeline with no audio anywhere exports without an audio stream. Gaps
    # between real audio clips still become silence — but a wholly silent track
    # would just be dead weight in the file.
    has_audio = any(piece.input_index is not None for piece in audio_pieces)

    filters.append(
        "".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[vout]"
    )
    if has_audio:
        filters.append(
            "".join(audio_labels) + f"concat=n={len(audio_labels)}:v=0:a=1[aout]"
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
