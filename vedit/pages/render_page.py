"""Render page: export settings on the left, render queue on the right."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class RenderPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        placeholder = QLabel("Export settings and render queue")
        placeholder.setObjectName("PlaceholderLabel")
        placeholder.setAlignment(Qt.AlignCenter)
        layout.addWidget(placeholder)
