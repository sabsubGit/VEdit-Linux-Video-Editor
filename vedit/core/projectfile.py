"""Saving and loading `.vedit` project files.

Plain JSON, and deliberately readable: a project is small, and being able to open
one in a text editor to see what an edit actually did is worth more than a compact
binary format.

Media is referenced by absolute path. Paths move, so loading reports which files
are missing rather than refusing to open — the timeline is still valid, and the
user can relink.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vedit.core.timebase import TimeBase
from vedit.media.probe import MediaInfo, UnsupportedMedia, probe
from vedit.timeline.framing import Framing, Region
from vedit.timeline.titles import DEFAULT_SIZE, Title
from vedit.timeline.model import Clip, Timeline, TimelineError, Track  # noqa: F401

# Version 2 added mixing: per-clip gain and fades, per-track gain and solo, and
# a master gain. Every one of them defaults to "as it was", so a version 1 file
# loads unchanged — the bump exists for the other direction. `load_project`
# refuses anything newer than it understands, and without the bump an older
# build would open a v2 project, silently drop every gain and fade, and write
# them away on the next save.
# Version 3 added per-clip mute and reverse. Same reasoning as the last bump:
# both default to "as it was", so a version 1 or 2 file loads unchanged, and the
# bump is what stops an older build silently dropping a reversal or a mute and
# writing the loss back out on the next save.
# Version 4 added the picture: per-clip framing (zoom and pan), a framing the
# clip travels to, rotation and flip. Same reasoning again — an untouched clip
# writes an identity framing, no move, a zero rotation and no flip, so every
# older file loads unchanged, and the bump is what stops a build without this
# feature opening a reframed project, showing it uncropped, and saving the
# framing away. It also added the cross dissolve, which is stored on the
# incoming clip as a length in frames and defaults to zero, and titles — clips
# that carry words instead of media, and so have no `media_id` to relink.
# Version 5 added the zoom region: a punch-in covering part of a clip rather
# than all of it, with a ramp at each end. Absent means no region, which is what
# every version 4 clip means, so those load unchanged; the bump is what stops a
# build without the feature opening a project, playing it un-punched, and saving
# the region away.
FORMAT_VERSION = 5
SUFFIX = ".vedit"


class ProjectFileError(Exception):
    """The file is not a project we can open."""


@dataclass(slots=True)
class LoadResult:
    timeline: Timeline
    media: list[MediaInfo]
    missing: list[str]          # paths that could not be opened
    playhead: int


# -- writing ------------------------------------------------------------------


def _clip_to_dict(clip: Clip) -> dict:
    return {
        "media_id": clip.media_id,
        "src_in": clip.src_in,
        "src_out": clip.src_out,
        "tl_start": clip.tl_start,
        "src_length": clip.src_length,
        "kind": clip.kind,
        "speed": clip.speed,
        "link_id": clip.link_id,
        "name": clip.name,
        "enabled": clip.enabled,
        "reversed": clip.reversed,
        "muted": clip.muted,
        "gain_db": clip.gain_db,
        "fade_in": clip.fade_in,
        "fade_out": clip.fade_out,
        # Nested rather than three flat keys: framing is one value everywhere
        # else in the app, and splitting it here would make this the one place
        # it could be half-written.
        "framing": {
            "zoom": clip.framing.zoom,
            "x": clip.framing.x,
            "y": clip.framing.y,
        },
        # Absent rather than null when the framing does not move, so a static
        # clip's entry looks the way it always did.
        **(
            {
                "framing_end": {
                    "zoom": clip.framing_end.zoom,
                    "x": clip.framing_end.x,
                    "y": clip.framing_end.y,
                }
            }
            if clip.framing_end is not None
            else {}
        ),
        # Absent when there is none, for the same reason `framing_end` is.
        **(
            {
                "zoom": {
                    "framing": {
                        "zoom": clip.zoom.framing.zoom,
                        "x": clip.zoom.framing.x,
                        "y": clip.zoom.framing.y,
                    },
                    "start": clip.zoom.start,
                    "end": clip.zoom.end,
                    "ramp_in": clip.zoom.ramp_in,
                    "ramp_out": clip.zoom.ramp_out,
                }
            }
            if clip.zoom is not None
            else {}
        ),
        "rotation": clip.rotation,
        "flipped": clip.flipped,
        "dissolve_in": clip.dissolve_in,
        **(
            {
                "title": {
                    "text": clip.title.text,
                    "size": clip.title.size,
                    "position": clip.title.position,
                    "align": clip.title.align,
                    "colour": clip.title.colour,
                    "shadow": clip.title.shadow,
                    "offset_x": clip.title.offset_x,
                    "offset_y": clip.title.offset_y,
                }
            }
            if clip.title is not None
            else {}
        ),
    }


def _title_from(raw) -> Title | None:
    """Read a stored title, or None for an ordinary clip.

    `Title` rejects an unknown size or position with a `ValueError`, which the
    loader already treats as "drop this clip and keep the project".
    """
    if not isinstance(raw, dict):
        return None
    return Title(
        text=str(raw.get("text", "")),
        size=str(raw.get("size", DEFAULT_SIZE)),
        position=str(raw.get("position", "centre")),
        align=str(raw.get("align", "centre")),
        colour=str(raw.get("colour", "#ffffff")),
        shadow=bool(raw.get("shadow", True)),
        offset_x=float(raw.get("offset_x", 0.0)),
        offset_y=float(raw.get("offset_y", 0.0)),
    )


def _framing_from(raw) -> Framing:
    """Read a stored framing, defaulting to "as it was" for an older file.

    `Framing` rejects an out-of-range value with a `ValueError`, which the
    loader already treats as "drop this clip and keep the project" — so a
    corrupt zoom costs one clip rather than the whole file.
    """
    if not isinstance(raw, dict):
        return Framing()
    return Framing(
        zoom=float(raw.get("zoom", 1.0)),
        x=float(raw.get("x", 0.0)),
        y=float(raw.get("y", 0.0)),
    )


def _region_from(raw) -> Region | None:
    """Read a stored zoom region, or None for a file written without one.

    `Region` rejects a nonsensical span the way `Framing` rejects a bad zoom,
    and the loader treats that the same way: one clip is dropped, the project
    still opens.
    """
    if not isinstance(raw, dict):
        return None
    return Region(
        framing=_framing_from(raw.get("framing")),
        start=int(raw.get("start", 0)),
        end=int(raw.get("end", 1)),
        ramp_in=int(raw.get("ramp_in", 0)),
        ramp_out=int(raw.get("ramp_out", 0)),
    )


def project_to_dict(timeline: Timeline, media: list[MediaInfo], playhead: int = 0) -> dict:
    return {
        "format": "vedit-project",
        "version": FORMAT_VERSION,
        "timebase": {
            "numerator": timeline.timebase.fps.numerator,
            "denominator": timeline.timebase.fps.denominator,
        },
        "width": timeline.width,
        "height": timeline.height,
        "sample_rate": timeline.sample_rate,
        "master_gain_db": timeline.master_gain_db,
        "playhead": playhead,
        # Only media actually used on the timeline is written; an unused pool
        # entry is a UI convenience, not part of the edit.
        "media": [
            {"media_id": info.media_id, "path": str(info.path), "name": info.name}
            for info in media
        ],
        "tracks": [
            {
                "kind": track.kind,
                "name": track.name,
                "muted": track.muted,
                "locked": track.locked,
                "gain_db": track.gain_db,
                "solo": track.solo,
                "clips": [_clip_to_dict(clip) for clip in track.clips],
            }
            for track in timeline.tracks
        ],
    }


def save_project(path: Path, timeline: Timeline, media: list[MediaInfo], playhead: int = 0) -> Path:
    """Write atomically, so an interrupted save cannot destroy the old file."""
    path = Path(path)
    if path.suffix != SUFFIX:
        path = path.with_suffix(SUFFIX)

    payload = project_to_dict(timeline, media, playhead)
    temporary = path.with_name(f"{path.stem}.part{path.suffix}")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


# -- reading ------------------------------------------------------------------


def load_project(path: Path) -> LoadResult:
    path = Path(path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectFileError(f"{path.name} could not be read: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("format") != "vedit-project":
        raise ProjectFileError(f"{path.name} is not a vedit project")

    version = payload.get("version", 0)
    if version > FORMAT_VERSION:
        raise ProjectFileError(
            f"{path.name} was written by a newer version of vedit "
            f"(format {version}, this build understands {FORMAT_VERSION})"
        )

    rate = payload.get("timebase") or {}
    try:
        fps = Fraction(int(rate.get("numerator", 30)), int(rate.get("denominator", 1)))
        timebase = TimeBase(fps)
    except (ValueError, ZeroDivisionError) as exc:
        raise ProjectFileError(f"{path.name} has an invalid frame rate") from exc

    timeline = Timeline(
        timebase=timebase,
        width=int(payload.get("width", 1920)),
        height=int(payload.get("height", 1080)),
        sample_rate=int(payload.get("sample_rate", 48000)),
        tracks=[],
        master_gain_db=float(payload.get("master_gain_db", 0.0)),
    )

    # Re-probe rather than trusting stored metadata: the file on disk is the
    # authority, and it may have been replaced since the project was saved.
    media: list[MediaInfo] = []
    missing: list[str] = []
    remap: dict[str, str] = {}
    for entry in payload.get("media", []):
        stored_id = entry.get("media_id", "")
        file_path = entry.get("path", "")
        try:
            info = probe(file_path)
        except UnsupportedMedia:
            missing.append(file_path)
            continue
        media.append(info)
        # media_id is derived from size and mtime, so a re-encoded file gets a
        # new id; map the old one across so clips still resolve.
        remap[stored_id] = info.media_id

    for raw_track in payload.get("tracks", []):
        kind = raw_track.get("kind")
        if kind not in ("video", "audio"):
            continue
        track = Track(
            kind=kind,
            name=raw_track.get("name", kind[0].upper() + "1"),
            muted=bool(raw_track.get("muted", False)),
            locked=bool(raw_track.get("locked", False)),
            gain_db=float(raw_track.get("gain_db", 0.0)),
            solo=bool(raw_track.get("solo", False)),
        )
        for raw_clip in raw_track.get("clips", []):
            media_id = raw_clip.get("media_id", "")
            if media_id not in remap and not isinstance(raw_clip.get("title"), dict):
                # Its media is missing; drop the clip rather than fail. A title
                # is exempt: it has no media to be missing, which is the whole
                # point of it.
                continue
            try:
                track.clips.append(
                    Clip(
                        media_id=remap.get(media_id, ""),
                        src_in=int(raw_clip["src_in"]),
                        src_out=int(raw_clip["src_out"]),
                        tl_start=int(raw_clip["tl_start"]),
                        src_length=int(raw_clip["src_length"]),
                        kind=raw_clip.get("kind", kind),
                        speed=float(raw_clip.get("speed", 1.0)),
                        link_id=raw_clip.get("link_id"),
                        name=raw_clip.get("name", ""),
                        enabled=bool(raw_clip.get("enabled", True)),
                        reversed=bool(raw_clip.get("reversed", False)),
                        muted=bool(raw_clip.get("muted", False)),
                        gain_db=float(raw_clip.get("gain_db", 0.0)),
                        fade_in=int(raw_clip.get("fade_in", 0)),
                        fade_out=int(raw_clip.get("fade_out", 0)),
                        framing=_framing_from(raw_clip.get("framing")),
                        zoom=_region_from(raw_clip.get("zoom")),
                        framing_end=(
                            _framing_from(raw_clip["framing_end"])
                            if isinstance(raw_clip.get("framing_end"), dict)
                            else None
                        ),
                        rotation=int(raw_clip.get("rotation", 0)),
                        flipped=bool(raw_clip.get("flipped", False)),
                        dissolve_in=int(raw_clip.get("dissolve_in", 0)),
                        title=_title_from(raw_clip.get("title")),
                    )
                )
            # TimelineError too: a value can be the right *type* and still be
            # rejected — a speed of 100x, a rotation of 45 degrees. Those are as
            # malformed as a gain of "loud", and cost one clip, not the project.
            except (KeyError, ValueError, TypeError, TimelineError):
                continue
        track.sort()
        timeline.tracks.append(track)

    if not timeline.tracks:
        timeline.tracks = Timeline.default(timebase).tracks

    timeline.validate()
    return LoadResult(
        timeline=timeline,
        media=media,
        missing=missing,
        playhead=int(payload.get("playhead", 0)),
    )
