"""Audio playback, mixing and the master clock.

`QAudioSink` is used rather than PortAudio/sounddevice so the app needs no audio
dependency beyond PySide6 itself. It is fed in *pull* mode: Qt calls `readData`
whenever the device wants more, which means the device's own consumption rate
sets the pace and we never have to guess at buffer timing.

`clock_frame()` is the value the whole player synchronises to. It is derived from
how many sample frames the device has actually consumed, which is the one number
that matches what a listener is hearing at that instant.

Several audio lanes play at once, so each lane gets a `_LaneReader` that hands
out PCM on demand and the mixer sums them. Pulling a fixed block from every lane
and adding is what keeps the lanes sample-aligned; letting each lane write into
the buffer at its own pace would drift them apart within seconds.

Levels are applied at three points, in this order: clip gain and fades inside the
lane reader, the lane fader as the mixer pulls each block, and the master fader
on the sum. The render graph applies the same three in the same order, which is
what makes the exported file sound like what was monitored.

Everything between the resampler and the final int16 cast is **float32**. A clip
boosted to +6 dB clipped in the reader, clipped again after the lane fader and
clipped a third time after the master is triple-clipped rubbish; in float there
is exactly one clip, at the end, where it belongs.
"""

from __future__ import annotations

import collections
import threading
from dataclasses import dataclass, field
from pathlib import Path

import av
import av.error
import numpy as np
from PySide6.QtCore import QIODevice
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from vedit.core.timebase import TimeBase
from vedit.player.segments import Playlist, Segment
from vedit.timeline.levels import from_db
from vedit.timeline.model import Timeline

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2      # int16 stereo
TARGET_BUFFER_SECONDS = 0.6         # how far ahead the decoder works
# A reversed clip is read in spans this long and flipped in memory. Long enough
# that the seek at the head of each one is amortised, short enough that a lane
# never holds more than a couple of hundred kilobytes of flipped audio no matter
# how long the clip is.
REVERSE_SPAN_SECONDS = 0.5
REVERSE_BLOCK = 4096                # sample-frames handed out at a time


def envelope(
    count: int,
    *,
    offset: int,
    length: int,
    fade_in: int,
    fade_out: int,
    gain: float,
) -> np.ndarray:
    """The gain curve for `count` sample-frames starting `offset` into a segment.

    Every position is measured from the start of the *segment*, not the start of
    the block. That is the whole point: blocks arrive about 50 ms at a time, so a
    two-second fade is spread across forty of them, and a curve derived from the
    position within a block would restart the ramp forty times.

    Linear ramps, matching the `curve=tri` default of ffmpeg's `afade` — the
    preview and the export have to agree on the shape, not only the length.

    A module-level function rather than a method so it can be tested with no
    file, no device and no thread.
    """
    if fade_in <= 0 and fade_out <= 0:
        return np.full(count, gain, dtype=np.float32)

    index = np.arange(offset, offset + count, dtype=np.float32)
    curve = np.ones(count, dtype=np.float32)
    if fade_in > 0:
        np.minimum(curve, index / float(fade_in), out=curve)
    if fade_out > 0:
        np.minimum(curve, (float(length) - index) / float(fade_out), out=curve)
    np.clip(curve, 0.0, 1.0, out=curve)
    return curve * np.float32(gain)


class _RingBuffer:
    """Bounded PCM buffer shared between the decode thread and Qt's audio pull.

    Bounded on purpose: an unbounded queue would let the decoder race ahead and
    make a seek take effect only after everything already buffered had played.
    """

    def __init__(self, capacity_bytes: int) -> None:
        self._buffer = bytearray()
        self._capacity = capacity_bytes
        self._lock = threading.Lock()
        self._space = threading.Condition(self._lock)
        self._eof = False

    def write(self, data: bytes, *, stop: threading.Event) -> bool:
        with self._space:
            while len(self._buffer) + len(data) > self._capacity and not stop.is_set():
                self._space.wait(0.05)
            if stop.is_set():
                return False
            self._buffer.extend(data)
            return True

    def read(self, size: int) -> bytes:
        with self._space:
            take = min(size, len(self._buffer))
            data = bytes(self._buffer[:take])
            del self._buffer[:take]
            self._space.notify_all()
            return data

    def available(self) -> int:
        with self._lock:
            return len(self._buffer)

    def clear(self) -> None:
        with self._space:
            self._buffer.clear()
            self._eof = False
            self._space.notify_all()

    def set_eof(self) -> None:
        with self._space:
            self._eof = True
            self._space.notify_all()

    @property
    def eof(self) -> bool:
        with self._lock:
            return self._eof


class _SinkDevice(QIODevice):
    """The QIODevice Qt pulls from, backed by the ring buffer."""

    def __init__(self, ring: _RingBuffer, parent=None) -> None:
        super().__init__(parent)
        self.ring = ring
        self.consumed_bytes = 0
        # Silence handed back on an underrun. It advances the device's own clock
        # but was never metered, so the meter has to discount it or one hiccup
        # offsets the bars ahead of the sound permanently. Written only by Qt's
        # audio thread and read only by the UI thread, so a stale read is
        # possible and a torn one is not — taking a lock inside the audio
        # callback would be the worse trade.
        self.padded_bytes = 0

    def readData(self, maxlen: int) -> bytes:
        data = self.ring.read(int(maxlen))
        if not data:
            # Underrun: hand back silence rather than nothing. Returning an empty
            # buffer makes QAudioSink go idle and stop, which would end playback
            # on a momentary decode hiccup.
            padding = min(int(maxlen), 4096)
            padding -= padding % BYTES_PER_FRAME
            self.consumed_bytes += padding
            self.padded_bytes += padding
            return bytes(padding)
        self.consumed_bytes += len(data)
        return data

    def writeData(self, data) -> int:  # pragma: no cover - output only
        return 0

    def bytesAvailable(self) -> int:
        return self.ring.available() + super().bytesAvailable()

    def isSequential(self) -> bool:
        return True


class _LaneReader:
    """Reads one audio lane as a continuous PCM stream.

    Hands back exactly as many samples as asked for, padding with silence for
    gaps and for sources that end early, so the mixer can add lanes together
    without worrying about which of them currently has content.

    Position is counted in **samples**, never in frames. Counting frames means
    rounding every decoded block to a whole frame: a 1024-sample AAC block is
    0.64 frames at 30 fps, and rounding that to 1 makes the reader believe it is
    1.56x further along than the audio it has actually produced. It then runs off
    the end of the timeline early and the lane goes silent for the rest of
    playback — on an 18-second timeline, after about 11 seconds.

    Hands back **float32**, already carrying each clip's own gain and fades. The
    lane fader and the master are applied further up, by the mixer.
    """

    def __init__(self, playlist: Playlist, timebase: TimeBase, start_frame: int) -> None:
        self.playlist = playlist
        self.timebase = timebase
        self._pending = np.zeros(0, dtype=np.float32)
        self._container = None
        self._segment: Segment | None = None
        self._frames = None
        self._resampler = None
        self._stream = None
        self._seek_target = 0.0
        self._sample_pos = self.samples_at(start_frame)
        # Reversed segments only: audio already decoded and flipped, waiting to
        # be handed out, and how far down the source the flip has got to.
        self._reversed_pending = np.zeros(0, dtype=np.float32)
        self._reversed_top = 0.0     # source seconds; the next span ends here
        self._reversed_floor = 0.0   # source seconds; the segment stops here

    # -- position --------------------------------------------------------------

    def samples_at(self, frame: int) -> int:
        """Sample offset of a timeline frame."""
        return int(round(float(self.timebase.frames_to_seconds(frame)) * SAMPLE_RATE))

    @property
    def elapsed_seconds(self) -> float:
        return self._sample_pos / SAMPLE_RATE

    @property
    def position(self) -> int:
        """Timeline frame this lane has produced audio up to."""
        return int(self.elapsed_seconds * float(self.timebase.fps))

    @property
    def finished(self) -> bool:
        return self._sample_pos >= self.samples_at(self.playlist.duration)

    def close(self) -> None:
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
            self._container = None
        self._frames = None
        self._segment = None
        self._reversed_pending = np.zeros(0, dtype=np.float32)

    # -- reading ---------------------------------------------------------------

    def read(self, wanted: int, stop: threading.Event) -> np.ndarray:
        """`wanted` interleaved stereo sample-frames as float32."""
        out = np.zeros(wanted * CHANNELS, dtype=np.float32)
        filled = 0

        while filled < wanted and not stop.is_set():
            if self._pending.size == 0 and not self._refill(stop):
                break  # nothing left anywhere; the rest stays silent
            take = min(wanted - filled, self._pending.size // CHANNELS)
            if take <= 0:
                break
            out[filled * CHANNELS : (filled + take) * CHANNELS] = self._pending[: take * CHANNELS]
            self._pending = self._pending[take * CHANNELS :]
            filled += take

        return out

    def _silence_to(self, frame: int) -> None:
        """Queue silence up to a timeline frame, exactly."""
        target = self.samples_at(frame)
        count = max(0, target - self._sample_pos)
        self._pending = np.zeros(count * CHANNELS, dtype=np.float32)
        self._sample_pos = max(self._sample_pos, target)

    def _refill(self, stop: threading.Event) -> bool:
        """Decode more audio into `_pending`. False once the timeline runs out."""
        while not stop.is_set():
            segment = self.playlist.at(self.position)
            if segment is None:
                return False

            if segment.is_gap or segment.path is None:
                self._silence_to(segment.tl_end)
                self.close()
                return True

            if self._segment is None or self._segment is not segment:
                if not self._open(segment):
                    self._silence_to(segment.tl_end)
                    return True

            block = self._next_block(segment)
            if block is None:
                # Source ran dry before the segment did; pad the remainder so the
                # lane stays aligned with the others.
                self._silence_to(segment.tl_end)
                self.close()
                return True

            self._pending = block
            return True
        return False

    # -- decoding --------------------------------------------------------------

    def _source_seconds_now(self, segment: Segment) -> float:
        """Exact source time for the current sample position.

        Derived from samples rather than the floored frame, so a seek cannot
        replay up to a frame of audio it has already emitted.
        """
        segment_start = float(self.timebase.frames_to_seconds(segment.tl_start))
        into_segment = max(0.0, self.elapsed_seconds - segment_start)
        source_start = float(self.timebase.frames_to_seconds(segment.src_start))
        travelled = into_segment * segment.speed
        return source_start - travelled if segment.reversed else source_start + travelled

    def _open(self, segment: Segment) -> bool:
        self.close()
        try:
            container = av.open(str(segment.path))
        except (av.error.FFmpegError, OSError):
            return False
        if not container.streams.audio:
            container.close()
            return False

        stream = container.streams.audio[0]
        seconds = self._source_seconds_now(segment)
        try:
            container.seek(int(seconds / float(stream.time_base)), stream=stream, backward=True)
        except (av.error.FFmpegError, OSError):
            container.close()
            return False

        self._container = container
        self._segment = segment
        self._stream = stream
        self._frames = container.decode(stream)
        self._resampler = av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)
        self._seek_target = seconds
        if segment.reversed:
            self._reversed_pending = np.zeros(0, dtype=np.float32)
            # Playing backwards, "where we are now" is the *top* of the span
            # still to be read, and the segment ends at its lower edge.
            self._reversed_top = seconds
            self._reversed_floor = segment.source_seconds(segment.tl_end, self.timebase)
        return True

    def _next_block(self, segment: Segment) -> np.ndarray | None:
        if segment.reversed:
            return self._next_block_reversed(segment)

        end_seconds = segment.source_seconds(segment.tl_end, self.timebase)
        while True:
            try:
                frame = next(self._frames)
            except (StopIteration, av.error.FFmpegError):
                return None

            frame_seconds = float((frame.pts or 0) * self._stream.time_base)
            length = float(frame.samples) / float(frame.rate or SAMPLE_RATE)
            if frame_seconds + length <= self._seek_target:
                continue  # still before the in-point after a keyframe seek
            if frame_seconds >= end_seconds:
                return None

            resampled = self._resampler.resample(frame)
            if not resampled:
                continue

            pieces = [self._flatten(r.to_ndarray()) for r in resampled]
            data = np.concatenate(pieces) if len(pieces) > 1 else pieces[0]
            data = self._trim(data, frame_seconds, self._seek_target, end_seconds)
            if data.size == 0:
                continue

            # Into float before anything is scaled: from here to the final cast
            # there is no int16 to wrap around.
            data = data.astype(np.float32)
            if segment.speed != 1.0:
                data = self._retime(data, segment.speed)

            produced = data.size // CHANNELS
            data = self._shape(data, segment, produced)

            # Count exactly what was produced. Anything else drifts.
            self._sample_pos += produced
            return data

    # -- reverse ---------------------------------------------------------------

    def _next_block_reversed(self, segment: Segment) -> np.ndarray | None:
        """One block of a clip playing backwards.

        Decoders only run forwards, so a span of source is decoded normally,
        flipped, and then handed out in ordinary-sized blocks. Spans are read
        from the top of the window down, which keeps the memory bounded however
        long the clip is — the alternative, buffering the whole clip the way the
        renderer's `areverse` does, would be unbounded on exactly the takes
        people reverse.

        The joins between spans are sample-exact; what they cannot carry across
        is the decoder's own filter state, so a span boundary can be a faint
        tick on dense material. Preview only — the render reverses the clip in
        one piece.
        """
        if self._reversed_pending.size == 0:
            span = self._read_reversed_span(segment)
            if span is None:
                return None
            self._reversed_pending = span

        take = min(REVERSE_BLOCK * CHANNELS, self._reversed_pending.size)
        data = self._reversed_pending[:take]
        self._reversed_pending = self._reversed_pending[take:]

        produced = data.size // CHANNELS
        data = self._shape(data, segment, produced)
        self._sample_pos += produced
        return data

    def _read_reversed_span(self, segment: Segment) -> np.ndarray | None:
        """Decode the next span down the source and flip it. None when spent."""
        # A fraction of a sample-frame left is nothing; stop rather than seek
        # for it.
        if self._reversed_top - self._reversed_floor < 1.0 / SAMPLE_RATE:
            return None

        top = self._reversed_top
        bottom = max(self._reversed_floor, top - REVERSE_SPAN_SECONDS)
        self._reversed_top = bottom

        data = self._decode_span(bottom, top)
        if data is None or data.size == 0:
            return None

        if segment.speed != 1.0:
            data = self._retime(data, segment.speed)
        return data.reshape(-1, CHANNELS)[::-1].reshape(-1)

    def _decode_span(self, start: float, end: float) -> np.ndarray | None:
        """Forward-decode `[start, end)` source seconds as float32 stereo."""
        if self._container is None or self._stream is None:
            return None
        try:
            self._container.seek(
                int(start / float(self._stream.time_base)), stream=self._stream, backward=True
            )
        except (av.error.FFmpegError, OSError):
            return None
        self._frames = self._container.decode(self._stream)

        pieces: list[np.ndarray] = []
        for frame in self._frames:
            frame_seconds = float((frame.pts or 0) * self._stream.time_base)
            if frame_seconds >= end:
                break
            length = float(frame.samples) / float(frame.rate or SAMPLE_RATE)
            if frame_seconds + length <= start:
                continue

            resampled = self._resampler.resample(frame)
            if not resampled:
                continue
            block = np.concatenate([self._flatten(r.to_ndarray()) for r in resampled])
            block = self._trim(block, frame_seconds, start, end)
            if block.size:
                pieces.append(block.astype(np.float32))

        if not pieces:
            return None
        return np.concatenate(pieces) if len(pieces) > 1 else pieces[0]

    def _shape(self, data: np.ndarray, segment: Segment, produced: int) -> np.ndarray:
        """Apply the clip's own gain and fades to a decoded block.

        Called before `_sample_pos` advances, because at that moment it is
        exactly the absolute position of this block's first sample — which is
        the offset the envelope needs to continue a ramp across a block boundary
        rather than restarting it.
        """
        if segment.gain == 1.0 and not segment.fade_in and not segment.fade_out:
            return data

        start = self.samples_at(segment.tl_start)
        curve = envelope(
            produced,
            offset=self._sample_pos - start,
            length=self.samples_at(segment.tl_end) - start,
            fade_in=self.samples_at(segment.tl_start + segment.fade_in) - start,
            fade_out=(
                self.samples_at(segment.tl_end)
                - self.samples_at(segment.tl_end - segment.fade_out)
            ),
            gain=segment.gain,
        )
        return (data.reshape(-1, CHANNELS) * curve[:, None]).reshape(-1)

    @staticmethod
    def _flatten(data: np.ndarray) -> np.ndarray:
        return data.reshape(-1) if data.ndim == 1 else data.T.reshape(-1)

    @staticmethod
    def _trim(data: np.ndarray, frame_seconds: float, start: float, end: float) -> np.ndarray:
        """Cut a decoded block to the part inside the clip's in/out points.

        Without this the first block after a seek replays audio from before the
        in-point, which is heard as a stutter at every cut.
        """
        total = data.size // CHANNELS
        if total == 0:
            return data[:0]
        lead = min(int(max(0.0, start - frame_seconds) * SAMPLE_RATE), total)
        tail = min(int(max(0.0, end - frame_seconds) * SAMPLE_RATE), total)
        if tail <= lead:
            return data[:0]
        return data[lead * CHANNELS : tail * CHANNELS]

    @staticmethod
    def _retime(data: np.ndarray, speed: float) -> np.ndarray:
        """Resample for a speed change.

        Straight index resampling, so pitch rises and falls with speed like a
        tape machine. The renderer uses `asetrate` for the same reason: a
        pitch-preserving preview would not match the exported file.
        """
        pairs = data.size // CHANNELS
        if pairs == 0:
            return data
        out_pairs = max(1, int(pairs / speed))
        picks = np.minimum((np.arange(out_pairs) * speed).astype(np.int64), pairs - 1)
        return data.reshape(-1, CHANNELS)[picks].reshape(-1)


class MixerState:
    """Fader positions shared between the UI thread and the decode thread.

    Lane and master gain are read live, once per mixed block, instead of being
    baked into the playlist. A fader is a continuous control: rebuilding the
    playlist and restarting the device on every pixel of a drag would stutter
    twenty times a second. Clip gain and fades *are* baked in, because they are
    part of the edit rather than a monitoring level.

    One uncontended lock per block, holding a handful of floats.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lanes: dict[str, float] = {}
        self._master = 1.0

    def set_lane(self, track_id: str, gain: float) -> None:
        with self._lock:
            self._lanes[track_id] = float(gain)

    def set_master(self, gain: float) -> None:
        with self._lock:
            self._master = float(gain)

    def load(self, timeline: Timeline) -> None:
        """Resync from the model, which stays the source of truth."""
        lanes = {track.track_id: from_db(track.gain_db) for track in timeline.audio_tracks}
        master = from_db(timeline.master_gain_db)
        with self._lock:
            self._lanes = lanes
            self._master = master

    def snapshot(self) -> tuple[dict[str, float], float]:
        with self._lock:
            return dict(self._lanes), self._master


@dataclass(frozen=True, slots=True)
class MeterFrame:
    """Peaks for one mixed block, stamped with where it lands in the output."""

    sample: int                        # output sample-frame index of the first sample
    count: int
    lanes: dict[str, float] = field(default_factory=dict)     # post-fader, 0..1
    lane_clipped: dict[str, bool] = field(default_factory=dict)
    master: float = 0.0                # post-master peak, 0..1
    master_clipped: bool = False


def _merge_frames(frames: list[MeterFrame]) -> MeterFrame:
    """Reduce several played blocks to one reading, taking the loudest of each."""
    lanes: dict[str, float] = {}
    clipped: dict[str, bool] = {}
    master = 0.0
    master_clipped = False
    for frame in frames:
        for track_id, peak in frame.lanes.items():
            lanes[track_id] = max(lanes.get(track_id, 0.0), peak)
        for track_id, flag in frame.lane_clipped.items():
            clipped[track_id] = clipped.get(track_id, False) or flag
        master = max(master, frame.master)
        master_clipped = master_clipped or frame.master_clipped
    first = frames[0]
    last = frames[-1]
    return MeterFrame(
        sample=first.sample,
        count=last.sample + last.count - first.sample,
        lanes=lanes,
        lane_clipped=clipped,
        master=master,
        master_clipped=master_clipped,
    )


class MeterQueue:
    """Peaks measured in the decode thread, delivered when they are heard.

    The decoder runs the better part of a second ahead of the device, so metering
    what has just been decoded would show levels for audio nobody has heard yet:
    the bars would visibly lead the playhead, which reads as the meters being
    broken rather than as them being early.

    The ring buffer is a strict FIFO, so the n-th sample-frame written is the
    n-th played. Each block is therefore stamped with its position in the output
    stream and held until the device reports having played past it.
    """

    def __init__(self, capacity: int = 96) -> None:      # about 4.8 s of blocks
        self._lock = threading.Lock()
        self._frames: collections.deque[MeterFrame] = collections.deque(maxlen=capacity)

    def push(self, frame: MeterFrame) -> None:
        with self._lock:
            self._frames.append(frame)

    def clear(self) -> None:
        with self._lock:
            self._frames.clear()

    def drain(self, heard_samples: float) -> MeterFrame | None:
        """Every block fully played by `heard_samples`, reduced to one reading.

        `heard_samples` is passed in rather than read from the device here, which
        keeps the whole compensation rule a pure function — testable offscreen
        with no QAudioSink anywhere near it.

        Reduced rather than returned one at a time because blocks arrive at 20 Hz
        and the meters repaint at 30: taking the loudest of whatever played since
        the last repaint shows a transient between two frames instead of skipping
        it.
        """
        with self._lock:
            taken: list[MeterFrame] = []
            while self._frames and self._frames[0].sample + self._frames[0].count <= heard_samples:
                taken.append(self._frames.popleft())
        return _merge_frames(taken) if taken else None


def _signature(playlists: list[Playlist]) -> tuple:
    """A cheap identity for what the decoder would actually read.

    Every edit emits `timeline_changed`, and the streamer used to stop and
    restart the device for all of them — an audible gap for a video-only trim, a
    selection change or a fader move. Comparing the reads themselves makes the
    common case free.
    """
    return tuple(
        (
            playlist.track_id,
            playlist.duration,
            tuple(
                (
                    segment.tl_start,
                    segment.tl_end,
                    segment.path,
                    segment.src_start,
                    segment.speed,
                    segment.reversed,
                    segment.gain,
                    segment.fade_in,
                    segment.fade_out,
                )
                for segment in playlist.segments
            ),
        )
        for playlist in playlists
    )


class AudioStreamer:
    """Decodes the audio playlist into PCM and plays it through QAudioSink."""

    def __init__(self, timebase: TimeBase) -> None:
        self.timebase = timebase
        self._playlists: list[Playlist] = []
        self._start_frame = 0

        capacity = int(SAMPLE_RATE * TARGET_BUFFER_SECONDS) * BYTES_PER_FRAME
        self._ring = _RingBuffer(capacity)
        self._device: _SinkDevice | None = None
        self._sink: QAudioSink | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._silent = False        # true when the timeline has no audio at all

        self.mixer = MixerState()
        self.meters = MeterQueue()
        # Sample-frames handed to the ring since start(). Shares its origin with
        # QAudioSink.processedUSecs(), which is the whole basis of the meter's
        # latency compensation.
        self._out_samples = 0
        self._signature: tuple | None = None

    # -- setup ------------------------------------------------------------------

    def set_playlists(self, playlists: list[Playlist], position: int) -> None:
        signature = _signature(playlists)
        if signature == self._signature:
            # Nothing the decoder would read has changed, so the device is left
            # alone. This is what makes a fader move, a selection change and a
            # video-only trim silent instead of each costing a restart.
            self._playlists = list(playlists)
            return

        was_active = self._active
        self.stop()
        self._playlists = list(playlists)
        self._signature = signature
        if was_active:
            self.start(position)

    @staticmethod
    def _format() -> QAudioFormat:
        fmt = QAudioFormat()
        fmt.setSampleRate(SAMPLE_RATE)
        fmt.setChannelCount(CHANNELS)
        fmt.setSampleFormat(QAudioFormat.Int16)
        return fmt

    def _has_audio(self) -> bool:
        return any(
            not segment.is_gap
            for playlist in self._playlists
            for segment in playlist.segments
        )

    # -- transport --------------------------------------------------------------

    @property
    def active(self) -> bool:
        """True when a device is open and will report a position to steer by."""
        return self._active

    def start(self, frame: int) -> None:
        self.stop()
        self._start_frame = frame

        if not self._has_audio():
            # Nothing to play: stay quiet and let the engine fall back to the
            # wall clock rather than opening a device for silence.
            self._silent = True
            self._active = False
            return

        device = QMediaDevices.defaultAudioOutput()
        if device is None or device.isNull():
            self._silent = True
            self._active = False
            return

        self._silent = False
        self._stop.clear()
        self._ring.clear()
        # Both counters restart with the device. Draining stale frames against a
        # reset processedUSecs() would flash every meter at full scale.
        self.meters.clear()
        self._out_samples = 0

        self._thread = threading.Thread(target=self._run, name="vedit-audio", daemon=True)
        self._thread.start()

        self._sink = QAudioSink(device, self._format())
        self._sink.setBufferSize(int(SAMPLE_RATE * 0.15) * BYTES_PER_FRAME)
        self._device = _SinkDevice(self._ring)
        self._device.open(QIODevice.ReadOnly)
        self._sink.start(self._device)
        self._active = True

    def stop(self) -> None:
        self._stop.set()
        if self._sink is not None:
            # `reset` before `stop`, and the order matters. `stop` lets the
            # device finish what it has already been handed, which on a sink
            # holding 150 ms plus whatever the OS has queued means sound
            # carrying on for a moment after the user hit pause. `reset`
            # discards it instead, so pause is immediate.
            self._sink.reset()
            self._sink.stop()
            self._sink = None
        if self._device is not None:
            self._device.close()
            self._device = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._ring.clear()
        self.meters.clear()
        self._active = False

    def shutdown(self) -> None:
        self.stop()

    # -- clock ------------------------------------------------------------------

    def _heard_seconds(self) -> float | None:
        """Seconds of audio the listener has actually heard since `start()`.

        `processedUSecs()` is used rather than counting the bytes we hand over,
        because it is reported at the device's own granularity (about 21 ms)
        rather than at whatever chunk size Qt happens to ask for. It still steps
        rather than flowing, which is why `MasterClock` interpolates between the
        values instead of using them directly.

        The device's own buffer sits between "processed" and "heard", so that
        latency is subtracted. One definition, used by both the clock and the
        meters — they have to mean the same thing by "now".
        """
        if not self._active or self._sink is None:
            return None

        processed = self._sink.processedUSecs() / 1_000_000.0
        buffered_bytes = max(0, self._sink.bufferSize() - self._sink.bytesFree())
        latency = (buffered_bytes / BYTES_PER_FRAME) / SAMPLE_RATE
        return max(0.0, processed - latency)

    def clock_frame(self) -> float | None:
        """Timeline frame the audio device has reached, or None if it is silent."""
        heard = self._heard_seconds()
        if heard is None:
            return None
        return self._start_frame + heard * float(self.timebase.fps)

    # -- metering ---------------------------------------------------------------

    def meter_peaks(self) -> MeterFrame | None:
        """Peaks for the audio being heard right now, or None if nothing new played.

        The underrun padding is subtracted because it advanced the device's clock
        without ever having been metered. Left in, a single hiccup would push the
        meters permanently ahead of the sound, and the error would accumulate.
        """
        heard = self._heard_seconds()
        if heard is None or self._device is None:
            return None
        padded = self._device.padded_bytes / BYTES_PER_FRAME
        return self.meters.drain(heard * SAMPLE_RATE - padded)

    # -- the decode thread ------------------------------------------------------

    def _resampler(self):
        return av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)

    def _silence(self, frames: int) -> bytes:
        seconds = float(self.timebase.frames_to_seconds(frames))
        return bytes(int(seconds * SAMPLE_RATE) * BYTES_PER_FRAME)

    def _run(self) -> None:
        """Pull an equal block from every lane, mix them, and hand it on.

        Mixing in float32 and clipping once at the end matters: summing two loud
        lanes directly in int16 wraps around, which is heard as a loud crackle
        rather than as distortion, and clipping at each stage instead would
        flatten a boosted clip three times over.

        Peaks are measured post-fader per lane and post-master for the mix, and
        stamped with where the block lands in the output so the meters can be
        shown when the block is heard rather than when it is decoded.
        """
        readers = [
            _LaneReader(playlist, self.timebase, self._start_frame)
            for playlist in self._playlists
        ]
        block = max(256, int(SAMPLE_RATE * 0.05))

        try:
            while not self._stop.is_set():
                if not readers:
                    self._ring.set_eof()
                    return

                lanes, master = self.mixer.snapshot()
                mixed = np.zeros(block * CHANNELS, dtype=np.float32)
                lane_peaks: dict[str, float] = {}
                lane_clipped: dict[str, bool] = {}
                alive = False

                for reader in readers:
                    if not reader.finished:
                        alive = True
                    pcm = reader.read(block, self._stop)
                    gain = lanes.get(reader.playlist.track_id, 1.0)
                    if gain != 1.0:
                        pcm = pcm * gain
                    peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
                    track_id = reader.playlist.track_id
                    lane_peaks[track_id] = min(1.0, peak / 32768.0)
                    lane_clipped[track_id] = peak >= 32767.0
                    mixed += pcm

                if self._stop.is_set():
                    return
                if not alive:
                    self._ring.set_eof()
                    return

                if master != 1.0:
                    mixed *= master

                # Measured before the clip, because that is the only moment an
                # overload is still visible.
                master_peak = float(np.max(np.abs(mixed))) if mixed.size else 0.0
                self.meters.push(
                    MeterFrame(
                        sample=self._out_samples,
                        count=block,
                        lanes=lane_peaks,
                        lane_clipped=lane_clipped,
                        master=min(1.0, master_peak / 32768.0),
                        master_clipped=master_peak > 32767.0,
                    )
                )
                self._out_samples += block

                np.clip(mixed, -32768.0, 32767.0, out=mixed)
                if not self._ring.write(mixed.astype(np.int16).tobytes(), stop=self._stop):
                    return
        except Exception:  # noqa: BLE001 - a bad file silences audio, nothing worse
            self._ring.set_eof()
        finally:
            for reader in readers:
                reader.close()
