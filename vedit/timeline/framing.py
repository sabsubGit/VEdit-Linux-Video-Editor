"""How a clip's picture is fitted into the project frame.

Like `model.py`, this imports nothing from Qt and nothing from FFmpeg — and for
the same reason. The preview blits with `QPainter` and the export builds an
FFmpeg filter chain, and the two must land on the same pixels; the only way to
be sure of that is for both to read their numbers from one place that can be
tested without either.

**Framing is defined on the project frame, not on the source.** What you see
today is the source rotated, then letterboxed into the project's width and
height. Framing picks a rectangle out of *that* and blows it up to fill the
frame. So "zoom" means "zoom into what I can already see", which is what anyone
who has not used an NLE before expects it to mean.

Pan is stored as a fraction of the *slack* — how far the window could move
before it fell off the edge — rather than as an offset in pixels. At zoom 1.0
there is no slack, so no combination of values can describe a window that hangs
off the frame, and changing the zoom can never invalidate a pan that was already
set. That is what keeps this module free of the clamping that fades need.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

# Zooming *out* would shrink the picture and leave black around it, which is
# picture-in-picture rather than framing, so 1.0 is the floor. The ceiling is
# where even a 4K source has less than a hundred pixels left to enlarge.
MIN_ZOOM = 1.0
MAX_ZOOM = 8.0

# Quarter turns only. Arbitrary angles need a resampling pass and a decision
# about what fills the corners; neither belongs in "turn my video upright".
ROTATIONS = (0, 90, 180, 270)


class FramingError(ValueError):
    """A framing value was rejected. The message is written to be shown.

    A `ValueError` on purpose. `projectfile` drops a clip whose fields fail to
    coerce and keeps the rest of the project; inheriting from the type it
    already catches means a corrupt framing costs one clip rather than the
    whole file, with no special case there.
    """


@dataclass(frozen=True, slots=True)
class Rect:
    """A rectangle in whatever space the caller is working in.

    Floats throughout: the window edges land on fractional pixels at most zoom
    levels, and rounding here rather than at the point of use is how the preview
    and the export would drift apart by a pixel.
    """

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def intersected(self, other: Rect) -> Rect:
        """The overlap, or a zero-sized rect at the origin if there is none."""
        left = max(self.x, other.x)
        top = max(self.y, other.y)
        right = min(self.right, other.right)
        bottom = min(self.bottom, other.bottom)
        if right <= left or bottom <= top:
            return Rect(0.0, 0.0, 0.0, 0.0)
        return Rect(left, top, right - left, bottom - top)

    @property
    def is_empty(self) -> bool:
        return self.width <= 0 or self.height <= 0


@dataclass(frozen=True, slots=True)
class Framing:
    """Which part of the project frame a clip's picture fills.

    Frozen, so it can be a plain default on the `slots=True` `Clip` without a
    factory, and so a preview value can be passed around during a drag with no
    risk of it being mutated into the model by accident.
    """

    zoom: float = 1.0   # 1.0 = the whole frame, 2.0 = half its width and height
    x: float = 0.0      # pan, -1..+1 of the slack the zoom leaves
    y: float = 0.0

    def __post_init__(self) -> None:
        if not MIN_ZOOM <= self.zoom <= MAX_ZOOM:
            raise FramingError(
                f"zoom {self.zoom:g}× is outside {MIN_ZOOM:g}× to {MAX_ZOOM:g}×"
            )
        for name, value in (("x", self.x), ("y", self.y)):
            if not -1.0 <= value <= 1.0:
                raise FramingError(f"pan {name}={value:g} is outside -1 to 1")

    @property
    def is_identity(self) -> bool:
        """Whether this framing changes anything.

        The export consults this to emit no filter at all for an untouched clip,
        which is what keeps an unedited timeline's command exactly what it was
        before this feature existed.
        """
        return self.zoom == 1.0 and self.x == 0.0 and self.y == 0.0

    def nudged(self, *, zoom: float | None = None, dx: float = 0.0, dy: float = 0.0) -> Framing:
        """A copy with the values moved and clamped into range.

        Clamped rather than rejected because this is what a drag calls: running
        the mouse past the edge of the legal range should stop at the edge, not
        raise at the user.
        """
        wanted = self.zoom if zoom is None else zoom
        return Framing(
            zoom=max(MIN_ZOOM, min(MAX_ZOOM, wanted)),
            x=max(-1.0, min(1.0, self.x + dx)),
            y=max(-1.0, min(1.0, self.y + dy)),
        )


IDENTITY = Framing()


def interpolate(start: Framing, end: Framing, progress: float) -> Framing:
    """The framing part-way through a move.

    Linear, and deliberately so: an eased push looks better but has to be
    expressed identically in an FFmpeg expression, and a curve that is subtly
    different in the export than in the preview is worse than no curve at all.

    `progress` is clamped, so a caller that computes it from a frame one past
    the end of the clip gets the end framing rather than an extrapolation.
    """
    t = max(0.0, min(1.0, progress))
    if t <= 0.0:
        return start
    if t >= 1.0:
        return end
    return Framing(
        zoom=start.zoom + (end.zoom - start.zoom) * t,
        x=start.x + (end.x - start.x) * t,
        y=start.y + (end.y - start.y) * t,
    )


def rotated_size(width: int, height: int, rotation: int) -> tuple[int, int]:
    """Source dimensions after a quarter turn."""
    return (height, width) if rotation in (90, 270) else (width, height)


def source_box(
    source: tuple[int, int], frame: tuple[int, int], rotation: int = 0
) -> Rect:
    """Where the source lands inside the project frame, before any framing.

    This is the existing `scale=...:force_original_aspect_ratio=decrease` and
    `pad=...` written as arithmetic. A 4:3 source in a 16:9 project gets bars
    down the sides, and this is the rectangle between them.
    """
    frame_w, frame_h = frame
    source_w, source_h = rotated_size(source[0], source[1], rotation)
    if source_w <= 0 or source_h <= 0:
        return Rect(0.0, 0.0, float(frame_w), float(frame_h))

    scale = min(frame_w / source_w, frame_h / source_h)
    width = source_w * scale
    height = source_h * scale
    return Rect((frame_w - width) / 2, (frame_h - height) / 2, width, height)


def window(framing: Framing, frame: tuple[int, int]) -> Rect:
    """The part of the project frame that ends up filling the project frame.

    The whole model in four lines: the window is the frame divided by the zoom,
    and the pan slides it across whatever room that leaves.
    """
    frame_w, frame_h = frame
    width = frame_w / framing.zoom
    height = frame_h / framing.zoom
    return Rect(
        (frame_w - width) / 2 * (1.0 + framing.x),
        (frame_h - height) / 2 * (1.0 + framing.y),
        width,
        height,
    )


def fill_zoom(source: tuple[int, int], frame: tuple[int, int], rotation: int = 0) -> float:
    """The zoom that makes the source cover the frame, leaving no bars.

    This is what "Fill Frame" applies, and it is the one-click fix for a video
    shot on a phone and dropped on a horizontal timeline: the picture stops
    being a tall sliver between two black slabs. Clamped to the legal range, so
    a source with an extreme aspect asks for what it can have rather than
    raising in the middle of a menu action.
    """
    box = source_box(source, frame, rotation)
    if box.width <= 0 or box.height <= 0:
        return 1.0
    needed = max(frame[0] / box.width, frame[1] / box.height)
    return max(MIN_ZOOM, min(MAX_ZOOM, needed))


def unorient(
    rect: Rect, source: tuple[int, int], rotation: int = 0, flipped: bool = False
) -> Rect:
    """Map a rectangle from the turned picture back onto the stored pixels.

    Framing is measured against the picture as shown — rotated and mirrored —
    but the decoder hands back the image the file actually holds. Without this
    step a quarter-turned clip would be read with its width and height swapped,
    and a mirrored one would pan the wrong way; both look plausible enough on a
    centred shot to slip through.
    """
    width, height = source
    turned_w, turned_h = rotated_size(width, height, rotation)

    x, y, w, h = rect.x, rect.y, rect.width, rect.height
    if flipped:
        # The mirror happens after the turn, so undo it in turned space.
        x = turned_w - (x + w)

    if rotation == 90:
        # A pixel at (x, y) turns clockwise to (height - y, x); this is that
        # backwards, applied to a whole rectangle.
        return Rect(y, height - (x + w), h, w)
    if rotation == 180:
        return Rect(width - (x + w), height - (y + h), w, h)
    if rotation == 270:
        return Rect(width - (y + h), x, h, w)
    return Rect(x, y, w, h)


def visible_source(
    framing: Framing,
    source: tuple[int, int],
    frame: tuple[int, int],
    rotation: int = 0,
    flipped: bool = False,
) -> tuple[Rect, Rect]:
    """What the preview must draw, as (rect in stored pixels, rect in the frame).

    The preview holds a decoded image at the source's own size, not a project
    frame, so it cannot simply crop. It has to answer two questions at once:
    which pixels of the file are still visible, and where in the output frame
    they belong. Both fall out of intersecting the framing window with the box
    the picture occupies.

    The first rect is in the image's own coordinates, ready to hand to
    `drawImage` — the caller still applies the turn and the mirror when it
    draws. The second is in project-frame coordinates *after* the zoom, so a
    caller scales it onto the widget by one factor for both axes. When the
    window lies entirely in the letterbox bars, both come back empty and the
    caller draws nothing but background.
    """
    box = source_box(source, frame, rotation)
    view = window(framing, frame)
    seen = box.intersected(view)
    if seen.is_empty:
        return Rect(0.0, 0.0, 0.0, 0.0), Rect(0.0, 0.0, 0.0, 0.0)

    # Where that piece sits in the turned picture...
    turned_w, turned_h = rotated_size(source[0], source[1], rotation)
    per_pixel_x = turned_w / box.width
    per_pixel_y = turned_h / box.height
    in_turned = Rect(
        (seen.x - box.x) * per_pixel_x,
        (seen.y - box.y) * per_pixel_y,
        seen.width * per_pixel_x,
        seen.height * per_pixel_y,
    )

    # And where it lands once the window has been blown back up to fill.
    in_frame = Rect(
        (seen.x - view.x) * framing.zoom,
        (seen.y - view.y) * framing.zoom,
        seen.width * framing.zoom,
        seen.height * framing.zoom,
    )
    return unorient(in_turned, source, rotation, flipped), in_frame


def framing_for_box(box: Rect, frame: tuple[int, int]) -> Framing:
    """The framing that fills the frame with `box`, given in frame coordinates.

    What the zoom tool commits: drag a rectangle round the thing you want to
    see and this is the framing that shows it. The zoom is the *smaller* of the
    two fits, so the box is always fully inside what ends up on screen — a
    marquee that is the wrong shape gives you a bit more than you asked for
    rather than cropping the thing you were pointing at.

    The result is centred on the box as far as the edges allow; a box drawn
    hard against one side simply pans as far as it can go.
    """
    frame_w, frame_h = frame
    if box.width <= 0 or box.height <= 0:
        return Framing()

    zoom = min(frame_w / box.width, frame_h / box.height)
    zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))

    width, height = frame_w / zoom, frame_h / zoom
    slack_x, slack_y = (frame_w - width) / 2, (frame_h - height) / 2
    wanted_x = box.x + box.width / 2 - width / 2
    wanted_y = box.y + box.height / 2 - height / 2
    return Framing(
        zoom=zoom,
        x=max(-1.0, min(1.0, wanted_x / slack_x - 1.0)) if slack_x > 0 else 0.0,
        y=max(-1.0, min(1.0, wanted_y / slack_y - 1.0)) if slack_y > 0 else 0.0,
    )


def pan_for_delta(
    framing: Framing, dx: float, dy: float, frame: tuple[int, int]
) -> Framing:
    """Framing panned by a drag measured in project-frame pixels.

    Dragging moves the *picture*, so the window moves the other way — pulling
    the image right shows more of what was off to the left. The conversion is
    here rather than in the overlay so the sign is decided once.
    """
    frame_w, frame_h = frame
    slack_x = (frame_w - frame_w / framing.zoom) / 2
    slack_y = (frame_h - frame_h / framing.zoom) / 2
    return framing.nudged(
        dx=-dx / slack_x if slack_x > 0 else 0.0,
        dy=-dy / slack_y if slack_y > 0 else 0.0,
    )


def describe(framing: Framing, rotation: int = 0, flipped: bool = False) -> str:
    """A short human reading of a clip's picture settings, for menus and status."""
    parts: list[str] = []
    if not framing.is_identity:
        parts.append(f"{framing.zoom:.2f}×".replace(".00×", "×"))
    if rotation:
        parts.append(f"{rotation}°")
    if flipped:
        parts.append("flipped")
    return " · ".join(parts) if parts else "none"


def with_zoom(framing: Framing, zoom: float) -> Framing:
    """Set the zoom, keeping the pan. Clamps, for the same reason `nudged` does."""
    return replace(framing, zoom=max(MIN_ZOOM, min(MAX_ZOOM, zoom)))


# -- zoom regions --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Region:
    """A zoom that lasts for part of a clip, and how fast it arrives.

    The gesture this exists for is "something happened over there — show me,
    for a moment". Applying a zoom to a whole clip cannot say that, and the way
    round it was to cut the clip into three by hand, which is a lot of work to
    ask for a two-second punch-in.

    `start` and `end` are clip-local frames, so the region survives a clip being
    moved along the timeline. The ramps are measured *inwards* from each edge:
    zero is a cut straight to the zoom, which is what a straight corner looks
    like on the lane, and dragging that corner in slopes it into a glide. They
    are the same shape as an audio fade, deliberately — it is the one handle on
    this timeline that already means "ease in over this long".
    """

    framing: Framing
    start: int
    end: int
    ramp_in: int = 0
    ramp_out: int = 0

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise FramingError("a zoom has to last at least one frame")
        if self.start < 0:
            raise FramingError("a zoom cannot start before the clip does")
        if self.ramp_in < 0 or self.ramp_out < 0:
            raise FramingError("a zoom ramp cannot be negative")
        if self.ramp_in + self.ramp_out > self.length:
            raise FramingError("the ramps are longer than the zoom itself")

    @property
    def length(self) -> int:
        return self.end - self.start

    @property
    def is_instant(self) -> bool:
        """No ramps at either end: the zoom simply cuts in and cuts out."""
        return self.ramp_in == 0 and self.ramp_out == 0

    def contains(self, local: int) -> bool:
        return self.start <= local < self.end

    def progress(self, local: int) -> float:
        """How much of the zoom applies at a clip-local frame, 0 to 1.

        Outside the region there is none; inside, the ramps carry it in and out
        and everything between is the zoom at full strength.

        The arithmetic is `envelope`'s from the audio side, frame for frame:
        `into / ramp_in` rising and `left / ramp_out` falling, taking whichever
        is lower where two short ramps overlap. That is not incidental — it is
        ffmpeg's linear `afade`/`xfade` shape, it is what the corner handle on
        an audio clip already means, and matching it is what lets the same
        gesture be learned once.
        """
        if not self.contains(local):
            return 0.0
        into = local - self.start
        left = self.end - local
        value = 1.0
        if self.ramp_in > 0:
            value = min(value, into / self.ramp_in)
        if self.ramp_out > 0:
            value = min(value, left / self.ramp_out)
        return max(0.0, min(1.0, value))

    def clamped_to(self, duration: int) -> Region:
        """The region trimmed to fit a clip of `duration` frames, or None-ish.

        A trim can shorten a clip out from under its own zoom, exactly as it can
        under a fade. Clamping rather than raising is what lets a trim handle
        keep dragging past it.
        """
        start = max(0, min(self.start, max(0, duration - 1)))
        end = max(start + 1, min(self.end, duration))
        length = end - start
        ramp_in = max(0, min(self.ramp_in, length))
        ramp_out = max(0, min(self.ramp_out, length - ramp_in))
        return Region(self.framing, start, end, ramp_in, ramp_out)


def region_framing(base: Framing, region: Region | None, local: int) -> Framing:
    """The framing actually showing, with a zoom region laid over the base.

    The base is whatever the clip was already doing — its own framing, or the
    value part-way through a move — so a region composes with those rather than
    replacing them.
    """
    if region is None:
        return base
    return interpolate(base, region.framing, region.progress(local))
