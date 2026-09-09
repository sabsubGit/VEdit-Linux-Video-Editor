"""Export presets.

Import is deliberately wide open; export is deliberately narrow. A short list of
presets that are known to work beats a wall of codec options nobody wants to
reason about — and the ones here cover delivery (H.264/HEVC MP4), editing
round-trip (ProRes, DNxHR) and web (VP9 WebM).

NVENC presets are offered only when the running ffmpeg actually has the encoder,
so the list cannot promise something that will fail at render time.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from vedit.core.ffmpeg import has_encoder


@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    name: str
    container: str            # file extension
    video_codec: str
    audio_codec: str
    description: str = ""
    quality: int = 20         # CRF / CQ; ignored by the intermediate codecs
    audio_bitrate: str = "320k"
    sample_rate: int = 48000
    width: int | None = None  # None means "use the project format"
    height: int | None = None
    extra_video: tuple[str, ...] = field(default_factory=tuple)
    requires_encoder: str | None = None

    # -- ffmpeg arguments ------------------------------------------------------

    def video_args(self) -> list[str]:
        args = ["-c:v", self.video_codec]

        if self.video_codec in ("libx264", "libx265"):
            args += ["-crf", str(self.quality), "-preset", "medium", "-pix_fmt", "yuv420p"]
        elif self.video_codec.endswith("_nvenc"):
            # Constant-quality VBR is the closest NVENC analogue of CRF.
            args += ["-rc", "vbr", "-cq", str(self.quality), "-preset", "p5", "-pix_fmt", "yuv420p"]
        elif self.video_codec == "libvpx-vp9":
            args += ["-crf", str(self.quality), "-b:v", "0", "-row-mt", "1", "-pix_fmt", "yuv420p"]

        args += list(self.extra_video)
        return args

    def audio_args(self) -> list[str]:
        args = ["-c:a", self.audio_codec, "-ar", str(self.sample_rate)]
        if self.audio_codec in ("aac", "libopus", "libmp3lame"):
            args += ["-b:a", self.audio_bitrate]
        args += ["-ac", "2"]
        return args

    def filename_for(self, stem: str) -> str:
        return f"{stem}.{self.container}"

    def with_quality(self, quality: int) -> Preset:
        return replace(self, quality=quality)

    def with_size(self, width: int | None, height: int | None) -> Preset:
        return replace(self, width=width, height=height)


ALL_PRESETS: tuple[Preset, ...] = (
    Preset(
        key="h264_mp4",
        name="H.264 · MP4",
        container="mp4",
        video_codec="libx264",
        audio_codec="aac",
        quality=20,
        description="The safe default. Plays everywhere.",
    ),
    Preset(
        key="h264_nvenc_mp4",
        name="H.264 · MP4 (NVENC)",
        container="mp4",
        video_codec="h264_nvenc",
        audio_codec="aac",
        quality=23,
        description="Same format, encoded on the GPU. Much faster, slightly larger.",
        requires_encoder="h264_nvenc",
    ),
    Preset(
        key="hevc_mp4",
        name="HEVC · MP4",
        container="mp4",
        video_codec="libx265",
        audio_codec="aac",
        quality=24,
        description="Smaller files at the same quality; slower, and less portable.",
    ),
    Preset(
        key="hevc_nvenc_mp4",
        name="HEVC · MP4 (NVENC)",
        container="mp4",
        video_codec="hevc_nvenc",
        audio_codec="aac",
        quality=26,
        description="GPU HEVC.",
        requires_encoder="hevc_nvenc",
    ),
    Preset(
        key="prores_mov",
        name="ProRes 422 · MOV",
        container="mov",
        video_codec="prores_ks",
        audio_codec="pcm_s16le",
        description="Edit-friendly intermediate. Large files, near-lossless.",
        extra_video=("-profile:v", "2", "-vendor", "apl0", "-pix_fmt", "yuv422p10le"),
    ),
    Preset(
        key="dnxhr_mov",
        name="DNxHR HQ · MOV",
        container="mov",
        video_codec="dnxhd",
        audio_codec="pcm_s16le",
        description="Avid-compatible intermediate.",
        extra_video=("-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"),
    ),
    Preset(
        key="vp9_webm",
        name="VP9 · WebM",
        container="webm",
        video_codec="libvpx-vp9",
        audio_codec="libopus",
        quality=31,
        description="For the web. Slow to encode.",
    ),
)


def available_presets() -> list[Preset]:
    """Presets this ffmpeg build can actually deliver."""
    return [
        preset
        for preset in ALL_PRESETS
        if preset.requires_encoder is None or has_encoder(preset.requires_encoder)
    ]


# -- resolutions ---------------------------------------------------------------
# The sizes people actually deliver. Anything else is still reachable by typing,
# but nobody should have to remember that 1440p is 2560 wide.


@dataclass(frozen=True, slots=True)
class Resolution:
    name: str
    width: int
    height: int

    @property
    def label(self) -> str:
        return f"{self.name} — {self.width}×{self.height}"


RESOLUTIONS: tuple[Resolution, ...] = (
    Resolution("4K UHD", 3840, 2160),
    Resolution("1440p QHD", 2560, 1440),
    Resolution("1080p Full HD", 1920, 1080),
    Resolution("720p HD", 1280, 720),
    Resolution("480p SD", 854, 480),
    Resolution("Vertical 9:16", 1080, 1920),
    Resolution("Square 1:1", 1080, 1080),
)


def resolution_for(width: int, height: int) -> Resolution | None:
    """The named resolution matching these dimensions, if there is one."""
    for resolution in RESOLUTIONS:
        if (resolution.width, resolution.height) == (width, height):
            return resolution
    return None


# -- quality -------------------------------------------------------------------
# CRF is a fine dial for someone who already knows what 23 means, and noise to
# everyone else — it is backwards (lower is better), its useful range differs per
# codec, and the same number means different things to x264 and VP9. So the UI
# offers four named levels and each codec family says what number it wants.


@dataclass(frozen=True, slots=True)
class QualityLevel:
    key: str
    name: str
    description: str
    # CRF (or NVENC CQ) per codec family, and roughly how many bits per pixel the
    # result costs — the basis of the size estimate shown next to the choice.
    crf: dict[str, int]
    bits_per_pixel: float


QUALITY_LEVELS: tuple[QualityLevel, ...] = (
    QualityLevel(
        key="draft",
        name="Draft — smallest file",
        description="Visibly soft on detailed shots. For a quick check or a rough cut.",
        crf={"h264": 28, "hevc": 32, "vp9": 40},
        bits_per_pixel=0.030,
    ),
    QualityLevel(
        key="standard",
        name="Standard — good for sharing",
        description="What most uploads and messaging apps end up at anyway.",
        crf={"h264": 23, "hevc": 27, "vp9": 34},
        bits_per_pixel=0.070,
    ),
    QualityLevel(
        key="high",
        name="High — recommended",
        description="Indistinguishable from the source on ordinary footage.",
        crf={"h264": 19, "hevc": 23, "vp9": 30},
        bits_per_pixel=0.130,
    ),
    QualityLevel(
        key="maximum",
        name="Maximum — near-lossless",
        description="For archiving or further grading. Files get large.",
        crf={"h264": 15, "hevc": 19, "vp9": 24},
        bits_per_pixel=0.250,
    ),
)

DEFAULT_QUALITY = "high"

# NVENC's CQ scale tracks CRF but runs a little hot: the same number looks worse
# than x264's, so every level is nudged up the scale.
NVENC_OFFSET = 3

# Fixed data rates, in bits per pixel, for the codecs that ignore CRF entirely.
INTERMEDIATE_BPP = {"prores_ks": 2.4, "dnxhd": 2.0}

_FAMILIES = {
    "libx264": "h264",
    "h264_nvenc": "h264",
    "libx265": "hevc",
    "hevc_nvenc": "hevc",
    "libvpx-vp9": "vp9",
}


def quality_level(key: str) -> QualityLevel:
    for level in QUALITY_LEVELS:
        if level.key == key:
            return level
    return quality_level(DEFAULT_QUALITY)


def is_adjustable(preset: Preset) -> bool:
    """False for the intermediates, whose rate is set by the profile, not by us."""
    return preset.video_codec in _FAMILIES


def crf_for(preset: Preset, level: QualityLevel) -> int:
    """The encoder number this preset wants for a named quality level."""
    family = _FAMILIES.get(preset.video_codec)
    if family is None:
        return preset.quality
    value = level.crf[family]
    return value + NVENC_OFFSET if preset.video_codec.endswith("_nvenc") else value


def estimated_size(
    preset: Preset,
    level: QualityLevel,
    width: int,
    height: int,
    fps: float,
    seconds: float,
) -> int:
    """Roughly how big the file will be, in bytes.

    A single number people can sanity-check a choice against, not a promise: real
    size swings with how much the picture moves. Deliberately derived from the
    same bits-per-pixel figures the levels are described by, so the estimate and
    the wording can never drift apart.
    """
    bpp = INTERMEDIATE_BPP.get(preset.video_codec, level.bits_per_pixel)
    video_bits = bpp * width * height * fps
    audio_bits = (
        _bitrate_bits(preset.audio_bitrate)
        if preset.audio_codec in ("aac", "libopus", "libmp3lame")
        else preset.sample_rate * 16 * 2
    )
    return int((video_bits + audio_bits) * max(0.0, seconds) / 8)


def _bitrate_bits(value: str) -> int:
    text = value.strip().lower()
    if text.endswith("k"):
        return int(float(text[:-1]) * 1000)
    if text.endswith("m"):
        return int(float(text[:-1]) * 1_000_000)
    return int(float(text))


def format_size(size_bytes: int) -> str:
    """Human file size, at the precision the estimate actually justifies."""
    megabytes = size_bytes / 1_000_000
    if megabytes >= 1000:
        return f"{megabytes / 1000:.1f} GB"
    if megabytes >= 10:
        return f"{megabytes:.0f} MB"
    return f"{megabytes:.1f} MB"


def preset_by_key(key: str) -> Preset | None:
    for preset in ALL_PRESETS:
        if preset.key == key:
            return preset
    return None


def default_preset() -> Preset:
    return ALL_PRESETS[0]
