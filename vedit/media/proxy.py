"""Background generation of proxies, waveform peaks and thumbnails.

Proxies are what make a Python playback engine viable. Decoding 4K H.264 in
process and pushing it through Qt at 30 fps is not realistic; decoding a 540p
short-GOP H.264 file is comfortable. The originals are never touched by the
preview — the renderer always goes back to them, so proxy quality has no bearing
on output quality.

`-g 12` on the proxy is the flag that matters most. A short GOP means a seek
lands near the target without a long decode run-up, which is the whole difference
between scrubbing that feels live and scrubbing that feels broken.
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from vedit.core.ffmpeg import FFmpegError, ffmpeg_path, has_encoder
from vedit.media.probe import MediaInfo

# Preview resolution. 360p rather than 540p: the viewer is a fraction of the
# screen, the export never reads a proxy, and the point of the file is to be
# cheap. Dropping it costs a little sharpness on a full-screen preview and buys
# two things — a proxy that is roughly twice as quick to *generate*, which is
# what the first minute after an import is waiting on, and a smaller frame to
# scale on every blit.
PROXY_HEIGHT = 360
# Pool thumbnails. Generated to fit a box rather than to a fixed height so an
# upright phone video does not come out three times taller than a 16:9 one; the
# pool letterboxes whatever arrives onto its own cell. Big enough to also serve
# the Media page's details pane without visibly softening.
THUMB_BOX = (320, 180)
PEAKS_RATE = 8000          # Hz the waveform pass decodes at
PEAKS_PER_SECOND = 100     # min/max buckets stored per second
PEAKS_MAGIC = b"VEPK\x01"

# Filmstrip: periodic frames tiled into one sprite sheet, drawn along video
# clips so a lane can be read at a glance. One sheet rather than many files
# means one decode and one texture, which matters when the timeline repaints
# on every frame during playback.
FILMSTRIP_HEIGHT = 54      # cell height in pixels; a video lane is 70 tall
FILMSTRIP_COLUMNS = 20
FILMSTRIP_MAX_CELLS = 300  # caps sheet size and generation time on long media
FILMSTRIP_MIN_CELLS = 8


def cache_root() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "vedit"


class Status(str, Enum):
    MISSING = "missing"      # nothing generated yet
    WORKING = "working"
    READY = "ready"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class CachePaths:
    proxy: Path
    peaks: Path
    thumb: Path
    strip: Path
    strip_meta: Path

    @classmethod
    def for_id(cls, media_id: str) -> CachePaths:
        root = cache_root()
        return cls(
            proxy=root / "proxies" / f"{media_id}.mp4",
            peaks=root / "peaks" / f"{media_id}.peaks",
            # The size is in the name: an older cache holds thumbnails from a
            # generator with different framing, and reusing those names would
            # keep serving them forever.
            thumb=root / "thumbs" / f"{media_id}-{THUMB_BOX[1]}.jpg",
            strip=root / "strips" / f"{media_id}.jpg",
            strip_meta=root / "strips" / f"{media_id}.json",
        )


def filmstrip_plan(duration: float, aspect: float) -> dict:
    """Work out the sheet layout for a clip of this length.

    One cell per second, clamped: short clips still get enough frames to read,
    and a two-hour source does not try to make seven thousand of them.
    """
    count = max(FILMSTRIP_MIN_CELLS, min(FILMSTRIP_MAX_CELLS, round(duration)))
    interval = duration / count if count else 1.0
    cell_width = max(2, int(round(FILMSTRIP_HEIGHT * aspect)))
    cell_width += cell_width % 2                     # even, for the scaler
    return {
        "count": count,
        "interval": interval,
        "cell_width": cell_width,
        "cell_height": FILMSTRIP_HEIGHT,
        "columns": FILMSTRIP_COLUMNS,
    }


def _proxy_video_encoder() -> list[str]:
    """Prefer NVENC — a GPU encode keeps ingest from saturating the CPU that the
    preview player needs. Falls back to x264 when NVENC is absent."""
    if has_encoder("h264_nvenc"):
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", "-cq", "30"]
    # `ultrafast` and a short GOP: this file is watched once, scrubbed through
    # and thrown away, so encode time matters and compression does not. The
    # twelve-frame GOP is what makes scrubbing quick — a seek only ever has a
    # few frames to decode before it reaches the one asked for.
    return [
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-g", "12", "-bf", "0",
    ]


# -- peak files ---------------------------------------------------------------


def write_peaks(path: Path, peaks: list[tuple[int, int]], per_second: int) -> None:
    """Store min/max pairs as int16 with a small header.

    A flat binary rather than numpy's format so the timeline can memory-map a
    slice of a long file without pulling the whole thing into memory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.stem}.part{path.suffix}")
    with temporary.open("wb") as handle:
        handle.write(PEAKS_MAGIC)
        handle.write(struct.pack("<II", per_second, len(peaks)))
        handle.write(struct.pack(f"<{len(peaks) * 2}h", *(v for pair in peaks for v in pair)))
    temporary.replace(path)


def read_peaks(path: Path) -> tuple[int, list[tuple[int, int]]]:
    """Return (buckets per second, [(min, max), ...])."""
    with path.open("rb") as handle:
        if handle.read(len(PEAKS_MAGIC)) != PEAKS_MAGIC:
            raise ValueError(f"{path} is not a vedit peaks file")
        per_second, count = struct.unpack("<II", handle.read(8))
        raw = struct.unpack(f"<{count * 2}h", handle.read(count * 4))
    return per_second, list(zip(raw[0::2], raw[1::2]))


# -- the worker ---------------------------------------------------------------


class _Signals(QObject):
    status = Signal(str, str)        # media_id, Status value
    thumb_ready = Signal(str, str)   # media_id, path
    peaks_ready = Signal(str, str)
    proxy_ready = Signal(str, str)
    strip_ready = Signal(str, str)
    failed = Signal(str, str)        # media_id, message
    progress = Signal(str, float)    # media_id, 0..1


class _IngestJob(QRunnable):
    """Thumbnail, then proxy, then peaks, for one source file.

    Ordered by how quickly each pays off: the thumbnail lands almost immediately
    so the pool stops looking empty, and the slow proxy runs behind it.
    """

    def __init__(self, info: MediaInfo, signals: _Signals) -> None:
        super().__init__()
        self.info = info
        self.signals = signals
        self.paths = CachePaths.for_id(info.media_id)
        self._cancelled = False
        self._process: subprocess.Popen | None = None

    def cancel(self) -> None:
        self._cancelled = True
        process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    # -- subprocess helper -----------------------------------------------------

    def _run(self, args: list[str], *, output: Path) -> None:
        """Run ffmpeg writing to a `.part` file, renamed only on success.

        Writing in place would leave a truncated file in the cache if the app is
        killed mid-encode, and that half-file would then be trusted forever.
        """
        output.parent.mkdir(parents=True, exist_ok=True)
        # The marker goes *before* the extension: ffmpeg picks its muxer from the
        # filename, so "foo.jpg.part" leaves it with no format to choose.
        temporary = output.with_name(f"{output.stem}.part{output.suffix}")
        full = [ffmpeg_path(), "-nostdin", "-v", "error", "-y", *args, str(temporary)]

        self._process = subprocess.Popen(
            full, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
        )
        _, stderr = self._process.communicate()
        code = self._process.returncode
        self._process = None

        if self._cancelled:
            temporary.unlink(missing_ok=True)
            return
        if code != 0:
            temporary.unlink(missing_ok=True)
            raise FFmpegError(f"ffmpeg exited {code}", command=full, stderr=stderr or "")
        if not temporary.exists():
            # A seek past the last decodable frame exits cleanly having written
            # nothing. Saying so here keeps that out of the generic handler,
            # where it would arrive as a bare FileNotFoundError from `replace`.
            raise FFmpegError("ffmpeg produced no output", command=full, stderr=stderr or "")
        temporary.replace(output)

    # -- stages ----------------------------------------------------------------

    def _make_thumb(self) -> None:
        """One representative frame for the pool.

        Announced even when it was already cached. The pool builds its icons
        from this signal and nothing else, so staying quiet about a warm cache
        is why a reopened project used to come back with a column of blanks.
        """
        if self.info.video is None:
            return
        if self.paths.thumb.exists():
            self.signals.thumb_ready.emit(self.info.media_id, str(self.paths.thumb))
            return

        # 10% in rather than frame zero: many clips open on black or a slate.
        # Frame zero is the fallback, because on a file whose duration is
        # overstated, or that has one keyframe and nothing after it, the seek
        # lands past the end and decodes nothing at all.
        seek = max(float(self.info.duration) * 0.1, 0.0)
        offsets = [seek, 0.0] if seek > 0 else [0.0]
        width, height = THUMB_BOX

        failure: FFmpegError | None = None
        for offset in offsets:
            try:
                self._run(
                    [
                        "-ss", f"{offset:.3f}",
                        "-i", str(self.info.path),
                        "-frames:v", "1",
                        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                        "-q:v", "3",
                    ],
                    output=self.paths.thumb,
                )
                break
            except FFmpegError as exc:
                failure = exc
        else:
            if not self._cancelled and failure is not None:
                raise failure

        if not self._cancelled and self.paths.thumb.exists():
            self.signals.thumb_ready.emit(self.info.media_id, str(self.paths.thumb))

    def _make_proxy(self) -> None:
        if self.info.video is None or self.paths.proxy.exists():
            return
        width, height = self.info.video.display_size
        if height <= PROXY_HEIGHT:
            # Already small enough to decode directly; a proxy would only cost
            # disk and add a generation loss for no gain.
            return

        args = [
            "-i", str(self.info.path),
            "-map", "0:v:0",
            "-vf", f"scale=-2:{PROXY_HEIGHT}",
            *_proxy_video_encoder(),
            "-g", "12",
            "-pix_fmt", "yuv420p",
        ]
        if self.info.has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
        else:
            args += ["-an"]
        args += ["-movflags", "+faststart"]

        self._run(args, output=self.paths.proxy)
        if not self._cancelled and self.paths.proxy.exists():
            self.signals.proxy_ready.emit(self.info.media_id, str(self.paths.proxy))

    def _make_filmstrip(self) -> None:
        """Tile periodic frames into one sprite sheet for the timeline.

        Runs last and reads the proxy when there is one: pulling 300 frames
        through a 540p short-GOP file is far cheaper than decoding the original,
        and the result is only ever drawn ~54 pixels tall.
        """
        if self.info.video is None or self.paths.strip.exists():
            return
        duration = float(self.info.duration)
        if duration <= 0:
            return

        width, height = self.info.video.display_size
        plan = filmstrip_plan(duration, width / height if height else 16 / 9)
        source = self.paths.proxy if self.paths.proxy.exists() else self.info.path
        rows = max(1, -(-plan["count"] // plan["columns"]))

        self._run(
            [
                "-i", str(source),
                "-vf", (
                    f"fps=1/{plan['interval']:.6f},"
                    f"scale={plan['cell_width']}:{plan['cell_height']},"
                    f"tile={plan['columns']}x{rows}:padding=0"
                ),
                "-frames:v", "1",
                "-q:v", "4",
            ],
            output=self.paths.strip,
        )

        if self._cancelled or not self.paths.strip.exists():
            return
        # The sheet alone does not say how to index it; store the layout beside it.
        self.paths.strip_meta.write_text(json.dumps({**plan, "rows": rows}))
        self.signals.strip_ready.emit(self.info.media_id, str(self.paths.strip))

    def _make_peaks(self) -> None:
        if self.info.audio is None or self.paths.peaks.exists():
            return

        args = [
            ffmpeg_path(), "-nostdin", "-v", "error",
            "-i", str(self.info.path),
            "-map", "0:a:0",
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ac", "1", "-ar", str(PEAKS_RATE),
            "-",
        ]
        bucket = max(PEAKS_RATE // PEAKS_PER_SECOND, 1)
        chunk_bytes = bucket * 2

        peaks: list[tuple[int, int]] = []
        self._process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        assert self._process.stdout is not None
        try:
            while not self._cancelled:
                raw = self._process.stdout.read(chunk_bytes)
                if not raw:
                    break
                count = len(raw) // 2
                if count == 0:
                    break
                samples = struct.unpack(f"<{count}h", raw[: count * 2])
                peaks.append((min(samples), max(samples)))
        finally:
            if self._process.stdout is not None:
                self._process.stdout.close()
            self._process.wait()
            self._process = None

        if self._cancelled or not peaks:
            return
        write_peaks(self.paths.peaks, peaks, PEAKS_PER_SECOND)
        self.signals.peaks_ready.emit(self.info.media_id, str(self.paths.peaks))

    # -- QRunnable -------------------------------------------------------------

    def run(self) -> None:
        media_id = self.info.media_id
        self.signals.status.emit(media_id, Status.WORKING.value)
        try:
            # A thumbnail is a nicety; the proxy and the peaks are what make the
            # media playable. One awkward frame must not cost it those, so this
            # stage reports and carries on where the others abort the job.
            try:
                self._make_thumb()
            except FFmpegError as exc:
                if not self._cancelled:
                    self.signals.failed.emit(
                        media_id, f"no thumbnail for {self.info.name}: {exc.detail() or exc}"
                    )
            self.signals.progress.emit(media_id, 0.15)
            self._make_peaks()
            self.signals.progress.emit(media_id, 0.35)
            self._make_proxy()
            self.signals.progress.emit(media_id, 0.85)
            self._make_filmstrip()
            self.signals.progress.emit(media_id, 1.0)
        except FFmpegError as exc:
            if not self._cancelled:
                self.signals.status.emit(media_id, Status.FAILED.value)
                self.signals.failed.emit(media_id, exc.detail() or str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - a worker must never take the app down
            if not self._cancelled:
                self.signals.status.emit(media_id, Status.FAILED.value)
                self.signals.failed.emit(media_id, str(exc))
            return

        if not self._cancelled:
            self.signals.status.emit(media_id, Status.READY.value)


class ProxyManager(QObject):
    """Owns the ingest thread pool and reports what is cached for each media id."""

    status = Signal(str, str)
    thumb_ready = Signal(str, str)
    peaks_ready = Signal(str, str)
    proxy_ready = Signal(str, str)
    strip_ready = Signal(str, str)
    failed = Signal(str, str)
    progress = Signal(str, float)

    def __init__(self, parent: QObject | None = None, max_jobs: int = 2) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        # Deliberately low: ingest must not starve the CPU that the preview
        # decoder is competing for, and concurrent NVENC sessions are limited.
        self._pool.setMaxThreadCount(max_jobs)

        self._signals = _Signals()
        self._signals.status.connect(self.status)
        self._signals.thumb_ready.connect(self.thumb_ready)
        self._signals.peaks_ready.connect(self.peaks_ready)
        self._signals.proxy_ready.connect(self.proxy_ready)
        self._signals.strip_ready.connect(self.strip_ready)
        self._signals.failed.connect(self.failed)
        self._signals.progress.connect(self.progress)

        self._jobs: dict[str, _IngestJob] = {}

    # -- queries ---------------------------------------------------------------

    @staticmethod
    def paths_for(media_id: str) -> CachePaths:
        return CachePaths.for_id(media_id)

    def status_of(self, info: MediaInfo) -> Status:
        if info.media_id in self._jobs:
            return Status.WORKING
        paths = CachePaths.for_id(info.media_id)
        wants_proxy = info.video is not None
        wants_peaks = info.audio is not None
        if wants_proxy and not paths.proxy.exists() and not paths.thumb.exists():
            return Status.MISSING
        if wants_peaks and not paths.peaks.exists():
            return Status.MISSING
        return Status.READY

    def playback_path(self, info: MediaInfo) -> Path:
        """Where the preview should decode from.

        Falls back to the original while the proxy is still generating, so media
        is playable the instant it is imported rather than after a wait.
        """
        proxy = CachePaths.for_id(info.media_id).proxy
        return proxy if proxy.exists() else info.path

    def thumb_for(self, media_id: str) -> Path | None:
        path = CachePaths.for_id(media_id).thumb
        return path if path.exists() else None

    def peaks_for(self, media_id: str) -> Path | None:
        path = CachePaths.for_id(media_id).peaks
        return path if path.exists() else None

    def strip_for(self, media_id: str) -> tuple[Path, dict] | None:
        """Sprite sheet and its layout, or None if it has not been made yet."""
        paths = CachePaths.for_id(media_id)
        if not (paths.strip.exists() and paths.strip_meta.exists()):
            return None
        try:
            return paths.strip, json.loads(paths.strip_meta.read_text())
        except (OSError, ValueError):
            return None

    # -- work ------------------------------------------------------------------

    def request(self, info: MediaInfo) -> None:
        if info.media_id in self._jobs:
            return
        job = _IngestJob(info, self._signals)
        self._jobs[info.media_id] = job
        self._signals.status.connect(lambda mid, _s, i=info.media_id: self._forget(mid, i))
        self._pool.start(job)

    def _forget(self, media_id: str, expected: str) -> None:
        if media_id == expected:
            self._jobs.pop(media_id, None)

    def shutdown(self) -> None:
        """Stop everything. Called on quit so ffmpeg children do not outlive us."""
        for job in list(self._jobs.values()):
            job.cancel()
        self._jobs.clear()
        self._pool.clear()
        self._pool.waitForDone(3000)
