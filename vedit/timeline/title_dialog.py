"""The dialog for writing a title.

Deliberately small. Every control here is one someone would look for — the
words, how big, where on screen — and nothing else, because the point of this
feature for the people vedit is for is putting a name on a shot, not building a
motion graphic. Anything that needs more than this needs a different program.

The preview along the top is the reason the dialog exists rather than a line
edit in the timeline: type into a box with no picture and you find out the text
was too long, or invisible against the sky, after the export. It is also where
the title is placed — drag the words to where they should sit, the same gesture
the viewer uses.
"""

from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
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

# Big enough to judge the words against the picture, and to drag them with any
# precision. The preview takes whatever extra height the dialog is given.
PREVIEW_HEIGHT = 340
DIALOG_WIDTH = 640


class _Preview(QWidget):
    """The title as it will be drawn, over a still of what is behind it.

    Dragging anywhere in it moves the title. Anywhere rather than only on the
    words: the preview shows one title and nothing else to pick up, and small
    text is a small target.
    """

    # The title's offset after a drag step, as fractions of the frame.
    moved = Signal(float, float)

    def __init__(self, backdrop: QImage | None, frame: tuple[int, int], parent=None) -> None:
        super().__init__(parent)
        # Painted through the real surface rather than reimplemented here, so
        # what this shows is what the viewer shows, by construction.
        from vedit.player.surface import VideoSurface

        self._title = Title()
        self._surface = VideoSurface()
        self._surface.set_frame_size(*frame)
        if backdrop is not None and not backdrop.isNull():
            self._surface.set_image(backdrop)
        self._press: QPoint | None = None
        self._start = (0.0, 0.0)
        self.setMinimumHeight(PREVIEW_HEIGHT)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip("Drag to move the title")

    def show_title(self, title: Title) -> None:
        self._title = title
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme.BG_DARKEST)
        self._surface.resize(self.size())
        self._surface.set_titles([self._title])
        self._surface.render(painter, self.rect().topLeft())
        painter.end()

    # -- dragging --------------------------------------------------------------

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.LeftButton:
            return
        self._press = event.position().toPoint()
        self._start = (self._title.offset_x, self._title.offset_y)
        self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:
        if self._press is None:
            return
        self._surface.resize(self.size())
        frame = self._surface.frame_rect()
        if frame.width() <= 0 or frame.height() <= 0:
            return
        # The same arithmetic as dragging a title in the viewer: the distance
        # moved, as a fraction of the picture it was moved across.
        pos = event.position().toPoint()
        dx = (pos.x() - self._press.x()) / frame.width()
        dy = (pos.y() - self._press.y()) / frame.height()
        self.moved.emit(
            max(-1.0, min(1.0, self._start[0] + dx)),
            max(-1.0, min(1.0, self._start[1] + dy)),
        )

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press = None
            self.setCursor(Qt.OpenHandCursor)


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
        self.setMinimumWidth(DIALOG_WIDTH)
        self._title = title
        # Where a drag has put it, measured from the Position and Align presets.
        # Kept here because the controls below rebuild the title from scratch,
        # and a title already dragged into place must not snap back on a retype.
        self._offset = (title.offset_x, title.offset_y)

        self.preview = _Preview(backdrop, frame, self)
        self.preview.moved.connect(self._on_dragged)

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
        self.position.currentIndexChanged.connect(self._on_position_chosen)

        self.align = QComboBox(self)
        for key in ALIGNMENTS:
            self.align.addItem(key.capitalize(), key)
        self.align.setCurrentIndex(ALIGNMENTS.index(title.align))
        self.align.currentIndexChanged.connect(self._on_align_chosen)

        self._colour = title.colour
        self.colour_button = QPushButton(self)
        self.colour_button.clicked.connect(self._pick_colour)
        self._show_colour()

        # Short enough to fit beside the form's labels; the reason is a tooltip.
        self.shadow = QCheckBox("Dark edge behind the text", self)
        self.shadow.setToolTip("Keeps the title readable over bright footage")
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

        hint = QLabel(
            "Shown over whatever is on the lanes below it. "
            "Drag the title in the preview to move it."
        )
        hint.setObjectName("PlaceholderLabel")
        hint.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        layout.addWidget(self.preview, 1)
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

    def _on_dragged(self, offset_x: float, offset_y: float) -> None:
        self._offset = (offset_x, offset_y)
        self._rebuild()

    def _on_position_chosen(self, *_) -> None:
        # Picking a preset means "put it there", so the drag along that axis is
        # dropped; otherwise Top would land wherever the last drag left it.
        self._offset = (self._offset[0], 0.0)
        self._rebuild()

    def _on_align_chosen(self, *_) -> None:
        self._offset = (0.0, self._offset[1])
        self._rebuild()

    def _rebuild(self, *_) -> None:
        try:
            self._title = Title(
                text=self.text.toPlainText(),
                size=self.size.currentData(),
                position=self.position.currentData(),
                align=self.align.currentData(),
                colour=self._colour,
                shadow=self.shadow.isChecked(),
                offset_x=self._offset[0],
                offset_y=self._offset[1],
            )
        except ValueError:
            return
        self.preview.show_title(self._title)
        self.changed.emit(self._title)

    def result_title(self) -> Title:
        return self._title
