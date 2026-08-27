"""Render page: export settings on the left, the queue on the right."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from vedit.core.ffmpeg import FFmpegError
from vedit.core.project import Project
from vedit.render.graph import RenderError, build_command
from vedit.render.job import RenderJob
from vedit.render.presets import Preset, available_presets
from vedit.render.queue import RenderQueue


class ExportSettings(QWidget):
    """Preset, quality, resolution and destination."""

    render_requested = Signal(object, Path)   # Preset, output path

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.presets = available_presets()

        self.preset_box = QComboBox()
        for preset in self.presets:
            self.preset_box.addItem(preset.name, preset.key)
        self.preset_box.currentIndexChanged.connect(self._on_preset_changed)

        self.preset_note = QLabel("")
        self.preset_note.setObjectName("PlaceholderLabel")
        self.preset_note.setWordWrap(True)

        self.quality = QSlider(Qt.Horizontal)
        self.quality.setRange(12, 34)
        self.quality.setValue(20)
        self.quality_label = QLabel("20")
        self.quality.valueChanged.connect(lambda v: self.quality_label.setText(str(v)))
        quality_row = QHBoxLayout()
        quality_row.addWidget(self.quality, 1)
        quality_row.addWidget(self.quality_label)

        self.width_box = QSpinBox()
        self.width_box.setRange(16, 16384)
        self.width_box.setSingleStep(2)
        self.height_box = QSpinBox()
        self.height_box.setRange(16, 16384)
        self.height_box.setSingleStep(2)
        size_row = QHBoxLayout()
        size_row.addWidget(self.width_box)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_box)
        self.match_button = QPushButton("Match timeline")
        self.match_button.clicked.connect(self.match_timeline)
        size_row.addWidget(self.match_button)

        self.name_edit = QLineEdit("timeline")
        self.folder_edit = QLineEdit(str(Path.home() / "Videos"))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.addRow("Format:", self.preset_box)
        form.addRow("", self.preset_note)
        form.addRow("Quality:", quality_row)
        form.addRow("Resolution:", size_row)
        form.addRow("File name:", self.name_edit)
        form.addRow("Folder:", folder_row)

        settings_box = QGroupBox("Export settings")
        settings_layout = QVBoxLayout(settings_box)
        settings_layout.addLayout(form)

        self.summary = QLabel("")
        self.summary.setObjectName("PlaceholderLabel")
        self.summary.setWordWrap(True)

        self.add_button = QPushButton("Add to Render Queue")
        self.add_button.setDefault(True)
        self.add_button.clicked.connect(self._request)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addWidget(settings_box)
        layout.addWidget(self.summary)
        layout.addStretch(1)
        layout.addWidget(self.add_button)

        project.timeline_changed.connect(self.refresh)
        self.match_timeline()
        self._on_preset_changed()

    # -- state -----------------------------------------------------------------

    def current_preset(self) -> Preset:
        preset = self.presets[max(0, self.preset_box.currentIndex())]
        preset = preset.with_quality(self.quality.value())
        return preset.with_size(self.width_box.value(), self.height_box.value())

    def match_timeline(self) -> None:
        self.width_box.setValue(self.project.timeline.width)
        self.height_box.setValue(self.project.timeline.height)

    def _on_preset_changed(self, *_) -> None:
        preset = self.presets[max(0, self.preset_box.currentIndex())]
        self.preset_note.setText(preset.description)
        # The intermediate codecs ignore CRF entirely, so hide the illusion of
        # control rather than letting the slider look meaningful.
        adjustable = preset.video_codec not in ("prores_ks", "dnxhd")
        self.quality.setEnabled(adjustable)
        self.quality_label.setEnabled(adjustable)
        if adjustable:
            self.quality.setValue(preset.quality)
        else:
            self.quality_label.setText("—")
        self.refresh()

    def refresh(self, *_) -> None:
        timebase = self.project.timebase
        duration = self.project.timeline.duration
        preset = self.presets[max(0, self.preset_box.currentIndex())]
        self.summary.setText(
            f"Timeline: {timebase.frames_to_timecode(duration)} "
            f"({duration} frames at {float(timebase.fps):g} fps)  →  "
            f"{self.name_edit.text() or 'timeline'}.{preset.container}"
        )

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Output folder", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def _request(self) -> None:
        preset = self.current_preset()
        stem = self.name_edit.text().strip() or "timeline"
        folder = Path(self.folder_edit.text().strip() or str(Path.home()))
        self.render_requested.emit(preset, folder / preset.filename_for(stem))


class RenderPage(QWidget):
    status_message = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.queue = RenderQueue(self)

        self.settings = ExportSettings(project, self)
        self.settings.render_requested.connect(self.enqueue)

        self.table = QTableView()
        self.table.setModel(self.queue)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)

        cancel = QPushButton("Cancel Selected")
        cancel.clicked.connect(self._cancel_selected)
        clear = QPushButton("Clear Finished")
        clear.clicked.connect(self.queue.clear_finished)

        buttons = QHBoxLayout()
        buttons.addWidget(cancel)
        buttons.addWidget(clear)
        buttons.addStretch(1)

        queue_panel = QWidget()
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(12, 12, 12, 12)
        queue_layout.setSpacing(8)
        queue_layout.addWidget(QLabel("Render queue"))
        queue_layout.addWidget(self.table, 1)
        queue_layout.addLayout(buttons)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.settings)
        splitter.addWidget(queue_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([560, 900])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self.queue.job_finished.connect(self._on_job_finished)

    # -- queueing --------------------------------------------------------------

    def enqueue(self, preset: Preset, output: Path) -> None:
        try:
            args = build_command(self.project.timeline, self.project.pool, preset, output)
        except (RenderError, FFmpegError) as exc:
            QMessageBox.warning(self, "Cannot render", str(exc))
            return

        if output.exists():
            answer = QMessageBox.question(
                self,
                "Overwrite?",
                f"{output.name} already exists in {output.parent}.\n\nReplace it?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        total = float(self.project.timeline.timebase.frames_to_seconds(self.project.timeline.duration))
        job = RenderJob(output.name, args, output, total, self)
        self.queue.add(job, preset.name)
        self.status_message.emit(f"Queued {output.name}")

    def _cancel_selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        job = self.queue.job_at(rows[0].row())
        if job is not None:
            self.queue.cancel(job)

    def _on_job_finished(self, job: RenderJob, ok: bool, message: str) -> None:
        if ok:
            self.status_message.emit(f"Rendered {job.output.name}")
        else:
            self.status_message.emit(f"{job.output.name}: {message.splitlines()[0] if message else 'failed'}")

    def shutdown(self) -> None:
        self.queue.cancel_all()
