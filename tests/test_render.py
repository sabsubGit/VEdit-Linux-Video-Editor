"""Render graph and preset tests.

These assert on the generated ffmpeg arguments rather than running ffmpeg, so
they stay fast. The end-to-end check that the command actually produces a correct
file lives in `tests/test_smoke.py`.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from vedit.core.timebase import TimeBase
from vedit.render.graph import RenderError, build_command
from vedit.render.presets import (
    ALL_PRESETS,
    DEFAULT_QUALITY,
    NVENC_OFFSET,
    QUALITY_LEVELS,
    RESOLUTIONS,
    available_presets,
    crf_for,
    estimated_size,
    format_size,
    is_adjustable,
    preset_by_key,
    quality_level,
    resolution_for,
)
from vedit.timeline.model import Clip, Timeline


class FakeInfo:
    def __init__(self, media_id, path):
        self.media_id = media_id
        self.path = Path(path)


class FakePool:
    def __init__(self, mapping):
        self._mapping = mapping

    def info_for(self, media_id):
        return self._mapping.get(media_id)


@pytest.fixture
def pool():
    return FakePool({"m1": FakeInfo("m1", "/media/a.mp4"), "m2": FakeInfo("m2", "/media/b.mov")})


@pytest.fixture
def timeline():
    return Timeline.default(TimeBase(30))


def add(timeline, media_id, tl_start, length, *, src_in=0, kind="video", track=None):
    clip = Clip(
        media_id=media_id,
        src_in=src_in,
        src_out=src_in + length,
        tl_start=tl_start,
        src_length=src_in + length + 500,
        kind=kind,
    )
    # `is None`, not `or`: Track defines __len__, so an empty lane is falsy and
    # `track or ...` would silently drop the clip on A1 instead.
    lane = timeline.lane_for(kind) if track is None else track
    lane.insert(clip)
    return clip


def filter_of(args):
    return args[args.index("-filter_complex") + 1]


class TestBuildCommand:
    def test_empty_timeline_is_refused(self, timeline, pool):
        with pytest.raises(RenderError, match="empty"):
            build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))

    def test_missing_media_is_refused(self, timeline):
        add(timeline, "gone", 0, 30)
        with pytest.raises(RenderError, match="media"):
            build_command(timeline, FakePool({}), preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))

    def test_one_input_per_source(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        add(timeline, "m1", 30, 30, src_in=100)   # same file again
        add(timeline, "m2", 60, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert args.count("-i") == 2, "a file used twice is still opened once"

    def test_trim_times_come_from_the_source_window(self, timeline, pool):
        add(timeline, "m1", 0, 30, src_in=60)     # 2s in, 1s long at 30fps
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "trim=start=2.000000:end=3.000000" in graph

    def test_every_segment_is_normalised_before_concat(self, timeline, pool):
        """The reason mixed formats can be cut together: concat demands identical
        inputs, so scale and fps are forced per segment beforehand."""
        add(timeline, "m1", 0, 30)
        add(timeline, "m2", 30, 30)
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert graph.count("scale=1920:1080") == 2
        assert graph.count("fps=30/1") == 2
        assert graph.count("setsar=1") == 2
        assert "concat=n=2:v=1:a=0" in graph

    def test_aspect_is_padded_not_stretched(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "force_original_aspect_ratio=decrease" in graph
        assert "pad=1920:1080" in graph

    def test_gaps_become_black_and_silence(self, timeline, pool):
        add(timeline, "m1", 60, 30)               # starts 2s in, leaving a gap
        add(timeline, "m1", 60, 30, kind="audio")
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "color=c=black:s=1920x1080" in graph
        assert "d=2.000000" in graph
        assert "anullsrc" in graph

    def test_trailing_gap_is_filled(self, timeline, pool):
        """Video ends at 1s but audio runs to 2s; the video track must be padded
        or the concat would produce a stream shorter than the timeline."""
        add(timeline, "m1", 0, 30)
        add(timeline, "m1", 0, 60, kind="audio")
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "color=c=black" in graph
        assert "concat=n=2:v=1:a=0" in graph

    def test_video_only_timeline_disables_audio(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert "-an" in args
        assert "[aout]" not in args

    def test_audio_is_mapped_when_present(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        add(timeline, "m1", 0, 30, kind="audio")
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert "[aout]" in args
        assert "-an" not in args

    def test_progress_is_requested_on_stdout(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        assert args[args.index("-progress") + 1] == "pipe:1"

    def test_faststart_only_for_mp4(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        mp4 = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4"))
        mov = build_command(timeline, pool, preset_by_key("prores_mov"), Path("/tmp/o.mov"))
        assert "+faststart" in mp4
        assert "+faststart" not in mov

    def test_preset_resolution_overrides_the_timeline(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        preset = preset_by_key("h264_mp4").with_size(1280, 720)
        graph = filter_of(build_command(timeline, pool, preset, Path("/tmp/o.mp4")))
        assert "scale=1280:720" in graph
        assert "1920" not in graph

    def test_fractional_rate_is_written_exactly(self, pool):
        timeline = Timeline.default(TimeBase(Fraction(30000, 1001)))
        add(timeline, "m1", 0, 30)
        graph = filter_of(build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/o.mp4")))
        assert "fps=30000/1001" in graph, "must not be flattened to 29.97"

    def test_output_path_is_last(self, timeline, pool):
        add(timeline, "m1", 0, 30)
        args = build_command(timeline, pool, preset_by_key("h264_mp4"), Path("/tmp/out.mp4"))
        assert args[-1] == "/tmp/out.mp4"


class TestPresets:
    def test_all_presets_have_unique_keys(self):
        keys = [preset.key for preset in ALL_PRESETS]
        assert len(keys) == len(set(keys))

    def test_available_presets_are_a_subset(self):
        assert set(available_presets()).issubset(set(ALL_PRESETS))

    def test_software_presets_are_always_available(self):
        """Presets with no encoder requirement must never be filtered out."""
        keys = {preset.key for preset in available_presets()}
        assert {"h264_mp4", "hevc_mp4", "prores_mov"} <= keys

    def test_x264_uses_crf(self):
        args = preset_by_key("h264_mp4").with_quality(18).video_args()
        assert args[args.index("-crf") + 1] == "18"

    def test_nvenc_uses_cq_not_crf(self):
        args = preset_by_key("h264_nvenc_mp4").with_quality(25).video_args()
        assert "-crf" not in args, "NVENC has no CRF; it would be silently ignored"
        assert args[args.index("-cq") + 1] == "25"

    def test_intermediates_carry_their_profile(self):
        assert "-profile:v" in preset_by_key("prores_mov").video_args()
        assert "dnxhr_hq" in preset_by_key("dnxhr_mov").video_args()

    def test_pcm_audio_gets_no_bitrate(self):
        args = preset_by_key("prores_mov").audio_args()
        assert "-b:a" not in args, "a bitrate is meaningless for uncompressed PCM"

    def test_lossy_audio_gets_a_bitrate(self):
        assert "-b:a" in preset_by_key("h264_mp4").audio_args()

    def test_filename_uses_the_container(self):
        assert preset_by_key("h264_mp4").filename_for("cut") == "cut.mp4"
        assert preset_by_key("prores_mov").filename_for("cut") == "cut.mov"


class TestLevels:
    """Gain, fades, solo and master in the filter graph.

    The first test is the important one: everything below only fires when it has
    something to say, so an unmixed timeline still renders the command it always
    did and every assertion elsewhere in this file stays honest.
    """

    def preset(self):
        return preset_by_key("h264_mp4")

    def build(self, timeline, pool):
        return filter_of(build_command(timeline, pool, self.preset(), Path("/out.mp4")))

    def test_an_unmixed_timeline_emits_nothing(self, timeline, pool):
        add(timeline, "m1", 0, 60)
        add(timeline, "m1", 0, 60, kind="audio")
        graph = self.build(timeline, pool)
        assert "volume=" not in graph
        assert "afade" not in graph
        assert "aout_pre" not in graph

    def test_clip_gain_becomes_a_volume_filter(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.gain_db = 6.0
        assert "volume=1.995262" in self.build(timeline, pool)

    def test_a_negative_clip_gain_attenuates(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.gain_db = -6.0
        assert "volume=0.501187" in self.build(timeline, pool)

    def test_fade_in_starts_at_the_pieces_own_zero(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.fade_in = 15                       # half a second at 30 fps
        assert "afade=t=in:st=0:d=0.500000" in self.build(timeline, pool)

    def test_fade_out_starts_a_fade_before_the_end(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.fade_out = 30                      # one second of a two-second clip
        assert "afade=t=out:st=1.000000:d=1.000000" in self.build(timeline, pool)

    def test_both_fades_on_one_piece(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.fade_in, clip.fade_out = 15, 15
        graph = self.build(timeline, pool)
        assert "afade=t=in:st=0:d=0.500000" in graph
        assert "afade=t=out:st=1.500000:d=0.500000" in graph

    def test_video_clips_never_carry_levels(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60)
        clip.gain_db = 6.0
        clip.fade_in = 15
        graph = self.build(timeline, pool)
        assert "volume=" not in graph
        assert "afade" not in graph

    def test_track_gain_lands_after_the_lane_concat(self, timeline, pool):
        add(timeline, "m1", 0, 60, kind="audio")
        timeline.audio_tracks[0].gain_db = -6.0
        graph = self.build(timeline, pool)
        assert "[alane0]volume=0.501187[alane0g]" in graph
        assert "[alane0g]anull[aout]" in graph

    def test_master_gain_lands_after_the_mix(self, timeline, pool):
        add(timeline, "m1", 0, 60, kind="audio")
        timeline.master_gain_db = -6.0
        graph = self.build(timeline, pool)
        assert "[aout_pre]" in graph
        assert "[aout_pre]volume=0.501187[aout]" in graph

    def test_master_gain_after_a_real_mix(self, timeline, pool):
        add(timeline, "m1", 0, 60, kind="audio", track=timeline.audio_tracks[0])
        add(timeline, "m2", 0, 60, kind="audio", track=timeline.audio_tracks[1])
        timeline.master_gain_db = 3.0
        graph = self.build(timeline, pool)
        assert "amix=inputs=2" in graph and "[aout_pre]" in graph
        assert graph.endswith("volume=1.412538[aout]")

    def test_a_muted_lane_is_left_out(self, timeline, pool):
        add(timeline, "m1", 0, 60, kind="audio", track=timeline.audio_tracks[0])
        add(timeline, "m2", 0, 60, kind="audio", track=timeline.audio_tracks[1])
        timeline.audio_tracks[1].muted = True
        graph = self.build(timeline, pool)
        assert "amix" not in graph
        assert "alane1" not in graph

    def test_solo_silences_the_others(self, timeline, pool):
        """The line that keeps preview and export agreeing about solo."""
        add(timeline, "m1", 0, 60, kind="audio", track=timeline.audio_tracks[0])
        add(timeline, "m2", 0, 60, kind="audio", track=timeline.audio_tracks[1])
        timeline.audio_tracks[1].solo = True
        graph = self.build(timeline, pool)
        assert "amix" not in graph
        assert "[1:a]" in graph and "[0:a]" not in graph

    def test_a_razored_half_does_not_restart_its_fade(self, timeline, pool):
        """Only the piece covering the clip's own edge carries the fade."""
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.fade_in = 15
        add(timeline, "m2", 60, 60, kind="audio")
        graph = self.build(timeline, pool)
        assert graph.count("afade=t=in") == 1


class TestQualityLevels:
    """The named levels that replaced the CRF slider.

    CRF is not one scale: 23 is a sensible x264 default, a soft x265 one and
    nearly unusable in VP9. The levels exist so the UI can ask for "high" and let
    each codec say what number that is.
    """

    def level(self, key):
        return quality_level(key)

    def test_every_level_covers_every_codec_family(self):
        for level in QUALITY_LEVELS:
            for preset in ALL_PRESETS:
                if is_adjustable(preset):
                    assert isinstance(crf_for(preset, level), int)

    def test_better_levels_mean_lower_crf(self):
        preset = preset_by_key("h264_mp4")
        numbers = [crf_for(preset, level) for level in QUALITY_LEVELS]
        assert numbers == sorted(numbers, reverse=True), (
            "CRF runs backwards: a better level must ask for a smaller number"
        )

    def test_codecs_get_their_own_numbers(self):
        level = self.level("high")
        assert crf_for(preset_by_key("h264_mp4"), level) < crf_for(
            preset_by_key("hevc_mp4"), level
        ) < crf_for(preset_by_key("vp9_webm"), level)

    def test_nvenc_is_offset_from_its_software_twin(self):
        level = self.level("standard")
        assert crf_for(preset_by_key("h264_nvenc_mp4"), level) == (
            crf_for(preset_by_key("h264_mp4"), level) + NVENC_OFFSET
        )

    def test_intermediates_keep_their_own_quality(self):
        """ProRes and DNxHR encode at the rate their profile dictates; a level
        must not quietly rewrite it."""
        for key in ("prores_mov", "dnxhr_mov"):
            preset = preset_by_key(key)
            assert not is_adjustable(preset)
            assert crf_for(preset, self.level("draft")) == preset.quality

    def test_an_unknown_key_falls_back_to_the_default(self):
        assert quality_level("nonsense").key == DEFAULT_QUALITY

    def test_the_chosen_crf_reaches_the_encoder(self):
        preset = preset_by_key("h264_mp4")
        level = self.level("maximum")
        args = preset.with_quality(crf_for(preset, level)).video_args()
        assert args[args.index("-crf") + 1] == str(crf_for(preset, level))


class TestSizeEstimate:
    def estimate(self, key, level="high", width=1920, height=1080, fps=30, seconds=60):
        return estimated_size(
            preset_by_key(key), quality_level(level), width, height, fps, seconds
        )

    def test_h264_1080p_is_in_the_right_ballpark(self):
        """A minute of 1080p30 at the recommended level is single-digit megabits
        per second — if this drifts into 100 MB/s the figures are wrong."""
        megabytes = self.estimate("h264_mp4") / 1_000_000
        assert 40 < megabytes < 100

    def test_prores_is_the_data_rate_prores_actually_uses(self):
        megabits_per_second = self.estimate("prores_mov") * 8 / 60 / 1_000_000
        assert 120 < megabits_per_second < 175

    def test_more_pixels_cost_more(self):
        assert self.estimate("h264_mp4", width=3840, height=2160) > self.estimate(
            "h264_mp4", width=1280, height=720
        )

    def test_better_quality_costs_more(self):
        assert self.estimate("h264_mp4", level="maximum") > self.estimate(
            "h264_mp4", level="draft"
        )

    def test_it_scales_with_duration(self):
        one = self.estimate("h264_mp4", seconds=10)
        two = self.estimate("h264_mp4", seconds=20)
        assert two == pytest.approx(one * 2, rel=0.01)

    def test_an_empty_timeline_estimates_nothing(self):
        assert self.estimate("h264_mp4", seconds=0) == 0

    def test_uncompressed_audio_counts_for_more_than_aac(self):
        """PCM is 1.5 Mbps a stereo pair; quoting it as 320k would understate a
        ProRes export by most of a gigabyte an hour."""
        video_only = estimated_size(
            preset_by_key("prores_mov"), quality_level("high"), 16, 16, 1, 60
        )
        assert video_only > 60 * 48000 * 16 * 2 / 8

    def test_formatting_switches_units_where_a_person_would(self):
        assert format_size(2_400_000) == "2.4 MB"
        assert format_size(240_000_000) == "240 MB"
        assert format_size(2_400_000_000) == "2.4 GB"


class TestResolutions:
    def test_the_common_ones_are_offered(self):
        sizes = {(r.width, r.height) for r in RESOLUTIONS}
        assert {(3840, 2160), (1920, 1080), (1280, 720)} <= sizes

    def test_every_offered_size_is_encodable(self):
        """Odd dimensions fail outright under yuv420p chroma subsampling."""
        for resolution in RESOLUTIONS:
            assert resolution.width % 2 == 0 and resolution.height % 2 == 0

    def test_a_size_can_be_recognised_by_its_numbers(self):
        assert resolution_for(1920, 1080).name == "1080p Full HD"
        assert resolution_for(1234, 567) is None

    def test_labels_carry_the_numbers(self):
        assert "3840×2160" in resolution_for(3840, 2160).label


class TestExportSettingsWidget:
    """The panel itself: the menus have to stay honest about what will be sent
    to ffmpeg, because that is the only thing the user sees before rendering."""

    @pytest.fixture
    def settings(self, qt_app):
        from vedit.core.project import Project
        from vedit.pages.render_page import ExportSettings

        project = Project()
        widget = ExportSettings(project)
        yield widget
        widget.deleteLater()

    def select_resolution(self, settings, text_fragment):
        box = settings.resolution_box
        for index in range(box.count()):
            if text_fragment in box.itemText(index):
                box.setCurrentIndex(index)
                return
        raise AssertionError(f"no resolution offering {text_fragment!r}")

    def test_it_opens_matching_the_timeline(self, settings):
        assert settings.size() == (
            settings.project.timeline.width,
            settings.project.timeline.height,
        )

    def test_choosing_a_resolution_sets_the_boxes(self, settings):
        self.select_resolution(settings, "720p")
        assert settings.size() == (1280, 720)
        assert settings.current_preset().width == 1280

    def test_typing_a_known_size_selects_that_entry(self, settings):
        settings.width_box.setValue(3840)
        settings.height_box.setValue(2160)
        assert "4K UHD" in settings.resolution_box.currentText()

    def test_typing_an_unknown_size_reads_as_custom(self, settings):
        settings.width_box.setValue(1234)
        settings.height_box.setValue(566)
        assert settings.resolution_box.currentData() == "custom"
        assert settings.size() == (1234, 566)

    def test_an_odd_size_is_rounded_before_it_reaches_ffmpeg(self, settings):
        settings.width_box.setValue(1235)
        assert settings.current_preset().width % 2 == 0

    def test_match_timeline_says_what_it_means(self, settings):
        assert "1920×1080" in settings.resolution_box.itemText(0)

    def test_match_timeline_follows_the_timeline(self, settings):
        """Appending the first 4K clip changes the project format underneath."""
        settings.project.timeline.width = 3840
        settings.project.timeline.height = 2160
        settings.project.timeline_changed.emit()
        assert settings.size() == (3840, 2160)

    def test_a_chosen_resolution_does_not_follow_the_timeline(self, settings):
        self.select_resolution(settings, "720p")
        settings.project.timeline.width = 3840
        settings.project.timeline.height = 2160
        settings.project.timeline_changed.emit()
        assert settings.size() == (1280, 720), "an explicit choice must stick"

    def test_the_quality_choice_reaches_the_preset(self, settings):
        settings.quality_box.setCurrentIndex(0)      # draft
        draft = settings.current_preset().quality
        settings.quality_box.setCurrentIndex(settings.quality_box.count() - 1)
        assert settings.current_preset().quality < draft

    def test_the_note_quotes_a_size_and_the_resolution(self, settings):
        self.select_resolution(settings, "720p")
        note = settings.quality_note.text()
        assert "1280×720" in note and ("MB" in note or "GB" in note)

    def test_the_estimate_grows_with_the_resolution(self, settings):
        def megabytes(fragment):
            self.select_resolution(settings, fragment)
            return settings.quality_note.text()

        assert megabytes("720p") != megabytes("4K UHD")

    def test_quality_is_disabled_for_the_intermediates(self, settings):
        for index in range(settings.preset_box.count()):
            if settings.preset_box.itemData(index) == "prores_mov":
                settings.preset_box.setCurrentIndex(index)
                break
        assert not settings.quality_box.isEnabled()
        assert "ProRes" in settings.quality_note.text()


class TestMutedClips:
    """A muted clip renders as silence of its own length — not as a shorter
    lane, and not as a decoded clip multiplied by zero."""

    def test_the_lane_holds_silence_where_the_clip_was(self, timeline, pool):
        add(timeline, "m1", 0, 60)
        muted = add(timeline, "m1", 0, 60, kind="audio")
        muted.muted = True

        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert "anullsrc" in chain
        assert "atrim" not in chain, "nothing is decoded for a muted clip"

    def test_the_neighbouring_clip_still_plays(self, timeline, pool):
        add(timeline, "m1", 0, 120)
        lane = timeline.audio_tracks[0]
        add(timeline, "m1", 0, 60, kind="audio", track=lane)
        muted = add(timeline, "m1", 60, 60, src_in=60, kind="audio", track=lane)
        muted.muted = True

        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert chain.count("atrim") == 1
        assert "anullsrc=r=48000:cl=stereo:d=2.000000" in chain

    def test_an_entirely_muted_timeline_exports_without_an_audio_stream(self, timeline, pool):
        add(timeline, "m1", 0, 60)
        muted = add(timeline, "m1", 0, 60, kind="audio")
        muted.muted = True

        args = build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4"))
        assert "-an" in args and "[aout]" not in args


class TestReversedClips:
    def test_video_is_reversed_after_the_trim(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60)
        clip.reversed = True

        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert "reverse,setpts=PTS-STARTPTS" in chain
        assert chain.index("trim=start=") < chain.index("reverse,")

    def test_the_trim_still_reads_the_same_window(self, timeline, pool):
        """`trim` only understands ascending times: reversing is a filter, not a
        backwards trim, so the span asked for is unchanged."""
        forward = add(timeline, "m1", 0, 60, src_in=30)
        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        window = "trim=start=1.000000:end=3.000000"
        assert window in chain

        forward.reversed = True
        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert window in chain

    def test_audio_is_reversed_before_its_fades(self, timeline, pool):
        add(timeline, "m1", 0, 60)
        clip = add(timeline, "m1", 0, 60, kind="audio")
        clip.reversed = True
        clip.fade_in = 15

        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert "areverse" in chain
        assert chain.index("areverse") < chain.index("afade=t=in"), (
            "a fade in belongs to the start of the clip as it is heard"
        )

    def test_a_forward_timeline_is_untouched(self, timeline, pool):
        add(timeline, "m1", 0, 60)
        add(timeline, "m1", 0, 60, kind="audio")

        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert "reverse" not in chain

    def test_reverse_and_speed_compose(self, timeline, pool):
        clip = add(timeline, "m1", 0, 60)
        clip.reversed = True
        clip.speed = 2.0

        chain = filter_of(build_command(timeline, pool, ALL_PRESETS[0], Path("/out.mp4")))
        assert "setpts=(PTS-STARTPTS)/2.000000,reverse" in chain

