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
