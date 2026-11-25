"""Keyboard layouts: vedit's own keys, Premiere's and Resolve's.

Pressed for real, in a real window. A layout that reads correctly as a table can
still do nothing: two actions sharing a key in one scope are ambiguous to Qt,
which answers by firing neither.
"""

from __future__ import annotations

import subprocess

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from vedit.core.project import Project
from vedit.keymap import COMMON, LAYOUTS, Keymap, keymap
from vedit.timeline import ops
from vedit.tools import Tool


@pytest.fixture(autouse=True)
def back_to_default():
    """The keymap is one per process; no test may leave another a foreign layout."""
    yield
    keymap.set_layout("vedit", save=False)


@pytest.fixture(scope="module")
def clip_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("keys") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True, capture_output=True,
    )
    return path


@pytest.fixture
def window(qt_app, clip_file, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    from vedit.app import MainWindow

    win = MainWindow(Project())
    win.resize(1400, 850)
    win.show()
    info = win.project.import_paths([clip_file])[0]
    win.project.edit("Add", lambda t: ops.append_media(t, info))
    win.show_page(1)
    qt_app.processEvents()
    yield win
    win.project.undo.mark_clean()
    win.engine.stop()
    win.close()


def press(qt_app, window, key, modifiers=Qt.NoModifier):
    QTest.keyClick(window.edit_page.timeline_panel.canvas, key, modifiers)
    qt_app.processEvents()


def clips(window, kind="video"):
    return [c for c in window.project.timeline.all_clips() if c.kind == kind]


def select_first(window):
    first = min(window.project.timeline.all_clips(), key=lambda c: (c.tl_start, c.kind))
    window.project.set_selection([first.clip_id])


class TestTheTables:
    @pytest.mark.parametrize("layout", list(LAYOUTS))
    def test_every_layout_names_every_action(self, layout):
        assert set(LAYOUTS[layout]) == set(LAYOUTS["vedit"])
        assert set(COMMON) <= set(LAYOUTS[layout])

    @pytest.mark.parametrize("layout", list(LAYOUTS))
    def test_no_key_is_given_to_two_actions(self, layout):
        """Every one of these lives in the Edit page's scope together."""
        from PySide6.QtGui import QKeySequence

        seen: dict[str, str] = {}
        for name, keys in LAYOUTS[layout].items():
            for key in keys:
                canonical = QKeySequence(key).toString()
                assert canonical, f"{key!r} is not a key Qt understands"
                assert canonical not in seen, (
                    f"{layout}: {key} is both {seen[canonical]} and {name}"
                )
                seen[canonical] = name

    def test_an_unknown_layout_or_action_is_refused(self, qt_app):
        from PySide6.QtGui import QAction

        with pytest.raises(ValueError):
            keymap.set_layout("avid", save=False)
        with pytest.raises(KeyError):
            keymap.bind(QAction("x"), "make_coffee")


class TestVeditKeys:
    def test_x_splits_and_delete_ripples(self, qt_app, window):
        window.project.set_playhead(45)
        press(qt_app, window, Qt.Key_X)
        assert len(clips(window)) == 2

        select_first(window)
        press(qt_app, window, Qt.Key_Delete)
        assert len(clips(window)) == 1
        assert clips(window)[0].tl_start == 0, "ripple delete closes the gap"

    def test_c_and_v_pick_the_tools(self, qt_app, window):
        press(qt_app, window, Qt.Key_C)
        assert window.edit_page.transport.current_tool() is Tool.CUT
        press(qt_app, window, Qt.Key_V)
        assert window.edit_page.transport.current_tool() is Tool.POINTER

    def test_s_toggles_snapping(self, qt_app, window):
        canvas = window.edit_page.timeline_panel.canvas
        assert canvas.snapping
        press(qt_app, window, Qt.Key_S)
        assert not canvas.snapping
        press(qt_app, window, Qt.Key_S)
        assert canvas.snapping


class TestPremiereKeys:
    def test_ctrl_k_splits_and_x_no_longer_does(self, qt_app, window):
        keymap.set_layout("premiere", save=False)
        window.project.set_playhead(45)
        press(qt_app, window, Qt.Key_X)
        assert len(clips(window)) == 1
        press(qt_app, window, Qt.Key_K, Qt.ControlModifier)
        assert len(clips(window)) == 2

    def test_delete_leaves_a_gap_and_shift_delete_closes_it(self, qt_app, window):
        keymap.set_layout("premiere", save=False)
        window.project.set_playhead(45)
        press(qt_app, window, Qt.Key_K, Qt.ControlModifier)

        select_first(window)
        press(qt_app, window, Qt.Key_Delete)
        assert [c.tl_start for c in clips(window)] == [45], "plain Delete lifts"

        window.project.undo_edit()
        select_first(window)
        press(qt_app, window, Qt.Key_Delete, Qt.ShiftModifier)
        assert [c.tl_start for c in clips(window)] == [0], "Shift+Delete ripples"


class TestResolveKeys:
    def test_a_and_b_pick_the_tools_and_v_and_c_do_not(self, qt_app, window):
        keymap.set_layout("resolve", save=False)
        transport = window.edit_page.transport
        press(qt_app, window, Qt.Key_C)
        assert transport.current_tool() is Tool.POINTER
        press(qt_app, window, Qt.Key_B)
        assert transport.current_tool() is Tool.CUT
        press(qt_app, window, Qt.Key_A)
        assert transport.current_tool() is Tool.POINTER

    def test_ctrl_b_splits_and_shift_backspace_ripples(self, qt_app, window):
        keymap.set_layout("resolve", save=False)
        window.project.set_playhead(45)
        press(qt_app, window, Qt.Key_B, Qt.ControlModifier)
        assert len(clips(window)) == 2

        select_first(window)
        press(qt_app, window, Qt.Key_Backspace, Qt.ShiftModifier)
        assert [c.tl_start for c in clips(window)] == [0]

    def test_n_toggles_snapping(self, qt_app, window):
        keymap.set_layout("resolve", save=False)
        canvas = window.edit_page.timeline_panel.canvas
        press(qt_app, window, Qt.Key_N)
        assert not canvas.snapping

    def test_the_transport_is_the_same_everywhere(self, qt_app, window):
        keymap.set_layout("resolve", save=False)
        window.project.set_playhead(10)
        press(qt_app, window, Qt.Key_Right)
        assert window.project.playhead == 11
        press(qt_app, window, Qt.Key_Home)
        assert window.project.playhead == 0


class TestWhatIsShown:
    def test_tooltips_menus_and_hints_follow_the_layout(self, qt_app, window):
        transport = window.edit_page.transport
        canvas = window.edit_page.timeline_panel.canvas
        clip = clips(window)[0]

        def menu_texts():
            return [a.text() for a in canvas.build_clip_menu(clip).actions()]

        assert transport.tool_buttons[Tool.CUT].toolTip().endswith("(C)")
        assert "Cut at Playhead\tX" in menu_texts()

        window.set_keyboard_layout("resolve")
        assert transport.tool_buttons[Tool.CUT].toolTip().endswith("(B)")
        assert "Cut at Playhead\tCtrl+B" in menu_texts()
        assert "Ripple Delete\tShift+Backspace" in menu_texts()
        assert window.keyboard_group.checkedAction().text() == "Resolve"

        seen = []
        window.edit_page.status_message.connect(seen.append)
        window.edit_page.set_tool(Tool.CUT)
        assert any("Esc or A to stop cutting" in line for line in seen)

    def test_the_choice_is_remembered(self, qt_app, window):
        window.set_keyboard_layout("premiere")
        fresh = Keymap()
        fresh.load()
        assert fresh.layout == "premiere"
        window.set_keyboard_layout("vedit")
        fresh.load()
        assert fresh.layout == "vedit"
