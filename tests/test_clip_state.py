"""Per-clip mute and per-clip reverse.

Two flags that look small in the model and reach a long way out of it: mute has
to become silence in the preview *and* in the export, and reverse has to survive
every operation that manipulates a source window — razor, trim, flatten — or a
reversed clip quietly plays the wrong material.
"""

from __future__ import annotations

import pytest

from vedit.core.timebase import TimeBase
from vedit.timeline import ops
from vedit.timeline.model import Clip, Timeline, TimelineError


def clip(start=0, length=100, *, src_in=0, src_length=None, kind="video", speed=1.0,
         link=None, backwards=False, muted=False):
    src_length = src_length if src_length is not None else src_in + length
    return Clip(
        media_id="m1", src_in=src_in, src_out=src_in + length, tl_start=start,
        src_length=src_length, kind=kind, speed=speed, link_id=link, name="clip",
        reversed=backwards, muted=muted,
    )


@pytest.fixture
def timeline():
    return Timeline.default(TimeBase(30))


def linked_pair(timeline, *, start=0, length=100):
    video = clip(start=start, length=length, kind="video", link="l1")
    audio = clip(start=start, length=length, kind="audio", link="l1")
    timeline.video_tracks[0].insert(video)
    timeline.audio_tracks[0].insert(audio)
    return video, audio


class TestClipMute:
    def test_clips_start_audible(self):
        assert clip(kind="audio").muted is False
        assert clip(kind="audio").audible is True

    def test_muting_makes_a_clip_inaudible_without_disabling_it(self, timeline):
        audio = clip(kind="audio")
        timeline.audio_tracks[0].insert(audio)
        ops.set_clip_muted(timeline, [audio], True)

        assert audio.muted is True
        assert audio.audible is False
        assert audio.enabled is True, "mute silences a clip, it does not remove it"

    def test_mute_leaves_the_clip_geometry_alone(self, timeline):
        audio = clip(start=50, length=100, kind="audio")
        timeline.audio_tracks[0].insert(audio)
        ops.set_clip_muted(timeline, [audio], True)

        assert (audio.tl_start, audio.duration) == (50, 100)

    def test_muting_picture_mutes_the_sound_linked_to_it(self, timeline):
        video, audio = linked_pair(timeline)
        ops.set_clip_muted(timeline, [video], True)

        assert audio.muted is True
        assert video.muted is False, "a video clip has no sound of its own to mute"

    def test_muting_one_cut_leaves_its_neighbours_alone(self, timeline):
        first = clip(start=0, length=50, kind="audio")
        second = clip(start=50, length=50, kind="audio", src_in=50, src_length=100)
        timeline.audio_tracks[0].insert(first)
        timeline.audio_tracks[0].insert(second)

        ops.set_clip_muted(timeline, [second], True)
        assert (first.muted, second.muted) == (False, True)

    def test_toggle_flips_the_whole_selection_uniformly(self, timeline):
        first = clip(start=0, length=50, kind="audio")
        second = clip(start=50, length=50, kind="audio", src_in=50, src_length=100, muted=True)
        timeline.audio_tracks[0].insert(first)
        timeline.audio_tracks[0].insert(second)

        ops.toggle_clip_mute(timeline, [first, second])
        assert (first.muted, second.muted) == (True, True), "led by the first clip's state"

    def test_video_only_selection_mutes_nothing(self, timeline):
        video = clip()
        timeline.video_tracks[0].insert(video)
        assert ops.set_clip_muted(timeline, [video], True) == []

    def test_a_locked_track_refuses(self, timeline):
        audio = clip(kind="audio")
        timeline.audio_tracks[0].insert(audio)
        timeline.audio_tracks[0].locked = True

        with pytest.raises(TimelineError):
            ops.set_clip_muted(timeline, [audio], True)

    def test_a_cut_carries_mute_to_both_halves(self, timeline):
        audio = clip(length=100, kind="audio", muted=True)
        timeline.audio_tracks[0].insert(audio)
        right = ops.razor(timeline, 40)[0]

        assert (audio.muted, right.muted) == (True, True)


class TestReverseGeometry:
    def test_reversing_leaves_the_clip_where_it_is(self, timeline):
        c = clip(start=100, length=100)
        timeline.video_tracks[0].insert(c)
        ops.set_reversed(timeline, [c], True)

        assert (c.tl_start, c.tl_end, c.source_span) == (100, 200, 100)

    def test_the_first_frame_played_is_the_end_of_the_window(self):
        c = clip(start=0, length=100, src_in=20, src_length=200, backwards=True)
        assert c.source_frame_at(0) == 120, "playing backwards starts at the out point"
        assert c.source_frame_at(50) == 70
        assert c.source_frame_at(100) == 20, "and finishes at the in point"

    def test_speed_still_scales_how_fast_it_walks_back(self):
        c = clip(start=0, length=100, backwards=True, speed=2.0)
        assert c.source_frame_at(0) == 100
        assert c.source_frame_at(10) == 80

    def test_the_source_window_is_reported_low_edge_first(self):
        c = clip(start=0, length=100, src_in=20, src_length=200, backwards=True)
        assert c.source_window_for(0, 100) == (20, 120)

    def test_a_forward_clip_reports_the_same_window_it_always_did(self):
        c = clip(start=0, length=100, src_in=20, src_length=200)
        assert c.source_window_for(0, 100) == (20, 120)

    def test_reverse_applies_to_the_whole_link_group(self, timeline):
        video, audio = linked_pair(timeline)
        ops.set_reversed(timeline, [video], True)

        assert (video.reversed, audio.reversed) == (True, True), "picture and sound turn together"

    def test_toggle_turns_a_reversed_clip_back_round(self, timeline):
        c = clip(backwards=True)
        timeline.video_tracks[0].insert(c)
        ops.toggle_reversed(timeline, [c])
        assert c.reversed is False

    def test_a_locked_track_refuses(self, timeline):
        c = clip()
        timeline.video_tracks[0].insert(c)
        timeline.video_tracks[0].locked = True

        with pytest.raises(TimelineError):
            ops.set_reversed(timeline, [c], True)


class TestReverseWithOtherEdits:
    def test_a_cut_splits_the_window_from_the_far_end(self, timeline):
        """The piece that plays second is the *lower* part of the source.

        Cutting on the raw offset would leave the two halves playing each
        other's material, which is the bug this exists to prevent.
        """
        c = clip(start=0, length=100, backwards=True)
        timeline.video_tracks[0].insert(c)
        right = ops.razor(timeline, 40)[0]

        assert (c.src_in, c.src_out) == (60, 100), "the first 40 frames play 100 down to 60"
        assert (right.src_in, right.src_out) == (0, 60)
        assert right.reversed is True

    def test_the_cut_is_seamless_across_the_join(self, timeline):
        c = clip(start=0, length=100, backwards=True)
        timeline.video_tracks[0].insert(c)
        right = ops.razor(timeline, 40)[0]

        assert c.source_frame_at(39) == right.source_frame_at(40) + 1, (
            "the frame before the cut and the frame after it are adjacent in the source"
        )

    def test_trimming_the_in_edge_moves_the_out_point(self, timeline):
        c = clip(start=0, length=100, src_in=0, src_length=200, backwards=True)
        timeline.video_tracks[0].insert(c)
        ops.trim(timeline, c, "in", 20)

        assert c.tl_start == 20
        assert (c.src_in, c.src_out) == (0, 80), "20 fewer frames come off the top of the window"

    def test_trimming_the_out_edge_extends_into_earlier_source(self, timeline):
        c = clip(start=0, length=100, src_in=50, src_length=200, backwards=True)
        timeline.video_tracks[0].insert(c)
        ops.trim(timeline, c, "out", 130)

        assert c.tl_end == 130
        assert (c.src_in, c.src_out) == (20, 150)

    def test_a_trim_stops_at_the_start_of_the_source(self, timeline):
        c = clip(start=0, length=100, src_in=10, src_length=110, backwards=True)
        timeline.video_tracks[0].insert(c)
        landed = ops.trim(timeline, c, "out", 200)

        assert c.src_in == 0
        assert landed == 110, "only the ten frames of unused head material are available"

    def test_reverse_and_speed_compose(self, timeline):
        c = clip(start=0, length=100, backwards=True)
        timeline.video_tracks[0].insert(c)
        ops.set_speed(timeline, [c], 2.0)

        assert c.duration == 50
        assert c.reversed is True
        assert c.source_frame_at(0) == 100
        assert c.source_frame_at(50) == 0


class TestProjectFileRoundTrip:
    def test_both_flags_survive_a_save(self, tmp_path):
        from vedit.core.projectfile import project_to_dict

        timeline = Timeline.default(TimeBase(30))
        audio = clip(kind="audio", muted=True, backwards=True)
        timeline.audio_tracks[0].insert(audio)

        written = project_to_dict(timeline, [])["tracks"]
        stored = next(t for t in written if t["clips"])["clips"][0]
        assert (stored["muted"], stored["reversed"]) == (True, True)
