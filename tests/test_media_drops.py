"""Dropping media onto the Media page's timeline.

"Append to Timeline" can only ever put a clip at the end. These lanes exist so
an import can be placed *where it belongs* without changing page first, and the
thing worth pinning down is that a drop here is the same edit the Edit page
would have made: same lane under the cursor, same frame under the cursor, same
single undo step.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QTest

from vedit.core.project import Project
from vedit.media.pool import MEDIA_MIME
from vedit.media.probe import AudioStream, MediaInfo, VideoStream
from vedit.pages.media_page import MediaPage


def make_media(media_id, *, audio=False):
    return MediaInfo(
        path=Path(f"/tmp/{media_id}.mp4"),
        media_id=media_id,
        container="mov,mp4",
        duration=Fraction(4),
        size=1000,
        video=VideoStream(
            index=0, codec="h264", width=1920, height=1080, fps=Fraction(30),
            pix_fmt="yuv420p", rotation=0, sample_aspect=Fraction(1),
            duration=Fraction(4), nb_frames=120,
        ),
        audio=AudioStream(
            index=1, codec="aac", sample_rate=48000, channels=2,
            channel_layout="stereo", duration=Fraction(4),
        ) if audio else None,
    )


def add_to_pool(project, info: MediaInfo) -> MediaInfo:
    """Put media in the pool without a file on disk.

    The public route is `add_paths`, which probes with ffprobe and so needs real
    media; these tests are about where a drop lands, not about decoding. This is
    the model's own insert, done by hand.
    """
    pool = project.pool
    row = len(pool._items)
    pool.beginInsertRows(pool.index(-1, -1).parent(), row, row)
    pool._items.append(info)
    pool._index_of[info.media_id] = row
    pool.endInsertRows()
    return info


@pytest.fixture
def page(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    project = Project()
    page = MediaPage(project)
    page.resize(1200, 800)
    page.show()
    qt_app.processEvents()
    yield page
    page.close()


def drop(qt_app, canvas, media_ids, point: QPoint) -> None:
    """Enter, move, drop — the sequence a real drag produces.

    The QMimeData is kept alive across all three: the events only borrow it,
    and letting Python collect one mid-drag crashes the interpreter.
    """
    payloads = []
    for factory in (QDragEnterEvent, QDragMoveEvent):
        data = QMimeData()
        data.setData(MEDIA_MIME, "\n".join(media_ids).encode())
        payloads.append(data)
        qt_app.sendEvent(
            canvas, factory(point, Qt.CopyAction, data, Qt.LeftButton, Qt.NoModifier)
        )
    data = QMimeData()
    data.setData(MEDIA_MIME, "\n".join(media_ids).encode())
    payloads.append(data)
    qt_app.sendEvent(
        canvas,
        QDropEvent(QPointF(point), Qt.CopyAction, data, Qt.LeftButton, Qt.NoModifier),
    )


def lane_centre(canvas, name: str) -> int:
    for track, top, height in canvas.track_rows():
        if track.name == name:
            return top + height // 2
    raise AssertionError(f"no lane {name} on this canvas")


class TestDropTimeline:
    def test_it_shows_every_lane_the_project_has(self, page):
        canvas = page.drop_timeline.panel.canvas
        assert [track.name for track in canvas.lanes()] == [
            "V3", "V2", "V1", "A1", "A2", "A3"
        ]

    def test_a_drop_lands_on_the_lane_under_the_cursor(self, qt_app, page):
        canvas = page.drop_timeline.panel.canvas
        add_to_pool(page.project, make_media("m1"))

        drop(qt_app, canvas, ["m1"], QPoint(400, lane_centre(canvas, "V2")))

        video = page.project.timeline.video_tracks
        assert [clip.media_id for clip in video[1]] == ["m1"]
        assert not video[0].clips and not video[2].clips

    def test_a_drop_lands_at_the_frame_under_the_cursor(self, qt_app, page):
        canvas = page.drop_timeline.panel.canvas
        add_to_pool(page.project, make_media("m1"))

        x = 500
        drop(qt_app, canvas, ["m1"], QPoint(x, lane_centre(canvas, "V1")))

        clip = page.project.timeline.lane_for("video").clips[0]
        assert clip.tl_start == canvas.frame_of(x)
        assert clip.tl_start > 0        # not merely appended at the start

    def test_a_drop_is_one_undo_step(self, qt_app, page):
        canvas = page.drop_timeline.panel.canvas
        add_to_pool(page.project, make_media("m1", audio=True))

        drop(qt_app, canvas, ["m1"], QPoint(300, lane_centre(canvas, "V1")))
        assert len(list(page.project.timeline.all_clips())) == 2

        page.project.undo_edit()
        assert list(page.project.timeline.all_clips()) == []

    def test_the_hint_gives_way_to_the_timeline(self, qt_app, page):
        """The prompt is for an empty timeline; once there are clips it is just
        a label sitting over them."""
        assert page.drop_timeline.hint.isVisible()

        add_to_pool(page.project, make_media("m1"))
        drop(qt_app, page.drop_timeline.panel.canvas, ["m1"],
             QPoint(300, lane_centre(page.drop_timeline.panel.canvas, "V1")))
        qt_app.processEvents()

        assert not page.drop_timeline.hint.isVisible()
        assert page.drop_timeline.duration.text() != "00:00:00:00"


class TestDeleteKeys:
    """Delete and Backspace on the drop timeline.

    The lanes here are the same canvas the Edit page uses, holding the same
    selection — but the shortcuts that act on a selection used to live only on
    the Edit and Audio pages, so a clip could be selected here and then not
    deleted. Pressing Delete did nothing at all, which reads as a frozen app
    rather than as a key that is not bound.

    The Media page is also the one place two things answer to Delete: the pool
    removes media, the timeline removes a clip. Both are scoped to their own
    widget, so the focus decides, and that is worth a test of its own.
    """

    def place(self, qt_app, page, media_id="m1"):
        """One clip on V1, selected, with the canvas focused."""
        canvas = page.drop_timeline.panel.canvas
        add_to_pool(page.project, make_media(media_id))
        drop(qt_app, canvas, [media_id], QPoint(200, lane_centre(canvas, "V1")))
        qt_app.processEvents()
        clip = page.project.timeline.video_tracks[0].clips[0]
        page.project.set_selection([clip.clip_id])
        canvas.setFocus()
        qt_app.processEvents()
        return canvas, clip

    def test_delete_removes_the_selected_clip(self, qt_app, page):
        canvas, _ = self.place(qt_app, page)
        QTest.keyClick(canvas, Qt.Key_Delete)
        qt_app.processEvents()
        assert page.project.timeline.video_tracks[0].clips == []

    def two_clips(self, qt_app, page):
        """Two clips on V1. Only a follower can show the difference between the keys."""
        canvas, first = self.place(qt_app, page, "m1")
        add_to_pool(page.project, make_media("m2"))
        drop(qt_app, canvas, ["m2"], QPoint(600, lane_centre(canvas, "V1")))
        qt_app.processEvents()
        clips = sorted(page.project.timeline.video_tracks[0].clips, key=lambda c: c.tl_start)
        assert len(clips) == 2
        page.project.set_selection([clips[0].clip_id])
        canvas.setFocus()
        return canvas, clips[0], clips[1]

    def test_backspace_leaves_the_gap(self, qt_app, page):
        canvas, first, second = self.two_clips(qt_app, page)
        follower_was = second.tl_start
        QTest.keyClick(canvas, Qt.Key_Backspace)
        qt_app.processEvents()

        remaining = page.project.timeline.video_tracks[0].clips
        assert [c.clip_id for c in remaining] == [second.clip_id]
        assert remaining[0].tl_start == follower_was, "Backspace should not close the gap"

    def test_delete_closes_the_gap(self, qt_app, page):
        canvas, first, second = self.two_clips(qt_app, page)
        follower_was = second.tl_start
        QTest.keyClick(canvas, Qt.Key_Delete)
        qt_app.processEvents()

        remaining = page.project.timeline.video_tracks[0].clips
        assert [c.clip_id for c in remaining] == [second.clip_id]
        assert remaining[0].tl_start < follower_was, "Delete should ripple the follower back"

    def test_delete_is_one_undo_step(self, qt_app, page):
        canvas, _ = self.place(qt_app, page)
        QTest.keyClick(canvas, Qt.Key_Delete)
        qt_app.processEvents()
        page.project.undo_edit()
        assert len(page.project.timeline.video_tracks[0].clips) == 1

    def test_delete_with_nothing_selected_says_so(self, qt_app, page):
        canvas, _ = self.place(qt_app, page)
        page.project.set_selection([])
        messages = []
        page.drop_timeline.status_message.connect(messages.append)
        QTest.keyClick(canvas, Qt.Key_Delete)
        qt_app.processEvents()
        assert len(page.project.timeline.video_tracks[0].clips) == 1
        assert messages == ["Select a clip first"]

    def test_the_pool_keeps_its_own_delete(self, qt_app, page):
        """Focus in the table removes media, not the clip that is still selected."""
        canvas, _ = self.place(qt_app, page)
        table = page.pool_panel.table
        table.selectRow(0)
        table.setFocus()
        qt_app.processEvents()

        QTest.keyClick(table, Qt.Key_Delete)
        qt_app.processEvents()

        assert page.project.pool.rowCount() == 0
        assert len(page.project.timeline.video_tracks[0].clips) == 1
