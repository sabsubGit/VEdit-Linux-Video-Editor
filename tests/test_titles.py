"""Titles: words drawn over the picture.

A title is the first thing on this timeline that is not backed by a file, which
is the whole difficulty. It has to be trimmable, movable, rippleable and
undoable like any clip, while never being mistaken for a shot — because the
timeline's rule is that the highest lane wins, and a caption that hid the thing
it captioned would be useless.
"""

from __future__ import annotations

import json

import pytest

from vedit.core.projectfile import project_to_dict
from vedit.core.timebase import TimeBase
from vedit.timeline import ops
from vedit.timeline import titles as T
from vedit.timeline.model import Clip, Timeline, TimelineError
from vedit.timeline.titles import Title, TitleError

HD = (1920, 1080)


@pytest.fixture
def timeline() -> Timeline:
    return Timeline.default(TimeBase(30))


def shot(track, media_id="m", start=0, length=120):
    clip = Clip(media_id=media_id, src_in=0, src_out=length, tl_start=start,
                src_length=length + 60, name=media_id)
    track.insert(clip)
    return clip


class TestTitleValues:
    def test_the_defaults_are_usable(self):
        assert not Title().is_empty

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"size": "enormous"}, "not one of"),
            ({"position": "middle"}, "not one of"),
            ({"align": "justified"}, "not one of"),
            ({"colour": "white"}, "rrggbb"),
            ({"colour": "#fff"}, "rrggbb"),
        ],
    )
    def test_bad_settings_are_rejected(self, kwargs, message):
        with pytest.raises(TitleError, match=message):
            Title(**kwargs)

    def test_a_title_error_is_a_value_error(self):
        """So the project loader's "drop the clip, keep the project" path
        catches it without a special case."""
        assert issubclass(TitleError, ValueError)

    def test_blank_text_is_recognised_as_empty(self):
        assert Title(text="   \n  ").is_empty


class TestLayout:
    def test_the_block_is_placed_then_split_into_lines(self):
        """A two-line lower third sits where a one-line one does, rather than
        drifting down the frame as text is added."""
        one = T.layout(Title(text="A", position="lower"), HD)
        two = T.layout(Title(text="A\nB", position="lower"), HD)
        assert len(two) == 2
        assert two[0].top < one[0].top < two[1].top

    def test_lines_are_evenly_spaced(self):
        lines = T.layout(Title(text="A\nB\nC"), HD)
        gaps = [b.top - a.top for a, b in zip(lines, lines[1:])]
        assert gaps[0] == pytest.approx(gaps[1])

    @pytest.mark.parametrize("position", T.POSITIONS)
    def test_nothing_falls_off_the_frame(self, position):
        lines = T.layout(Title(text="A\nB\nC", position=position, size="huge"), HD)
        assert lines[0].top >= 0
        assert lines[-1].top + lines[-1].height <= HD[1] + 1

    def test_size_is_a_fraction_of_the_frame(self):
        """So a title looks the same on a 720p project as on a 4K one."""
        small = T.layout(Title(), (1280, 720))[0].font_px
        big = T.layout(Title(), (3840, 2160))[0].font_px
        assert big / small == pytest.approx(3.0)

    def test_top_and_bottom_sit_either_side_of_centre(self):
        top = T.layout(Title(position="top"), HD)[0].top
        centre = T.layout(Title(position="centre"), HD)[0].top
        bottom = T.layout(Title(position="bottom"), HD)[0].top
        assert top < centre < bottom

    def test_blank_lines_are_kept_as_spacing(self):
        assert len(T.layout(Title(text="A\n\nB"), HD)) == 3


class TestQuoting:
    """Three layers eat characters on the way to `drawtext`. Getting this wrong
    does not produce wrong text — it stops the export dead."""

    @pytest.mark.parametrize("char", [":", ",", ";", "[", "]", "=", "\\"])
    def test_every_dangerous_character_is_escaped(self, char):
        assert T.quote(f"a{char}b").count("\\") >= 1

    def test_a_backslash_is_escaped_before_anything_else(self):
        """Otherwise the escapes added afterwards get escaped themselves."""
        assert T.quote("\\") == "'\\\\'"

    def test_the_value_comes_back_quoted(self):
        """This build needs escaping *and* quoting; either alone lets a colon
        through and the graph stops parsing."""
        assert T.quote("plain") == "'plain'"

    def test_a_quote_closes_escapes_and_reopens(self):
        """One cannot appear inside quotes at all."""
        assert T.quote("it's") == "'it'\\''s'"

    def test_a_per_cent_sign_is_left_alone(self):
        """Neither escaping nor doubling works for `%`; `expansion=none` does,
        and the render passes it before `text=`."""
        assert T.quote("100%") == "'100%'"


class TestOnTheTimeline:
    def test_a_title_needs_no_media(self, timeline):
        clip = ops.add_title(timeline, 0)
        assert clip.is_title and clip.media_id == ""

    def test_it_lands_above_the_picture_by_default(self, timeline):
        """A title is nearly always meant to sit over a shot, and V1 is where
        the shot is."""
        shot(timeline.video_tracks[0])
        clip = ops.add_title(timeline, 10)
        assert timeline.track_of(clip).name != "V1"

    def test_it_never_wins_the_picture(self, timeline):
        """The rule that makes a caption a caption rather than a blank card."""
        picture = shot(timeline.video_tracks[0])
        ops.add_title(timeline, 0)
        assert timeline.video_clip_at(10) is picture

    def test_it_is_found_as_a_title_instead(self, timeline):
        shot(timeline.video_tracks[0])
        clip = ops.add_title(timeline, 0)
        assert [c.clip_id for c in timeline.titles_at(10)] == [clip.clip_id]

    def test_titles_stack_lowest_lane_first(self, timeline):
        """Drawing order, so one on V3 lands on top of one on V2."""
        lower = ops.add_title(timeline, 0, track=timeline.video_tracks[1],
                              title=Title(text="lower"))
        upper = ops.add_title(timeline, 0, track=timeline.video_tracks[2],
                              title=Title(text="upper"))
        assert [c.title.text for c in timeline.titles_at(10)] == ["lower", "upper"]

    def test_a_disabled_title_does_not_draw(self, timeline):
        clip = ops.add_title(timeline, 0)
        clip.enabled = False
        assert timeline.titles_at(10) == []

    def test_a_muted_lane_hides_its_titles(self, timeline):
        clip = ops.add_title(timeline, 0)
        timeline.track_of(clip).muted = True
        assert timeline.titles_at(10) == []

    def test_it_takes_its_name_from_its_first_line(self, timeline):
        clip = ops.add_title(timeline, 0, title=Title(text="Chapter Two\nThe Walk"))
        assert clip.name == "Chapter Two"

    def test_a_title_cannot_go_on_an_audio_track(self, timeline):
        with pytest.raises(TimelineError, match="video track"):
            Clip(media_id="", src_in=0, src_out=30, tl_start=0, src_length=30,
                 kind="audio", title=Title())

    def test_editing_rewrites_the_words_and_the_name(self, timeline):
        clip = ops.add_title(timeline, 0)
        ops.set_title(timeline, clip, Title(text="Somewhere else"))
        assert clip.title.text == "Somewhere else"
        assert clip.name == "Somewhere else"

    def test_editing_something_that_is_not_a_title_refuses(self, timeline):
        picture = shot(timeline.video_tracks[0])
        with pytest.raises(TimelineError, match="not a title"):
            ops.set_title(timeline, picture, Title())

    def test_a_locked_track_refuses(self, timeline):
        clip = ops.add_title(timeline, 0)
        timeline.track_of(clip).locked = True
        with pytest.raises(TimelineError, match="locked"):
            ops.set_title(timeline, clip, Title(text="no"))


class TestOrdinaryEditsWorkOnIt:
    """The reason a title is a `Clip` and not a new kind of object."""

    def test_it_can_be_cut(self, timeline):
        clip = ops.add_title(timeline, 0, title=Title(text="Split me"))
        track = timeline.track_of(clip)
        ops.razor(timeline, 20)
        assert len(track.clips) == 2
        assert all(half.is_title for half in track.clips)
        assert all(half.title.text == "Split me" for half in track.clips)

    def test_it_can_be_moved(self, timeline):
        clip = ops.add_title(timeline, 0)
        ops.move_clips(timeline, [clip], 45)
        assert clip.tl_start == 45

    def test_it_can_be_trimmed(self, timeline):
        clip = ops.add_title(timeline, 0)
        ops.trim(timeline, clip, "out", 30)
        assert clip.tl_end == 30

    def test_it_can_be_deleted(self, timeline):
        clip = ops.add_title(timeline, 0)
        track = timeline.track_of(clip)
        ops.lift(timeline, [clip])
        assert track.clips == []

    def test_it_undoes(self, timeline):
        from vedit.core.commands import UndoStack

        undo = UndoStack(timeline)
        undo.apply("title", lambda t: ops.add_title(t, 0))
        assert any(c.is_title for c in timeline.all_clips())
        undo.undo()
        assert not any(c.is_title for c in timeline.all_clips())


class TestProjectFile:
    def test_it_round_trips(self, timeline):
        ops.add_title(timeline, 0, title=Title(
            text="Two\nlines", size="large", position="lower",
            align="left", colour="#ffcc00", shadow=False,
        ))
        raw = json.loads(json.dumps(project_to_dict(timeline, [])))
        stored = next(
            clip for track in raw["tracks"] for clip in track["clips"] if "title" in clip
        )
        assert stored["title"] == {
            "text": "Two\nlines", "size": "large", "position": "lower",
            "align": "left", "colour": "#ffcc00", "shadow": False,
            "offset_x": 0.0, "offset_y": 0.0,
        }

    def test_an_ordinary_clip_stores_no_title(self, timeline):
        shot(timeline.video_tracks[0])
        raw = project_to_dict(timeline, [])
        assert "title" not in raw["tracks"][0]["clips"][0]

    def test_a_title_survives_a_load_with_no_media_at_all(self, timeline, tmp_path):
        """It has no media to relink, which is exactly why the loader must not
        drop it for having none."""
        from vedit.core.projectfile import load_project, save_project

        ops.add_title(timeline, 0, title=Title(text="Alone"))
        path = save_project(tmp_path / "titles.vedit", timeline, [], 0)
        loaded = load_project(path).timeline
        survivors = [c for c in loaded.all_clips() if c.is_title]
        assert [c.title.text for c in survivors] == ["Alone"]


class TestTheAddTitleCommand:
    """The whole command, not just the op underneath it.

    Every test above exercised `ops.add_title`, and the canvas method that calls
    it shipped broken — it passed keywords through a helper that took none, so
    the toolbar button raised a `TypeError` before an op was ever reached. These
    drive the command the way the button does.
    """

    @pytest.fixture
    def canvas(self, qt_app, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from vedit.core.project import Project
        from vedit.timeline.view import TimelineCanvas

        project = Project()
        canvas = TimelineCanvas(project)
        canvas.resize(900, 400)
        return canvas

    def accept(self, monkeypatch, title=None):
        """Stand in for the dialog, accepting with a given title."""
        from vedit.timeline import title_dialog

        class Stub:
            DialogCode = type("C", (), {"Accepted": 1})

            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return 1

            def result_title(self):
                return title or Title(text="From the button")

        monkeypatch.setattr(title_dialog, "TitleDialog", Stub)

    def test_the_command_puts_a_title_on_the_timeline(self, canvas, monkeypatch):
        self.accept(monkeypatch)
        canvas.add_title()
        titles = [c for c in canvas.timeline.all_clips() if c.is_title]
        assert [c.title.text for c in titles] == ["From the button"]

    def test_it_lands_at_the_playhead(self, canvas, monkeypatch):
        self.accept(monkeypatch)
        canvas.project.set_playhead(45)
        canvas.add_title()
        clip = next(c for c in canvas.timeline.all_clips() if c.is_title)
        assert clip.tl_start == 45

    def test_it_is_one_undo_step(self, canvas, monkeypatch):
        self.accept(monkeypatch)
        canvas.add_title()
        canvas.project.undo_edit()
        assert not any(c.is_title for c in canvas.timeline.all_clips())

    def test_an_empty_title_is_refused_rather_than_added(self, canvas, monkeypatch):
        messages = []
        canvas.status_message.connect(messages.append)
        self.accept(monkeypatch, Title(text="   "))
        canvas.add_title()
        assert not any(c.is_title for c in canvas.timeline.all_clips())
        assert messages and "words" in messages[0]

    def test_cancelling_adds_nothing(self, canvas, monkeypatch):
        from vedit.timeline import title_dialog

        class Cancelled:
            DialogCode = type("C", (), {"Accepted": 1})

            def __init__(self, *args, **kwargs):
                pass

            def exec(self):
                return 0

        monkeypatch.setattr(title_dialog, "TitleDialog", Cancelled)
        canvas.add_title()
        assert not any(c.is_title for c in canvas.timeline.all_clips())

    def test_editing_an_existing_title_goes_through_the_same_funnel(
        self, canvas, monkeypatch
    ):
        self.accept(monkeypatch)
        canvas.add_title()
        clip = next(c for c in canvas.timeline.all_clips() if c.is_title)

        self.accept(monkeypatch, Title(text="Rewritten"))
        canvas.edit_title(clip)
        assert clip.title.text == "Rewritten"


class TestWhereTitlesLand:
    """A title needs empty lane either side of it to be dragged and stretched,
    which is why it gets one of its own rather than a gap in an existing one."""

    def test_the_first_title_goes_above_the_picture(self, timeline):
        shot(timeline.video_tracks[0])
        clip = ops.add_title(timeline, 0)
        assert timeline.track_of(clip).name == "V2"

    def test_a_second_title_shares_the_lane_when_there_is_room(self, timeline):
        first = ops.add_title(timeline, 0)
        second = ops.add_title(timeline, 300)
        assert timeline.track_of(second) is timeline.track_of(first)

    def test_an_overlapping_title_gets_a_lane_of_its_own(self, timeline):
        first = ops.add_title(timeline, 0)
        second = ops.add_title(timeline, 10)
        assert timeline.track_of(second) is not timeline.track_of(first)

    def test_a_new_lane_is_made_when_the_existing_ones_are_full(self, timeline):
        before = len(timeline.video_tracks)
        for start in range(0, 40, 10):
            ops.add_title(timeline, start)
        assert len(timeline.video_tracks) > before

    def test_a_locked_lane_is_skipped(self, timeline):
        timeline.video_tracks[1].locked = True
        clip = ops.add_title(timeline, 0)
        assert timeline.track_of(clip) is not timeline.video_tracks[1]


class TestDraggingATitle:
    """Positioning by hand. The presets stay the anchor the drag is measured
    from, so a nudged centre title is still a centred title."""

    def test_an_offset_moves_the_words(self):
        plain = T.layout(Title(), HD)[0].top
        moved = T.layout(Title(offset_y=0.2), HD)[0].top
        assert moved > plain

    def test_the_offset_scales_with_the_frame(self):
        """Fractions, not pixels, so a title dragged into place on an HD
        project is in the same place on a 4K one."""
        hd = T.layout(Title(offset_y=0.25), (1920, 1080))[0].top / 1080
        uhd = T.layout(Title(offset_y=0.25), (3840, 2160))[0].top / 2160
        assert hd == pytest.approx(uhd)

    @pytest.mark.parametrize("text", ["A", "A\nB", "A\nB\nC"])
    @pytest.mark.parametrize("offset", [-1.0, 1.0])
    def test_it_cannot_be_dragged_out_of_sight(self, text, offset):
        """Dragged as far as it goes, some of the block is still on screen.

        Some of the *block*, not the first line: with several lines, dragging
        the lot upwards is meant to take the early ones off the top. What must
        not happen is the title vanishing entirely, leaving nothing to grab and
        no sign of why the export has words you cannot see in the viewer.
        """
        lines = T.layout(Title(text=text, offset_y=offset), HD)
        top = lines[0].top
        bottom = lines[-1].top + lines[-1].height
        assert bottom > 0, "the block ran off the top"
        assert top < HD[1], "the block ran off the bottom"

    @pytest.mark.parametrize("value", [1.5, -2.0])
    def test_an_impossible_offset_is_rejected(self, value):
        with pytest.raises(TitleError, match="outside"):
            Title(offset_x=value)

    def test_the_horizontal_offset_is_reported_in_frame_pixels(self):
        assert T.horizontal_offset(Title(offset_x=0.25), HD) == pytest.approx(480.0)

    def test_it_survives_a_project_file(self, timeline):
        ops.add_title(timeline, 0, title=Title(offset_x=-0.3, offset_y=0.4))
        stored = next(
            c for tr in project_to_dict(timeline, [])["tracks"] for c in tr["clips"]
            if "title" in c
        )
        assert stored["title"]["offset_x"] == -0.3
        assert stored["title"]["offset_y"] == 0.4

    def test_the_export_moves_the_text_too(self):
        """Both consumers read the same offset, or the preview would lie about
        where the caption ended up."""
        from vedit.render.graph import Piece, _titles

        centred = _titles(Piece(0, 0.0, 1.0, 1.0, titles=(Title(text="x"),)), *HD)
        nudged = _titles(
            Piece(0, 0.0, 1.0, 1.0, titles=(Title(text="x", offset_x=0.25),)), *HD
        )
        assert "(w-text_w)/2+0.0" in centred
        assert "(w-text_w)/2+480.0" in nudged


class TestSettingHowLongATitleLasts:
    """Dragging its edges, the only way a length gets set.

    `add_title` gives the clip a source window exactly as long as the default
    length, because that is the honest description of a clip with no source.
    But `_delta_bounds` reads spare source as the limit on a trim, so both
    handles stopped dead at the length it was created with: a title could be
    made shorter and then only back to where it started, never longer.
    """

    def test_it_can_be_dragged_longer(self, timeline):
        title = ops.add_title(timeline, 30)
        assert ops.trim(timeline, title, "out", 300) == 300
        assert title.tl_end == 300
        assert title.duration == 270

    def test_it_can_be_dragged_shorter_and_long_again(self, timeline):
        title = ops.add_title(timeline, 0)
        ops.trim(timeline, title, "out", 20)
        assert title.duration == 20
        ops.trim(timeline, title, "out", 400)
        assert title.duration == 400

    def test_the_head_can_be_dragged_earlier(self, timeline):
        """The end stays put and the title starts sooner — the same gesture as
        on a shot, which on a shot needs unused footage to spend."""
        title = ops.add_title(timeline, 100)
        end = title.tl_end
        ops.trim(timeline, title, "in", 40)
        assert (title.tl_start, title.tl_end) == (40, end)

    def test_it_still_stops_at_the_start_of_the_timeline(self, timeline):
        title = ops.add_title(timeline, 10)
        assert ops.trim(timeline, title, "in", -50) == 0
        assert title.tl_start == 0

    def test_it_still_stops_at_a_neighbour(self, timeline):
        """Unlimited source, but not unlimited room."""
        track = timeline.video_tracks[0]
        first = ops.add_title(timeline, 0, track=track)
        second = ops.add_title(timeline, first.tl_end + 30, track=track)
        assert ops.trim(timeline, first, "out", 100_000) == second.tl_start
        assert first.tl_end == second.tl_start

    def test_it_cannot_be_dragged_away_to_nothing(self, timeline):
        title = ops.add_title(timeline, 0)
        ops.trim(timeline, title, "out", 0)
        assert title.duration >= 1

    def test_a_half_of_a_cut_title_lengthens_by_what_was_asked(self, timeline):
        """The window of a razored title starts partway in. Re-anchoring it at
        zero without allowing for that lengthened the clip by the offset."""
        title = ops.add_title(timeline, 0)
        ops.razor(timeline, 45)
        right = [c for c in timeline.track_of(title).clips if c.tl_start == 45][0]
        assert right.src_in > 0, "the fixture must exercise the offset window"

        ops.trim(timeline, right, "out", right.tl_end + 15)
        assert right.duration == 60
        assert right.tl_end == 105

    def test_the_window_stays_consistent_after_a_trim(self, timeline):
        """`duration` is derived from the window, so the two must not drift."""
        title = ops.add_title(timeline, 12)
        ops.trim(timeline, title, "out", 200)
        assert title.src_in == 0
        assert title.src_out == title.src_length
        assert title.duration == title.tl_end - title.tl_start

    def test_a_shot_still_cannot_be_stretched_past_its_footage(self, timeline):
        """The title exemption must not leak onto clips that have real source."""
        clip = shot(timeline.video_tracks[0], length=120)
        clip.src_length = 150          # 30 frames of tail and no more
        landed = ops.trim(timeline, clip, "out", 100_000)
        assert landed == 150

    def test_undo_puts_the_length_back(self, timeline):
        from vedit.core.project import Project

        project = Project(TimeBase(30))
        title = ops.add_title(project.timeline, 0)
        before = title.duration
        project.edit("Trim out", lambda t: ops.trim(t, title, "out", 500))
        project.undo.undo()
        again = project.timeline.video_tracks[title_lane(project.timeline, title)]
        assert again.clips[0].duration == before


def title_lane(timeline, title) -> int:
    for index, track in enumerate(timeline.video_tracks):
        if any(c.is_title for c in track.clips):
            return index
    raise AssertionError("no title lane")
