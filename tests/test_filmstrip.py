"""Filmstrip layout, indexing and generation."""

from __future__ import annotations

import json
import subprocess

import pytest
from PySide6.QtGui import QPixmap

from vedit.media.proxy import (
    FILMSTRIP_MAX_CELLS,
    FILMSTRIP_MIN_CELLS,
    filmstrip_plan,
)
from vedit.timeline.filmstrip import Filmstrip, FilmstripCache


def strip(count=20, columns=5, cell_width=96, cell_height=54, interval=1.0):
    return Filmstrip(
        pixmap=QPixmap(), count=count, columns=columns,
        cell_width=cell_width, cell_height=cell_height, interval=interval,
    )


class TestPlan:
    def test_roughly_one_cell_per_second(self):
        assert filmstrip_plan(30.0, 16 / 9)["count"] == 30

    def test_short_media_still_gets_enough_cells(self):
        plan = filmstrip_plan(2.0, 16 / 9)
        assert plan["count"] == FILMSTRIP_MIN_CELLS
        assert plan["interval"] == pytest.approx(2.0 / FILMSTRIP_MIN_CELLS)

    def test_long_media_is_capped(self):
        """Two hours must not try to make seven thousand thumbnails."""
        plan = filmstrip_plan(7200.0, 16 / 9)
        assert plan["count"] == FILMSTRIP_MAX_CELLS
        assert plan["interval"] == pytest.approx(24.0)

    def test_interval_always_spans_the_media(self):
        for duration in (1.0, 17.2, 60.0, 3600.0):
            plan = filmstrip_plan(duration, 16 / 9)
            assert plan["count"] * plan["interval"] == pytest.approx(duration)

    @pytest.mark.parametrize("aspect", [16 / 9, 4 / 3, 1.0, 9 / 16, 484 / 480])
    def test_cell_width_is_even_and_keeps_aspect(self, aspect):
        plan = filmstrip_plan(30.0, aspect)
        assert plan["cell_width"] % 2 == 0, "odd widths upset the scaler"
        assert plan["cell_width"] / plan["cell_height"] == pytest.approx(aspect, abs=0.05)

    def test_portrait_media_gets_a_narrow_cell(self):
        assert filmstrip_plan(30.0, 9 / 16)["cell_width"] < 40


class TestIndexing:
    def test_maps_seconds_to_cells(self):
        s = strip(count=20, interval=1.0)
        assert s.index_for(0.0) == 0
        assert s.index_for(0.9) == 0
        assert s.index_for(1.0) == 1
        assert s.index_for(5.5) == 5

    def test_clamps_past_the_ends(self):
        s = strip(count=20, interval=1.0)
        assert s.index_for(-5.0) == 0
        assert s.index_for(9999.0) == 19, "never indexes past the last cell"

    def test_non_unit_interval(self):
        s = strip(count=10, interval=2.5)
        assert s.index_for(2.4) == 0
        assert s.index_for(2.6) == 1
        assert s.index_for(25.0) == 9

    def test_zero_interval_is_safe(self):
        assert strip(interval=0.0).index_for(12.0) == 0

    def test_cell_rect_wraps_rows(self):
        s = strip(count=20, columns=5, cell_width=96, cell_height=54)
        assert s.cell(0).topLeft().toTuple() == (0, 0)
        assert s.cell(4).topLeft().toTuple() == (4 * 96, 0)
        assert s.cell(5).topLeft().toTuple() == (0, 54), "wrapped to the next row"
        assert s.cell(12).topLeft().toTuple() == (2 * 96, 2 * 54)

    def test_cell_rect_is_clamped(self):
        s = strip(count=6, columns=5)
        assert s.cell(99) == s.cell(5)
        assert s.cell(-3) == s.cell(0)

    def test_aspect(self):
        assert strip(cell_width=96, cell_height=54).aspect == pytest.approx(16 / 9)


class TestCache:
    def test_missing_returns_none(self):
        assert FilmstripCache().get("nope", None) is None

    def test_unreadable_sheet_is_remembered_as_missing(self, tmp_path):
        bad = tmp_path / "bad.jpg"
        bad.write_text("not an image")
        cache = FilmstripCache()
        meta = {"count": 4, "columns": 2, "cell_width": 8, "cell_height": 8, "interval": 1}
        assert cache.get("m", (bad, meta)) is None
        assert "m" in cache._missing, "must not retry a broken sheet on every repaint"

    def test_zero_count_is_rejected(self, tmp_path):
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=32x32",
             "-frames:v", "1", str(tmp_path / "s.jpg")],
            check=True, capture_output=True,
        )
        meta = {"count": 0, "columns": 2, "cell_width": 8, "cell_height": 8, "interval": 1}
        assert FilmstripCache().get("m", (tmp_path / "s.jpg", meta)) is None


class TestGeneration:
    def test_sheet_and_meta_are_produced(self, tmp_path):
        """End to end: the ffmpeg tile pass really does emit a usable grid."""
        source = tmp_path / "src.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
             "-i", "testsrc2=size=320x180:rate=30", "-t", "10",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source)],
            check=True, capture_output=True,
        )
        plan = filmstrip_plan(10.0, 320 / 180)
        rows = max(1, -(-plan["count"] // plan["columns"]))
        sheet = tmp_path / "sheet.jpg"
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(source), "-vf",
             f"fps=1/{plan['interval']:.6f},scale={plan['cell_width']}:{plan['cell_height']},"
             f"tile={plan['columns']}x{rows}:padding=0",
             "-frames:v", "1", "-q:v", "4", str(sheet)],
            check=True, capture_output=True,
        )
        assert sheet.exists()

        pixmap = QPixmap(str(sheet))
        assert not pixmap.isNull()
        assert pixmap.width() == plan["columns"] * plan["cell_width"]
        assert pixmap.height() == rows * plan["cell_height"]

        loaded = FilmstripCache().get("m", (sheet, {**plan, "rows": rows}))
        assert loaded is not None
        assert loaded.count == plan["count"]
        # The last real cell must fall inside the sheet.
        assert loaded.cell(loaded.count - 1).right() <= pixmap.width()
