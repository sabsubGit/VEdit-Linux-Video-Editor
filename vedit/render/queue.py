"""The render queue: jobs run one at a time, in order.

Sequential on purpose. Two concurrent x264 encodes on eight cores finish no
sooner than the same two run back to back, and running them together makes the
whole machine — including the preview player — unresponsive while they do.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal

from vedit.render.job import JobState, RenderJob

COLUMNS = ("Output", "Preset", "Progress", "Status")


class RenderQueue(QAbstractTableModel):
    job_finished = Signal(object, bool, str)   # RenderJob, ok, message
    queue_finished = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._jobs: list[RenderJob] = []
        self._labels: dict[int, str] = {}
        self._running: RenderJob | None = None

    # -- Qt model --------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._jobs)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        job = self._jobs[index.row()]

        if role == Qt.DisplayRole:
            if index.column() == 0:
                return job.output.name
            if index.column() == 1:
                return self._labels.get(id(job), "")
            if index.column() == 2:
                return f"{job.value * 100:.0f}%"
            if index.column() == 3:
                return job.state.value
        elif role == Qt.ToolTipRole:
            return job.message or str(job.output)
        elif role == Qt.TextAlignmentRole and index.column() in (2, 3):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    # -- queue -----------------------------------------------------------------

    def job_at(self, row: int) -> RenderJob | None:
        return self._jobs[row] if 0 <= row < len(self._jobs) else None

    @property
    def running(self) -> RenderJob | None:
        return self._running

    def add(self, job: RenderJob, preset_name: str = "") -> None:
        row = len(self._jobs)
        self.beginInsertRows(QModelIndex(), row, row)
        self._jobs.append(job)
        self._labels[id(job)] = preset_name
        self.endInsertRows()

        job.progress.connect(lambda _v, j=job: self._touch(j, 2))
        job.state_changed.connect(lambda _s, j=job: self._touch(j, 3))
        job.finished.connect(lambda ok, msg, j=job: self._on_finished(j, ok, msg))

        self.pump()

    def pump(self) -> None:
        """Start the next queued job if nothing is running."""
        if self._running is not None:
            return
        for job in self._jobs:
            if job.state is JobState.QUEUED:
                self._running = job
                job.start()
                return

    def cancel(self, job: RenderJob) -> None:
        job.cancel()

    def cancel_all(self) -> None:
        for job in self._jobs:
            if not job.state.finished:
                job.cancel()

    def clear_finished(self) -> None:
        keep = [job for job in self._jobs if not job.state.finished]
        if len(keep) == len(self._jobs):
            return
        self.beginResetModel()
        self._jobs = keep
        self.endResetModel()

    def _touch(self, job: RenderJob, column: int) -> None:
        try:
            row = self._jobs.index(job)
        except ValueError:
            return
        index = self.index(row, column)
        self.dataChanged.emit(index, index)

    def _on_finished(self, job: RenderJob, ok: bool, message: str) -> None:
        if self._running is job:
            self._running = None
        self._touch(job, 2)
        self._touch(job, 3)
        self.job_finished.emit(job, ok, message)
        self.pump()
        if self._running is None:
            self.queue_finished.emit()
