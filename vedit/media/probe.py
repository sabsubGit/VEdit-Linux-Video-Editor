"""Turning a file on disk into a `MediaInfo` the rest of the app can reason about.

Import breadth is free here: everything goes through ffprobe, so whatever the
system FFmpeg can demux, vedit can import. There is deliberately no format
allowlist — the failure mode we care about is a camera handing you something
unusual and the editor refusing it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vedit.core.ffmpeg import FFmpegError, probe_json
from vedit.core.timebase import TimeBase, nearest_standard_rate


class UnsupportedMedia(Exception):
    """The file exists but has no stream we can use."""


@dataclass(frozen=True, slots=True)
class VideoStream:
    index: int
    codec: str
    width: int
    height: int
    fps: Fraction
    pix_fmt: str
    rotation: int          # 0, 90, 180 or 270, already normalised
    sample_aspect: Fraction
    duration: Fraction | None
    nb_frames: int | None

    @property
    def display_size(self) -> tuple[int, int]:
        """Size after rotation and non-square pixels — what the viewer must show.

        Phone footage is the case that matters: a portrait clip is stored
        landscape with a 90° display matrix, and ignoring that shows it sideways.
        """
        width = int(round(self.width * float(self.sample_aspect)))
        height = self.height
        if self.rotation in (90, 270):
            width, height = height, width
        return max(width, 1), max(height, 1)

    @property
    def timebase(self) -> TimeBase:
        return TimeBase(self.fps)


@dataclass(frozen=True, slots=True)
class AudioStream:
    index: int
    codec: str
    sample_rate: int
    channels: int
    channel_layout: str
    duration: Fraction | None


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Everything the app knows about one source file."""

    path: Path
    media_id: str
    container: str
    duration: Fraction     # seconds
    size: int              # bytes
    video: VideoStream | None
    audio: AudioStream | None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    def frame_count(self, timebase: TimeBase) -> int:
        """Length in whole frames at `timebase`.

        Rounds up so the tail of a clip is never silently clipped off by a
        fraction of a frame.
        """
        return timebase.seconds_to_frames_ceil(self.duration)

    def describe(self) -> str:
        """One-line summary for the pool's detail column."""
        parts: list[str] = []
        if self.video is not None:
            width, height = self.video.display_size
            parts.append(f"{width}×{height}")
            parts.append(f"{float(self.video.fps):g} fps")
            parts.append(self.video.codec)
        if self.audio is not None:
            channels = {1: "mono", 2: "stereo"}.get(self.audio.channels, f"{self.audio.channels}ch")
            parts.append(f"{self.audio.codec} {channels}")
        return " · ".join(parts) if parts else self.container


def media_id_for(path: Path) -> str:
    """Stable identity for a file: path + size + mtime.

    Also the proxy cache key, which is why mtime is in it — re-exporting a source
    under the same name has to invalidate its proxy.
    """
    stat = path.stat()
    digest = hashlib.sha1(
        f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode()
    ).hexdigest()
    return digest[:16]


def _rational(text: str | None, default: Fraction | None = None) -> Fraction | None:
    """Parse ffprobe's "num/den" (or plain number) fields, tolerating "0/0"."""
    if not text:
        return default
    try:
        if "/" in text:
            num, den = text.split("/", 1)
            if int(den) == 0:
                return default
            return Fraction(int(num), int(den))
        return Fraction(text)
    except (ValueError, ZeroDivisionError):
        return default


def _rotation_of(stream: dict) -> int:
    """Read display rotation from side data or the legacy `rotate` tag."""
    degrees = 0.0
    for side_data in stream.get("side_data_list", []) or []:
        if "rotation" in side_data:
            try:
                degrees = float(side_data["rotation"])
            except (TypeError, ValueError):
                degrees = 0.0
            break
    else:
        tag = (stream.get("tags") or {}).get("rotate")
        if tag is not None:
            try:
                degrees = float(tag)
            except (TypeError, ValueError):
                degrees = 0.0

    # FFmpeg reports counter-clockwise negatives; normalise to 0/90/180/270.
    normalised = int(round(-degrees)) % 360
    return min((0, 90, 180, 270), key=lambda step: min(abs(normalised - step), 360 - abs(normalised - step)))


def _frame_rate_of(stream: dict) -> Fraction:
    """Pick a frame rate, preferring the average over the nominal rate.

    `r_frame_rate` is the smallest rate that can express every timestamp, so for
    interlaced or variable-rate sources it comes back at double the real rate.
    `avg_frame_rate` is what a human means by "frame rate".
    """
    average = _rational(stream.get("avg_frame_rate"))
    if average and average > 0:
        return nearest_standard_rate(average)
    nominal = _rational(stream.get("r_frame_rate"))
    if nominal and nominal > 0:
        return nearest_standard_rate(nominal)
    return Fraction(25)  # A last resort for still images and odd streams.


def _duration_of(entry: dict) -> Fraction | None:
    value = entry.get("duration")
    if value in (None, "N/A"):
        return None
    try:
        return Fraction(str(value)).limit_denominator(1000000)
    except (ValueError, ZeroDivisionError):
        return None


def _int_of(entry: dict, key: str) -> int | None:
    value = entry.get(key)
    if value in (None, "N/A"):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pick_video(streams: list[dict]) -> dict | None:
    """First real video stream, skipping embedded cover art.

    Album art is muxed as a still video stream; treating an MP3's cover as the
    video track would give a one-frame clip and a very confusing timeline.
    """
    for stream in streams:
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        return stream
    return None


def _pick_audio(streams: list[dict]) -> dict | None:
    for stream in streams:
        if stream.get("codec_type") == "audio":
            return stream
    return None


def probe(path: str | Path) -> MediaInfo:
    """Probe `path`, raising `UnsupportedMedia` if there is nothing usable in it."""
    path = Path(path).expanduser()
    if not path.is_file():
        raise UnsupportedMedia(f"{path} is not a file")

    try:
        data = probe_json(path)
    except FFmpegError as exc:
        raise UnsupportedMedia(f"{path.name} could not be read: {exc.detail() or exc}") from exc

    streams = data.get("streams") or []
    container = data.get("format") or {}

    video_raw = _pick_video(streams)
    audio_raw = _pick_audio(streams)
    if video_raw is None and audio_raw is None:
        raise UnsupportedMedia(f"{path.name} has no video or audio stream")

    video = None
    if video_raw is not None:
        video = VideoStream(
            index=int(video_raw.get("index", 0)),
            codec=video_raw.get("codec_name", "unknown"),
            width=int(video_raw.get("width") or 0),
            height=int(video_raw.get("height") or 0),
            fps=_frame_rate_of(video_raw),
            pix_fmt=video_raw.get("pix_fmt", "yuv420p"),
            rotation=_rotation_of(video_raw),
            sample_aspect=_rational(video_raw.get("sample_aspect_ratio"), Fraction(1)) or Fraction(1),
            duration=_duration_of(video_raw),
            nb_frames=_int_of(video_raw, "nb_frames"),
        )
        if video.width <= 0 or video.height <= 0:
            video = None

    audio = None
    if audio_raw is not None:
        audio = AudioStream(
            index=int(audio_raw.get("index", 0)),
            codec=audio_raw.get("codec_name", "unknown"),
            sample_rate=_int_of(audio_raw, "sample_rate") or 48000,
            channels=_int_of(audio_raw, "channels") or 2,
            channel_layout=audio_raw.get("channel_layout", ""),
            duration=_duration_of(audio_raw),
        )

    if video is None and audio is None:
        raise UnsupportedMedia(f"{path.name} has no usable stream")

    duration = _duration_of(container)
    if duration is None:
        # Some MKV and MPEG-TS files carry no container duration; fall back to the
        # longest stream, then to a frame count.
        candidates = [s.duration for s in (video, audio) if s is not None and s.duration]
        if candidates:
            duration = max(candidates)
        elif video is not None and video.nb_frames:
            duration = Fraction(video.nb_frames) / video.fps
        else:
            duration = Fraction(0)

    return MediaInfo(
        path=path,
        media_id=media_id_for(path),
        container=container.get("format_name", path.suffix.lstrip(".")),
        duration=max(duration, Fraction(0)),
        size=_int_of(container, "size") or path.stat().st_size,
        video=video,
        audio=audio,
    )
