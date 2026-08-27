"""Audio playback and the master clock.

`QAudioSink` is used rather than PortAudio/sounddevice so the app needs no audio
dependency beyond PySide6 itself. It is fed in *pull* mode: Qt calls `readData`
whenever the device wants more, which means the device's own consumption rate
sets the pace and we never have to guess at buffer timing.

`clock_frame()` is the value the whole player synchronises to. It is derived from
how many sample frames the device has actually consumed, which is the one number
that matches what a listener is hearing at that instant.
"""

from __future__ import annotations

import threading
from pathlib import Path

import av
import av.error
import numpy as np
from PySide6.QtCore import QIODevice
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

from vedit.core.timebase import TimeBase
from vedit.player.segments import Playlist, Segment

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = CHANNELS * 2      # int16 stereo
TARGET_BUFFER_SECONDS = 0.6         # how far ahead the decoder works


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

    def readData(self, maxlen: int) -> bytes:
        data = self.ring.read(int(maxlen))
        if not data:
            # Underrun: hand back silence rather than nothing. Returning an empty
            # buffer makes QAudioSink go idle and stop, which would end playback
            # on a momentary decode hiccup.
            padding = min(int(maxlen), 4096)
            padding -= padding % BYTES_PER_FRAME
            self.consumed_bytes += padding
            return bytes(padding)
        self.consumed_bytes += len(data)
        return data

    def writeData(self, data) -> int:  # pragma: no cover - output only
        return 0

    def bytesAvailable(self) -> int:
        return self.ring.available() + super().bytesAvailable()

    def isSequential(self) -> bool:
        return True


class AudioStreamer:
    """Decodes the audio playlist into PCM and plays it through QAudioSink."""

    def __init__(self, timebase: TimeBase) -> None:
        self.timebase = timebase
        self._playlist = Playlist([], 0)
        self._start_frame = 0

        capacity = int(SAMPLE_RATE * TARGET_BUFFER_SECONDS) * BYTES_PER_FRAME
        self._ring = _RingBuffer(capacity)
        self._device: _SinkDevice | None = None
        self._sink: QAudioSink | None = None

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._active = False
        self._silent = False        # true when the timeline has no audio at all

    # -- setup ------------------------------------------------------------------

    def set_playlist(self, playlist: Playlist, position: int) -> None:
        was_active = self._active
        self.stop()
        self._playlist = playlist
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
        return any(not segment.is_gap for segment in self._playlist.segments)

    # -- transport --------------------------------------------------------------

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
            self._sink.stop()
            self._sink = None
        if self._device is not None:
            self._device.close()
            self._device = None
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        self._ring.clear()
        self._active = False

    def shutdown(self) -> None:
        self.stop()

    # -- clock ------------------------------------------------------------------

    def clock_frame(self) -> float | None:
        """Timeline frame the audio device has reached, or None if it is silent.

        `processedUSecs()` is used rather than counting the bytes we hand over,
        because it is reported at the device's own granularity (about 21 ms)
        rather than at whatever chunk size Qt happens to ask for. It still steps
        rather than flowing, which is why `MasterClock` interpolates between the
        values instead of using them directly.

        The device's own buffer sits between "processed" and "heard", so that
        latency is subtracted to keep picture aligned with sound.
        """
        if not self._active or self._sink is None:
            return None

        processed = self._sink.processedUSecs() / 1_000_000.0
        buffered_bytes = max(0, self._sink.bufferSize() - self._sink.bytesFree())
        latency = (buffered_bytes / BYTES_PER_FRAME) / SAMPLE_RATE
        heard = max(0.0, processed - latency)

        return self._start_frame + heard * float(self.timebase.fps)

    # -- the decode thread ------------------------------------------------------

    def _resampler(self):
        return av.AudioResampler(format="s16", layout="stereo", rate=SAMPLE_RATE)

    def _silence(self, frames: int) -> bytes:
        seconds = float(self.timebase.frames_to_seconds(frames))
        return bytes(int(seconds * SAMPLE_RATE) * BYTES_PER_FRAME)

    def _run(self) -> None:
        position = self._start_frame
        try:
            while not self._stop.is_set():
                segment = self._playlist.at(position)
                if segment is None:
                    self._ring.set_eof()
                    return
                if not self._feed_segment(segment, position):
                    return
                position = segment.tl_end
        except Exception:  # noqa: BLE001 - a bad file silences audio, nothing worse
            self._ring.set_eof()

    def _feed_segment(self, segment: Segment, from_frame: int) -> bool:
        if segment.is_gap or segment.path is None:
            return self._ring.write(
                self._silence(segment.tl_end - from_frame), stop=self._stop
            )
        return self._feed_file(segment, from_frame)

    def _feed_file(self, segment: Segment, from_frame: int) -> bool:
        try:
            container = av.open(str(segment.path))
        except (av.error.FFmpegError, OSError):
            return self._ring.write(self._silence(segment.tl_end - from_frame), stop=self._stop)

        try:
            if not container.streams.audio:
                return self._ring.write(
                    self._silence(segment.tl_end - from_frame), stop=self._stop
                )
            stream = container.streams.audio[0]

            start_seconds = segment.source_seconds(from_frame, self.timebase)
            end_seconds = segment.source_seconds(segment.tl_end, self.timebase)
            container.seek(
                int(start_seconds / float(stream.time_base)),
                stream=stream,
                backward=True,
            )

            resampler = self._resampler()
            for frame in container.decode(stream):
                if self._stop.is_set():
                    return False

                frame_seconds = float((frame.pts or 0) * stream.time_base)
                if frame_seconds + float(frame.samples) / (frame.rate or SAMPLE_RATE) < start_seconds:
                    continue  # still before the in-point after the keyframe seek
                if frame_seconds >= end_seconds:
                    break

                for resampled in resampler.resample(frame):
                    data = resampled.to_ndarray()
                    pcm = self._trim(data, frame_seconds, start_seconds, end_seconds)
                    if pcm.size and not self._ring.write(pcm.tobytes(), stop=self._stop):
                        return False
            return True
        finally:
            container.close()

    @staticmethod
    def _trim(data: np.ndarray, frame_seconds: float, start: float, end: float) -> np.ndarray:
        """Cut a decoded block down to the part inside the clip's in/out points.

        Without this the first block after a seek would replay audio from before
        the in-point, which is heard as a stutter at every cut.
        """
        samples = data.reshape(-1) if data.ndim == 1 else data.T.reshape(-1)
        total_pairs = samples.size // CHANNELS
        if total_pairs == 0:
            return samples[:0]

        lead = int(max(0.0, start - frame_seconds) * SAMPLE_RATE)
        tail_limit = int(max(0.0, end - frame_seconds) * SAMPLE_RATE)
        first = min(lead, total_pairs)
        last = min(tail_limit, total_pairs)
        if last <= first:
            return samples[:0]
        return samples[first * CHANNELS : last * CHANNELS]
