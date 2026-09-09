"""The dialog for writing a title.

Deliberately small. Every control here is one someone would look for — the
words, how big, where on screen — and nothing else, because the point of this
feature for the people vedit is for is putting a name on a shot, not building a
motion graphic. Anything that needs more than this needs a different program.

The preview strip along the top is the reason the dialog exists rather than a
line edit in the timeline: type into a box with no picture and you find out the
text was too long, or invisible against the sky, after the export.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from vedit import theme
from vedit.timeline.titles import ALIGNMENTS, POSITIONS, SIZES, Title

POSITION_LABELS = {
    "top": "Top",
    "centre": "Centre",
    "lower": "Lower third",
    "bottom": "Bottom",
}
SIZE_LABELS = {"small": "Small", "medium": "Medium", "large": "Large", "huge": "Huge"}


class _Preview(QWidget):
    """The title as it will be drawn, over a still of what is behind it."""

    def __init__(self, backdrop: QImage | None, frame: tuple[int, int], parent=None) -> None:
        super().__init__(parent)
        self._backdrop = backdrop
        self._frame = frame
        self._title = Title()
        self.setMinimumHeight(150)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)

    def show_title(self, title: Title) -> None:
        self._title = title
        self.update()

    def paintEvent(self, event) -> None:
        # Painted through the real surface rather than reimplemented here, so
        # what this shows is what the viewer shows, by construction.
        from vedit.player.surface import VideoSurface

        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.BG_DARKEST)

        surface = VideoSurface()
        surface.resize(self.width(), self.height())
        surface.set_frame_size(*self._frame)
        if self._backdrop is not None and not self._backdrop.isNull():
            surface.set_image(self._backdrop)
        surface.set_titles([self._title])
        surface.render(painter, self.rect().topLeft())
        painter.end()


class TitleDialog(QDialog):
    """Write a title. Returns the new `Title` from `result_title()`."""

    changed = Signal(object)

    def __init__(
        self,
        title: Title,
        *,
        frame: tuple[int, int] = (1920, 1080),
        backdrop: QImage | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Title")
        self.setMinimumWidth(460)
        self._title = title

        self.preview = _Preview(backdrop, frame, self)

        self.text = QPlainTextEdit(title.text, self)
        self.text.setPlaceholderText("Type the title. Enter starts a new line.")
        self.text.setFixedHeight(78)
        self.text.textChanged.connect(self._rebuild)

        self.size = QComboBox(self)
        for key in SIZES:
            self.size.addItem(SIZE_LABELS[key], key)
        self.size.setCurrentIndex(list(SIZES).index(title.size))
        self.size.currentIndexChanged.connect(self._rebuild)

        self.position = QComboBox(self)
        for key in POSITIONS:
            self.position.addItem(POSITION_LABELS[key], key)
        self.position.setCurrentIndex(POSITIONS.index(title.position))
        self.position.currentIndexChanged.connect(self._rebuild)

        self.align = QComboBox(self)
        for key in ALIGNMENTS:
            self.align.addItem(key.capitalize(), key)
        self.align.setCurrentIndex(ALIGNMENTS.index(title.align))
        self.align.currentIndexChanged.connect(self._rebuild)

        self._colour = title.colour
        self.colour_button = QPushButton(self)
        self.colour_button.clicked.connect(self._pick_colour)
        self._show_colour()

        self.shadow = QCheckBox("Dark edge (keeps it readable over bright footage)", self)
        self.shadow.setChecked(title.shadow)
        self.shadow.toggled.connect(self._rebuild)

        colour_row = QHBoxLayout()
        colour_row.setContentsMargins(0, 0, 0, 0)
        colour_row.addWidget(self.colour_button)
        colour_row.addStretch(1)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("Text:", self.text)
        form.addRow("Size:", self.size)
        form.addRow("Position:", self.position)
        form.addRow("Align:", self.align)
        form.addRow("Colour:", colour_row)
        form.addRow("", self.shadow)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        hint = QLabel("Shown over whatever is on the lanes below it.")
        hint.setObjectName("PlaceholderLabel")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self.preview)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._rebuild()
        self.text.setFocus()

    # -- state -----------------------------------------------------------------

    def _show_colour(self) -> None:
        self.colour_button.setText(self._colour)
        self.colour_button.setStyleSheet(
            f"background: {self._colour}; "
            f"color: {'#000' if QColor(self._colour).lightness() > 128 else '#fff'};"
        )

    def _pick_colour(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._colour), self, "Title colour")
        if chosen.isValid():
            self._colour = chosen.name()
            self._show_colour()
            self._rebuild()

    def _rebuild(self) -> None:
        try:
            self._title = Title(
                text=self.text.toPlainText(),
                size=self.size.currentData(),
                position=self.position.currentData(),
                align=self.align.currentData(),
                colour=self._colour,
                shadow=self.shadow.isChecked(),
            )
        except ValueError:
            return
        self.preview.show_title(self._title)
        self.changed.emit(self._title)

    def result_title(self) -> Title:
        return self._title
