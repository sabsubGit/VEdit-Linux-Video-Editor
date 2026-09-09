"""The modal tools, and the gestures that belong to each.

A tool that silently changes what a click means is a trap unless picking it up
and putting it down are both obvious, so what is pinned here is mostly the
edges: that Escape always gets you out, that a tool the page cannot offer is not
offered, and that the tool actually changes what the mouse does.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QMouseEvent

from vedit.core.project import Project
from vedit.timeline import ops
from vedit.timeline.framing import Framing
from vedit.timeline.model import Clip
from vedit.tools import BY_TOOL, TOOLS, VIEWER_TOOLS, Tool


def press(app, widget, pos, button=Qt.LeftButton):
    app.sendEvent(widget, QMouseEvent(
        QMouseEvent.Type.MouseButtonPress, QPointF(pos), QPointF(pos),
        button, button, Qt.NoModifier))


def release(app, widget, pos):
    app.sendEvent(widget, QMouseEvent(
        QMouseEvent.Type.MouseButtonRelease, QPointF(pos), QPointF(pos),
        Qt.LeftButton, Qt.NoButton, Qt.NoModifier))


def move(app, widget, pos, held=Qt.NoButton):
    app.sendEvent(widget, QMouseEvent(
        QMouseEvent.Type.MouseMove, QPointF(pos), QPointF(pos),
        Qt.NoButton, held, Qt.NoModifier))


@pytest.fixture
def canvas(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from vedit.timeline.view import TimelineCanvas

    project = Project()
    canvas = TimelineCanvas(project)
    canvas.resize(1000, 400)
    canvas.px_per_frame = 4.0
    canvas.release_auto_fit()
    track = project.timeline.lane_for("video")
    track.insert(Clip(media_id="m", src_in=0, src_out=120, tl_start=0,
                      src_length=200, name="shot"))
    return canvas


def lane_point(canvas, frame, lane="V1"):
    for track, top, height in canvas.track_rows():
        if track.name == lane:
            return QPoint(int(canvas.x_of(frame)), top + height // 2)
    raise AssertionError(f"no lane {lane}")


class TestTheToolList:
    def test_select_is_first(self):
        """It is the tool you are in, so it is the one at the near end."""
        assert TOOLS[0].tool is Tool.POINTER

    def test_every_tool_has_a_key_and_a_tooltip(self):
        for spec in TOOLS:
            assert spec.key and spec.tip

    def test_the_keys_are_all_different(self):
        keys = [spec.key for spec in TOOLS]
        assert len(set(keys)) == len(keys)

    def test_the_modal_ones_explain_themselves(self):
        """Select needs no hint; the rest have changed what the mouse does and
        have to say so."""
        assert BY_TOOL[Tool.POINTER].hint == ""
        for tool in (Tool.CUT, Tool.REFRAME, Tool.ZOOM):
            assert BY_TOOL[tool].hint

    def test_the_viewer_tools_are_the_picture_ones(self):
        assert set(VIEWER_TOOLS) == {Tool.REFRAME, Tool.ZOOM}


class TestTheCutTool:
    def test_holding_it_changes_the_cursor(self, canvas):
        canvas.set_tool(Tool.CUT)
        assert canvas.cursor().shape() == Qt.BitmapCursor
        canvas.set_tool(Tool.POINTER)
        assert canvas.cursor().shape() != Qt.BitmapCursor

    def test_hovering_previews_where_the_cut_would_land(self, qt_app, canvas):
        """The point of the tool over the X key: you see the frame before you
        commit to it."""
        canvas.set_tool(Tool.CUT)
        move(qt_app, canvas, lane_point(canvas, 40))
        assert canvas._cut_frame == 40

    def test_leaving_the_timeline_clears_the_preview(self, qt_app, canvas):
        canvas.set_tool(Tool.CUT)
        move(qt_app, canvas, lane_point(canvas, 40))
        qt_app.sendEvent(canvas, __import__("PySide6.QtCore", fromlist=["QEvent"]).QEvent(
            __import__("PySide6.QtCore", fromlist=["QEvent"]).QEvent.Type.Leave))
        assert canvas._cut_frame is None

    def test_clicking_cuts_there(self, qt_app, canvas):
        canvas.set_tool(Tool.CUT)
        point = lane_point(canvas, 40)
        press(qt_app, canvas, point)
        release(qt_app, canvas, point)
        track = canvas.timeline.lane_for("video")
        assert [c.tl_start for c in track.clips] == [0, 40]

    def test_it_cuts_every_lane_not_just_the_one_clicked(self, qt_app, canvas):
        """A shot and its sound are two clips; cutting one and not the other is
        never what was meant."""
        audio = canvas.timeline.lane_for("audio")
        audio.insert(Clip(media_id="m", src_in=0, src_out=120, tl_start=0,
                          src_length=200, kind="audio", name="shot"))
        canvas.set_tool(Tool.CUT)
        point = lane_point(canvas, 40)
        press(qt_app, canvas, point)
        release(qt_app, canvas, point)
        assert len(audio.clips) == 2

    def test_it_does_not_select_or_move_anything(self, qt_app, canvas):
        """The pointer's press handler must not also run, or a cut would drag
        the clip it just made."""
        canvas.set_tool(Tool.CUT)
        point = lane_point(canvas, 40)
        press(qt_app, canvas, point)
        assert canvas.project.selected_ids == []
        assert canvas._mode.name == "IDLE"
        release(qt_app, canvas, point)

    def test_clicking_the_ruler_still_scrubs(self, qt_app, canvas):
        """The cut tool owns the lanes, not the ruler — moving the playhead has
        to keep working whatever is held."""
        canvas.set_tool(Tool.CUT)
        press(qt_app, canvas, QPoint(int(canvas.x_of(50)), 8))
        assert canvas.project.playhead == 50
        assert len(canvas.timeline.lane_for("video").clips) == 1

    def test_cutting_where_there_is_nothing_says_so(self, qt_app, canvas):
        messages = []
        canvas.status_message.connect(messages.append)
        canvas.set_tool(Tool.CUT)
        point = lane_point(canvas, 500)
        press(qt_app, canvas, point)
        release(qt_app, canvas, point)
        assert messages and "Nothing to cut" in messages[-1]


class TestThePageWiring:
    @pytest.fixture
    def page(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.pages.edit_page import EditPage
        from vedit.player.engine import PlaybackEngine

        project = Project()
        engine = PlaybackEngine(project)
        page = EditPage(project, engine)
        page.resize(1200, 800)
        # Shown, because `isVisible` is False for every child of a window that
        # was never shown — the visibility assertions below would pass for the
        # wrong reason otherwise.
        page.show()
        yield page
        engine.stop()
        page.close()

    def test_it_starts_on_the_pointer(self, page):
        assert page.transport.current_tool() is Tool.POINTER

    @pytest.mark.parametrize("tool", list(Tool))
    def test_picking_a_tool_reaches_both_widgets(self, page, tool):
        page.set_tool(tool)
        assert page.transport.current_tool() is tool
        assert page.timeline_panel.canvas.tool is tool

    def test_the_tools_are_exclusive(self, page):
        page.set_tool(Tool.CUT)
        page.set_tool(Tool.ZOOM)
        checked = [t for t, b in page.transport.tool_buttons.items() if b.isChecked()]
        assert checked == [Tool.ZOOM]

    def _escape(self, page):
        """Press the key, rather than calling the handler that it maps to.

        The old version of this test called `set_tool(POINTER)` directly, which
        proves nothing about Escape — and Escape was in fact doing nothing at
        all, because the page installed a second Escape shortcut alongside the
        shared Deselect one and Qt answers an ambiguous shortcut by firing
        neither of them.
        """
        from PySide6.QtTest import QTest

        # `QTest.keyClick` rather than a hand-built QKeyEvent: a shortcut is
        # resolved by Qt's shortcut map before the event reaches the widget, and
        # an event posted straight at the widget never passes through it.
        # `WidgetWithChildrenShortcut` needs the window to be active as well as
        # focused, which offscreen it is not unless it is said so explicitly.
        page.activateWindow()
        page.windowHandle().requestActivate()
        page.timeline_panel.canvas.setFocus()
        QApplication.processEvents()
        QTest.keyClick(page.timeline_panel.canvas, Qt.Key_Escape)
        QApplication.processEvents()

    def test_escape_puts_the_tool_down(self, page):
        page.set_tool(Tool.CUT)
        self._escape(page)
        assert page.transport.current_tool() is Tool.POINTER
        assert page.timeline_panel.canvas.cursor().shape() != Qt.BitmapCursor

    @pytest.mark.parametrize("tool", [Tool.CUT, Tool.REFRAME, Tool.ZOOM])
    def test_escape_works_from_every_modal_tool(self, page, tool):
        page.set_tool(tool)
        self._escape(page)
        assert page.transport.current_tool() is Tool.POINTER

    def test_escape_still_clears_the_selection_once_the_tool_is_down(self, page):
        """Both jobs, in order: the tool first, then the selection. Losing the
        second to the first would be the same bug the other way round."""
        clip = Clip(media_id="m", src_in=0, src_out=60, tl_start=0, src_length=60)
        page.project.timeline.lane_for("video").insert(clip)
        page.project.set_selection([clip.clip_id])
        page.set_tool(Tool.CUT)

        self._escape(page)
        assert page.transport.current_tool() is Tool.POINTER
        assert page.project.selected_ids == [clip.clip_id], "the tool goes first"

        self._escape(page)
        assert page.project.selected_ids == []

    def test_the_toolbar_never_takes_the_keyboard(self, page):
        """A toolbar that steals focus from the timeline means the next arrow
        key does nothing."""
        for button in page.transport.tool_buttons.values():
            assert button.focusPolicy() == Qt.NoFocus
        for name in ("title_button", "dissolve_button", "snap_toggle",
                     "thumbs_toggle", "fit_button"):
            assert getattr(page.transport, name).focusPolicy() == Qt.NoFocus

    def test_the_viewer_tools_show_the_overlay(self, page):
        page.project.timeline.lane_for("video").insert(
            Clip(media_id="m", src_in=0, src_out=60, tl_start=0, src_length=60)
        )
        page.set_tool(Tool.REFRAME)
        assert page.reframe.isVisible()
        page.set_tool(Tool.POINTER)
        assert not page.reframe.isVisible()

    def test_the_bar_belongs_to_reframe_alone(self, page):
        """Zoom had it too for a while, so a zoom could be undone where it was
        made. A zoom is now a block on the timeline with its own menu, and a bar
        floating over the picture for as long as the tool is held sits on top of
        the thing you are trying to look at."""
        page.project.timeline.lane_for("video").insert(
            Clip(media_id="m", src_in=0, src_out=60, tl_start=0, src_length=60)
        )
        page.set_tool(Tool.ZOOM)
        assert not page.reframe.bar.isVisible()

        page.set_tool(Tool.REFRAME)
        assert page.reframe.bar.isVisible()
        assert page.reframe.start_button.isVisible()
        assert page.reframe.move_button.isVisible()


class TestTheAudioPage:
    @pytest.fixture
    def page(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.pages.audio_page import AudioPage
        from vedit.player.engine import PlaybackEngine

        project = Project()
        engine = PlaybackEngine(project)
        page = AudioPage(project, engine)
        page.resize(1200, 800)
        page.show()
        yield page
        engine.stop()
        page.close()

    def test_the_picture_tools_are_not_offered(self, page):
        """There are no video lanes here to point them at."""
        for tool in VIEWER_TOOLS:
            assert not page.transport.tool_buttons[tool].isVisible()

    def test_select_and_cut_still_are(self, page):
        for tool in (Tool.POINTER, Tool.CUT):
            assert page.transport.tool_buttons[tool].isVisible()

    def test_cut_reaches_this_page_s_own_timeline(self, page):
        page.transport.tool_group.idClicked.emit(
            [spec.tool for spec in TOOLS].index(Tool.CUT)
        )
        assert page.timeline_panel.canvas.tool is Tool.CUT


class TestPuttingTheFramingBack:
    """Reframing has to be undoable later, not only while Ctrl+Z reaches it.

    Double-clicking the picture resets it, but that is a gesture you have to be
    told about, so the Reframe tool carries a Reset button. A *zoom region* is
    removed from its own block on the timeline instead — see
    `test_zoom_region.py` — because that is the thing you can point at.
    """

    @pytest.fixture
    def page(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.pages.edit_page import EditPage
        from vedit.player.engine import PlaybackEngine

        project = Project()
        engine = PlaybackEngine(project)
        page = EditPage(project, engine)
        page.resize(1200, 800)
        page.show()
        project.timeline.lane_for("video").insert(
            Clip(media_id="m", src_in=0, src_out=60, tl_start=0, src_length=60)
        )
        page.set_tool(Tool.REFRAME)
        yield page
        engine.stop()

    def _clip(self, page):
        return page.project.timeline.lane_for("video").clips[0]

    def test_reset_is_offered_only_once_there_is_something_to_reset(self, page):
        page._sync_reframe()
        assert not page.reframe.reset_button.isEnabled()

        ops.set_framing(page.project.timeline, [self._clip(page)], Framing(zoom=2.5))
        page._sync_reframe()
        assert page.reframe.reset_button.isEnabled()

    def test_pressing_it_puts_the_whole_frame_back(self, page):
        ops.set_framing(page.project.timeline, [self._clip(page)], Framing(zoom=3.0, x=0.4))
        page._sync_reframe()
        page.reframe.reset_button.click()
        assert self._clip(page).framing == Framing()

    def test_it_clears_a_move_as_well_as_a_zoom(self, page):
        clip = self._clip(page)
        ops.set_framing(page.project.timeline, [clip], Framing(zoom=2.0))
        ops.set_framing_move(page.project.timeline, [clip], Framing(zoom=4.0))
        page._sync_reframe()
        page.reframe.reset_button.click()
        assert not self._clip(page).has_framing
        assert not self._clip(page).has_move

    def test_it_is_one_undo_step(self, page):
        ops.set_framing(page.project.timeline, [self._clip(page)], Framing(zoom=2.0))
        page.project.undo.mark_clean()
        page._sync_reframe()
        page.reframe.reset_button.click()
        assert self._clip(page).framing == Framing()
        page.project.undo.undo()
        assert self._clip(page).framing == Framing(zoom=2.0)

    def test_it_does_nothing_over_a_gap(self, page):
        """Parked where there is no clip, the button must not raise."""
        page.project.set_playhead(5000)
        page._sync_reframe()
        page.reframe.reset_button.click()
