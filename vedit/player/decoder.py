"""Video decode thread.

Runs ahead of playback filling a small queue of decoded frames tagged with the
timeline frame they belong to. The UI thread only ever pops from that queue, so a
slow decode shows up as a dropped frame rather than a frozen interface.

Seeking is the operation that decides whether scrubbing feels alive. PyAV seeks
to the keyframe at or before the target, so we then decode forward and discard
until we reach the requested frame. That run-up is short because preview media is
proxied with a 12-frame GOP; on an original file with a 250-frame GOP the same
code still works, just less briskly.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import av
import av.error
from PySide6.QtGui import QImage

from vedit.core.timebase import TimeBase
from vedit.player.segments import Playlist, Segment

QUEUE_DEPTH = 8


@dataclass(slots=True)
class DecodedFrame:
    frame_index: int      # timeline frame this image belongs to
    image: QImage


def _to_qimage(frame) -> QImage:
    """Convert a PyAV frame to a QImage.

    libswscale does the colour conversion in C, which at proxy resolution costs
    around a millisecond — far cheaper than doing it in Python, and simple enough
    that it needs no GL path.
    """
    array = frame.to_ndarray(format="rgb24")
    height, width, _ = array.shape
    image = QImage(array.data, width, height, 3 * width, QImage.Format_RGB888)
    # The ndarray owns the buffer; copy so it can be freed independently of Qt.
    return image.copy()


class _OpenSource:
    """One open container, positioned somewhere in one segment."""

    def __init__(self, path: Path) -> None:
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        # Frame-level threading adds latency after a seek; slice threading gives
        # most of the speed-up without it.
        self.stream.thread_type = "SLICE"
        self.stream.thread_count = 0
        self._frames = None

    def seek(self, seconds: float) -> None:
        offset = int(seconds / float(self.stream.time_base))
        self.container.seek(offset, stream=self.stream, backward=True, any_frame=False)
        self._frames = None

    def frames(self):
        if self._frames is None:
            self._frames = self.container.decode(self.stream)
        return self._frames

    def seconds_of(self, frame) -> float:
        pts = frame.pts if frame.pts is not None else 0
        return float(pts * self.stream.time_base)

    def close(self) -> None:
        try:
            self.container.close()
        except Exception:
            pass


class VideoDecoder:
    """Background decoder feeding a bounded frame queue."""

    def __init__(self, timebase: TimeBase) -> None:
        self.timebase = timebase

        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._thread: threading.Thread | None = None
        self._running = False

        self._playlist = Playlist([], 0)
        self._queue: list[DecodedFrame] = []
        self._seek_to: int | None = None
        self._generation = 0        # bumped on seek so stale frames are dropped
        self._position = 0
        self._at_end = False
        self._error: str | None = None

        self._source: _OpenSource | None = None
        self._source_path: Path | None = None
        self._segment: Segment | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="vedit-video", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._wake:
            self._running = False
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._close_source()

    # -- control ---------------------------------------------------------------

    def set_playlist(self, playlist: Playlist, position: int) -> None:
        with self._wake:
            self._playlist = playlist
            self._queue.clear()
            self._seek_to = position
            self._generation += 1
            self._at_end = False
            self._wake.notify_all()

    def seek(self, frame: int) -> None:
        with self._wake:
            self._seek_to = max(0, frame)
            self._queue.clear()
            self._generation += 1
            self._at_end = False
            self._wake.notify_all()

    @property
    def at_end(self) -> bool:
        with self._lock:
            return self._at_end and not self._queue

    def take_error(self) -> str | None:
        with self._lock:
            error, self._error = self._error, None
        return error

    # -- consuming -------------------------------------------------------------

    def frame_for(self, frame_index: int) -> DecodedFrame | None:
        """Newest queued frame at or before `frame_index`, discarding older ones.

        Dropping rather than queueing is deliberate: if decode falls behind, the
        picture should skip to stay with the clock, not drift further back.
        """
        with self._wake:
            chosen: DecodedFrame | None = None
            while self._queue and self._queue[0].frame_index <= frame_index:
                chosen = self._queue.pop(0)
            if chosen is not None:
                self._wake.notify_all()
            return chosen

    def peek_any(self) -> DecodedFrame | None:
        """First queued frame regardless of time — used to show something during
        a scrub, where waiting for an exact match would look frozen."""
        with self._lock:
            return self._queue[0] if self._queue else None

    # -- the thread ------------------------------------------------------------

    def _close_source(self) -> None:
        if self._source is not None:
            self._source.close()
            self._source = None
            self._source_path = None
            self._segment = None

    def _open_for(self, segment: Segment, frame_index: int) -> None:
        if segment.path is None:
            self._close_source()
            self._segment = segment
            return

        if self._source is None or self._source_path != segment.path:
            self._close_source()
            self._source = _OpenSource(segment.path)
            self._source_path = segment.path

        self._segment = segment
        self._source.seek(segment.source_seconds(frame_index, self.timebase))

    def _run(self) -> None:
        while True:
            with self._wake:
                if not self._running:
                    break

                if self._seek_to is not None:
                    target, self._seek_to = self._seek_to, None
                    generation = self._generation
                    playlist = self._playlist
                    do_seek = True
                else:
                    target = self._position
                    generation = self._generation
                    playlist = self._playlist
                    do_seek = False

                if not do_seek and (len(self._queue) >= QUEUE_DEPTH or self._at_end):
                    self._wake.wait(0.05)
                    continue

            try:
                if do_seek:
                    self._do_seek(playlist, target, generation)
                else:
                    self._decode_one(playlist, generation)
            except Exception as exc:  # noqa: BLE001 - a bad file must not kill playback
                with self._wake:
                    self._error = str(exc)
                    self._at_end = True
                self._close_source()

    def _do_seek(self, playlist: Playlist, target: int, generation: int) -> None:
        segment = playlist.at(target)
        if segment is None:
            with self._wake:
                if generation == self._generation:
                    self._position = target
                    self._at_end = True
            return

        self._position = target
        self._open_for(segment, target)

        if segment.is_gap:
            with self._wake:
                if generation == self._generation:
                    self._queue.append(DecodedFrame(target, QImage()))
            return

        # Decode forward from the keyframe to the exact requested frame.
        assert self._source is not None
        wanted = segment.source_seconds(target, self.timebase)
        tolerance = float(self.timebase.frame_duration()) / 2

        for frame in self._source.frames():
            if self._generation != generation:
                return
            position = self._source.seconds_of(frame)
            if position + tolerance < wanted:
                continue
            index = segment.timeline_frame_for(position, self.timebase)
            with self._wake:
                if generation != self._generation:
                    return
                self._queue.append(DecodedFrame(max(index, segment.tl_start), _to_qimage(frame)))
                self._position = index + 1
                self._wake.notify_all()
            return

        with self._wake:
            if generation == self._generation:
                self._at_end = True

    def _decode_one(self, playlist: Playlist, generation: int) -> None:
        position = self._position
        segment = self._segment

        # Crossing a clip boundary: open the next source and carry on.
        if segment is None or position >= segment.tl_end:
            segment = playlist.at(position)
            if segment is None:
                with self._wake:
                    if generation == self._generation:
                        self._at_end = True
                return
            self._open_for(segment, position)

        assert segment is not None
        if segment.is_gap:
            with self._wake:
                if generation != self._generation:
                    return
                self._queue.append(DecodedFrame(position, QImage()))
                self._position = min(position + 1, segment.tl_end)
                if self._position >= segment.tl_end:
                    self._segment = None
            return

        assert self._source is not None
        try:
            frame = next(self._source.frames())
        except (StopIteration, av.error.EOFError):
            # Source ran out early — advance past this segment rather than stall.
            with self._wake:
                if generation == self._generation:
                    self._position = segment.tl_end
                    self._segment = None
            return

        position_seconds = self._source.seconds_of(frame)
        index = max(segment.timeline_frame_for(position_seconds, self.timebase), segment.tl_start)

        if index >= segment.tl_end:
            with self._wake:
                if generation == self._generation:
                    self._position = segment.tl_end
                    self._segment = None
            return

        image = _to_qimage(frame)
        with self._wake:
            if generation != self._generation:
                return
            self._queue.append(DecodedFrame(index, image))
            self._position = index + 1
            self._wake.notify_all()
