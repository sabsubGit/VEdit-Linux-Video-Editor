"""Text over the picture.

Qt-free and FFmpeg-free, like `framing.py`, and for the same reason: the viewer
draws titles with `QPainter` and the export draws them with `drawtext`, and the
two have to put the same words in the same place. The line layout below is the
one description of where the text goes, and both read it.

Glyph-for-glyph agreement is not achievable — two different rasterisers will
never lay out a font identically — but position, size and line spacing are, and
those are what someone notices. Both sides ask for the same family, which on a
normal Linux box is the same file.

Sizes are fractions of the frame height rather than points, so a title looks the
same on a 720p project as on a 4K one. Points would mean a title that reads
correctly in the viewer and comes out tiny in the export.
"""

from __future__ import annotations

from dataclasses import dataclass

# The family both sides ask for. FFmpeg resolves it through fontconfig and Qt
# through its own database; on a normal Linux install they land on the same
# file, which is as close to identical as two rasterisers get.
FONT_FAMILY = "DejaVu Sans"

# Fractions of the frame height. A "small" title is about the size of a
# subtitle; "huge" is a title card you could read across a room.
SIZES = {"small": 0.055, "medium": 0.080, "large": 0.115, "huge": 0.160}
DEFAULT_SIZE = "medium"

POSITIONS = ("top", "centre", "lower", "bottom")
ALIGNMENTS = ("left", "centre", "right")

# Keeps text off the very edge of frame, where a TV would crop it.
MARGIN = 0.06
LINE_SPACING = 1.25   # multiples of the font size


class TitleError(ValueError):
    """A title setting was rejected. A `ValueError` so the project loader's
    existing "drop the clip, keep the project" path catches it."""


@dataclass(frozen=True, slots=True)
class Title:
    """Words drawn over whatever is underneath at that point in the timeline."""

    text: str = "Title"
    size: str = DEFAULT_SIZE
    position: str = "centre"
    align: str = "centre"
    colour: str = "#ffffff"
    # A soft dark edge behind the glyphs. On by default because a title is
    # usually over footage, and white text over a bright sky is unreadable
    # without it — the one setting most likely to be wanted and least likely to
    # be found.
    shadow: bool = True
    # Nudged away from where `position` and `align` put it, as a fraction of the
    # frame. Dragging the title in the viewer writes these, and the presets stay
    # the anchor it is measured from — so a title dragged a little off centre is
    # still a centred title, and changing the size keeps it where you put it.
    offset_x: float = 0.0
    offset_y: float = 0.0

    def __post_init__(self) -> None:
        if self.size not in SIZES:
            raise TitleError(f"{self.size!r} is not one of {', '.join(SIZES)}")
        if self.position not in POSITIONS:
            raise TitleError(f"{self.position!r} is not one of {', '.join(POSITIONS)}")
        if self.align not in ALIGNMENTS:
            raise TitleError(f"{self.align!r} is not one of {', '.join(ALIGNMENTS)}")
        for name, value in (("offset_x", self.offset_x), ("offset_y", self.offset_y)):
            if not -1.0 <= value <= 1.0:
                raise TitleError(f"{name}={value:g} is outside -1 to 1")
        if not (
            len(self.colour) == 7
            and self.colour.startswith("#")
            and all(c in "0123456789abcdefABCDEF" for c in self.colour[1:])
        ):
            raise TitleError(f"{self.colour!r} is not a #rrggbb colour")

    @property
    def lines(self) -> list[str]:
        """The text as drawn. Blank lines are kept: they are spacing someone
        typed on purpose."""
        return self.text.split("\n")

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


@dataclass(frozen=True, slots=True)
class Line:
    """One line of a title, placed in project-frame pixels."""

    text: str
    top: float          # top of the line's box
    height: float       # the box's height, one line of leading
    font_px: float


def layout(title: Title, frame: tuple[int, int]) -> list[Line]:
    """Where each line of a title sits in the project frame.

    The block is placed as a whole and then split into lines, so a two-line
    lower third sits where a one-line one does rather than drifting down the
    frame as text is added.
    """
    frame_w, frame_h = frame
    font_px = SIZES[title.size] * frame_h
    line_height = font_px * LINE_SPACING
    lines = title.lines
    block = line_height * len(lines)
    margin = MARGIN * frame_h

    if title.position == "top":
        top = margin
    elif title.position == "centre":
        top = (frame_h - block) / 2
    elif title.position == "lower":
        # The lower third: low enough to sit under a face, high enough to clear
        # the bottom of frame and anything burned in down there.
        top = frame_h * 0.72 - block / 2
    else:
        top = frame_h - block - margin

    # Clamped at both ends, however many lines there are and however big. A
    # four-line "huge" lower third would otherwise run off the bottom of the
    # frame, and text you cannot see is worse than text in the wrong place.
    lowest = frame_h - block - margin
    if lowest < margin:
        # Taller than the frame allows: centre it and let it overflow evenly
        # rather than pinning it to one edge.
        top = (frame_h - block) / 2
    else:
        top = max(margin, min(top, lowest))
    # The drag, applied last so it moves the whole block off whichever anchor
    # the preset chose. Clamped so a title can be dragged to an edge but not
    # off the frame entirely.
    top += title.offset_y * frame_h
    top = max(-block + line_height * 0.5, min(top, frame_h - line_height * 0.5))
    return [
        Line(text=text, top=top + index * line_height, height=line_height, font_px=font_px)
        for index, text in enumerate(lines)
    ]


def horizontal_offset(title: Title, frame: tuple[int, int]) -> float:
    """How far the drag has moved the text sideways, in frame pixels."""
    return title.offset_x * frame[0]


def horizontal_margin(frame: tuple[int, int]) -> float:
    return MARGIN * frame[0]


def quote(text: str) -> str:
    """Turn text into a `drawtext` `text=` value, quotes and all.

    Three layers eat characters on the way to the filter — the filtergraph
    splits on `,` and `;`, each filter splits its options on `:`, and drawtext
    has its own idea about `'`. This build needs the value both escaped *and*
    quoted; either alone lets a colon through and the graph stops parsing, which
    is the failure mode that matters: a stray apostrophe in someone's title does
    not come out wrong, it stops the export dead.

    A quote cannot appear inside quotes at all, so it is spelled by closing,
    escaping one, and opening again.

    `%` is deliberately *not* escaped here. Neither a backslash nor doubling
    works for it; the fix
    is `expansion=none`, which has to be passed **before** `text=` — see
    `render/graph.py`.
    """
    out = text.replace("\\", "\\\\")
    for char in (":", ",", ";", "[", "]", "="):
        out = out.replace(char, f"\\{char}")
    out = out.replace("'", "'\\''")
    return f"'{out}'"
