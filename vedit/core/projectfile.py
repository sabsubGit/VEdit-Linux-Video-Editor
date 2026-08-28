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
from vedit.timeline.model import Clip, Timeline, Track  # noqa: F401

# Version 2 added mixing: per-clip gain and fades, per-track gain and solo, and
# a master gain. Every one of them defaults to "as it was", so a version 1 file
# loads unchanged — the bump exists for the other direction. `load_project`
# refuses anything newer than it understands, and without the bump an older
# build would open a v2 project, silently drop every gain and fade, and write
# them away on the next save.
FORMAT_VERSION = 2
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
        "gain_db": clip.gain_db,
        "fade_in": clip.fade_in,
        "fade_out": clip.fade_out,
    }


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
            if media_id not in remap:
                continue  # its media is missing; drop the clip rather than fail
            try:
                track.clips.append(
                    Clip(
                        media_id=remap[media_id],
                        src_in=int(raw_clip["src_in"]),
                        src_out=int(raw_clip["src_out"]),
                        tl_start=int(raw_clip["tl_start"]),
                        src_length=int(raw_clip["src_length"]),
                        kind=raw_clip.get("kind", kind),
                        speed=float(raw_clip.get("speed", 1.0)),
                        link_id=raw_clip.get("link_id"),
                        name=raw_clip.get("name", ""),
                        enabled=bool(raw_clip.get("enabled", True)),
                        gain_db=float(raw_clip.get("gain_db", 0.0)),
                        fade_in=int(raw_clip.get("fade_in", 0)),
                        fade_out=int(raw_clip.get("fade_out", 0)),
                    )
                )
            except (KeyError, ValueError, TypeError):
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
