"""The project's picture size and frame rate.

Normally nobody needs this: the first clip dropped on an empty timeline sets
both. It is here for when that guess is wrong — a vertical video cut from
horizontal footage, or a 25 fps delivery from a 60 fps screen recording.
"""

from __future__ import annotations

from fractions import Fraction

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
)

from vedit.core.project import Project
from vedit.core.timebase import STANDARD_RATES
from vedit.render.presets import RESOLUTIONS

CUSTOM = "Custom"


def rate_label(fps: Fraction) -> str:
    return f"{float(fps):.5g} fps"


class ProjectSettingsDialog(QDialog):
    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Project Settings")
        self.project = project
        width, height, fps = project.format

        self.size_box = QComboBox(self)
        for resolution in RESOLUTIONS:
            self.size_box.addItem(resolution.label, (resolution.width, resolution.height))
        self.size_box.addItem(CUSTOM, None)

        self.width_box = QSpinBox(self)
        self.height_box = QSpinBox(self)
        for box, value in ((self.width_box, width), (self.height_box, height)):
            box.setRange(16, 8192)
            box.setSingleStep(2)
            box.setValue(value)
        size_row = QHBoxLayout()
        size_row.addWidget(self.width_box)
        size_row.addWidget(QLabel("×", self))
        size_row.addWidget(self.height_box)
        size_row.addStretch(1)

        self.rate_box = QComboBox(self)
        rates = list(STANDARD_RATES)
        if fps not in rates:
            rates.append(fps)
        for rate in sorted(rates):
            self.rate_box.addItem(rate_label(rate), rate)
        self.rate_box.setCurrentIndex(sorted(rates).index(fps))

        form = QFormLayout()
        form.addRow("Picture size", self.size_box)
        form.addRow("", size_row)
        form.addRow("Frame rate", self.rate_box)

        layout = QVBoxLayout(self)
        layout.addLayout(form)

        if not project.can_change_rate:
            # Said here rather than discovered: a greyed-out box with no reason
            # given reads as a bug.
            self.rate_box.setEnabled(False)
            note = QLabel(
                "The frame rate can only be changed while the timeline is empty — "
                "every cut is counted in frames at this rate.",
                self,
            )
            note.setWordWrap(True)
            note.setObjectName("Hint")
            layout.addWidget(note)

        hint = QLabel(
            "A new project takes its size and frame rate from the first clip "
            "you put on the timeline.",
            self,
        )
        hint.setWordWrap(True)
        hint.setObjectName("Hint")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._sync_size_box()
        self.size_box.activated.connect(self._on_size_chosen)
        self.width_box.valueChanged.connect(self._sync_size_box)
        self.height_box.valueChanged.connect(self._sync_size_box)

    def _on_size_chosen(self, *_) -> None:
        size = self.size_box.currentData()
        if size is None:
            return
        size = tuple(size)
        for box, value in ((self.width_box, size[0]), (self.height_box, size[1])):
            box.blockSignals(True)
            box.setValue(value)
            box.blockSignals(False)

    def _sync_size_box(self, *_) -> None:
        """Show the name for the typed size, or Custom when it has none."""
        size = (self.width_box.value(), self.height_box.value())
        index = self.size_box.count() - 1
        for row in range(self.size_box.count()):
            data = self.size_box.itemData(row)
            if data is not None and tuple(data) == size:
                index = row
                break
        self.size_box.setCurrentIndex(index)

    def chosen(self) -> tuple[int, int, Fraction]:
        return self.width_box.value(), self.height_box.value(), self.rate_box.currentData()
