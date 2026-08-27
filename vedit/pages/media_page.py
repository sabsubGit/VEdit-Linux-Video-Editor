"""Media page: the media pool, plus metadata for whatever is selected."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class MediaPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        placeholder = QLabel("Media pool — drop files here")
        placeholder.setObjectName("PlaceholderLabel")
        placeholder.setAlignment(Qt.AlignCenter)
        layout.addWidget(placeholder)
