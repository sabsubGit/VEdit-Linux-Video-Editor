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


def preset_by_key(key: str) -> Preset | None:
    for preset in ALL_PRESETS:
        if preset.key == key:
            return preset
    return None


def default_preset() -> Preset:
    return ALL_PRESETS[0]
