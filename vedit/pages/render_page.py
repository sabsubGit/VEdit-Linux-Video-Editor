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
from vedit.render.presets import (
    QUALITY_LEVELS,
    RESOLUTIONS,
    DEFAULT_QUALITY,
    Preset,
    available_presets,
    crf_for,
    estimated_size,
    format_size,
    is_adjustable,
    quality_level,
    resolution_for,
)
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

        # Named levels rather than a CRF slider: the number is backwards, its
        # useful range moves with the codec, and nobody outside encoding knows
        # what 23 looks like. The note below the box says what each one costs.
        self.quality_box = QComboBox()
        for level in QUALITY_LEVELS:
            self.quality_box.addItem(level.name, level.key)
        self.quality_box.setCurrentIndex(
            next(i for i, lv in enumerate(QUALITY_LEVELS) if lv.key == DEFAULT_QUALITY)
        )
        self.quality_box.currentIndexChanged.connect(self.refresh)

        self.quality_note = QLabel("")
        self.quality_note.setObjectName("PlaceholderLabel")
        self.quality_note.setWordWrap(True)

        # The named sizes people actually deliver, with the boxes left in for
        # anything else — typing a size the list knows simply selects it again.
        self.resolution_box = QComboBox()
        self.resolution_box.addItem("Match timeline", "match")
        for resolution in RESOLUTIONS:
            self.resolution_box.addItem(resolution.label, resolution.name)
        self.resolution_box.addItem("Custom", "custom")
        self.resolution_box.currentIndexChanged.connect(self._on_resolution_chosen)

        self.width_box = QSpinBox()
        self.height_box = QSpinBox()
        for box in (self.width_box, self.height_box):
            box.setRange(16, 16384)
            # Even dimensions only: yuv420p chroma is subsampled 2x, so an odd
            # width or height makes most encoders fail outright.
            box.setSingleStep(2)
            box.setFixedWidth(84)
            box.setAlignment(Qt.AlignRight)

        for box in (self.width_box, self.height_box):
            box.valueChanged.connect(self._on_size_typed)

        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        size_row.addWidget(self.width_box)
        size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_box)
        size_row.addStretch(1)

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
        form.addRow("Quality:", self.quality_box)
        form.addRow("", self.quality_note)
        form.addRow("Resolution:", self.resolution_box)
        form.addRow("", size_row)
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

        self.name_edit.textChanged.connect(self.refresh)
        project.timeline_changed.connect(self._on_timeline_changed)
        self.match_timeline()
        self._on_preset_changed()

    # -- state -----------------------------------------------------------------

    def base_preset(self) -> Preset:
        return self.presets[max(0, self.preset_box.currentIndex())]

    def quality(self):
        """The chosen quality level."""
        return quality_level(self.quality_box.currentData())

    def size(self) -> tuple[int, int]:
        # yuv420p subsamples chroma by two, so an odd dimension makes most
        # encoders fail. The step is 2, but a typed value can still be odd.
        width, height = self.width_box.value(), self.height_box.value()
        return width - width % 2, height - height % 2

    def current_preset(self) -> Preset:
        preset = self.base_preset()
        preset = preset.with_quality(crf_for(preset, self.quality()))
        return preset.with_size(*self.size())

    def match_timeline(self) -> None:
        self._set_size(self.project.timeline.width, self.project.timeline.height)

    # -- resolution ------------------------------------------------------------

    def _set_size(self, width: int, height: int) -> None:
        """Drive the boxes from a choice, without that reading as a typed size."""
        for box, value in ((self.width_box, width), (self.height_box, height)):
            blocked = box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(blocked)
        self.refresh()

    def _on_resolution_chosen(self, *_) -> None:
        data = self.resolution_box.currentData()
        if data == "match":
            self.match_timeline()
            return
        if data == "custom":
            self.refresh()
            return
        for resolution in RESOLUTIONS:
            if resolution.name == data:
                self._set_size(resolution.width, resolution.height)
                return

    def _on_size_typed(self, *_) -> None:
        """Typing a size re-labels the menu rather than fighting it.

        A size the list knows selects that entry, the timeline's own size selects
        Match timeline, and anything else is Custom — so the menu always says
        what the boxes hold instead of pointing at a size that is no longer set.
        """
        width, height = self.size()
        timeline = self.project.timeline
        if (width, height) == (timeline.width, timeline.height):
            target = "match"
        else:
            named = resolution_for(width, height)
            target = named.name if named is not None else "custom"
        index = self.resolution_box.findData(target)
        if index >= 0 and index != self.resolution_box.currentIndex():
            blocked = self.resolution_box.blockSignals(True)
            self.resolution_box.setCurrentIndex(index)
            self.resolution_box.blockSignals(blocked)
        self.refresh()

    def _on_timeline_changed(self, *_) -> None:
        """Following the timeline is the whole point of Match timeline: adding a
        4K clip to an empty project changes the format under us."""
        if self.resolution_box.currentData() == "match":
            self.match_timeline()
        self.refresh()

    # -- notes -----------------------------------------------------------------

    def _on_preset_changed(self, *_) -> None:
        preset = self.base_preset()
        self.preset_note.setText(preset.description)
        # The intermediates encode at a rate fixed by their profile, so offering
        # a quality choice there would be an illusion of control.
        adjustable = is_adjustable(preset)
        self.quality_box.setEnabled(adjustable)
        self.refresh()

    def refresh(self, *_) -> None:
        timebase = self.project.timebase
        duration = self.project.timeline.duration
        preset = self.base_preset()
        level = self.quality()
        width, height = self.size()
        seconds = float(timebase.frames_to_seconds(duration))
        # An empty timeline has no size to estimate, so the rate is quoted
        # instead — still the number that makes the choice concrete.
        rate = seconds <= 0
        size = estimated_size(
            preset, level, width, height, float(timebase.fps), 60.0 if rate else seconds
        )
        cost = f"≈ {format_size(size)}{' per minute' if rate else ''} at {width}×{height}."
        explanation = (
            level.description
            if is_adjustable(preset)
            else f"Fixed by the {preset.name.split(' · ')[0]} profile."
        )
        self.quality_note.setText(f"{explanation}  {cost}")

        # Say what matching the timeline currently means, rather than making
        # people select it to find out.
        self.resolution_box.setItemText(
            0, f"Match timeline — {self.project.timeline.width}×{self.project.timeline.height}"
        )

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
