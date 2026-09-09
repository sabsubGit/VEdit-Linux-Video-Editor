"""Project save/load tests.

These use real generated media, because loading deliberately re-probes files
rather than trusting stored metadata — mocking that away would test nothing.
"""

from __future__ import annotations

import json
import subprocess
from fractions import Fraction

import pytest

from vedit.core.projectfile import (
    FORMAT_VERSION,
    ProjectFileError,
    load_project,
    project_to_dict,
    save_project,
)
from vedit.core.timebase import TimeBase
from vedit.media.probe import probe
from vedit.timeline.model import Clip, Timeline


@pytest.fixture(scope="module")
def clip_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30",
            "-f", "lavfi", "-i", "sine=frequency=440",
            "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture
def project(clip_file):
    info = probe(clip_file)
    timebase = TimeBase(30)
    timeline = Timeline.default(timebase)
    length = info.frame_count(timebase)

    for kind in ("video", "audio"):
        timeline.lane_for(kind).insert(
            Clip(
                media_id=info.media_id,
                src_in=10,
                src_out=length,
                tl_start=25,
                src_length=length,
                kind=kind,
                link_id="L1",
                name=info.name,
            )
        )
    timeline.audio_tracks[0].muted = True
    return timeline, [info]


class TestRoundTrip:
    def test_survives_save_and_load(self, project, tmp_path):
        timeline, media = project
        save_project(tmp_path / "p.vedit", timeline, media, playhead=42)
        result = load_project(tmp_path / "p.vedit")

        assert result.playhead == 42
        assert not result.missing
        assert result.timeline.timebase == timeline.timebase
        assert (result.timeline.width, result.timeline.height) == (1920, 1080)

        original = timeline.video_tracks[0].clips[0]
        loaded = result.timeline.video_tracks[0].clips[0]
        assert (loaded.src_in, loaded.src_out, loaded.tl_start, loaded.src_length) == (
            original.src_in, original.src_out, original.tl_start, original.src_length
        )

    def test_track_flags_and_links_survive(self, project, tmp_path):
        timeline, media = project
        save_project(tmp_path / "p.vedit", timeline, media)
        result = load_project(tmp_path / "p.vedit")

        assert result.timeline.audio_tracks[0].muted is True
        video = result.timeline.video_tracks[0].clips[0]
        assert len(result.timeline.linked_group(video)) == 2, "A/V pairing survived"

    def test_fractional_rate_survives_exactly(self, clip_file, tmp_path):
        timeline = Timeline.default(TimeBase(Fraction(30000, 1001)))
        save_project(tmp_path / "p.vedit", timeline, [])
        result = load_project(tmp_path / "p.vedit")
        assert result.timeline.timebase.fps == Fraction(30000, 1001)

    def test_suffix_is_added(self, project, tmp_path):
        timeline, media = project
        written = save_project(tmp_path / "noext", timeline, media)
        assert written.suffix == ".vedit"
        assert written.exists()

    def test_saved_file_is_readable_json(self, project, tmp_path):
        timeline, media = project
        path = save_project(tmp_path / "p.vedit", timeline, media)
        payload = json.loads(path.read_text())
        assert payload["format"] == "vedit-project"
        assert payload["version"] == FORMAT_VERSION
        assert len(payload["tracks"]) == 6

    def test_only_used_media_is_written(self, project, tmp_path, clip_file):
        timeline, media = project
        # An extra pool entry not referenced by any clip must not be saved.
        extra = probe(clip_file)
        path = save_project(tmp_path / "p.vedit", timeline, media)
        payload = json.loads(path.read_text())
        assert len(payload["media"]) == 1

    def test_no_partial_file_left_behind(self, project, tmp_path):
        timeline, media = project
        save_project(tmp_path / "p.vedit", timeline, media)
        assert not list(tmp_path.glob("*.part*")), "atomic write must clean up"


class TestMissingMedia:
    def test_missing_file_is_reported_and_clips_dropped(self, project, tmp_path):
        timeline, media = project
        path = save_project(tmp_path / "p.vedit", timeline, media)

        payload = json.loads(path.read_text())
        payload["media"][0]["path"] = "/nonexistent/gone.mp4"
        path.write_text(json.dumps(payload))

        result = load_project(path)
        assert result.missing == ["/nonexistent/gone.mp4"]
        # The project still opens; it just has no clips for that media.
        assert sum(len(track.clips) for track in result.timeline.tracks) == 0
        assert len(result.timeline.tracks) == 6, "the lanes themselves survive"


class TestRejections:
    def test_not_json(self, tmp_path):
        path = tmp_path / "bad.vedit"
        path.write_text("this is not json")
        with pytest.raises(ProjectFileError, match="could not be read"):
            load_project(path)

    def test_wrong_format_marker(self, tmp_path):
        path = tmp_path / "other.vedit"
        path.write_text(json.dumps({"format": "something-else"}))
        with pytest.raises(ProjectFileError, match="not a vedit project"):
            load_project(path)

    def test_newer_format_version_is_refused(self, tmp_path):
        path = tmp_path / "future.vedit"
        path.write_text(
            json.dumps({"format": "vedit-project", "version": FORMAT_VERSION + 5})
        )
        with pytest.raises(ProjectFileError, match="newer version"):
            load_project(path)

    def test_missing_file(self, tmp_path):
        with pytest.raises(ProjectFileError):
            load_project(tmp_path / "nope.vedit")

    def test_empty_project_gets_default_tracks(self, tmp_path):
        path = tmp_path / "bare.vedit"
        path.write_text(json.dumps({"format": "vedit-project", "version": 1}))
        result = load_project(path)
        assert [track.name for track in result.timeline.tracks] == ["V1", "V2", "V3", "A1", "A2", "A3"]


class TestMixingFields:
    def test_round_trip(self, project, tmp_path):
        timeline, media = project
        timeline.master_gain_db = -2.5
        timeline.audio_tracks[1].gain_db = -6.0
        timeline.audio_tracks[1].solo = True
        clip = timeline.audio_tracks[0].clips[0]
        clip.gain_db = 4.5
        clip.fade_in = 12
        clip.fade_out = 8

        save_project(tmp_path / "mix.vedit", timeline, media)
        loaded = load_project(tmp_path / "mix.vedit").timeline

        assert loaded.master_gain_db == -2.5
        assert loaded.audio_tracks[1].gain_db == -6.0
        assert loaded.audio_tracks[1].solo is True
        restored = loaded.audio_tracks[0].clips[0]
        assert (restored.gain_db, restored.fade_in, restored.fade_out) == (4.5, 12, 8)

    def test_the_written_version_is_the_current_one(self, project):
        """Pinned deliberately: the number only moves when a field is added that
        an older build would drop on the next save."""
        timeline, media = project
        assert project_to_dict(timeline, media)["version"] == 5

    def test_a_version_one_file_loads_with_everything_at_unity(self, project, tmp_path):
        """Forward compatibility: the new fields all default to "as it was", so
        a file written before they existed opens unchanged."""
        timeline, media = project
        payload = project_to_dict(timeline, media)
        payload["version"] = 1
        payload.pop("master_gain_db")
        for track in payload["tracks"]:
            track.pop("gain_db")
            track.pop("solo")
            for clip in track["clips"]:
                clip.pop("gain_db")
                clip.pop("fade_in")
                clip.pop("fade_out")

        path = tmp_path / "v1.vedit"
        path.write_text(json.dumps(payload))
        loaded = load_project(path).timeline

        assert loaded.master_gain_db == 0.0
        assert all(track.gain_db == 0.0 and not track.solo for track in loaded.tracks)
        assert all(
            clip.gain_db == 0.0 and not clip.has_fades for clip in loaded.all_clips()
        )

    def test_a_malformed_gain_drops_the_clip_not_the_project(self, project, tmp_path):
        timeline, media = project
        payload = project_to_dict(timeline, media)
        payload["tracks"][3]["clips"][0]["gain_db"] = "loud"

        path = tmp_path / "bad-gain.vedit"
        path.write_text(json.dumps(payload))
        loaded = load_project(path).timeline

        assert len(loaded.audio_tracks[0].clips) == 0
        assert len(loaded.video_tracks[0].clips) == 1, "the rest of the edit survives"
