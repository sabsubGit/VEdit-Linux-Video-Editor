"""Timeline model and edit-operation tests.

No Qt and no media files: the model is deliberately pure so this runs fast and
tells you unambiguously whether a bug is in the data layer or the UI.
"""

from __future__ import annotations

import pytest

from vedit.core.commands import UndoStack
from vedit.core.timebase import TimeBase
from vedit.timeline import ops
from vedit.timeline.model import Clip, Timeline, TimelineError, Track


def make_clip(start=0, length=100, *, src_in=0, src_length=None, kind="video", link=None, name="clip"):
    src_length = src_length if src_length is not None else src_in + length
    return Clip(
        media_id="m1",
        src_in=src_in,
        src_out=src_in + length,
        tl_start=start,
        src_length=src_length,
        kind=kind,
        link_id=link,
        name=name,
    )


@pytest.fixture
def timeline():
    return Timeline.default(TimeBase(30))


@pytest.fixture
def linked(timeline):
    """A 100-frame linked A/V pair at frame 0, then a second pair at 100."""
    for index, start in enumerate((0, 100)):
        link_id = f"L{index}"
        for kind in ("video", "audio"):
            clip = make_clip(start, 100, src_length=200, kind=kind, link=link_id, name=f"pair{index}")
            timeline.tracks[0 if kind == "video" else 1].insert(clip)
    return timeline


# -- Clip ---------------------------------------------------------------------


class TestClip:
    def test_geometry(self):
        clip = make_clip(start=50, length=25)
        assert clip.duration == 25
        assert clip.tl_end == 75
        assert clip.covers(50) and clip.covers(74)
        assert not clip.covers(75), "tl_end is exclusive"

    def test_crosses_excludes_edges(self):
        clip = make_clip(start=10, length=10)
        assert clip.crosses(15)
        assert not clip.crosses(10), "cutting at the head is a no-op"
        assert not clip.crosses(20), "cutting at the tail is a no-op"

    def test_source_frame_mapping(self):
        clip = make_clip(start=100, length=50, src_in=30, src_length=200)
        assert clip.source_frame_at(100) == 30
        assert clip.source_frame_at(110) == 40

    def test_head_and_tail_room(self):
        clip = make_clip(start=0, length=50, src_in=20, src_length=200)
        assert clip.head_room == 20
        assert clip.tail_room == 130

    @pytest.mark.parametrize(
        "kwargs,message",
        [
            (dict(src_in=-1, src_out=10, tl_start=0, src_length=10), "negative"),
            (dict(src_in=5, src_out=5, tl_start=0, src_length=10), "one frame"),
            (dict(src_in=0, src_out=20, tl_start=0, src_length=10), "exceeds"),
            (dict(src_in=0, src_out=10, tl_start=-5, src_length=10), "before zero"),
        ],
    )
    def test_invalid_clips_rejected(self, kwargs, message):
        with pytest.raises(TimelineError, match=message):
            Clip(media_id="m", **kwargs)


# -- Track --------------------------------------------------------------------


class TestTrack:
    def test_insert_keeps_sorted(self):
        track = Track(kind="video", name="V1")
        track.insert(make_clip(200, 50))
        track.insert(make_clip(0, 50))
        track.insert(make_clip(100, 50))
        assert [c.tl_start for c in track.clips] == [0, 100, 200]

    def test_insert_rejects_overlap(self):
        track = Track(kind="video", name="V1")
        track.insert(make_clip(0, 100))
        with pytest.raises(TimelineError, match="overlap"):
            track.insert(make_clip(50, 100))

    def test_abutting_clips_do_not_overlap(self):
        track = Track(kind="video", name="V1")
        track.insert(make_clip(0, 100))
        track.insert(make_clip(100, 100))  # starts exactly where the last ended
        assert len(track) == 2

    def test_gaps(self):
        track = Track(kind="video", name="V1")
        track.insert(make_clip(50, 50))
        track.insert(make_clip(200, 50))
        assert track.gaps() == [(0, 50), (100, 200)]
        assert track.gaps(until=400) == [(0, 50), (100, 200), (250, 400)]

    def test_no_gaps_when_contiguous_from_zero(self):
        track = Track(kind="video", name="V1")
        track.insert(make_clip(0, 50))
        track.insert(make_clip(50, 50))
        assert track.gaps() == []

    def test_neighbours(self):
        track = Track(kind="video", name="V1")
        first, second, third = make_clip(0, 10), make_clip(10, 10), make_clip(20, 10)
        for clip in (first, second, third):
            track.insert(clip)
        assert track.neighbours(second) == (first, third)
        assert track.neighbours(first) == (None, second)
        assert track.neighbours(third) == (second, None)

    def test_validate_catches_wrong_kind(self):
        track = Track(kind="video", name="V1")
        track.clips.append(make_clip(kind="audio"))
        with pytest.raises(TimelineError, match="audio clip"):
            track.validate()


# -- Timeline -----------------------------------------------------------------


class TestTimeline:
    def test_default_has_one_video_and_one_audio_lane(self, timeline):
        assert [t.name for t in timeline.tracks] == ["V1", "A1"]
        assert len(timeline.video_tracks) == 1
        assert len(timeline.audio_tracks) == 1

    def test_duration_is_the_longest_track(self, timeline):
        timeline.tracks[0].insert(make_clip(0, 100))
        timeline.tracks[1].insert(make_clip(0, 250, kind="audio"))
        assert timeline.duration == 250

    def test_linked_group_spans_tracks(self, linked):
        video = linked.tracks[0].clips[0]
        group = linked.linked_group(video)
        assert len(group) == 2
        assert {c.kind for c in group} == {"video", "audio"}

    def test_unlinked_clip_is_its_own_group(self, timeline):
        clip = make_clip(0, 10)
        timeline.tracks[0].insert(clip)
        assert timeline.linked_group(clip) == [clip]

    def test_snapshot_restore_is_a_deep_copy(self, linked):
        snapshot = linked.snapshot()
        linked.tracks[0].clips[0].tl_start = 999
        linked.restore(snapshot)
        assert linked.tracks[0].clips[0].tl_start == 0


# -- razor --------------------------------------------------------------------


class TestRazor:
    def test_splits_across_all_tracks(self, linked):
        created = ops.razor(linked, 50)
        assert len(created) == 2, "both halves of the linked pair get cut"
        assert [c.tl_start for c in linked.tracks[0].clips] == [0, 50, 100]

    def test_halves_are_contiguous_and_preserve_source(self, linked):
        ops.razor(linked, 50)
        left, right = linked.tracks[0].clips[0], linked.tracks[0].clips[1]
        assert left.tl_end == right.tl_start
        assert left.src_out == right.src_in, "no source frames lost or repeated"
        assert left.duration + right.duration == 100

    def test_cut_on_a_boundary_is_a_noop(self, linked):
        before = len(linked.tracks[0].clips)
        assert ops.razor(linked, 100) == []
        assert len(linked.tracks[0].clips) == before

    def test_cut_in_empty_space_is_a_noop(self, timeline):
        timeline.tracks[0].insert(make_clip(0, 10))
        assert ops.razor(timeline, 500) == []

    def test_each_half_is_linked_to_its_own_partner(self, linked):
        """A cut must leave two independent A/V pairs, not one four-clip group —
        otherwise dragging the left half would drag the right half with it."""
        ops.razor(linked, 50)
        left, right = linked.tracks[0].clips[0], linked.tracks[0].clips[1]

        assert len(linked.linked_group(left)) == 2
        assert len(linked.linked_group(right)) == 2
        assert left.link_id != right.link_id

        # The right video half is paired with the right audio half specifically.
        assert {c.kind for c in linked.linked_group(right)} == {"video", "audio"}
        assert all(c.tl_start == 50 for c in linked.linked_group(right))

    def test_moving_one_half_leaves_the_other(self, linked):
        ops.razor(linked, 50)
        left = linked.tracks[0].clips[0]
        ops.move_clips(linked, [left], 300)
        assert linked.tracks[0].clips[0].tl_start == 50, "the right half stayed put"

    def test_locked_track_is_not_cut(self, linked):
        linked.tracks[1].locked = True
        ops.razor(linked, 50)
        assert len(linked.tracks[0].clips) == 3
        assert len(linked.tracks[1].clips) == 2, "locked audio lane untouched"


# -- deletion -----------------------------------------------------------------


class TestDeletion:
    def test_lift_leaves_a_gap(self, linked):
        ops.lift(linked, [linked.tracks[0].clips[0]])
        assert [c.tl_start for c in linked.tracks[0].clips] == [100]
        assert linked.tracks[0].gaps() == [(0, 100)]

    def test_lift_removes_the_linked_partner(self, linked):
        ops.lift(linked, [linked.tracks[0].clips[0]])
        assert len(linked.tracks[1].clips) == 1, "audio partner went with it"

    def test_ripple_delete_closes_the_gap(self, linked):
        ops.ripple_delete(linked, [linked.tracks[0].clips[0]])
        assert [c.tl_start for c in linked.tracks[0].clips] == [0]
        assert linked.tracks[0].gaps() == []

    def test_ripple_delete_shifts_every_track(self, linked):
        """The sync guarantee: rippling video must move audio by the same amount."""
        ops.ripple_delete(linked, [linked.tracks[0].clips[0]])
        assert linked.tracks[0].clips[0].tl_start == 0
        assert linked.tracks[1].clips[0].tl_start == 0

    def test_ripple_delete_closes_only_its_own_span(self, timeline):
        """A ripple closes the hole it made and nothing else.

        Gaps the user created by trimming must survive, shifted — a ripple that
        also swallowed unrelated gaps would silently rewrite parts of the edit
        the user never touched.
        """
        for kind, index in (("video", 0), ("audio", 1)):
            for number, start in enumerate((0, 60, 160)):
                timeline.tracks[index].insert(
                    make_clip(start, 60, src_length=200, kind=kind, link=f"L{number}")
                )
        # Trim the first clip short, opening a gap at 30..60.
        ops.trim(timeline, timeline.tracks[0].clips[0], "out", 30)
        assert timeline.tracks[0].gaps() == [(30, 60), (120, 160)]

        ops.ripple_delete(timeline, [timeline.tracks[0].clips[0]])

        # The deleted 0..30 is closed; both other gaps just move back by 30.
        assert [(c.tl_start, c.tl_end) for c in timeline.tracks[0].clips] == [(30, 90), (130, 190)]
        assert timeline.tracks[0].gaps() == [(0, 30), (90, 130)]
        # And audio tracked video exactly through all of it.
        assert [(c.tl_start, c.tl_end) for c in timeline.tracks[1].clips] == [
            (c.tl_start, c.tl_end) for c in timeline.tracks[0].clips
        ]

    def test_ripple_delete_range(self, linked):
        ops.ripple_delete_range(linked, 50, 150)
        # 0-50 survives, 150-200 slides back to 50.
        assert [(c.tl_start, c.tl_end) for c in linked.tracks[0].clips] == [(0, 50), (50, 100)]
        assert linked.duration == 100

    def test_delete_on_locked_track_refused(self, linked):
        linked.tracks[0].locked = True
        with pytest.raises(TimelineError, match="locked"):
            ops.lift(linked, [linked.tracks[0].clips[0]])


# -- moving -------------------------------------------------------------------


class TestMove:
    def test_move_shifts_the_whole_link_group(self, timeline):
        for kind, index in (("video", 0), ("audio", 1)):
            timeline.tracks[index].insert(make_clip(0, 50, src_length=50, kind=kind, link="L"))
        ops.move_clips(timeline, [timeline.tracks[0].clips[0]], 200)
        assert timeline.tracks[0].clips[0].tl_start == 200
        assert timeline.tracks[1].clips[0].tl_start == 200, "audio followed video"

    def test_move_onto_occupied_space_is_refused(self, linked):
        with pytest.raises(TimelineError, match="in the way"):
            ops.move_clips(linked, [linked.tracks[0].clips[0]], 100)

    def test_failed_move_leaves_the_timeline_untouched(self, linked):
        before = [(c.tl_start, c.tl_end) for c in linked.tracks[0].clips]
        with pytest.raises(TimelineError):
            ops.move_clips(linked, [linked.tracks[0].clips[0]], 100)
        assert [(c.tl_start, c.tl_end) for c in linked.tracks[0].clips] == before

    def test_dragging_before_zero_clamps(self, timeline):
        clip = make_clip(100, 50)
        timeline.tracks[0].insert(clip)
        ops.move_clips(timeline, [clip], -500)
        assert clip.tl_start == 0, "stops at the start rather than failing"

    def test_move_to_absolute_frame(self, timeline):
        clip = make_clip(10, 50)
        timeline.tracks[0].insert(clip)
        ops.move_clips_to(timeline, [clip], 300)
        assert clip.tl_start == 300

    def test_move_between_tracks(self, timeline):
        timeline.tracks.append(Track(kind="video", name="V2"))
        clip = make_clip(0, 50)
        timeline.tracks[0].insert(clip)
        ops.move_clips(timeline, [clip], 0, target_track=timeline.tracks[2])
        assert len(timeline.tracks[0].clips) == 0
        assert len(timeline.tracks[2].clips) == 1


# -- trimming -----------------------------------------------------------------


class TestTrim:
    def test_trim_out_shortens(self, timeline):
        clip = make_clip(0, 100, src_length=200)
        timeline.tracks[0].insert(clip)
        assert ops.trim(timeline, clip, "out", 60) == 60
        assert clip.duration == 60
        assert clip.src_in == 0, "trimming the tail must not move the head"

    def test_trim_out_extends_into_tail_material(self, timeline):
        clip = make_clip(0, 100, src_length=200)
        timeline.tracks[0].insert(clip)
        ops.trim(timeline, clip, "out", 150)
        assert clip.duration == 150

    def test_trim_out_clamps_at_end_of_source(self, timeline):
        clip = make_clip(0, 100, src_length=120)
        timeline.tracks[0].insert(clip)
        assert ops.trim(timeline, clip, "out", 500) == 120, "stops at available source"
        assert clip.src_out == clip.src_length

    def test_trim_in_moves_start_and_source_together(self, timeline):
        clip = make_clip(100, 100, src_in=50, src_length=200)
        timeline.tracks[0].insert(clip)
        ops.trim(timeline, clip, "in", 120)
        assert clip.tl_start == 120
        assert clip.src_in == 70, "the source window slid with the edge"
        assert clip.duration == 80

    def test_trim_in_clamps_at_start_of_source(self, timeline):
        clip = make_clip(100, 100, src_in=20, src_length=200)
        timeline.tracks[0].insert(clip)
        assert ops.trim(timeline, clip, "in", 0) == 80, "only 20 frames of head exist"
        assert clip.src_in == 0

    def test_trim_clamps_against_neighbour(self, timeline):
        first = make_clip(0, 100, src_length=400)
        second = make_clip(100, 100, src_length=400)
        timeline.tracks[0].insert(first)
        timeline.tracks[0].insert(second)
        assert ops.trim(timeline, first, "out", 300) == 100, "blocked by the next clip"

    def test_trim_cannot_go_below_one_frame(self, timeline):
        clip = make_clip(0, 100, src_length=200)
        timeline.tracks[0].insert(clip)
        ops.trim(timeline, clip, "out", -50)
        assert clip.duration == 1

    def test_trim_applies_to_link_group(self, timeline):
        for kind, index in (("video", 0), ("audio", 1)):
            timeline.tracks[index].insert(make_clip(0, 100, src_length=200, kind=kind, link="L"))
        ops.trim(timeline, timeline.tracks[0].clips[0], "out", 60)
        assert timeline.tracks[1].clips[0].duration == 60, "audio trimmed to match"

    def test_trim_group_uses_most_restrictive_limit(self, timeline):
        """Video has 200 frames of source, audio only 120. The pair must stop at
        120 or the two would fall out of sync."""
        timeline.tracks[0].insert(make_clip(0, 100, src_length=200, kind="video", link="L"))
        timeline.tracks[1].insert(make_clip(0, 100, src_length=120, kind="audio", link="L"))
        assert ops.trim(timeline, timeline.tracks[0].clips[0], "out", 200) == 120

    def test_trim_bounds_reports_limits(self, timeline):
        clip = make_clip(100, 100, src_in=20, src_length=200)
        timeline.tracks[0].insert(clip)
        assert ops.trim_bounds(timeline, clip, "in") == (80, 199)


# -- linking ------------------------------------------------------------------


class TestLinking:
    def test_unlink_then_move_independently(self, linked):
        video = linked.tracks[0].clips[0]
        ops.unlink(linked, video)
        ops.move_clips(linked, [video], 300)
        assert linked.tracks[0].clips[-1].tl_start == 300
        assert linked.tracks[1].clips[0].tl_start == 0, "audio stayed put"

    def test_link_joins_clips(self, timeline):
        first = make_clip(0, 10, kind="video")
        second = make_clip(0, 10, kind="audio")
        timeline.tracks[0].insert(first)
        timeline.tracks[1].insert(second)
        ops.link(timeline, [first, second])
        assert len(timeline.linked_group(first)) == 2


# -- undo ---------------------------------------------------------------------


class TestUndo:
    def _state(self, timeline):
        return [
            [(c.clip_id, c.tl_start, c.src_in, c.src_out, c.link_id) for c in track.clips]
            for track in timeline.tracks
        ]

    @pytest.mark.parametrize(
        "label,edit",
        [
            ("razor", lambda t: ops.razor(t, 50)),
            ("lift", lambda t: ops.lift(t, [t.tracks[0].clips[0]])),
            ("ripple", lambda t: ops.ripple_delete(t, [t.tracks[0].clips[0]])),
            ("move", lambda t: ops.move_clips(t, [t.tracks[0].clips[1]], 300)),
            ("trim out", lambda t: ops.trim(t, t.tracks[0].clips[0], "out", 60)),
            ("trim in", lambda t: ops.trim(t, t.tracks[0].clips[0], "in", 30)),
            ("range", lambda t: ops.ripple_delete_range(t, 25, 175)),
            ("unlink", lambda t: ops.unlink(t, t.tracks[0].clips[0])),
        ],
    )
    def test_every_op_undoes_exactly(self, linked, label, edit):
        """The property that matters: undo restores the model exactly, for every
        operation, with no drift in positions or source windows."""
        stack = UndoStack(linked)
        before = self._state(linked)

        stack.apply(label, edit)
        assert self._state(linked) != before, f"{label} did not change anything"

        stack.undo()
        assert self._state(linked) == before

        stack.redo()
        assert self._state(linked) != before

        stack.undo()
        assert self._state(linked) == before

    def test_a_rejected_edit_is_not_recorded(self, linked):
        stack = UndoStack(linked)
        before = self._state(linked)
        with pytest.raises(TimelineError):
            stack.apply("bad move", lambda t: ops.move_clips(t, [t.tracks[0].clips[0]], 100))
        assert self._state(linked) == before
        assert not stack.can_undo, "a failed edit must not enter the history"

    def test_new_edit_clears_the_redo_branch(self, linked):
        stack = UndoStack(linked)
        stack.apply("razor", lambda t: ops.razor(t, 50))
        stack.undo()
        assert stack.can_redo
        stack.apply("razor", lambda t: ops.razor(t, 25))
        assert not stack.can_redo

    def test_nested_apply_collapses_to_one_step(self, linked):
        stack = UndoStack(linked)

        def compound(timeline):
            stack.apply("inner", lambda t: ops.razor(t, 25))
            stack.apply("inner", lambda t: ops.razor(t, 75))

        stack.apply("compound", compound)
        assert len(linked.tracks[0].clips) == 4
        stack.undo()
        assert len(linked.tracks[0].clips) == 2, "one undo reverted both cuts"

    def test_labels_and_dirty_tracking(self, linked):
        stack = UndoStack(linked)
        assert not stack.is_dirty
        stack.apply("Razor", lambda t: ops.razor(t, 50))
        assert stack.undo_label == "Razor"
        assert stack.is_dirty
        stack.mark_clean()
        assert not stack.is_dirty
        stack.undo()
        assert stack.is_dirty
        assert stack.redo_label == "Razor"

    def test_history_is_capped(self, linked):
        stack = UndoStack(linked, limit=5)
        for frame in range(10, 90, 10):
            stack.apply("razor", lambda t, f=frame: ops.razor(t, f))
        assert len(stack._done) == 5
