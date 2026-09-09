"""Locating and running the system ffmpeg/ffprobe binaries.

Two FFmpeg builds are in play in this app and the split is deliberate:

* **PyAV's bundled build** does in-process decoding for the preview player.
* **The system binaries reached through here** do proxy generation and the final
  render.

Keeping them apart means a version difference between the two can never quietly
change what comes out of a render.
"""

from __future__ import annotations

import functools
import json
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed. Carries the tail of stderr, which is
    where FFmpeg puts the one line that actually explains the problem."""

    def __init__(self, message: str, *, command: list[str] | None = None, stderr: str = ""):
        super().__init__(message)
        self.command = command or []
        self.stderr = stderr

    def detail(self) -> str:
        """Last few stderr lines — what to show a user in a dialog."""
        lines = [line for line in self.stderr.strip().splitlines() if line.strip()]
        return "\n".join(lines[-4:])


class FFmpegMissing(FFmpegError):
    """Neither ffmpeg nor ffprobe is on PATH. Fatal, and worth saying plainly."""


@functools.cache
def ffmpeg_path() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FFmpegMissing("ffmpeg was not found on PATH; install it to use vedit")
    return path


@functools.cache
def ffprobe_path() -> str:
    path = shutil.which("ffprobe")
    if path is None:
        raise FFmpegMissing("ffprobe was not found on PATH; install it to use vedit")
    return path


@functools.cache
def encoders() -> frozenset[str]:
    """Encoder names this build supports, so the UI can offer NVENC only when it
    is really there rather than failing at render time."""
    try:
        result = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()

    names: set[str] = set()
    for line in result.stdout.splitlines():
        # Lines look like " V....D libx264   libx264 H.264 ..." after a header.
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS":
            names.add(parts[1])
    return frozenset(names)


def has_encoder(name: str) -> bool:
    return name in encoders()


@functools.cache
def filters() -> frozenset[str]:
    """Filter names this build supports.

    Titles need `drawtext`, which is only present when FFmpeg was built with
    libfreetype — common but not universal, and a build without it fails at
    render time with a message nobody could act on. Asking first means the app
    can say so while the title is being written instead.
    """
    try:
        result = subprocess.run(
            [ffmpeg_path(), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()

    names: set[str] = set()
    for line in result.stdout.splitlines():
        # Lines look like " T. drawtext  V->V  Draw text on top of ..." after
        # a header; the first column is a short flag field.
        parts = line.split()
        if len(parts) >= 3 and len(parts[0]) <= 3 and "->" in parts[2]:
            names.add(parts[1])
    return frozenset(names)


def has_filter(name: str) -> bool:
    return name in filters()


def run(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg/ffprobe to completion, raising FFmpegError on a non-zero exit."""
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"timed out after {timeout}s", command=args) from exc
    except OSError as exc:
        raise FFmpegError(str(exc), command=args) from exc

    if result.returncode != 0:
        raise FFmpegError(
            f"exited with code {result.returncode}",
            command=args,
            stderr=result.stderr,
        )
    return result


def probe_json(path: str | Path) -> dict:
    """Raw `ffprobe -show_format -show_streams` output as a dict."""
    args = [
        ffprobe_path(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    result = run(args, timeout=60)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise FFmpegError(f"ffprobe returned unparseable JSON for {path}", command=args) from exc
