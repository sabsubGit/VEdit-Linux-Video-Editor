"""Dark theme. Video work is done in dim rooms and against dark surrounds so the
image being graded isn't fighting the UI for your eyes."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

# Single source of truth for colours. Widgets and the QGraphicsView-painted
# timeline both read from here so the custom-drawn canvas matches the chrome.
BG_DARKEST = QColor("#101216")   # viewer surround, timeline background
BG_DARK = QColor("#171a1f")      # page background
BG_PANEL = QColor("#1d2127")     # panels, media pool
BG_RAISED = QColor("#252a32")    # buttons, headers, track headers
BG_HOVER = QColor("#2f353f")
BORDER = QColor("#343b45")

TEXT = QColor("#dfe3ea")
TEXT_DIM = QColor("#8c94a1")
TEXT_FAINT = QColor("#5c646f")

ACCENT = QColor("#45d68a")       # selection, active page, focus
ACCENT_DIM = QColor("#2c6b4d")
PLAYHEAD = QColor("#ff4a4a")

CLIP_VIDEO = QColor("#3a6ea5")
CLIP_VIDEO_SEL = QColor("#4a86c4")
CLIP_AUDIO = QColor("#3d8a6b")
CLIP_AUDIO_SEL = QColor("#4aa383")
# A lighter edge of the clip's own colour, rather than a dark outline: it reads
# as one object and keeps clips distinct from each other where they abut.
CLIP_VIDEO_EDGE = QColor("#7fb4e6")
CLIP_AUDIO_EDGE = QColor("#7fd4b0")
CLIP_EDGE_SEL = QColor("#ffffff")
CLIP_BORDER = QColor("#0d0f12")
WAVEFORM = QColor("#c9f0dd")

OK = QColor("#5ac977")
WARN = QColor("#e0a336")
ERROR = QColor("#e05c5c")

# -- mixer ---------------------------------------------------------------------
# Three zones on the meter rather than one colour: the point of a meter is to be
# readable out of the corner of your eye, and a bar that changes colour as it
# approaches the top says "close to clipping" without being read.
METER_BG = QColor("#0b0d10")
METER_LOW = QColor("#43c46a")     # green, up to -12 dBFS
METER_MID = QColor("#e0c336")     # amber, -12 to -3
METER_HIGH = QColor("#e05c5c")    # red, above -3
METER_PEAK = QColor("#f2f5fa")    # the held-peak line
METER_CLIP = QColor("#ff3b3b")    # the latching clip flag
METER_GRID = QColor("#2a3038")

FADER_TRACK = QColor("#0b0d10")
FADER_CAP = QColor("#c8cedb")
FADER_UNITY = QColor("#5c646f")   # the 0 dB reference mark
SOLO = QColor("#e0c336")

# Deliberately not the waveform's own colour: the volume line has to be legible
# lying across a waveform, so it sits well off it on the wheel.
GAIN_LINE = QColor("#ffd166")
FADE_CURVE = QColor("#f2f5fa")


def palette() -> QPalette:
    """The Fusion palette, repainted in our own colours.

    The stylesheet cannot reach everything: checkbox and radio indicators, focus
    rings and the widgets Fusion draws itself all take their colour from the
    palette's Highlight role, which is blue by default. Setting it here is what
    keeps those in step with ACCENT rather than leaving blue marks around the app.
    """
    pal = QPalette()
    pal.setColor(QPalette.Window, BG_DARK)
    pal.setColor(QPalette.WindowText, TEXT)
    pal.setColor(QPalette.Base, BG_DARKEST)
    pal.setColor(QPalette.AlternateBase, BG_PANEL)
    pal.setColor(QPalette.Text, TEXT)
    pal.setColor(QPalette.Button, BG_RAISED)
    pal.setColor(QPalette.ButtonText, TEXT)
    pal.setColor(QPalette.ToolTipBase, BG_RAISED)
    pal.setColor(QPalette.ToolTipText, TEXT)
    pal.setColor(QPalette.Link, ACCENT)
    pal.setColor(QPalette.Highlight, ACCENT)
    pal.setColor(QPalette.HighlightedText, BG_DARKEST)
    pal.setColor(QPalette.PlaceholderText, TEXT_FAINT)
    for group in (QPalette.Disabled,):
        pal.setColor(group, QPalette.Text, TEXT_FAINT)
        pal.setColor(group, QPalette.ButtonText, TEXT_FAINT)
        pal.setColor(group, QPalette.WindowText, TEXT_FAINT)
    return pal


def stylesheet() -> str:
    """Application-wide Qt stylesheet built from the palette above."""
    return f"""
    QWidget {{
        background: {BG_DARK.name()};
        color: {TEXT.name()};
        font-size: 13px;
    }}
    QMainWindow, QDialog {{ background: {BG_DARK.name()}; }}

    /* Page switcher across the top: Media / Edit / Render */
    #PageBar {{
        background: {BG_DARKEST.name()};
        border-bottom: 1px solid {BORDER.name()};
    }}
    #PageButton {{
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        padding: 9px 26px;
        color: {TEXT_DIM.name()};
        font-size: 13px;
        font-weight: 500;
    }}
    #PageButton:hover {{ color: {TEXT.name()}; background: {BG_RAISED.name()}; }}
    #PageButton:checked {{
        color: {TEXT.name()};
        border-bottom: 2px solid {ACCENT.name()};
    }}
    #AppTitle {{ color: {TEXT_FAINT.name()}; font-weight: 600; padding-left: 14px; }}

    QPushButton {{
        background: {BG_RAISED.name()};
        border: 1px solid {BORDER.name()};
        border-radius: 4px;
        padding: 5px 14px;
    }}
    QPushButton:hover {{ background: {BG_HOVER.name()}; }}
    QPushButton:pressed {{ background: {ACCENT_DIM.name()}; }}
    QPushButton:disabled {{ color: {TEXT_FAINT.name()}; background: {BG_PANEL.name()}; }}
    QPushButton:default {{ border-color: {ACCENT_DIM.name()}; }}

    /* Drawn here rather than left to the style: with a dark palette Fusion's own
       indicator is a dark box on a dark background with no visible edge. */
    QCheckBox::indicator, QRadioButton::indicator {{
        width: 12px;
        height: 12px;
        border: 1px solid {BORDER.name()};
        background: {BG_DARKEST.name()};
    }}
    QCheckBox::indicator {{ border-radius: 3px; }}
    QRadioButton::indicator {{ border-radius: 7px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
        border-color: {TEXT_FAINT.name()};
    }}
    QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
        background: {ACCENT.name()};
        border-color: {ACCENT.name()};
    }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background: {BG_PANEL.name()};
        border-color: {BG_HOVER.name()};
    }}

    QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{
        background: {BG_DARKEST.name()};
        border: 1px solid {BORDER.name()};
        border-radius: 4px;
        padding: 4px 7px;
        selection-background-color: {ACCENT_DIM.name()};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
        border-color: {ACCENT.name()};
    }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{
        background: {BG_PANEL.name()};
        border: 1px solid {BORDER.name()};
        selection-background-color: {ACCENT_DIM.name()};
    }}

    QListView, QTreeView, QTableView {{
        background: {BG_PANEL.name()};
        border: 1px solid {BORDER.name()};
        border-radius: 4px;
        selection-background-color: {ACCENT_DIM.name()};
        outline: none;
    }}
    QListView::item, QTreeView::item {{ padding: 3px; }}
    QListView::item:hover, QTreeView::item:hover {{ background: {BG_HOVER.name()}; }}
    QHeaderView::section {{
        background: {BG_RAISED.name()};
        border: none;
        border-right: 1px solid {BORDER.name()};
        border-bottom: 1px solid {BORDER.name()};
        padding: 5px 8px;
        color: {TEXT_DIM.name()};
        font-weight: 500;
    }}

    QSplitter::handle {{ background: {BORDER.name()}; }}

    /* Mixer */
    #MixerPanel {{
        background: {BG_DARKEST.name()};
        border-top: 1px solid {BORDER.name()};
    }}
    #ChannelStrip, #MasterStrip {{ background: {BG_PANEL.name()}; border-radius: 5px; }}
    #MasterStrip {{ background: {BG_RAISED.name()}; }}
    #StripName {{ color: {TEXT.name()}; font-weight: 600; }}
    #StripValue {{ color: {TEXT_DIM.name()}; font-family: monospace; font-size: 11px; }}
    #MuteButton, #SoloButton {{
        background: {BG_RAISED.name()};
        border: 1px solid {BORDER.name()};
        border-radius: 3px;
        padding: 2px 0px;
        font-size: 11px;
        font-weight: 600;
        color: {TEXT_DIM.name()};
    }}
    #MuteButton:hover, #SoloButton:hover {{ background: {BG_HOVER.name()}; }}
    #MuteButton:checked {{ background: {ERROR.name()}; color: #ffffff; border-color: {ERROR.name()}; }}
    #SoloButton:checked {{ background: {SOLO.name()}; color: #1a1a1a; border-color: {SOLO.name()}; }}
    QSplitter::handle:horizontal {{ width: 1px; }}
    QSplitter::handle:vertical {{ height: 1px; }}

    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
    QScrollBar::handle {{ background: {BG_HOVER.name()}; border-radius: 5px; min-height: 28px; min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {TEXT_FAINT.name()}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QStatusBar {{
        background: {BG_DARKEST.name()};
        border-top: 1px solid {BORDER.name()};
        color: {TEXT_DIM.name()};
    }}
    QStatusBar::item {{ border: none; }}

    QToolTip {{
        background: {BG_RAISED.name()};
        color: {TEXT.name()};
        border: 1px solid {BORDER.name()};
        padding: 4px 7px;
    }}

    QMenuBar {{ background: {BG_DARKEST.name()}; }}
    QMenuBar::item:selected {{ background: {BG_HOVER.name()}; }}
    QMenu {{ background: {BG_PANEL.name()}; border: 1px solid {BORDER.name()}; padding: 4px; }}
    QMenu::item {{ padding: 5px 26px 5px 20px; }}
    QMenu::item:selected {{ background: {ACCENT_DIM.name()}; }}
    QMenu::separator {{ height: 1px; background: {BORDER.name()}; margin: 4px 8px; }}

    QProgressBar {{
        background: {BG_DARKEST.name()};
        border: 1px solid {BORDER.name()};
        border-radius: 3px;
        height: 15px;
        text-align: center;
        color: {TEXT.name()};
    }}
    QProgressBar::chunk {{ background: {ACCENT_DIM.name()}; border-radius: 2px; }}

    QGroupBox {{
        border: 1px solid {BORDER.name()};
        border-radius: 4px;
        margin-top: 9px;
        padding-top: 9px;
        font-weight: 500;
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 9px; padding: 0 5px; color: {TEXT_DIM.name()}; }}

    QSlider::groove:horizontal {{ background: {BG_DARKEST.name()}; height: 4px; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {ACCENT_DIM.name()}; border-radius: 2px; }}
    QSlider::handle:horizontal {{
        background: {TEXT.name()}; width: 11px; margin: -5px 0; border-radius: 5px;
    }}

    #PlaceholderLabel {{ color: {TEXT_FAINT.name()}; font-size: 14px; }}
    """
