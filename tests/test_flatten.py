"""Multi-lane flattening.

Preview and render must agree on what is visible, so both are driven from the
same rule: the topmost video lane with a clip at a given frame wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vedit.core.timebase import TimeBase
from vedit.player.segments import audio_playlists, build_video_playlist
from vedit.render.graph import _video_pieces
from vedit.timeline.model import Clip, Timeline


class FakeInfo:
    def __init__(self, media_id):
        self.media_id = media_id
        self.path = Path(f"/media/{media_id}.mp4")


class FakePool:
    def info_for(self, media_id):
        return FakeInfo(media_id)


class FakeProxies:
    def playback_path(self, info):
        return info.path


@pytest.fixture
def timeline():
    return Timeline.default(TimeBase(30))


def put(timeline, lane_index, media_id, start, length, *, kind="video", src_in=0):
    lanes = timeline.video_tracks if kind == "video" else timeline.audio_tracks
    c = Clip(
        media_id=media_id, src_in=src_in, src_out=src_in + length, tl_start=start,
        src_length=src_in + length + 1000, kind=kind, name=media_id,
    )
    lanes[lane_index].insert(c)
    return c


def visible(playlist):
    return [(s.tl_start, s.tl_end, s.media_id or None) for s in playlist.segments]


class TestVideoFlatten:
    def test_single_lane_behaves_as_before(self, timeline):
        put(timeline, 0, "a", 0, 100)
        playlist = build_video_playlist(timeline, FakePool(), FakeProxies())
        assert visible(playlist) == [(0, 100, "a")]

    def test_upper_lane_covers_lower(self, timeline):
        """The whole point of a second video lane: V2 occludes V1."""
        put(timeline, 0, "under", 0, 100)
        put(timeline, 1, "over", 40, 20)
        assert visible(build_video_playlist(timeline, FakePool(), FakeProxies())) == [
            (0, 40, "under"),
            (40, 60, "over"),
            (60, 100, "under"),
        ]

    def test_highest_lane_of_three_wins(self, timeline):
        put(timeline, 0, "v1", 0, 100)
        put(timeline, 1, "v2", 0, 100)
        put(timeline, 2, "v3", 0, 100)
        assert visible(build_video_playlist(timeline, FakePool(), FakeProxies())) == [(0, 100, "v3")]

    def test_source_offset_is_correct_after_being_covered(self, timeline):
        """The uncovered tail must resume at the right source frame, not restart."""
        put(timeline, 0, "under", 0, 100)
        put(timeline, 1, "over", 40, 20)
        tail = build_video_playlist(timeline, FakePool(), FakeProxies()).segments[2]
        assert tail.media_id == "under"
        assert tail.src_start == 60, "resumes 60 frames into the source, not at 0"

    def test_gap_where_no_lane_has_content(self, timeline):
        put(timeline, 0, "a", 0, 50)
        put(timeline, 1, "b", 80, 20)
        segments = build_video_playlist(timeline, FakePool(), FakeProxies()).segments
        assert [(s.tl_start, s.tl_end, s.is_gap) for s in segments] == [
            (0, 50, False), (50, 80, True), (80, 100, False),
        ]

    def test_muted_video_lane_is_ignored(self, timeline):
        put(timeline, 0, "under", 0, 100)
        put(timeline, 1, "over", 0, 100)
        timeline.video_tracks[1].muted = True
        assert visible(build_video_playlist(timeline, FakePool(), FakeProxies())) == [(0, 100, "under")]

    def test_disabled_clip_is_ignored(self, timeline):
        put(timeline, 0, "under", 0, 100)
        over = put(timeline, 1, "over", 0, 100)
        over.enabled = False
        assert visible(build_video_playlist(timeline, FakePool(), FakeProxies())) == [(0, 100, "under")]

    def test_adjacent_pieces_of_one_clip_are_merged(self, timeline):
        """Splitting at every lane edge produces neighbouring pieces of the same
        clip; they must merge so the decoder does not re-seek a file it is
        already positioned in."""
        put(timeline, 0, "a", 0, 100)
        put(timeline, 1, "b", 30, 10)
        put(timeline, 2, "c", 60, 10)
        segments = build_video_playlist(timeline, FakePool(), FakeProxies()).segments
        assert [s.media_id for s in segments] == ["a", "b", "a", "c", "a"]

    def test_empty_timeline(self, timeline):
        assert not build_video_playlist(timeline, FakePool(), FakeProxies())


class TestRenderAgreesWithPreview:
    def test_same_visible_order(self, timeline):
        put(timeline, 0, "under", 0, 100)
        put(timeline, 1, "over", 40, 20)

        preview = build_video_playlist(timeline, FakePool(), FakeProxies())
        inputs = {"under": 0, "over": 1}
        pieces = _video_pieces(timeline, inputs, FakePool())

        assert len(pieces) == len(preview.segments)
        for piece, segment in zip(pieces, preview.segments):
            expected = None if segment.is_gap else inputs[segment.media_id]
            assert piece.input_index == expected

    def test_render_trims_the_uncovered_tail_correctly(self, timeline):
        put(timeline, 0, "under", 0, 100)
        put(timeline, 1, "over", 40, 20)
        pieces = _video_pieces(timeline, {"under": 0, "over": 1}, FakePool())
        tail = pieces[2]
        # 60 frames at 30fps = 2.0s into the source.
        assert tail.start == pytest.approx(2.0)
        assert tail.end == pytest.approx(100 / 30)


class TestAudioLanes:
    def test_one_playlist_per_populated_lane(self, timeline):
        put(timeline, 0, "a", 0, 100, kind="audio")
        put(timeline, 1, "b", 0, 100, kind="audio")
        assert len(audio_playlists(timeline, FakePool(), FakeProxies())) == 2

    def test_empty_lanes_are_skipped(self, timeline):
        put(timeline, 0, "a", 0, 100, kind="audio")
        assert len(audio_playlists(timeline, FakePool(), FakeProxies())) == 1

    def test_muted_lane_is_skipped(self, timeline):
        put(timeline, 0, "a", 0, 100, kind="audio")
        put(timeline, 1, "b", 0, 100, kind="audio")
        timeline.audio_tracks[0].muted = True
        lists = audio_playlists(timeline, FakePool(), FakeProxies())
        assert len(lists) == 1
        assert lists[0].segments[0].media_id == "b"
