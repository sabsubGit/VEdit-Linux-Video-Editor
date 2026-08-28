"""Clip speed, multi-lane flattening and track management."""

from __future__ import annotations

import pytest

from vedit.core.timebase import TimeBase
from vedit.timeline import ops
from vedit.timeline.model import Clip, Timeline, TimelineError


def clip(start=0, length=100, *, src_in=0, src_length=None, kind="video", speed=1.0, link=None):
    src_length = src_length if src_length is not None else src_in + length
    return Clip(
        media_id="m1", src_in=src_in, src_out=src_in + length, tl_start=start,
        src_length=src_length, kind=kind, speed=speed, link_id=link, name="clip",
    )


@pytest.fixture
def timeline():
    return Timeline.default(TimeBase(30))


class TestClipSpeed:
    def test_default_speed_leaves_duration_as_source_span(self):
        c = clip(length=100)
        assert c.speed == 1.0
        assert c.duration == c.source_span == 100

    @pytest.mark.parametrize("speed,expected", [(0.5, 200), (1.0, 100), (2.0, 50), (4.0, 25)])
    def test_duration_scales_inversely_with_speed(self, speed, expected):
        assert clip(length=100, speed=speed).duration == expected

    def test_source_span_is_untouched_by_speed(self):
        c = clip(length=100, speed=2.0)
        assert c.source_span == 100, "speed changes how long it plays, not what it plays"

    def test_source_frame_mapping_follows_speed(self):
        c = clip(start=0, length=100, speed=2.0)
        assert c.source_frame_at(0) == 0
        assert c.source_frame_at(10) == 20, "at 2x, 10 timeline frames eat 20 source frames"

    def test_tl_end_uses_scaled_duration(self):
        assert clip(start=100, length=100, speed=2.0).tl_end == 150

    @pytest.mark.parametrize("bad", [0, -1, 0.01, 50.0])
    def test_absurd_speeds_rejected(self, bad):
        with pytest.raises(TimelineError):
            clip(speed=bad)

    def test_a_minimum_of_one_frame_survives(self):
        assert clip(length=3, speed=10.0).duration == 1


class TestSetSpeed:
    def test_retime_keeps_the_head_and_moves_the_tail(self, timeline):
        c = clip(start=100, length=100)
        timeline.video_tracks[0].insert(c)
        ops.set_speed(timeline, [c], 2.0)
        assert c.tl_start == 100, "the in-point stays put"
        assert c.duration == 50
        assert c.tl_end == 150

    def test_retime_applies_to_the_link_group(self, timeline):
        for kind in ("video", "audio"):
            timeline.lane_for(kind).insert(clip(0, 100, kind=kind, link="L"))
        ops.set_speed(timeline, [timeline.video_tracks[0].clips[0]], 2.0)
        assert timeline.audio_tracks[0].clips[0].speed == 2.0, "sound retimed with picture"
        assert timeline.audio_tracks[0].clips[0].duration == 50

    def test_slowing_down_is_capped_by_the_next_clip(self, timeline):
        first = clip(0, 100, src_length=400)
        second = clip(100, 100)
        timeline.video_tracks[0].insert(first)
        timeline.video_tracks[0].insert(second)
        ops.set_speed(timeline, [first], 0.5)
        assert first.tl_end <= second.tl_start, "must not overlap its neighbour"
        timeline.validate()

    def test_speeding_up_leaves_a_gap_rather_than_rippling(self, timeline):
        first = clip(0, 100)
        second = clip(100, 100)
        timeline.video_tracks[0].insert(first)
        timeline.video_tracks[0].insert(second)
        ops.set_speed(timeline, [first], 2.0)
        assert second.tl_start == 100, "the rest of the edit is left alone"
        assert timeline.video_tracks[0].gaps() == [(50, 100)]

    def test_razor_on_a_retimed_clip_cuts_at_the_right_source_frame(self, timeline):
        c = clip(0, 100, speed=2.0)     # 50 frames long on the timeline
        timeline.video_tracks[0].insert(c)
        ops.razor(timeline, 25)
        left, right = timeline.video_tracks[0].clips
        assert left.source_span == 50 and right.source_span == 50
        assert left.src_out == right.src_in, "no source frames lost at the cut"
        assert right.speed == 2.0, "the new piece keeps the retime"

    def test_trim_on_a_retimed_clip_converts_by_speed(self, timeline):
        c = clip(0, 100, src_length=400, speed=2.0)
        timeline.video_tracks[0].insert(c)
        ops.trim(timeline, c, "out", 60)     # +10 timeline frames
        assert c.duration == 60
        assert c.source_span == 120, "10 timeline frames at 2x consumed 20 source frames"

    def test_retime_survives_undo(self, timeline):
        from vedit.core.commands import UndoStack

        c = clip(0, 100)
        timeline.video_tracks[0].insert(c)
        stack = UndoStack(timeline)
        stack.apply("speed", lambda t: ops.set_speed(t, [c], 4.0))
        assert timeline.video_tracks[0].clips[0].duration == 25
        stack.undo()
        assert timeline.video_tracks[0].clips[0].duration == 100
        assert timeline.video_tracks[0].clips[0].speed == 1.0


class TestTrackManagement:
    def test_add_track_names_and_groups(self, timeline):
        track = timeline.add_track("video")
        assert track.name == "V4"
        assert timeline.tracks.index(track) < timeline.tracks.index(timeline.audio_tracks[0])

    def test_remove_and_renumber(self, timeline):
        ops.remove_track(timeline, timeline.video_tracks[1])
        assert [t.name for t in timeline.video_tracks] == ["V1", "V2"]

    def test_last_lane_of_a_kind_is_protected(self):
        timeline = Timeline.default(TimeBase(30), video_tracks=1, audio_tracks=1)
        with pytest.raises(TimelineError, match="last video track"):
            ops.remove_track(timeline, timeline.video_tracks[0])

    def test_clear_track_keeps_the_lane(self, timeline):
        timeline.video_tracks[0].insert(clip(0, 50))
        ops.clear_track(timeline, timeline.video_tracks[0])
        assert timeline.video_tracks[0].clips == []
        assert timeline.video_tracks[0] in timeline.tracks

    def test_clear_refuses_a_locked_lane(self, timeline):
        timeline.video_tracks[0].locked = True
        with pytest.raises(TimelineError, match="locked"):
            ops.clear_track(timeline, timeline.video_tracks[0])

    def test_video_priority_is_lane_order(self, timeline):
        assert timeline.video_priority(timeline.video_tracks[0]) == 0
        assert timeline.video_priority(timeline.video_tracks[2]) == 2
