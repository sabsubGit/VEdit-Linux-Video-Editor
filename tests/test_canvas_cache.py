"""The timeline's lanes are drawn once and kept.

The playhead moves thirty or sixty times a second and nothing behind it
changes, but redrawing it used to mean laying out every clip, filmstrip and
waveform on the timeline — several milliseconds taken from the thread that also
feeds the decoder and the audio device, which is why the picture drifted behind
the sound on a busy edit.

A cache is only ever as good as its invalidation, so most of this file is about
that: every route that changes what a lane looks like has to drop it. The rule
is that `TimelineCanvas.update` itself invalidates, so the only way to keep a
stale picture is to deliberately bypass it — which exactly one method does.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QImage

from vedit.core.project import Project
from vedit.timeline import ops
from vedit.timeline.framing import Framing, Region
from vedit.timeline.model import Clip


@pytest.fixture
def canvas(qt_app, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    from vedit.timeline.view import TimelineCanvas

    project = Project()
    canvas = TimelineCanvas(project)
    canvas.resize(1200, 400)
    canvas.px_per_frame = 4.0
    canvas.release_auto_fit()
    for start in (0, 200):
        project.timeline.lane_for("video").insert(
            Clip(media_id="m", src_in=0, src_out=150, tl_start=start,
                 src_length=400, name=f"shot{start}")
        )
    return canvas


def warm(canvas):
    """Paint once, so there is something cached to go stale."""
    target = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
    canvas.render(target)
    assert canvas._cache is not None
    return target


class TestItIsActuallyUsed:
    def test_the_first_paint_fills_it(self, canvas):
        assert canvas._cache is None
        warm(canvas)

    def test_a_second_paint_reuses_it(self, canvas):
        warm(canvas)
        before = canvas._cache
        canvas.render(QImage(canvas.width(), canvas.height(), QImage.Format_RGB32))
        assert canvas._cache is before

    def test_moving_the_playhead_keeps_it(self, canvas):
        """The whole point: a playhead move is a blit and two thin strips."""
        warm(canvas)
        before = canvas._cache
        canvas.project.set_playhead(40)
        assert canvas._cache is before

    def test_a_resize_rebuilds_it(self, canvas):
        warm(canvas)
        canvas.resize(900, 400)
        canvas.render(QImage(900, 400, QImage.Format_RGB32))
        assert canvas._cache.width() >= 900 * canvas.devicePixelRatioF() - 1


class TestEverythingThatMustDropIt:
    """One case per route that changes what a lane looks like."""

    def test_a_model_change(self, canvas):
        warm(canvas)
        canvas.project.timeline_changed.emit()
        assert canvas._cache is None

    def test_a_selection_change(self, canvas):
        warm(canvas)
        clip = canvas.timeline.lane_for("video").clips[0]
        canvas.project.set_selection([clip.clip_id])
        assert canvas._cache is None

    def test_a_zoom_change(self, canvas):
        warm(canvas)
        canvas.px_per_frame = 8.0
        canvas.update()
        assert canvas._cache is None

    def test_a_scroll_taken_through_the_playhead_path(self, canvas):
        """`refresh_playhead` is the one method allowed to keep the cache, so
        it has to notice when a scroll has moved everything under it."""
        warm(canvas)
        canvas.scroll_x += 120
        canvas.refresh_playhead()
        assert canvas._cache is None

    def test_a_tool_change(self, canvas):
        from vedit.tools import Tool

        warm(canvas)
        canvas.set_tool(Tool.CUT)
        assert canvas._cache is None

    def test_a_new_zoom_region(self, canvas):
        warm(canvas)
        clip = canvas.timeline.lane_for("video").clips[0]
        ops.set_zoom_region(
            canvas.timeline, [clip], Region(Framing(zoom=2.0), 10, 60)
        )
        canvas.project.timeline_changed.emit()
        assert canvas._cache is None


class TestTheCachedPictureIsTheRealOne:
    """Cheaper is no use if it is different."""

    def _paint(self, canvas):
        target = QImage(canvas.width(), canvas.height(), QImage.Format_RGB32)
        target.fill(0)
        canvas.render(target)
        return target

    def test_cached_and_uncached_paints_match(self, canvas):
        canvas.project.set_playhead(60)
        fresh = self._paint(canvas)
        assert canvas._cache is not None, "the second paint should be cached"
        cached = self._paint(canvas)
        assert cached == fresh

    def test_it_matches_after_the_playhead_has_moved_over_it(self, canvas):
        """The case the cache exists for, and the one where a stale picture
        would show up as the lanes freezing while the marker travels."""
        for frame in range(0, 120, 7):
            canvas.project.set_playhead(frame)
            self._paint(canvas)

        moved = self._paint(canvas)
        canvas.update()                       # force a full rebuild
        assert canvas._cache is None
        assert self._paint(canvas) == moved

    def test_an_edit_mid_playback_shows_up(self, canvas):
        """A stale cache would keep drawing the clip at its old length."""
        canvas.project.set_playhead(30)
        before = self._paint(canvas)

        clip = canvas.timeline.lane_for("video").clips[0]
        ops.trim(canvas.timeline, clip, "out", 80)
        canvas.project.timeline_changed.emit()
        assert self._paint(canvas) != before
