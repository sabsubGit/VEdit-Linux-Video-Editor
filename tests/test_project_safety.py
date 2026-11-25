"""Autosave and recovery, offline media and relinking, and the project format.

The three things that decide whether a real edit survives contact with a crash,
a moved folder, or footage that is not 1080p30.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from fractions import Fraction

import pytest

from vedit.core import recovery
from vedit.core.project import FormatError, Project, RelinkError, matched_rate
from vedit.timeline import ops


def make(path, *, size="320x240", rate="30", seconds=3, audio=True, video=True):
    args = ["ffmpeg", "-y", "-v", "error"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=frequency=440"]
    args += ["-t", str(seconds)]
    if video:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        args += ["-c:a", "aac"]
    subprocess.run(args + [str(path)], check=True, capture_output=True)
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    folder = tmp_path_factory.mktemp("media")
    return {
        "hd30": make(folder / "hd30.mp4"),
        "tall60": make(folder / "tall60.mp4", size="360x640", rate="60"),
        "pal": make(folder / "pal.mp4", size="640x360", rate="25"),
        "short": make(folder / "short.mp4", seconds=1),
        "sound": make(folder / "sound.m4a", video=False),
    }


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    made = Project()
    yield made
    made.undo.mark_clean()
    made.shutdown()


def add(project, path):
    info = project.import_paths([path])[0]
    project.match_format(info)
    project.edit("Add", lambda t: ops.append_media(t, info))
    return info


# -- format --------------------------------------------------------------------


class TestFormat:
    def test_the_first_clip_sets_the_project_format(self, project, media):
        add(project, media["tall60"])
        assert project.format == (360, 640, Fraction(60))
        # Counted at the rate it was matched to, not the default.
        assert project.timeline.duration == 180

    def test_later_clips_do_not_change_it(self, project, media):
        add(project, media["pal"])
        add(project, media["tall60"])
        assert project.format == (640, 360, Fraction(25))

    def test_sound_alone_does_not_decide_the_picture(self, project, media):
        add(project, media["sound"])
        assert project.format == (1920, 1080, Fraction(30))

    def test_a_format_chosen_by_hand_is_left_alone(self, project, media):
        project.set_format(1080, 1920, Fraction(25))
        add(project, media["hd30"])
        assert project.format == (1080, 1920, Fraction(25))

    def test_size_can_change_under_clips_but_rate_cannot(self, project, media):
        add(project, media["hd30"])
        project.undo.mark_clean()
        assert project.set_format(1280, 720, Fraction(30))
        assert project.format[:2] == (1280, 720)
        assert project.is_dirty, "a format change has to be saved"
        with pytest.raises(FormatError):
            project.set_format(1280, 720, Fraction(25))
        assert project.format == (1280, 720, Fraction(30))

    def test_odd_sizes_are_made_even(self, project):
        project.set_format(1281, 721, Fraction(30))
        assert project.format[:2] == (1280, 720)

    def test_everything_shares_the_one_timebase(self, project, media):
        """The engine and pool hold the project's timebase object; a rate change
        that replaced it would leave them counting at the old rate."""
        before = project.timebase
        add(project, media["pal"])
        assert project.timebase is before
        assert project.timeline.timebase is before
        assert before.fps == 25

    def test_changing_rate_forgets_history_counted_at_the_old_one(self, project, media):
        info = add(project, media["hd30"])
        project.edit("Clear", lambda t: ops.ripple_delete(t, list(t.all_clips())))
        assert project.can_change_rate and project.undo.can_undo
        project.set_format(1920, 1080, Fraction(25))
        assert not project.undo.can_undo
        assert info is not None

    def test_matched_rate_snaps_and_caps(self):
        assert matched_rate(Fraction(2997, 100)) == Fraction(30000, 1001)
        assert matched_rate(Fraction(240)) == Fraction(60)
        assert matched_rate(Fraction(59)) in (Fraction(60), Fraction(60000, 1001))

    def test_a_loaded_project_keeps_its_rate_and_master_gain(self, project, media, tmp_path):
        add(project, media["pal"])
        project.edit("Master", lambda t: setattr(t, "master_gain_db", -6.0))
        saved = project.save_to(tmp_path / "pal.vedit")

        project.reset()
        assert project.format == (1920, 1080, Fraction(30))
        timebase = project.timebase
        project.load_from(saved)
        assert project.format == (640, 360, Fraction(25))
        assert project.timebase is timebase
        assert project.timeline.master_gain_db == -6.0

    def test_new_project_lets_the_next_first_clip_decide_again(self, project, media):
        project.set_format(1080, 1920, Fraction(25))
        project.reset()
        add(project, media["tall60"])
        assert project.format == (360, 640, Fraction(60))


# -- offline media -------------------------------------------------------------


@pytest.fixture
def lost(project, media, tmp_path):
    """A saved project whose footage has since been moved elsewhere."""
    footage = tmp_path / "footage"
    footage.mkdir()
    for name in ("hd30", "short"):
        shutil.copy2(media[name], footage / f"{name}.mp4")
    add(project, footage / "hd30.mp4")
    add(project, footage / "short.mp4")
    (tmp_path / "projects").mkdir()
    saved = project.save_to(tmp_path / "projects" / "cut.vedit")

    moved = tmp_path / "elsewhere" / "deep"
    moved.parent.mkdir()
    footage.rename(moved)
    project.reset()
    project.load_from(saved)
    return saved, moved


class TestOffline:
    def test_clips_stay_on_the_timeline(self, project, lost):
        assert len(project.offline_media()) == 2
        assert project.offline_clip_count() == 4
        assert all(project.is_offline(c.media_id) for c in project.timeline.all_clips())
        assert not project.is_dirty

    def test_saving_while_offline_loses_nothing(self, project, lost):
        saved, _ = lost
        project.save_to(saved)
        project.reset()
        project.load_from(saved)
        assert project.offline_clip_count() == 4

    def test_a_folder_search_relinks_by_name(self, project, lost):
        _, moved = lost
        found = project.relink_from_folder(moved.parent)
        assert sorted(found) == ["hd30.mp4", "short.mp4"]
        assert not project.offline_media()
        assert project.offline_clip_count() == 0
        for clip in project.timeline.all_clips():
            assert project.media_for(clip.media_id) is not None
        assert project.is_dirty, "the new locations have to be saved"

    def test_locating_one_file_finds_its_neighbours(self, project, lost):
        _, moved = lost
        target = next(i for i in project.offline_media() if i.name == "hd30.mp4")
        found = project.locate(target.media_id, moved / "hd30.mp4")
        assert sorted(found) == ["hd30.mp4", "short.mp4"]

    def test_the_wrong_file_is_refused(self, project, lost, media):
        _, moved = lost
        long_one = next(i for i in project.offline_media() if i.name == "hd30.mp4")
        with pytest.raises(RelinkError, match="shorter"):
            project.relink(long_one.media_id, moved / "short.mp4")
        with pytest.raises(RelinkError, match="no picture"):
            project.relink(long_one.media_id, media["sound"])
        with pytest.raises(RelinkError):
            project.relink(long_one.media_id, moved / "nothing-here.mp4")
        assert len(project.offline_media()) == 2

    def test_undo_after_a_relink_does_not_bring_the_old_id_back(self, project, lost):
        _, moved = lost
        project.edit("Trim", lambda t: ops.ripple_delete(t, [next(iter(t.all_clips()))]))
        project.relink_from_folder(moved)
        project.undo_edit()
        assert project.offline_clip_count() == 0
        for clip in project.timeline.all_clips():
            assert project.media_for(clip.media_id) is not None

    def test_an_offline_project_still_builds_a_render(self, project, lost, tmp_path):
        """Offline clips export as gaps; the command must still be buildable
        once at least one real file is back."""
        from vedit.render.graph import build_command
        from vedit.render.presets import default_preset

        _, moved = lost
        short = next(i for i in project.offline_media() if i.name == "short.mp4")
        project.relink(short.media_id, moved / "short.mp4")
        args = build_command(
            project.timeline, project.pool, default_preset(), tmp_path / "out.mp4"
        )
        assert args.count("-i") == 1


# -- autosave ------------------------------------------------------------------


class TestAutosave:
    def test_nothing_is_written_for_a_clean_project(self, project):
        assert project.autosave() is None
        assert not recovery.session_file().exists()

    def test_unsaved_work_is_written_once_per_change(self, project, media):
        add(project, media["hd30"])
        written = project.autosave()
        assert written == recovery.session_file() and written.exists()
        stamp = written.stat().st_mtime_ns
        assert project.autosave() is None, "nothing changed, nothing to write"
        assert written.stat().st_mtime_ns == stamp
        add(project, media["short"])
        assert project.autosave() == written

    def test_it_never_touches_the_project_file(self, project, media, tmp_path):
        add(project, media["hd30"])
        saved = project.save_to(tmp_path / "mine.vedit")
        before = saved.read_text()
        add(project, media["short"])
        project.autosave()
        assert saved.read_text() == before
        note = json.loads(recovery.session_file().read_text())[recovery.KEY]
        assert note["original"] == str(saved)

    def test_saving_removes_it(self, project, media, tmp_path):
        add(project, media["hd30"])
        project.autosave()
        project.save_to(tmp_path / "mine.vedit")
        assert not recovery.session_file().exists()

    def test_a_live_session_is_not_offered_for_recovery(self, project, media):
        add(project, media["hd30"])
        project.autosave()
        assert recovery.pending() == []

    def test_a_dead_session_is_recovered_as_unsaved_work(self, project, media, tmp_path):
        add(project, media["pal"])
        saved = project.save_to(tmp_path / "mine.vedit")
        add(project, media["short"])
        autosave = project.autosave()

        # Stand in for a crash: the file is left behind under a pid that is gone.
        dead = subprocess.Popen(["true"])
        dead.wait()
        payload = json.loads(autosave.read_text())
        payload[recovery.KEY]["pid"] = dead.pid
        orphan = autosave.with_name(f"session-{dead.pid}.vedit")
        orphan.write_text(json.dumps(payload))
        project.reset()

        found = recovery.pending()
        assert [item.path for item in found] == [orphan]
        assert found[0].name == "mine"

        project.recover_from(found[0])
        assert project.path == str(saved)
        assert project.is_dirty, "recovered work is not yet saved"
        assert project.format == (640, 360, Fraction(25))
        assert len(list(project.timeline.all_clips())) == 4
        assert not orphan.exists()
        assert recovery.session_file().exists(), "and it is protected again at once"
        assert os.getpid() != dead.pid
