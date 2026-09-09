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
import time
from dataclasses import dataclass
from pathlib import Path

import av
import av.error
import numpy as np
from PySide6.QtGui import QImage

from vedit.core.timebase import TimeBase
from vedit.player.segments import Playlist, Segment

QUEUE_DEPTH = 8

# How far behind the frame being asked for the decoder may fall before it stops
# turning what it decodes into images. Long enough to ride out a hiccup, short
# enough that the picture is never visibly stale.
LATE_SECONDS = 0.33
# Further behind than this and walking the stream will never close the gap, so
# seek instead and accept the keyframe run-up. Two seconds, because a seek on
# long-GOP media costs more than decoding a handful of frames and thrashing
# between the two would be worse than either.
SKIP_SECONDS = 2.0
# The least time between two catch-up seeks. On media that cannot be decoded in
# real time the seek does not close the gap — the run-up costs more than the
# time it saves — so it comes due again immediately, and left ungated the
# decoder spends the whole of playback seeking and delivers almost nothing.
SKIP_INTERVAL_SECONDS = 3.0
# The most frames in a row that may be decoded without being converted. Skipping
# the conversion helps the decoder catch up, but only if it *can*: if it cannot,
# an ungated rule discards every frame for ever and the viewer freezes on
# whatever was last handed to it. Past this many, one gets through regardless —
# a picture running behind is worth having, a still one is not.
MAX_DROPPED_RUN = 4


@dataclass(slots=True)
class DecodedFrame:
    frame_index: int      # timeline frame this image belongs to
    image: QImage


def _to_qimage(frame) -> QImage:
    """Convert a PyAV frame to a QImage.

    libswscale does the colour conversion in C, which at proxy resolution costs
    around a millisecond — far cheaper than doing it in Python, and simple enough
    that it needs no GL path.

    swscale pads each row up to an alignment boundary, so for widths whose byte
    length is not already aligned the result is **not** C-contiguous: a 484-wide
    frame is 1452 bytes per row but is handed back with a stride of 1488. Wrapping
    that buffer directly raises BufferError, which killed the decode thread and
    left the viewer black while audio carried on playing. Common widths like 1920
    and 1280 are already aligned, which is why this only shows up on unusual
    sizes.
    """
    array = frame.to_ndarray(format="rgb24")
    if not array.flags["C_CONTIGUOUS"]:
        array = np.ascontiguousarray(array)

    height, width, _ = array.shape
    image = QImage(array.data, width, height, 3 * width, QImage.Format_RGB888)
    # The ndarray owns the buffer; copy so it can be freed independently of Qt.
    return image.copy()


class _OpenSource:
    """One open container, positioned somewhere in one segment."""

    def __init__(self, path: Path) -> None:
        self.container = av.open(str(path))
        self.stream = self.container.streams.video[0]
        # Frame-level threading as well as slice, which is worth three to six
        # times the sequential decode rate: on a 1080p60 source a frame costs
        # 0.56 ms rather than 3.48 ms, and on a 540p proxy 0.18 ms rather than
        # 0.69 ms. This was `SLICE` alone on the theory that frame threading
        # delays the first frame after a seek; measured, it does not — a seek
        # plus one frame is 3.69 ms against 3.53 ms on a proxy, and slightly
        # *faster* on the original. The scrub is what that theory was
        # protecting and it is not paying for this.
        self.stream.thread_type = "AUTO"
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

        # The frame the viewer last asked for. Without it the decode thread has
        # no idea the clock has moved on: it walks the stream frame by frame,
        # and on media it cannot decode in real time it falls behind and stays
        # behind, for ever. See `_lateness`.
        self._wanted: int | None = None
        # Catch-up state: when the last skip-seek happened, and how many frames
        # in a row have been decoded without being shown. Both exist to stop the
        # cure being worse than the disease; see the constants above.
        self._last_skip = 0.0
        self._dropped_run = 0
        fps = float(timebase.fps)
        self._late_frames = max(2, int(round(LATE_SECONDS * fps)))
        self._skip_frames = max(self._late_frames * 2, int(round(SKIP_SECONDS * fps)))

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
            self._wanted = None
            self._generation += 1
            self._at_end = False
            self._wake.notify_all()

    def seek(self, frame: int) -> None:
        with self._wake:
            self._seek_to = max(0, frame)
            self._queue.clear()
            # Cleared, not carried: a request from before the jump says nothing
            # about where we are going, and left in place it would read as being
            # hopelessly behind and trigger an immediate skip back.
            self._wanted = None
            self._dropped_run = 0
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
            # Recorded even when nothing is waiting: this is the only channel by
            # which the decode thread learns where playback has actually got to.
            self._wanted = frame_index
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

        if not self._decode_at(segment, target, generation):
            with self._wake:
                if generation == self._generation:
                    self._at_end = True

    def _decode_at(self, segment: Segment, target: int, generation: int) -> bool:
        """Queue the one frame that plays at `target`, from an already-seeked source.

        Decodes forward from the keyframe the seek landed on and takes the first
        frame at or after the wanted source time. False if the source ran out
        before reaching it.
        """
        assert self._source is not None
        frame_duration = float(self.timebase.frame_duration())
        wanted = segment.source_seconds(target, self.timebase)
        if segment.reversed:
            # `source_seconds` gives the source *boundary* that timeline frame
            # sits at — for a reversed segment that is the top of the frame
            # playing there, so the frame itself starts one duration earlier.
            # Without this the very first frame back is one past the end of the
            # source and never arrives.
            wanted -= frame_duration
        tolerance = frame_duration / 2

        for frame in self._source.frames():
            if self._generation != generation:
                return True
            position = self._source.seconds_of(frame)
            if position + tolerance < wanted:
                continue
            # Reversed decoding asks for one specific frame and gets it; the
            # forward path decodes a run and lets each frame say where it goes.
            index = target if segment.reversed else segment.timeline_frame_for(
                position, self.timebase
            )
            with self._wake:
                if generation != self._generation:
                    return True
                self._queue.append(DecodedFrame(max(index, segment.tl_start), _to_qimage(frame)))
                self._position = max(index, target) + 1
                self._wake.notify_all()
            return True

        return False

    def _lateness(self, position: int) -> int:
        """How many frames behind the viewer's last request the decoder is.

        Negative — the normal, healthy case — means it is running ahead and
        filling the queue. Positive means frames are being decoded that the
        clock has already gone past, and every one of them is wasted work that
        makes the next one later still.
        """
        with self._lock:
            wanted = self._wanted
        return None if wanted is None else wanted - position

    def _decode_one(self, playlist: Playlist, generation: int) -> None:
        position = self._position
        segment = self._segment

        # Falling behind the clock is self-reinforcing: every frame decoded late
        # is decoded instead of the one actually wanted, so without this the gap
        # only ever grows and the picture drifts seconds behind the sound.
        late = self._lateness(position)
        now = time.monotonic()
        if (
            late is not None
            and late > self._skip_frames
            and now - self._last_skip >= SKIP_INTERVAL_SECONDS
        ):
            # Too far back for decoding to close the gap. Jump, and drop the
            # queued frames that are all now in the past. Rate-limited, because
            # if the seek does not close the gap it is due again at once, and
            # a decoder that only ever seeks shows nothing at all.
            with self._wake:
                if generation != self._generation:
                    return
                target = self._wanted
                self._queue.clear()
            self._last_skip = now
            self._dropped_run = 0
            self._do_seek(playlist, target, generation)
            return

        # Close enough that the stream will catch up on its own, but far enough
        # that these frames will never be shown. Decode them — the codec needs
        # them to reach the ones that will be — but skip the colour conversion,
        # which is the expensive half. Never more than a few in a row: on media
        # that cannot be decoded in real time the condition never clears, and
        # dropping unconditionally would mean the viewer is handed nothing for
        # as long as playback lasts.
        drop = late is not None and late > self._late_frames
        if drop and self._dropped_run >= MAX_DROPPED_RUN:
            drop = False
        self._dropped_run = self._dropped_run + 1 if drop else 0

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
        if not segment.is_gap and segment.reversed:
            self._decode_reversed(segment, position, generation)
            return

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

        if drop:
            # Nothing is queued: a null image means "gap" downstream and would
            # blank the viewer. Leaving the last good frame up is right — the
            # picture holds for a moment instead of flickering to black.
            with self._wake:
                if generation == self._generation:
                    self._position = index + 1
            return

        image = _to_qimage(frame)
        with self._wake:
            if generation != self._generation:
                return
            self._queue.append(DecodedFrame(index, image))
            self._position = index + 1
            self._wake.notify_all()

    def _decode_reversed(self, segment: Segment, position: int, generation: int) -> None:
        """One frame of a clip playing backwards.

        Codecs decode forwards only, so every frame of a reversed clip is a
        fresh seek plus a short run-up from the preceding keyframe — the same
        work a scrub does, once per frame. On proxy media, whose GOP is twelve
        frames, that is a handful of small decodes per displayed frame and the
        prefetch queue absorbs it. It is deliberately not the buffer-the-whole-
        clip approach the renderer's `reverse` filter takes: preview must not be
        able to exhaust memory on a long clip, and dropping a frame here only
        costs a stutter.
        """
        self._open_for(segment, position)
        if self._decode_at(segment, position, generation):
            return
        # The run-up found nothing — treat this frame as spent rather than
        # spinning on it, and let the next one seek somewhere new.
        with self._wake:
            if generation == self._generation:
                self._position = position + 1
                if self._position >= segment.tl_end:
                    self._segment = None
