"""The tool you are holding.

A modal tool — pick it, and the pointer and the click mean something else until
you pick another — is the model every drawing program uses and the one most
people already have. It also makes the features findable: a row of tools says
what the program can do, where a menu of verbs only answers a question you knew
to ask.

Only the tools that change what a click *does* live here. Snap and thumbnails
are settings, and Fit is a command; none of them change the meaning of the
mouse, so none of them belong in a group where picking one unpicks the rest.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Tool(Enum):
    POINTER = "pointer"
    CUT = "cut"
    REFRAME = "reframe"
    ZOOM = "zoom"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    tool: Tool
    icon: str
    label: str
    key: str
    hint: str          # shown in the status bar while the tool is held
    tip: str           # the tooltip on its button


TOOLS = (
    ToolSpec(
        Tool.POINTER,
        icon="pointer",
        label="Select",
        key="V",
        hint="",
        tip="Select, move and trim clips",
    ),
    ToolSpec(
        Tool.CUT,
        icon="cut",
        label="Cut",
        key="C",
        hint="Click the timeline to cut there. Esc or V to stop cutting.",
        tip="Cut clips where you click",
    ),
    ToolSpec(
        Tool.REFRAME,
        icon="reframe",
        label="Reframe",
        key="T",
        hint="Drag the picture to move it, drag a corner to zoom, "
             "double-click to reset.",
        tip="Move and zoom the picture by dragging it",
    ),
    ToolSpec(
        Tool.ZOOM,
        icon="zoom",
        label="Zoom",
        key="Z",
        hint="Drag a box round what should fill the frame. "
             "Double-click to zoom back out.",
        tip="Drag a box round part of the picture to fill the frame with it",
    ),
)

BY_TOOL = {spec.tool: spec for spec in TOOLS}

# The tools that work in the viewer rather than on the timeline.
VIEWER_TOOLS = (Tool.REFRAME, Tool.ZOOM)
