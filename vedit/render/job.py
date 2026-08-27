"""One render: an ffmpeg process, its progress, and its outcome.

Run through `QProcess` rather than a thread so output arrives as event-loop
signals — no polling and no cross-thread marshalling.

Progress comes from `-progress pipe:1`, which emits `out_time_us` as key=value
lines on stdout. Parsing that against the known timeline length gives a real
percentage, instead of the usual trick of scraping the human-readable log.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, Signal


class JobState(str, Enum):
    QUEUED = "Queued"
    RUNNING = "Rendering"
    DONE = "Done"
    FAILED = "Failed"
    CANCELLED = "Cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


class RenderJob(QObject):
    progress = Signal(float)          # 0..1
    state_changed = Signal(object)    # JobState
    finished = Signal(bool, str)      # ok, message

    def __init__(
        self,
        name: str,
        args: list[str],
        output: Path,
        total_seconds: float,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.args = args
        self.output = output
        self.total_seconds = max(total_seconds, 0.001)

        self.state = JobState.QUEUED
        self.value = 0.0
        self.message = ""
        self._stderr_tail: list[str] = []
        self._process: QProcess | None = None
        self._cancelled = False

    # -- running ---------------------------------------------------------------

    def start(self) -> None:
        if self.state is not JobState.QUEUED:
            return
        self.output.parent.mkdir(parents=True, exist_ok=True)

        process = QProcess(self)
        process.setProgram(self.args[0])
        process.setArguments(self.args[1:])
        process.readyReadStandardOutput.connect(self._read_progress)
        process.readyReadStandardError.connect(self._read_stderr)
        process.finished.connect(self._on_finished)
        process.errorOccurred.connect(self._on_error)

        self._process = process
        self._set_state(JobState.RUNNING)
        process.start()

    def cancel(self) -> None:
        self._cancelled = True
        if self._process is not None and self._process.state() != QProcess.NotRunning:
            self._process.terminate()
            if not self._process.waitForFinished(2000):
                self._process.kill()
        elif not self.state.finished:
            self._set_state(JobState.CANCELLED)
            self.finished.emit(False, "Cancelled")

    # -- process output --------------------------------------------------------

    def _read_progress(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode(errors="replace")
        for line in text.splitlines():
            key, separator, value = line.partition("=")
            if not separator:
                continue
            if key.strip() == "out_time_us":
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                self.value = max(0.0, min(seconds / self.total_seconds, 1.0))
                self.progress.emit(self.value)

    def _read_stderr(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardError()).decode(errors="replace")
        for line in text.splitlines():
            if line.strip():
                self._stderr_tail.append(line.strip())
        # Only the tail is useful; ffmpeg's opening banter is noise.
        del self._stderr_tail[:-12]

    def _error_detail(self) -> str:
        return "\n".join(self._stderr_tail[-4:]) or "ffmpeg failed with no output"

    def _on_error(self, _error) -> None:
        if self.state.finished:
            return
        self._set_state(JobState.FAILED)
        self.message = self._error_detail()
        self.finished.emit(False, self.message)

    def _on_finished(self, code: int, _status) -> None:
        if self.state.finished:
            return

        if self._cancelled:
            # A cancelled render leaves a truncated file that would otherwise
            # look like a finished export.
            self.output.unlink(missing_ok=True)
            self._set_state(JobState.CANCELLED)
            self.message = "Cancelled"
            self.finished.emit(False, self.message)
            return

        if code == 0:
            self.value = 1.0
            self.progress.emit(1.0)
            self._set_state(JobState.DONE)
            self.message = str(self.output)
            self.finished.emit(True, self.message)
        else:
            self.output.unlink(missing_ok=True)
            self._set_state(JobState.FAILED)
            self.message = self._error_detail()
            self.finished.emit(False, self.message)

    def _set_state(self, state: JobState) -> None:
        self.state = state
        self.state_changed.emit(state)
