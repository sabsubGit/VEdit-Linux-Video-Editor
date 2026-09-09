"""Cross dissolves.

The model deliberately has no overlap — `Track.insert` rejects it and every
consumer assumes it — so a dissolve is stored as a property of the incoming
clip's *head* and paid for out of the outgoing clip's unused source. Nothing
moves, no two clips ever occupy the same frame, and the timeline's arithmetic is
untouched. These tests are mostly about that: what limits the length, what
happens when the limit moves, and that the picture really does mix.
"""

from __future__ import annotations

import pytest

from vedit.core.timebase import TimeBase
from vedit.timeline import ops
from vedit.timeline.model import Clip, Timeline, TimelineError


def put(track, media_id, start, length, *, src_in=0, src_length=None, **kwargs):
    clip = Clip(
        media_id=media_id,
        src_in=src_in,
        src_out=src_in + length,
        tl_start=start,
        src_length=src_length if src_length is not None else src_in + length + 60,
        name=media_id,
        **kwargs,
    )
    track.insert(clip)
    return clip


@pytest.fixture
def timeline() -> Timeline:
    return Timeline.default(TimeBase(30))


@pytest.fixture
def pair(timeline):
    """Two abutting shots, each with plenty of unused source either side."""
    track = timeline.lane_for("video")
    a = put(track, "a", 0, 60, src_in=30, src_length=200)
    b = put(track, "b", 60, 60, src_in=30, src_length=200)
    return timeline, track, a, b


class TestLength:
    def test_a_dissolve_takes_the_length_it_is_given(self, pair):
        timeline, track, a, b = pair
        assert ops.set_dissolve(timeline, b, 15) == 15
        assert track.dissolve_before(b) == (a, 15)

    def test_nothing_moves_and_the_edit_stays_the_same_length(self, pair):
        """The whole reason it is stored as a head property rather than an
        overlap."""
        timeline, track, a, b = pair
        before = (a.tl_start, a.tl_end, b.tl_start, b.tl_end, timeline.duration)
        ops.set_dissolve(timeline, b, 20)
        assert (a.tl_start, a.tl_end, b.tl_start, b.tl_end, timeline.duration) == before

    def test_it_cannot_outrun_the_outgoing_clip_s_spare_footage(self, timeline):
        """The outgoing shot has to keep showing something after its own last
        frame, and it can only do that while there is source left."""
        track = timeline.lane_for("video")
        put(track, "a", 0, 60, src_length=65)      # five frames of tail
        b = put(track, "b", 60, 60)
        assert ops.set_dissolve(timeline, b, 30) == 5

    def test_it_cannot_be_longer_than_either_shot(self, timeline):
        track = timeline.lane_for("video")
        put(track, "a", 0, 10, src_length=200)
        b = put(track, "b", 10, 60)
        assert ops.set_dissolve(timeline, b, 40) == 10

    def test_a_reversed_clip_spends_the_other_end_of_its_source(self, timeline):
        """Playing on past the out-point of a reversed clip means counting
        further *down*, so it is the head that has to have room."""
        track = timeline.lane_for("video")
        a = put(track, "a", 0, 60, src_in=40, src_length=100, reversed=True)
        b = put(track, "b", 60, 60)
        assert a.spare_room == 40
        assert ops.set_dissolve(timeline, b, 20) == 20

    def test_a_retimed_clip_spends_its_source_faster(self, timeline):
        track = timeline.lane_for("video")
        put(track, "a", 0, 60, src_length=80, speed=2.0)   # 20 spare source
        b = put(track, "b", 30, 60)
        # At 2x, ten timeline frames eat twenty source frames.
        assert ops.set_dissolve(timeline, b, 30) == 10


class TestRefusals:
    def test_the_first_clip_has_nothing_to_dissolve_from(self, timeline):
        track = timeline.lane_for("video")
        a = put(track, "a", 0, 60)
        with pytest.raises(TimelineError, match="nothing before"):
            ops.set_dissolve(timeline, a, 15)

    def test_a_gap_before_the_clip_refuses(self, timeline):
        track = timeline.lane_for("video")
        put(track, "a", 0, 60)
        b = put(track, "b", 90, 60)
        with pytest.raises(TimelineError, match="gap"):
            ops.set_dissolve(timeline, b, 15)

    def test_an_outgoing_clip_with_no_spare_footage_refuses(self, timeline):
        track = timeline.lane_for("video")
        put(track, "a", 0, 60, src_length=60)
        b = put(track, "b", 60, 60)
        with pytest.raises(TimelineError, match="no unused footage"):
            ops.set_dissolve(timeline, b, 15)

    def test_a_refusal_leaves_no_dissolve_behind(self, timeline):
        track = timeline.lane_for("video")
        put(track, "a", 0, 60, src_length=60)
        b = put(track, "b", 60, 60)
        with pytest.raises(TimelineError):
            ops.set_dissolve(timeline, b, 15)
        assert b.dissolve_in == 0

    def test_audio_clips_are_not_offered_one(self, timeline):
        track = timeline.lane_for("audio")
        put(track, "a", 0, 60, kind="audio")
        b = put(track, "b", 60, 60, kind="audio")
        with pytest.raises(TimelineError, match="picture"):
            ops.set_dissolve(timeline, b, 15)

    def test_a_locked_track_refuses(self, pair):
        timeline, track, a, b = pair
        track.locked = True
        with pytest.raises(TimelineError, match="locked"):
            ops.set_dissolve(timeline, b, 15)


class TestLaterEditsCannotBreakIt:
    """The length is worked out on demand rather than stored clamped, so an
    edit can shrink what is available without leaving a broken dissolve."""

    def test_losing_the_spare_footage_shortens_it(self, pair):
        """`src_length` shrinks rather than `src_out`, because moving the
        out-point would move the clip's end and break the abutment the dissolve
        needs — the point here is that the *handle* got smaller."""
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 25)
        a.src_length = a.src_out + 5      # only five frames of tail now
        assert track.dissolve_before(b)[1] == 5

    def test_using_up_the_outgoing_clip_removes_it_entirely(self, pair):
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 25)
        a.src_length = a.src_out
        assert track.dissolve_before(b) is None

    def test_a_gap_opening_before_it_removes_it(self, pair):
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 15)
        ops.move_clips(timeline, [b], 30)
        assert track.dissolve_before(b) is None

    def test_the_request_is_remembered_so_it_comes_back(self, pair):
        """Shortening and then undoing a trim should restore the dissolve the
        user asked for, not the reduced one they were temporarily given."""
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 25)
        original, a.src_length = a.src_length, a.src_out + 5
        assert track.dissolve_before(b)[1] == 5
        a.src_length = original
        assert track.dissolve_before(b)[1] == 25


class TestSound:
    def test_it_cross_fades_the_linked_audio(self, timeline):
        """The picture mixes; the sound has to go with it, or a dissolve is a
        visual effect with a hard cut underneath."""
        video, audio = timeline.lane_for("video"), timeline.lane_for("audio")
        put(video, "a", 0, 60, link_id="la")
        put(audio, "a", 0, 60, kind="audio", link_id="la")
        b = put(video, "b", 60, 60, link_id="lb")
        b_audio = put(audio, "b", 60, 60, kind="audio", link_id="lb")

        ops.set_dissolve(timeline, b, 15)
        assert b_audio.fade_in == 15
        assert audio.clips[0].fade_out == 15

    def test_removing_the_dissolve_removes_the_cross_fade(self, timeline):
        video, audio = timeline.lane_for("video"), timeline.lane_for("audio")
        put(video, "a", 0, 60, link_id="la")
        put(audio, "a", 0, 60, kind="audio", link_id="la")
        b = put(video, "b", 60, 60, link_id="lb")
        b_audio = put(audio, "b", 60, 60, kind="audio", link_id="lb")

        ops.set_dissolve(timeline, b, 15)
        ops.set_dissolve(timeline, b, 0)
        assert b_audio.fade_in == 0 and audio.clips[0].fade_out == 0

    def test_a_clip_with_no_sound_is_no_obstacle(self, pair):
        timeline, track, a, b = pair
        assert ops.set_dissolve(timeline, b, 15) == 15


class TestPreviewPlaylists:
    """The preview needs two streams across a transition, because a decoder
    reads one file at a time. These pin the second one's shape."""

    def build(self, timeline):
        from vedit.player.segments import build_dissolve_playlist, build_video_playlist

        class FakeInfo:
            def __init__(self, media_id):
                self.media_id = media_id
                self.path = f"/tmp/{media_id}.mp4"

        class FakePool:
            def info_for(self, media_id):
                return FakeInfo(media_id)

        class FakeProxies:
            def playback_path(self, info):
                return info.path

        return (
            build_video_playlist(timeline, FakePool(), FakeProxies()),
            build_dissolve_playlist(timeline, FakePool(), FakeProxies()),
        )

    def test_without_a_dissolve_the_second_stream_is_all_gap(self, pair):
        """A timeline with no transitions must cost nothing to decode."""
        timeline, track, a, b = pair
        _, under = self.build(timeline)
        assert all(segment.is_gap for segment in under.segments)

    def test_the_second_stream_covers_exactly_the_transition(self, pair):
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 20)
        _, under = self.build(timeline)
        live = [s for s in under.segments if not s.is_gap]
        assert len(live) == 1
        assert (live[0].tl_start, live[0].tl_end) == (60, 80)

    def test_the_second_stream_reads_the_outgoing_clip_s_spare_source(self, pair):
        """Past its own out-point — that is what a dissolve spends."""
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 20)
        _, under = self.build(timeline)
        live = next(s for s in under.segments if not s.is_gap)
        assert live.media_id == "a"
        assert live.src_start == a.src_out

    def test_the_main_stream_marks_the_transition(self, pair):
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 20)
        video, _ = self.build(timeline)
        during = video.at(65)
        assert during.dissolve == 20 and during.dissolve_from == 60
        assert video.at(90).dissolve == 0, "over by then"

    def test_the_transition_is_not_merged_into_the_rest_of_its_clip(self, pair):
        """It looks different from the shot it belongs to, so it has to stay a
        segment of its own or the viewer would blend the whole clip."""
        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 20)
        video, _ = self.build(timeline)
        spans = [(s.tl_start, s.tl_end) for s in video.segments if s.media_id == "b"]
        assert spans == [(60, 80), (80, 120)]

    def test_render_and_preview_split_at_the_same_places(self, pair):
        """The standing contract between the two flatteners."""
        from vedit.render.graph import _video_pieces

        timeline, track, a, b = pair
        ops.set_dissolve(timeline, b, 20)

        class FakeInfo:
            def __init__(self, media_id):
                self.media_id, self.path = media_id, f"/tmp/{media_id}.mp4"

        class FakePool:
            def info_for(self, media_id):
                return FakeInfo(media_id)

        video, _ = self.build(timeline)
        pieces = _video_pieces(timeline, {"a": 0, "b": 1}, FakePool())
        assert len(pieces) == len(video.segments)
        assert (pieces[1].under is not None) == bool(video.segments[1].dissolve)


class TestTheMixRamp:
    """How far through the transition a given frame is.

    Must match `xfade`, which is fully the outgoing shot at offset zero and
    fully the incoming one a transition-length later. A frame of bias here would
    put the preview and the export out of step the whole way through.
    """

    @staticmethod
    def mix(frame, start, length):
        return max(0.0, min(1.0, (frame - start) / length))

    def test_it_begins_on_the_outgoing_shot(self):
        assert self.mix(60, 60, 20) == 0.0

    def test_it_ends_on_the_incoming_one(self):
        assert self.mix(80, 60, 20) == 1.0

    def test_the_middle_is_halfway(self):
        assert self.mix(70, 60, 20) == 0.5
