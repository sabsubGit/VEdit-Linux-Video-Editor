"""Autosave files, and finding the ones a crash left behind.

An autosave is an ordinary project file written somewhere else, with a note of
which project it belongs to. It is never written over the user's own file: an
autosave that replaced the project would turn "I did not mean to save that" into
something that cannot be taken back.

One file per running vedit, named for its process. A clean exit deletes it, so
any file found at startup whose process is gone is, by construction, work that
was never saved.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

SUFFIX = ".vedit"
KEY = "recovery"


def state_root() -> Path:
    """Where vedit keeps what must survive a crash: autosaves and its log.

    The state directory rather than the cache: a cache may be emptied by anyone
    at any time, and this is the one place that would hurt.
    """
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "vedit"


def recovery_dir() -> Path:
    return state_root() / "recovery"


def session_file() -> Path:
    """This process's autosave."""
    return recovery_dir() / f"session-{os.getpid()}{SUFFIX}"


def note(original: str | None) -> dict:
    """The extra top-level entry that marks a project file as an autosave."""
    return {
        KEY: {
            "original": original,
            "pid": os.getpid(),
            "saved_at": time.time(),
        }
    }


@dataclass(frozen=True, slots=True)
class Recovery:
    """One autosave left behind by a vedit that did not exit cleanly."""

    path: Path
    original: str | None     # the project it belongs to; None if never saved
    saved_at: float

    @property
    def name(self) -> str:
        return Path(self.original).stem if self.original else "Untitled"

    @property
    def when(self) -> str:
        return time.strftime("%d %b %H:%M", time.localtime(self.saved_at))


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pending() -> list[Recovery]:
    """Autosaves whose vedit is no longer running, newest first.

    A file belonging to a live process is another window's work in progress and
    is left alone.
    """
    folder = recovery_dir()
    if not folder.is_dir():
        return []

    found: list[Recovery] = []
    for path in folder.glob(f"session-*{SUFFIX}"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            info = payload.get(KEY) or {}
            pid = int(info.get("pid", 0))
        except (OSError, ValueError, AttributeError):
            continue
        if pid == os.getpid() or _alive(pid):
            continue
        found.append(
            Recovery(
                path=path,
                original=info.get("original") or None,
                saved_at=float(info.get("saved_at") or path.stat().st_mtime),
            )
        )
    found.sort(key=lambda item: item.saved_at, reverse=True)
    return found


def discard(path: Path) -> None:
    Path(path).unlink(missing_ok=True)
