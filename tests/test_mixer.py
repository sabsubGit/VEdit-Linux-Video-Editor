"""Mixer widgets and the Audio page.

Offscreen, so these prove the widgets construct, paint without throwing, and
route a fader move to the two places it has to go. Anything involving a real
audio device — `processedUSecs`, actual latency, sound coming out — is untested
by design; that is precisely why `MeterQueue.drain` takes the heard position as
an argument, and those tests live in `test_player.py`.
"""

from __future__ import annotations

import pytest

from vedit.core.project import Project
from vedit.mixer.meter import LevelMeter
from vedit.mixer.panel import MixerPanel
from vedit.mixer.strip import ChannelStrip, Fader, MasterStrip
from vedit.player.audio import AudioStreamer
from vedit.timeline import levels
from vedit.timeline.model import Clip, Track


@pytest.fixture
def project():
    return Project()


@pytest.fixture
def panel(project):
    panel = MixerPanel(project, AudioStreamer(project.timebase))
    panel.resize(900, 226)
    return panel


class TestLevelMeter:
    def test_paints_at_rest(self):
        meter = LevelMeter()
        meter.resize(12, 160)
        meter.grab()

    def test_paints_at_every_level(self):
        meter = LevelMeter()
        meter.resize(12, 160)
        for peak in (0.0, 0.01, 0.2, 0.7, 1.0):
            meter.feed(peak, 0.0, clipped=peak >= 1.0)
            meter.grab()

    def test_zero_db_lines_up_with_the_faders_unity(self):
        """The alignment the whole custom-painting decision was made for."""
        meter = LevelMeter()
        meter.resize(12, 160)
        fader = Fader()
        fader.resize(32, 160)
        assert meter.y_for(0.0) == pytest.approx(fader._y_for(0.0), abs=0.01)

    def test_a_click_clears_the_clip_latch(self):
        meter = LevelMeter()
        meter.feed(1.0, 0.0, clipped=True)
        assert meter.ballistics.clipped
        meter.clear_clip()
        assert not meter.ballistics.clipped

    def test_reset_keeps_the_latch(self):
        meter = LevelMeter()
        meter.feed(1.0, 0.0, clipped=True)
        meter.reset()
        assert meter.ballistics.clipped
        assert meter.ballistics.level_db == levels.METER_FLOOR_DB


class TestFader:
    def test_starts_at_unity(self):
        assert Fader().db() == 0.0

    def test_value_is_clamped(self):
        fader = Fader()
        fader.set_db(99.0)
        assert fader.db() == 12.0

    def test_travel_round_trips_through_the_shared_curve(self):
        fader = Fader()
        fader.resize(32, 200)
        for db in (-48.0, -12.0, 0.0, 6.0):
            assert fader._db_at(fader._y_for(db)) == pytest.approx(db, abs=0.2)

    def test_paints_at_the_ends_of_the_travel(self):
        fader = Fader()
        fader.resize(32, 200)
        for db in (-60.0, 0.0, 12.0):
            fader.set_db(db)
            fader.grab()

    def test_double_click_returns_to_unity(self, qtbot=None):
        fader = Fader()
        fader.set_db(-20.0)
        moved: list[float] = []
        fader.moved.connect(moved.append)
        fader.mouseDoubleClickEvent(None)
        assert fader.db() == 0.0
        assert moved == [0.0]


class TestChannelStrip:
    def test_takes_its_state_from_the_track(self):
        strip = ChannelStrip(Track(kind="audio", name="A2", gain_db=-6.0, solo=True, muted=True))
        assert strip.fader.db() == -6.0
        assert strip.solo.isChecked() and strip.mute.isChecked()
        assert strip.name.text() == "A2"

    def test_toggling_state_from_the_model_does_not_re_emit(self):
        """`refresh` must not fire the signal it is reacting to, or muting from
        the track header would bounce back as a second edit."""
        track = Track(kind="audio", name="A1")
        strip = ChannelStrip(track)
        fired: list[tuple[str, bool]] = []
        strip.mute_toggled.connect(lambda *args: fired.append(args))
        track.muted = True
        strip.refresh(track)
        assert fired == []

    def test_a_held_fader_is_not_yanked_by_a_refresh(self):
        track = Track(kind="audio", name="A1")
        strip = ChannelStrip(track)
        strip.fader.set_db(-12.0)
        strip.fader._dragging = True
        track.gain_db = 3.0
        strip.refresh(track)
        assert strip.fader.db() == -12.0

    def test_paints(self):
        strip = ChannelStrip(Track(kind="audio", name="A1"))
        strip.resize(78, 220)
        strip.grab()

    def test_master_has_no_mute_or_solo(self):
        master = MasterStrip()
        assert not hasattr(master, "mute")
        assert not hasattr(master, "solo")
        master.resize(84, 220)
        master.grab()


class TestMixerPanel:
    def test_one_strip_per_audio_lane(self, panel, project):
        assert list(panel.strips) == [t.track_id for t in project.timeline.audio_tracks]

    def test_paints(self, panel):
        panel.grab()

    def test_a_fader_move_goes_live_without_an_edit(self, panel, project):
        """The move must reach the audio thread and *not* the undo stack, or a
        drag would leave sixty steps behind it."""
        track_id = list(panel.strips)[0]
        panel._lane_moved(track_id, -6.0)
        assert panel.streamer.mixer.snapshot()[0][track_id] == pytest.approx(0.501187, abs=1e-5)
        assert project.timeline.audio_tracks[0].gain_db == 0.0
        assert not project.undo.can_undo

    def test_releasing_the_fader_commits_one_edit(self, panel, project):
        track_id = list(panel.strips)[0]
        panel._lane_committed(track_id, -6.0)
        assert project.timeline.audio_tracks[0].gain_db == -6.0
        assert project.undo.undo_label == "A1 gain"
        project.undo.undo()
        assert project.timeline.audio_tracks[0].gain_db == 0.0

    def test_master_fader_commits(self, panel, project):
        panel._master_committed(-3.0)
        assert project.timeline.master_gain_db == -3.0
        assert project.undo.undo_label == "Master gain"

    def test_mute_and_solo_are_undoable(self, panel, project):
        track_id = list(panel.strips)[1]
        panel._solo(track_id, True)
        assert project.timeline.audio_tracks[1].solo
        project.undo.undo()
        assert not project.timeline.audio_tracks[1].solo

    def test_strips_are_rebuilt_only_when_the_lane_set_changes(self, panel, project):
        before = panel.strips[list(panel.strips)[0]]
        panel.refresh()
        assert panel.strips[list(panel.strips)[0]] is before, "same widgets"

        project.edit("Add audio track", lambda t: t.add_track("audio"))
        assert len(panel.strips) == 4

    def test_stopping_playback_drops_the_bars(self, panel):
        strip = panel.strips[list(panel.strips)[0]]
        strip.meter.feed(1.0, 0.0)
        panel.on_playing(False)
        assert strip.meter.ballistics.level_db == levels.METER_FLOOR_DB

    def test_a_tick_with_a_silent_streamer_is_harmless(self, panel):
        """No device, so `meter_peaks` returns None and every bar decays."""
        panel._tick()
        panel._tick()


class TestAudioPage:
    def test_shows_only_audio_lanes(self, project):
        from vedit.pages.audio_page import AudioPage
        from vedit.player.engine import PlaybackEngine

        engine = PlaybackEngine(project)
        page = AudioPage(project, engine)
        try:
            canvas = page.timeline_panel.canvas
            assert [t.name for t in canvas.lanes()] == ["A1", "A2", "A3"]
            assert canvas.fade_handles and canvas.volume_lines
            page.resize(1400, 800)
            page.grab()
        finally:
            engine.stop()

    def test_paints_with_a_mixed_clip_on_it(self, project):
        from vedit.pages.audio_page import AudioPage
        from vedit.player.engine import PlaybackEngine

        clip = Clip(
            media_id="m1", src_in=0, src_out=200, tl_start=0, src_length=400,
            kind="audio", name="music", gain_db=-6.0, fade_in=30, fade_out=40,
        )
        project.timeline.audio_tracks[0].insert(clip)

        engine = PlaybackEngine(project)
        page = AudioPage(project, engine)
        try:
            page.resize(1400, 800)
            page.grab()
        finally:
            engine.stop()


class TestCanvasLevels:
    """Gain and fade interaction on the timeline canvas."""

    @pytest.fixture
    def canvas(self, project):
        from vedit.timeline.view import AUDIO_TRACK_HEIGHT_TALL, TimelineCanvas

        clip = Clip(
            media_id="m1", src_in=0, src_out=200, tl_start=0, src_length=400,
            kind="audio", name="music",
        )
        project.timeline.audio_tracks[0].insert(clip)
        canvas = TimelineCanvas(
            project,
            kinds=("audio",),
            track_heights={"video": 70, "audio": AUDIO_TRACK_HEIGHT_TALL},
            fade_handles=True,
            volume_lines=True,
        )
        canvas.resize(1200, 400)
        return canvas

    def clip_of(self, canvas):
        return canvas.timeline.audio_tracks[0].clips[0]

    def test_unity_sits_where_the_curve_says(self, canvas):
        from PySide6.QtCore import QRectF

        rect = QRectF(0, 0, 400, 100)
        band = canvas._gain_band(rect)
        y = canvas._gain_y(0.0, rect)
        assert (band.bottom() - y) / band.height() == pytest.approx(levels.UNITY_FRACTION)

    def test_gain_geometry_round_trips(self, canvas):
        from PySide6.QtCore import QRectF

        rect = QRectF(0, 0, 400, 100)
        for db in (-40.0, -6.0, 0.0, 8.0):
            assert canvas._gain_db_at(canvas._gain_y(db, rect), rect) == pytest.approx(db, abs=0.5)

    def test_short_lanes_do_not_offer_fade_handles(self, project):
        """The Edit page's 56 px lanes must keep behaving exactly as they did:
        a corner square there would swallow most of the trim target."""
        from PySide6.QtCore import QRectF
        from vedit.timeline.view import TimelineCanvas

        clip = Clip(
            media_id="m1", src_in=0, src_out=200, tl_start=0, src_length=400, kind="audio",
        )
        project.timeline.audio_tracks[0].insert(clip)
        edit = TimelineCanvas(project)
        assert not edit._fades_grabbable(clip, QRectF(0, 0, 400, 54))

        tall = TimelineCanvas(project, fade_handles=True)
        assert tall._fades_grabbable(clip, QRectF(0, 0, 400, 112))

    def test_a_narrow_clip_does_not_offer_fade_handles(self, canvas):
        from PySide6.QtCore import QRectF

        assert not canvas._fades_grabbable(self.clip_of(canvas), QRectF(0, 0, 20, 112))

    def test_hit_zones(self, canvas):
        from PySide6.QtCore import QPoint
        from vedit.timeline.view import HEADER_WIDTH, RULER_HEIGHT, Zone

        clip = self.clip_of(canvas)
        row = canvas.row_for(clip)
        assert row is not None
        top, height = row
        rect = canvas.clip_rect(clip, top, height)

        corner = canvas.hit_test(QPoint(int(rect.left()) + 4, int(rect.top()) + 4))
        assert corner is not None and corner.zone is Zone.FADE_IN

        # Just below the corner square the trim handle must still be reachable.
        trim = canvas.hit_test(QPoint(int(rect.left()) + 2, int(rect.top()) + 40))
        assert trim is not None and trim.zone is Zone.IN

        gain_y = canvas._gain_y(clip.gain_db, rect)
        gain = canvas.hit_test(QPoint(int(rect.center().x()), int(gain_y)))
        assert gain is not None and gain.zone is Zone.GAIN

        body = canvas.hit_test(QPoint(int(rect.center().x()), int(rect.bottom()) - 4))
        assert body is not None and body.zone is Zone.BODY

        assert canvas.hit_test(QPoint(HEADER_WIDTH - 5, RULER_HEIGHT + 10)) is None

    def test_the_fade_corner_wins_over_the_trim_edge(self, canvas):
        from PySide6.QtCore import QPoint
        from vedit.timeline.view import Zone

        clip = self.clip_of(canvas)
        top, height = canvas.row_for(clip)
        rect = canvas.clip_rect(clip, top, height)
        # A point inside both targets: the fade square must claim it.
        hit = canvas.hit_test(QPoint(int(rect.left()) + 1, int(rect.top()) + 2))
        assert hit.zone is Zone.FADE_IN

    def test_menu_offers_normalise_only_for_audio(self, canvas, project):
        clip = self.clip_of(canvas)
        labels = [a.text() for a in canvas.build_clip_menu(clip).actions() if a.text()]
        assert any("Normalise" in label for label in labels)

        video = Clip(
            media_id="m1", src_in=0, src_out=50, tl_start=0, src_length=50, kind="video",
        )
        project.timeline.video_tracks[0].insert(video)
        labels = [a.text() for a in canvas.build_clip_menu(video).actions() if a.text()]
        assert not any("Normalise" in label for label in labels)

    def test_reset_and_clear_appear_only_when_there_is_something_to_reset(self, canvas):
        clip = self.clip_of(canvas)
        labels = [a.text() for a in canvas.build_clip_menu(clip).actions() if a.text()]
        assert not any("Reset Gain" in label for label in labels)
        assert not any("Clear Fades" in label for label in labels)

        clip.gain_db = -6.0
        clip.fade_in = 10
        labels = [a.text() for a in canvas.build_clip_menu(clip).actions() if a.text()]
        assert "Reset Gain (now -6.0 dB)" in labels
        assert "Clear Fades" in labels

    def test_normalise_without_peaks_says_so(self, canvas):
        messages: list[str] = []
        canvas.status_message.connect(messages.append)
        canvas._normalise([self.clip_of(canvas)])
        assert messages == ["No peak data for those clips yet"]

    def test_muting_from_the_header_is_undoable(self, canvas, project):
        from PySide6.QtCore import QPoint

        top, _height = canvas.track_rows()[0][1], canvas.track_rows()[0][2]
        canvas._toggle_header(QPoint(10, top + 5))
        assert project.timeline.audio_tracks[0].muted
        assert project.undo.undo_label == "Mute A1"
        project.undo.undo()
        assert not project.timeline.audio_tracks[0].muted

    def test_video_track_cannot_be_added_from_an_audio_only_canvas(self, canvas, project):
        labels = [
            a.text() for a in canvas.build_track_menu(project.timeline.audio_tracks[0]).actions()
        ]
        assert "Add Video Track" not in labels
        assert "Add Audio Track" in labels
