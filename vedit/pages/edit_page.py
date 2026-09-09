"""Edit page: viewer and transport on top, timeline below."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dataclasses import replace

from vedit import icons, theme
from vedit.tools import BY_TOOL, TOOLS, VIEWER_TOOLS, Tool
from vedit.core.project import Project
from vedit.pages.actions import TimelineActions
from vedit.player.engine import PlaybackEngine
from vedit.player.reframe import ReframeOverlay
from vedit.player.surface import VideoSurface
from vedit.timeline import ops
from vedit.timeline.model import TimelineError
from vedit.timeline.view import TimelinePanel


class TransportBar(QWidget):
    """Timecode readout and transport buttons under the viewer."""

    def __init__(self, project: Project, engine: PlaybackEngine, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.engine = engine

        self.timecode = QLabel("00:00:00:00")
        self.timecode.setStyleSheet(
            f"font-family: monospace; font-size: 17px; color: {theme.TEXT.name()};"
        )
        self.duration = QLabel("/ 00:00:00:00")
        self.duration.setObjectName("PlaceholderLabel")
        self.duration.setStyleSheet(
            f"font-family: monospace; color: {theme.TEXT_FAINT.name()};"
        )

        self.go_start = QPushButton("|◀")
        self.step_back = QPushButton("◀|")
        self.play_button = QPushButton("▶")
        self.step_forward = QPushButton("|▶")
        self.go_end = QPushButton("▶|")
        for button, tip in (
            (self.go_start, "Go to start (Home)"),
            (self.step_back, "Previous frame (←)"),
            (self.play_button, "Play / pause (Space)"),
            (self.step_forward, "Next frame (→)"),
            (self.go_end, "Go to end (End)"),
        ):
            button.setToolTip(tip)
            button.setFixedWidth(46)

        self.go_start.clicked.connect(lambda: engine.seek(0))
        self.step_back.clicked.connect(lambda: engine.step(-1))
        self.play_button.clicked.connect(engine.toggle)
        self.step_forward.clicked.connect(lambda: engine.step(1))
        self.go_end.clicked.connect(lambda: engine.seek(project.timeline.duration))

        # The tools that change what a click means, as one exclusive group —
        # pick one and the rest let go, the way every drawing program works.
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_buttons: dict[Tool, QToolButton] = {}
        for index, spec in enumerate(TOOLS):
            button = self._tool_button(spec.icon, spec.tip, spec.key, toggle=True)
            self.tool_group.addButton(button, index)
            self.tool_buttons[spec.tool] = button
        self.tool_buttons[Tool.POINTER].setChecked(True)
        # Kept under its old name: the Audio page and the Edit menu both reach
        # for it, and it is still the thing that arms reframing.
        self.reframe_toggle = self.tool_buttons[Tool.REFRAME]

        self.title_button = self._tool_button("title", "Add a title", "Ctrl+T")
        self.dissolve_button = self._tool_button(
            "dissolve", "Cross dissolve into the selected clip", ""
        )
        self.snap_toggle = self._tool_button("snap", "Snap to edges", "", toggle=True)
        self.snap_toggle.setChecked(True)
        self.thumbs_toggle = self._tool_button(
            "thumbs", "Show frame thumbnails along video clips", "", toggle=True
        )
        self.thumbs_toggle.setChecked(True)
        self.fit_button = self._tool_button("fit", "Zoom the timeline to fit", "F")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(8)
        layout.addWidget(self.timecode)
        layout.addWidget(self.duration)
        layout.addStretch(1)
        for button in (self.go_start, self.step_back, self.play_button, self.step_forward, self.go_end):
            layout.addWidget(button)
        layout.addStretch(1)
        # The tools you hold, then the things you do, then the settings, then
        # the view — grouped so the strip can be scanned rather than read.
        for spec in TOOLS:
            layout.addWidget(self.tool_buttons[spec.tool])
        layout.addWidget(self._divider())
        layout.addWidget(self.title_button)
        layout.addWidget(self.dissolve_button)
        layout.addWidget(self._divider())
        layout.addWidget(self.snap_toggle)
        layout.addWidget(self.thumbs_toggle)
        layout.addWidget(self._divider())
        layout.addWidget(self.fit_button)

        engine.state_changed.connect(self._on_state)
        project.timeline_changed.connect(self.refresh)
        self.refresh()

    def current_tool(self) -> Tool:
        for tool, button in self.tool_buttons.items():
            if button.isChecked():
                return tool
        return Tool.POINTER

    def select_tool(self, tool: Tool) -> None:
        self.tool_buttons[tool].setChecked(True)

    def _tool_button(self, name: str, what: str, key: str, *, toggle: bool = False):
        """One icon button. The tooltip carries the words the icon cannot."""
        button = QToolButton(self)
        # Never takes focus: a toolbar that steals the keyboard from the
        # timeline means the next arrow key does nothing.
        button.setFocusPolicy(Qt.NoFocus)
        button.setIcon(icons.icon(name))
        button.setIconSize(QSize(icons.SIZE, icons.SIZE))
        button.setAutoRaise(True)
        button.setCheckable(toggle)
        button.setToolTip(f"{what}  ({key})" if key else what)
        return button

    def _divider(self) -> QWidget:
        """A hairline between tool groups.

        A plain coloured widget rather than a `QFrame` line: the app stylesheet
        wins over a frame's palette colour, so the line came out invisible and
        the groups ran together.
        """
        line = QWidget(self)
        line.setFixedWidth(1)
        line.setMinimumHeight(20)
        line.setStyleSheet(f"background: {theme.BORDER.name()};")
        return line

    def _on_state(self, playing: bool) -> None:
        self.play_button.setText("⏸" if playing else "▶")

    def refresh(self, *_) -> None:
        timebase = self.project.timebase
        self.timecode.setText(timebase.frames_to_timecode(self.project.playhead))
        self.duration.setText(f"/ {timebase.frames_to_timecode(self.project.timeline.duration)}")


class EditPage(QWidget):
    status_message = Signal(str)

    def __init__(self, project: Project, engine: PlaybackEngine, parent=None) -> None:
        super().__init__(parent)
        self.project = project

        self.surface = VideoSurface(self)
        # The engine is owned by the window and shared with the Audio page: two
        # engines would mean two decoder threads and two audio devices fighting
        # over one timeline.
        self.engine = engine
        self.timeline_panel = TimelinePanel(project, self)
        self.transport = TransportBar(project, self.engine, self)

        self.timeline_panel.status_message.connect(self.status_message)
        self.engine.error.connect(lambda why: self.status_message.emit(f"Playback: {why}"))
        self.transport.snap_toggle.toggled.connect(
            lambda on: setattr(self.timeline_panel.canvas, "snapping", on)
        )
        self.transport.fit_button.clicked.connect(self.timeline_panel.canvas.zoom_to_fit)
        self.transport.thumbs_toggle.toggled.connect(self._set_filmstrips)
        self.transport.title_button.clicked.connect(
            lambda: self.timeline_panel.canvas.add_title()
        )
        self.transport.dissolve_button.clicked.connect(self._dissolve_selected)
        self.transport.tool_group.idClicked.connect(
            lambda index: self.set_tool(TOOLS[index].tool)
        )

        # Reframing is armed rather than always on: the handles and the thirds
        # grid are useful while you are framing and clutter while you are not.
        self.reframe = ReframeOverlay(self.surface, self.surface)
        self.reframe.changed.connect(self._reframe_preview)
        self.reframe.committed.connect(self._reframe_commit)
        self.reframe.status_message.connect(self.status_message)
        self.reframe.seek_requested.connect(self._reframe_seek)
        self.reframe.move_requested.connect(self._reframe_set_move)
        self.reframe.reset_requested.connect(self._reframe_reset)
        self.reframe.zoomed.connect(self._zoom_region)
        self.reframe.title_moved.connect(self._title_moved)
        self.reframe.title_committed.connect(self._title_committed)
        project.playhead_changed.connect(lambda *_: self._sync_reframe())
        project.timeline_changed.connect(self._sync_reframe)

        # The engine drives the playhead while playing; the canvas drives it
        # while scrubbing. Both routes end at project.set_playhead.
        self.engine.position_changed.connect(self._on_engine_position)
        project.playhead_changed.connect(lambda *_: self.transport.refresh())

        viewer = QWidget()
        viewer_layout = QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(0)
        viewer_layout.addWidget(self.surface, 1)
        viewer_layout.addWidget(self.transport)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(viewer)
        splitter.addWidget(self.timeline_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([520, 380])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self.actions = TimelineActions(self, project, engine, self.timeline_panel)
        self.actions.status_message.connect(self.status_message)

        # Page-scoped like every other shortcut here. They live on this page
        # rather than in `TimelineActions` because the Audio page shares those
        # and has no picture to point them at.
        for spec in TOOLS:
            action = QAction(spec.label, self)
            action.setShortcut(QKeySequence(spec.key))
            action.setShortcutContext(Qt.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda _=False, t=spec.tool: self.set_tool(t))
            self.addAction(action)

        # Escape goes through the shared action rather than a second one of its
        # own: two Escape shortcuts in the same scope are ambiguous, and Qt
        # answers an ambiguous shortcut by firing neither.
        self.actions.escape_hook = self._escape_tool

    def _escape_tool(self) -> bool:
        """Escape's first job on this page: put down whatever tool is held."""
        if self.transport.current_tool() is Tool.POINTER:
            return False
        self.set_tool(Tool.POINTER)
        self.status_message.emit("Back to the pointer")
        return True

    def focus_default(self) -> None:
        """Where the keyboard should land when this page comes to the front.

        The transport shortcuts are installed with `WidgetWithChildrenShortcut`,
        so they only fire while focus is inside the page — the canvas is both the
        widget that wants the arrow keys and a guarantee that Space keeps working.
        """
        self.timeline_panel.canvas.setFocus()

    def _set_filmstrips(self, enabled: bool) -> None:
        self.timeline_panel.canvas.show_filmstrips = enabled
        self.timeline_panel.canvas.update()

    def _dissolve_selected(self) -> None:
        """Half a second into whichever clip is selected, or the one on screen.

        A length rather than a menu, because the button is the quick way and the
        right-click menu is where the other lengths live. Half a second is the
        dissolve people mean when they say "dissolve".
        """
        clip = None
        for candidate in self.project.selected_clips():
            if candidate.kind == "video" and not candidate.is_title:
                clip = candidate
                break
        if clip is None:
            clip = self.project.timeline.video_clip_at(self.project.playhead)
        if clip is None:
            self.status_message.emit("Select the clip to dissolve into")
            return
        frames = max(1, int(round(0.5 * float(self.project.timebase.fps))))
        self._run("Dissolve", ops.set_dissolve, clip, frames)

    # -- reframing -------------------------------------------------------------

    def _reframe_clip(self):
        """The clip the tool acts on: the one whose picture is on screen.

        The playhead rather than the selection, because reframing is done by
        eye. What you are looking at is what you are adjusting, and needing to
        also select it first would be a rule with no reason behind it.
        """
        return self.project.timeline.video_clip_at(self.project.playhead)

    def set_tool(self, tool: Tool) -> None:
        """Hand the tool to the two widgets that care and say what it does.

        The hint in the status bar is the whole reason a modal tool is
        acceptable: a pointer that has silently changed meaning is a trap, and a
        line of text is what turns it back into a choice.
        """
        self.transport.select_tool(tool)
        self.timeline_panel.canvas.set_tool(tool)
        self.reframe.set_tool(tool)
        self._sync_reframe()

        spec = BY_TOOL[tool]
        if spec.hint:
            self.status_message.emit(f"{spec.label}: {spec.hint}")
        if tool in VIEWER_TOOLS and self._reframe_clip() is None:
            self.status_message.emit(
                f"{spec.label}: park the playhead over a clip first"
            )

    def _editing_end(self, clip) -> str:
        """Which end of a move the playhead is closest to.

        Halfway is the boundary. Crude, but it means the answer is always the
        end you can see, and the Start / End buttons put the playhead squarely
        on one or the other anyway.
        """
        if clip is None or not clip.has_move:
            return "start"
        middle = clip.tl_start + clip.duration / 2
        return "end" if self.project.playhead >= middle else "start"

    def _title_moved(self, clip, title) -> None:
        """Live during the drag: the words follow the mouse without the model
        being touched, exactly as a reframe drag does."""
        # Straight to the viewer, off the model, the way a reframe drag goes.
        # Deliberately without re-syncing the grab rects: they are anchored to
        # where the drag started, and moving them underneath it would make the
        # title slide away from the pointer.
        self.surface.set_titles(
            [title if c is clip else c.title
             for c in self.project.timeline.titles_at(self.project.playhead)]
        )

    def _title_committed(self, clip, title) -> None:
        self._run("Move title", ops.set_title, clip, title)

    def _sync_titles(self) -> None:
        """Tell the overlay where the words are, so they can be grabbed.

        The rects are asked for by *title* rather than read off the surface,
        because this runs when the timeline changes and the surface has not
        necessarily repainted yet.
        """
        clips = [c for c in self.project.timeline.titles_at(self.project.playhead)
                 if not c.title.is_empty]
        rects = self.surface.title_rects([c.title for c in clips])
        self.reframe.set_titles(list(zip(clips, rects)))

    def _sync_reframe(self, *_) -> None:
        self._sync_titles()
        clip = self._reframe_clip()
        if clip is None:
            self.reframe.set_target(None)
            return
        editing = self._editing_end(clip)
        shown = clip.framing_end if editing == "end" else clip.framing
        self.reframe.set_target(
            shown or clip.framing,
            moves=clip.has_move,
            editing=editing,
            resettable=clip.has_framing,
        )

    def _reframe_seek(self, which: str) -> None:
        """Jump to the end being framed, so the viewer shows what you edit."""
        clip = self._reframe_clip()
        if clip is None:
            return
        self.engine.seek(clip.tl_start if which == "start" else clip.tl_end - 1)

    def _reframe_set_move(self, wanted: bool) -> None:
        """Give the framing somewhere to travel to, or take the travel away."""
        clip = self._reframe_clip()
        if clip is None:
            return
        # A new move starts where the clip already is, so adding one changes
        # nothing until an end is actually framed — no surprise jump.
        end = clip.framing if wanted else None
        self._run("Move framing" if wanted else "Remove move",
                  ops.set_framing_move, [clip], end)
        if wanted:
            self._reframe_seek("end")
            self.status_message.emit("Now frame the end of the shot")

    def _zoom_region(self, framing) -> None:
        """A box dragged with the Zoom tool, as a punch-in round the playhead.

        Re-dragging inside a region that already exists changes what it shows
        and leaves its length and ramps alone — otherwise adjusting the framing
        would silently throw away a duration that had been set by hand.
        """
        # First, and whatever else happens: the box drag pushed a live framing
        # straight to the viewer to follow the mouse, and that override outranks
        # the model until it is taken away. Left in place it holds the *whole*
        # clip at the zoom — so the punch-in appears to bleed across the shot,
        # and resetting it changes the model while the viewer carries on
        # showing the override.
        self.engine.preview_picture(None)

        clip = self._reframe_clip()
        if clip is None:
            return
        playhead = self.project.playhead
        existing = clip.zoom
        if existing is not None and existing.contains(playhead - clip.tl_start):
            region = replace(existing, framing=framing)
        else:
            frames = max(
                1,
                round(ops.DEFAULT_ZOOM_SECONDS * float(self.project.timebase.fps)),
            )
            region = ops.zoom_region_for(clip, playhead, framing, frames=frames)

        self._run("Zoom in", ops.set_zoom_region, [clip], region)
        seconds = region.length / float(self.project.timebase.fps)
        self.status_message.emit(
            f"Zoomed to {framing.zoom:.2f}\u00d7 for {seconds:.1f}s \u2014 drag the "
            "band on the clip to change how long, or its corners to ease it in"
            .replace(".00\u00d7", "\u00d7")
        )

    def _reframe_reset(self) -> None:
        """Back to the whole frame, from the viewer rather than the clip menu.

        The same op the clip menu's Reset Picture calls, so a zoom applied in
        the viewer is undone in the viewer — the place you are already looking
        when you decide you do not want it after all.
        """
        self.engine.preview_picture(None)
        clip = self._reframe_clip()
        if clip is None:
            return
        self._run("Reset picture", ops.reset_framing, [clip])
        self.status_message.emit("Picture reset to the whole frame")

    def _run(self, label: str, func, *args) -> None:
        try:
            self.project.edit(label, lambda t: func(t, *args))
        except TimelineError as exc:
            self.status_message.emit(str(exc))

    def _reframe_preview(self, framing) -> None:
        """Live, off the model: the viewer repaints from the frame it already has."""
        clip = self._reframe_clip()
        self.engine.preview_picture(
            framing,
            clip.rotation if clip else 0,
            clip.flipped if clip else False,
        )

    def _reframe_commit(self, framing) -> None:
        """One undo step for the whole gesture, then the viewer goes back to
        following the model."""
        clip = self._reframe_clip()
        if clip is not None:
            label = f"Reframe {clip.name or 'clip'}"
            if self.reframe.editing == "end":
                self._run(label, ops.set_framing_move, [clip], framing)
            else:
                self._run(label, ops.set_framing, [clip], framing)
        self.engine.preview_picture(None)

    # -- glue ------------------------------------------------------------------

    def _on_engine_position(self, frame: int) -> None:
        self.project.set_playhead(frame)
        if self.engine.playing:
            # `set_playhead` above has already asked for the marker's own strip
            # to be redrawn. Scrolling moves everything and needs the whole
            # canvas, which `refresh_playhead` works out for itself — repainting
            # it here unconditionally would undo that and cost a full lay-out of
            # every clip on every frame.
            self.timeline_panel.canvas.ensure_visible(frame)
            self.timeline_panel.canvas.refresh_playhead()
