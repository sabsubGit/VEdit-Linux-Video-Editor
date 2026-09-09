"""Multi-lane flattening.

Preview and render must agree on what is visible, so both are driven from the
same rule: the topmost video lane with a clip at a given frame wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vedit.core.timebase import TimeBase
from vedit.player.segments import audio_playlists, build_playlist, build_video_playlist
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


class TestSegmentLevels:
    """Segments carry the clip's level shape, so the audio thread never has to
    look a clip up while it is decoding."""

    def build(self, timeline, lane=0):
        return build_playlist(
            timeline, timeline.audio_tracks[lane], FakePool(), FakeProxies()
        )

    def test_playlist_knows_its_lane(self, timeline):
        put(timeline, 1, "a", 0, 100, kind="audio")
        playlist = self.build(timeline, lane=1)
        assert playlist.track_id == timeline.audio_tracks[1].track_id

    def test_gain_is_carried_as_a_linear_multiplier(self, timeline):
        clip = put(timeline, 0, "a", 0, 100, kind="audio")
        clip.gain_db = -6.0
        segment = self.build(timeline).at(0)
        assert segment.gain == pytest.approx(0.501187, abs=1e-5)
        assert segment.clip_id == clip.clip_id

    def test_fades_are_carried_in_frames(self, timeline):
        clip = put(timeline, 0, "a", 0, 100, kind="audio")
        clip.fade_in, clip.fade_out = 12, 8
        segment = self.build(timeline).at(0)
        assert (segment.fade_in, segment.fade_out) == (12, 8)

    def test_gaps_stay_at_unity(self, timeline):
        clip = put(timeline, 0, "a", 50, 100, kind="audio")
        clip.gain_db = -12.0
        gap = self.build(timeline).at(0)
        assert gap.is_gap
        assert gap.gain == 1.0 and gap.fade_in == 0

    def test_video_segments_never_carry_levels(self, timeline):
        clip = put(timeline, 0, "v", 0, 100)
        clip.gain_db = 6.0
        clip.fade_in = 10
        segment = build_video_playlist(timeline, FakePool(), FakeProxies()).at(0)
        assert segment.gain == 1.0 and segment.fade_in == 0

    def test_two_clips_of_one_source_are_not_merged(self, timeline):
        """They may carry different gain, so `clip_id` keeps them apart even
        though the file, speed and source offsets line up."""
        first = put(timeline, 0, "a", 0, 100)
        put(timeline, 0, "a", 100, 100, src_in=100)
        first.gain_db = -6.0
        playlist = build_video_playlist(timeline, FakePool(), FakeProxies())
        assert len(playlist.segments) == 2


class TestSoloInPreview:
    def test_solo_silences_the_other_lanes(self, timeline):
        put(timeline, 0, "a", 0, 100, kind="audio")
        put(timeline, 1, "b", 0, 100, kind="audio")
        timeline.audio_tracks[1].solo = True
        lists = audio_playlists(timeline, FakePool(), FakeProxies())
        assert len(lists) == 1
        assert lists[0].segments[0].media_id == "b"

    def test_a_muted_and_soloed_lane_stays_silent(self, timeline):
        put(timeline, 0, "a", 0, 100, kind="audio")
        put(timeline, 1, "b", 0, 100, kind="audio")
        timeline.audio_tracks[0].muted = True
        timeline.audio_tracks[0].solo = True
        lists = audio_playlists(timeline, FakePool(), FakeProxies())
        assert [p.segments[0].media_id for p in lists] == ["b"]


class TestMutedClipsInPreview:
    """A muted clip becomes a gap of exactly its own length, so the lane's
    timing is identical whether or not anything is muted."""

    def build(self, timeline, lane=0):
        return build_playlist(
            timeline, timeline.audio_tracks[lane], FakePool(), FakeProxies()
        )

    def test_a_muted_clip_reads_nothing_from_disk(self, timeline):
        clip = put(timeline, 0, "a", 0, 100, kind="audio")
        clip.muted = True
        segment = self.build(timeline).at(0)
        assert segment.is_gap, "nothing is decoded for a clip nobody can hear"

    def test_it_still_occupies_its_own_span(self, timeline):
        put(timeline, 0, "a", 0, 100, kind="audio")
        muted = put(timeline, 0, "a", 100, 100, kind="audio", src_in=100)
        muted.muted = True
        playlist = self.build(timeline)

        assert [(s.tl_start, s.tl_end) for s in playlist.segments] == [(0, 100), (100, 200)]
        assert playlist.at(0).is_gap is False
        assert playlist.at(100).is_gap is True

    def test_muting_the_audio_leaves_the_picture_playing(self, timeline):
        video = put(timeline, 0, "a", 0, 100)
        audio = put(timeline, 0, "a", 0, 100, kind="audio")
        video.link_id = audio.link_id = "l1"
        audio.muted = True

        assert build_video_playlist(timeline, FakePool(), FakeProxies()).at(0).is_gap is False
        assert self.build(timeline).at(0).is_gap is True

    def test_a_muted_video_clip_is_not_affected(self, timeline):
        """`muted` is an audio property; nothing sets it on picture, and the
        video flatten does not consult it."""
        clip = put(timeline, 0, "a", 0, 100)
        clip.muted = True
        assert build_video_playlist(timeline, FakePool(), FakeProxies()).at(0).is_gap is False


class TestReversedSegments:
    def test_the_segment_starts_at_the_out_point(self, timeline):
        clip = put(timeline, 0, "a", 0, 100, kind="audio", src_in=20)
        clip.reversed = True
        segment = build_playlist(
            timeline, timeline.audio_tracks[0], FakePool(), FakeProxies()
        ).at(0)

        assert segment.reversed is True
        assert segment.src_start == 120

    def test_source_time_walks_backwards_through_the_clip(self, timeline):
        clip = put(timeline, 0, "a", 0, 100, kind="audio", src_in=20)
        clip.reversed = True
        segment = build_playlist(
            timeline, timeline.audio_tracks[0], FakePool(), FakeProxies()
        ).at(0)

        timebase = timeline.timebase
        assert segment.source_seconds(0, timebase) == pytest.approx(120 / 30)
        assert segment.source_seconds(50, timebase) == pytest.approx(70 / 30)
        assert segment.source_seconds(100, timebase) == pytest.approx(20 / 30)

    def test_a_decoded_frame_maps_back_to_its_timeline_position(self, timeline):
        clip = put(timeline, 0, "a", 0, 100, kind="audio", src_in=20)
        clip.reversed = True
        segment = build_playlist(
            timeline, timeline.audio_tracks[0], FakePool(), FakeProxies()
        ).at(0)

        timebase = timeline.timebase
        for frame in (0, 25, 60, 99):
            seconds = segment.source_seconds(frame, timebase)
            assert segment.timeline_frame_for(seconds, timebase) == frame

    def test_neighbouring_pieces_of_one_reversed_clip_still_merge(self, timeline):
        """A clip on a lower lane splits the flatten at its edges without ever
        winning them. The pieces of the reversed clip above are one continuous
        backwards read and must rejoin, or the decoder re-seeks for nothing.
        """
        clip = put(timeline, 1, "a", 0, 200)
        clip.reversed = True
        put(timeline, 0, "b", 100, 50)

        playlist = build_video_playlist(timeline, FakePool(), FakeProxies())
        assert visible(playlist) == [(0, 200, "a")]

    def test_a_forward_and_a_reversed_clip_never_merge(self, timeline):
        first = put(timeline, 0, "a", 0, 100)
        second = put(timeline, 0, "a", 100, 100, src_in=100)
        second.reversed = True

        playlist = build_video_playlist(timeline, FakePool(), FakeProxies())
        assert len(playlist.segments) == 2

