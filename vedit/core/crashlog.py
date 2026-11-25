"""A log file, and a net under every unhandled exception.

Without this an exception raised inside a Qt slot is printed to a terminal
nobody is looking at and the editor carries on in whatever state it was left —
or, for a hard fault inside a native library, simply vanishes. Neither leaves
anything to go on afterwards. Here both end up in a file, and the first also
gives the application a chance to autosave and say so.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from vedit.core.recovery import state_root

log = logging.getLogger("vedit")

_fault_file = None  # kept open for the life of the process; faulthandler needs it


def log_path() -> Path:
    return state_root() / "vedit.log"


def install(on_error: Callable[[str], None] | None = None) -> Path:
    """Start logging to a file and route unhandled exceptions into it.

    `on_error` is called with a one-line description after each one is logged.
    It runs on whichever thread raised, so it must only do thread-safe things.
    """
    global _fault_file

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)

    # Segfaults and aborts inside Qt, PyAV or a driver never reach Python's
    # exception machinery; this writes the Python stack at the moment of death.
    try:
        _fault_file = open(path.with_name("fault.log"), "a", encoding="utf-8")
        faulthandler.enable(_fault_file)
    except OSError:
        _fault_file = None

    def report(kind, value, trace) -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, trace)
            return
        log.error("Unhandled exception", exc_info=(kind, value, trace))
        sys.__excepthook__(kind, value, trace)
        if on_error is not None:
            try:
                on_error(f"{kind.__name__}: {value}")
            except Exception:  # noqa: BLE001 - the net must not throw
                log.exception("The error handler itself failed")

    sys.excepthook = report
    threading.excepthook = lambda args: report(
        args.exc_type, args.exc_value, args.exc_traceback
    )
    log.info("vedit started")
    return path
