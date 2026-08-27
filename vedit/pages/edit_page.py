"""Edit page: preview viewer above, timeline below."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


class EditPage(QWidget):
    def __init__(self, project, parent=None):
        super().__init__(parent)
        self.project = project
        layout = QVBoxLayout(self)
        placeholder = QLabel("Preview and timeline")
        placeholder.setObjectName("PlaceholderLabel")
        placeholder.setAlignment(Qt.AlignCenter)
        layout.addWidget(placeholder)
