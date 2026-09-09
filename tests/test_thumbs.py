"""Pool thumbnails: one cell shape, whatever the source or the cache state.

The bugs these cover both showed up as "the thumbnails are inconsistent": a
column where some rows had a picture and some had nothing, and where the ones
that did had it in a different shape each time.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPixmap

from vedit.core.ffmpeg import FFmpegError
from vedit.core.timebase import TimeBase
from vedit.media import thumbs
from vedit.media.pool import MediaPool
from vedit.media.probe import AudioStream, MediaInfo, VideoStream
from vedit.media.proxy import CachePaths, ProxyManager, _IngestJob, _Signals


def video_stream(width=1920, height=1080):
    return VideoStream(
        index=0, codec="h264", width=width, height=height, fps=Fraction(30),
        pix_fmt="yuv420p", rotation=0, sample_aspect=Fraction(1),
        duration=Fraction(5), nb_frames=150,
    )


def audio_stream():
    return AudioStream(
        index=1, codec="aac", sample_rate=48000, channels=2,
        channel_layout="stereo", duration=Fraction(5),
    )


def media(media_id="m1", *, video=True, audio=False, path="/tmp/clip.mp4"):
    return MediaInfo(
        path=Path(path), media_id=media_id, container="mov,mp4",
        duration=Fraction(5), size=1234,
        video=video_stream() if video else None,
        audio=audio_stream() if audio else None,
    )


def filled(width, height, colour="#ff0000") -> QPixmap:
    pixmap = QPixmap(width, height)
    pixmap.fill(QColor(colour))
    return pixmap


class TestCellShape:
    """Every cell is the same rectangle — that is the whole point of the module."""

    @pytest.mark.parametrize(
        "source_size",
        [(1920, 1080), (640, 480), (608, 1080), (1920, 816), (7, 3)],
    )
    def test_letterbox_always_fills_the_cell(self, source_size):
        cell = thumbs.letterbox(filled(*source_size), thumbs.POOL_CELL)
        assert cell.size() == thumbs.POOL_CELL

    def test_letterbox_keeps_the_aspect_ratio(self):
        """A 1:1 source must come back square, not stretched to 16:9."""
        cell = thumbs.letterbox(filled(200, 200), QSize(96, 54))
        image = cell.toImage()
        # The image is centred, so the row through the middle has the source
        # colour in the middle 54 pixels and background either side.
        middle = image.height() // 2
        red = [x for x in range(image.width())
               if QColor(image.pixel(x, middle)) == QColor("#ff0000")]
        assert len(red) == pytest.approx(54, abs=2)
        assert min(red) > 0 and max(red) < image.width() - 1

    def test_a_null_source_still_gives_a_cell(self):
        assert thumbs.letterbox(QPixmap(), QSize(96, 54)).size() == QSize(96, 54)

    def test_every_state_is_the_same_size(self):
        """Audio, pending and ready must not shift the column when they swap."""
        size = QSize(96, 54)
        sizes = {
            thumbs.render(QPixmap(), has_video=False, size=size).size(),
            thumbs.render(QPixmap(), has_video=True, size=size).size(),
            thumbs.render(filled(1920, 1080), has_video=True, size=size).size(),
        }
        assert sizes == {size}

    def test_audio_and_pending_are_told_apart(self):
        size = QSize(96, 54)
        audio = thumbs.render(QPixmap(), has_video=False, size=size).toImage()
        pending = thumbs.render(QPixmap(), has_video=True, size=size).toImage()
        assert audio != pending

    def test_thumbnail_tolerates_a_missing_file(self, tmp_path):
        cell = thumbs.thumbnail(tmp_path / "gone.jpg", has_video=True)
        assert cell.size() == thumbs.POOL_CELL


class TestPoolIcons:
    """Every row carries an icon, so no row's name starts in a different place."""

    def pool(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        return MediaPool(ProxyManager(), TimeBase(30))

    def test_video_awaiting_its_thumbnail_still_has_an_icon(self, tmp_path, monkeypatch):
        pool = self.pool(tmp_path, monkeypatch)
        icon = pool._icon_for(media("v1"))
        assert not icon.isNull()

    def test_audio_has_an_icon_it_will_never_get_a_frame_for(self, tmp_path, monkeypatch):
        pool = self.pool(tmp_path, monkeypatch)
        icon = pool._icon_for(media("a1", video=False, audio=True))
        assert not icon.isNull()

    def test_a_ready_thumbnail_is_normalised_to_the_cell(self, tmp_path, monkeypatch):
        pool = self.pool(tmp_path, monkeypatch)
        path = tmp_path / "wide.jpg"
        filled(320, 136).save(str(path))

        pool._on_thumb("m1", str(path))
        icon = pool._thumbs["m1"]
        assert icon.availableSizes()[0] == thumbs.POOL_CELL

    def test_an_unreadable_thumbnail_leaves_the_placeholder(self, tmp_path, monkeypatch):
        """A half-written file is a reason to keep a cell, not to blank the row."""
        pool = self.pool(tmp_path, monkeypatch)
        before = pool._icon_for(media("m1"))
        pool._on_thumb("m1", str(tmp_path / "not-an-image.jpg"))
        assert pool._thumbs["m1"] is before


class TestIngestAnnouncesThumbnails:
    """The pool builds its icons from `thumb_ready` and nothing else."""

    def job(self, tmp_path, monkeypatch, info=None):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        signals = _Signals()
        seen: list[tuple[str, str]] = []
        signals.thumb_ready.connect(lambda mid, path: seen.append((mid, path)))
        return _IngestJob(info or media(), signals), seen

    def test_a_cached_thumbnail_is_announced_anyway(self, tmp_path, monkeypatch):
        """Reopening a project against a warm cache used to show blank rows:
        the stage returned early and nobody ever heard about the file."""
        job, seen = self.job(tmp_path, monkeypatch)
        job.paths.thumb.parent.mkdir(parents=True, exist_ok=True)
        job.paths.thumb.write_bytes(b"cached")

        job._make_thumb()
        assert seen == [("m1", str(job.paths.thumb))]

    def test_audio_only_media_generates_nothing(self, tmp_path, monkeypatch):
        job, seen = self.job(
            tmp_path, monkeypatch, media("a1", video=False, audio=True)
        )
        job._make_thumb()
        assert seen == []

    def test_a_failed_seek_falls_back_to_the_first_frame(self, tmp_path, monkeypatch):
        """An overstated duration seeks past the end and decodes nothing."""
        job, seen = self.job(tmp_path, monkeypatch)
        offsets: list[str] = []

        def fake_run(args, *, output):
            offsets.append(args[args.index("-ss") + 1])
            if float(offsets[-1]) > 0:
                raise FFmpegError("ffmpeg produced no output", command=[], stderr="")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"frame")

        monkeypatch.setattr(job, "_run", fake_run)
        job._make_thumb()

        assert [float(offset) for offset in offsets] == [0.5, 0.0]
        assert len(seen) == 1

    def test_a_thumbnail_that_cannot_be_made_does_not_stop_the_ingest(
        self, tmp_path, monkeypatch
    ):
        """The proxy and the peaks are what make media playable; one awkward
        frame must not cost it those."""
        job, _ = self.job(tmp_path, monkeypatch)
        stages: list[str] = []

        def fail(*_args, **_kwargs):
            raise FFmpegError("no frame", command=[], stderr="")

        monkeypatch.setattr(job, "_make_thumb", fail)
        monkeypatch.setattr(job, "_make_peaks", lambda: stages.append("peaks"))
        monkeypatch.setattr(job, "_make_proxy", lambda: stages.append("proxy"))
        monkeypatch.setattr(job, "_make_filmstrip", lambda: stages.append("strip"))

        statuses: list[str] = []
        job.signals.status.connect(lambda _mid, status: statuses.append(status))

        job.run()

        assert stages == ["peaks", "proxy", "strip"]
        assert statuses[-1] == "ready"


class TestCacheNaming:
    def test_the_thumbnail_size_is_in_its_name(self, tmp_path, monkeypatch):
        """Older caches hold thumbnails framed differently; reusing the name
        would keep serving them forever."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.media.proxy import THUMB_BOX

        assert CachePaths.for_id("abc").thumb.stem.endswith(str(THUMB_BOX[1]))
