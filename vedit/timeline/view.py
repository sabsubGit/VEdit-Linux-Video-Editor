"""The timeline canvas.

This is one custom-painted widget rather than a `QGraphicsView` full of items.
Clips are rectangles in a sorted list, so hit-testing is a few lines, and drawing
waveforms and drag ghosts directly is far simpler than coordinating item state —
with no scene-graph cost when a timeline gets long.

Drags never touch the model while the mouse is down. Moving and trimming paint a
ghost at the proposed position and commit once on release, so one gesture becomes
one undo step instead of hundreds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygon,
)
from PySide6.QtWidgets import QHBoxLayout, QMenu, QScrollBar, QVBoxLayout, QWidget

from vedit import icons, theme
from vedit.tools import Tool
from vedit.core.project import Project
from vedit.media.pool import MediaPool
from vedit.timeline import framing, levels, ops, titles as titles_mod
from vedit.timeline.model import (
    MAX_GAIN_DB,
    MIN_GAIN_DB,
    Clip,
    Timeline,
    TimelineError,
    Track,
    TrackKind,
)
from vedit.timeline.filmstrip import FilmstripCache, draw_filmstrip
from vedit.timeline.waveform import WaveformCache, draw_waveform

RULER_HEIGHT = 26
HEADER_WIDTH = 108
VIDEO_TRACK_HEIGHT = 70
AUDIO_TRACK_HEIGHT = 56
# The Audio page's lanes. Twice the height is what makes a waveform something
# you can edit against rather than a decoration, and it is what gives the fade
# handles room to be distinct from the trim handles.
AUDIO_TRACK_HEIGHT_TALL = 112
# The Media page's lanes. Short enough that the pool keeps most of the page,
# tall enough that a lane is still something you can aim a drop at.
VIDEO_TRACK_HEIGHT_MINI = 46
AUDIO_TRACK_HEIGHT_MINI = 34
TRACK_GAP = 2
CLIP_RADIUS = 7           # corner rounding on clip rectangles
NAME_BAND_HEIGHT = 21     # solid strip at the foot of a clip holding its name
TRIM_GRAB_PX = 7          # how close to an edge counts as grabbing it
FADE_GRAB_PX = 13         # height of the corner square that takes a fade drag
FADE_HANDLE_PX = 15       # how far in from the edge that square reaches
GAIN_GRAB_PX = 5          # vertical tolerance on the volume line
ZOOM_GRAB_PX = 10         # how close to a zoom region's edge counts as grabbing it
ZOOM_BAND_PX = 22         # height of the band the zoom envelope is drawn in
# Below this many pixels wide a region cannot show four separate handles, so it
# offers only its two edges — the ramps stay reachable from the clip menu. Two
# seconds of timeline is a handful of pixels when the view is zoomed out, and a
# handle you cannot hit is worse than one that is not offered.
ZOOM_NARROW_PX = 44
# Where the band stops meaning "the ramp corner" and starts meaning "the edge",
# as a fraction of its height from the top. Below half because moving a zoom's
# start and end is the everyday adjustment and easing it is the occasional one,
# so the edges get the larger share of the target.
ZOOM_CORNER_BAND = 0.42
# Peak target for Normalise. -3 dBFS rather than 0: it leaves headroom for the
# lane and master faders to add to without the sum clipping immediately.
NORMALISE_TARGET_DB = -3.0
SNAP_PX = 9               # snapping tolerance, in screen pixels
MIN_PX_PER_FRAME = 0.002
MAX_PX_PER_FRAME = 40.0


class Zone(Enum):
    BODY = auto()
    IN = auto()
    OUT = auto()
    FADE_IN = auto()
    FADE_OUT = auto()
    GAIN = auto()
    # The zoom region's own edges, and the two corners that slope it. Four
    # handles on one band: where the punch-in starts and stops, and how long it
    # takes to arrive at each end.
    ZOOM_IN = auto()
    ZOOM_OUT = auto()
    ZOOM_RAMP_IN = auto()
    ZOOM_RAMP_OUT = auto()

ZOOM_ZONES = (Zone.ZOOM_IN, Zone.ZOOM_OUT, Zone.ZOOM_RAMP_IN, Zone.ZOOM_RAMP_OUT)


@dataclass(slots=True)
class Hit:
    track: Track
    clip: Clip
    zone: Zone


class Mode(Enum):
    IDLE = auto()
    SCRUB = auto()
    MOVE = auto()
    TRIM = auto()
    ZOOM = auto()
    FADE = auto()
    GAIN = auto()


class TimelineCanvas(QWidget):
    """Ruler, track headers and clip lanes, all painted here."""

    playhead_moved = Signal(int)
    zoom_changed = Signal()
    status_message = Signal(str)

    def __init__(
        self,
        project: Project,
        parent=None,
        *,
        kinds: tuple[TrackKind, ...] = ("video", "audio"),
        track_heights: dict[str, int] | None = None,
        fade_handles: bool = False,
        volume_lines: bool = False,
    ) -> None:
        """A canvas over some or all of the timeline's lanes.

        Parameterised rather than subclassed. `track_rows()` is the one place
        the lane set is decided and everything else — hit testing, painting,
        scrolling, drops — derives from it, so a `kinds` filter carries almost
        the whole audio-only view on its own. The feature flags are separate
        from the filter because they compose: fade handles on the Edit page
        would be a flag change, not a new class.
        """
        super().__init__(parent)
        self.project = project
        self.waveforms = WaveformCache()
        self.filmstrips = FilmstripCache()
        self.show_filmstrips = True

        self.kinds = kinds
        self.track_heights = track_heights or {
            "video": VIDEO_TRACK_HEIGHT,
            "audio": AUDIO_TRACK_HEIGHT,
        }
        self.fade_handles = fade_handles
        self.volume_lines = volume_lines

        self.px_per_frame = 2.0
        self.scroll_x = 0.0
        self.scroll_y = 0.0

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setMinimumHeight(
            RULER_HEIGHT + sum(self.track_heights[kind] for kind in self.kinds) + 24
        )

        self._mode = Mode.IDLE
        self._press_pos = QPoint()
        self._press_frame = 0
        self._drag_clips: list[Clip] = []
        self._drag_delta = 0
        self._drag_track: Track | None = None
        self._drag_target: Track | None = None   # lane the drag would land on
        self._drag_kind: str = "video"
        self._trim_clip: Clip | None = None
        self._trim_edge: str = "out"
        self._trim_frame = 0
        self._level_clip: Clip | None = None      # the clip a gain or fade drag holds
        self._fade_edge: str = "in"
        self._fade_frames = 0
        self._gain_db = 0.0
        self._gain_press_db = 0.0
        self._drop_frame: int | None = None
        self._snap_line: int | None = None
        # Armed so a brand-new window fits itself once it has real geometry.
        self._auto_fit = True
        self.snapping = True
        # Which tool the mouse is holding. Only Cut changes anything here; the
        # viewer tools are the viewer's business, and Pointer is the ordinary
        # select-move-trim behaviour every other branch already implements.
        self._tool = Tool.POINTER
        self._cut_frame: int | None = None
        # A zoom region being dragged: which clip, which of its four handles,
        # and the value the drag has reached. Held off the model until release,
        # like every other drag here, so one gesture is one undo step.
        self._zoom_clip: Clip | None = None
        self._zoom_zone: Zone | None = None
        self._zoom_region = None
        # The clip whose zoom block the pointer is over, so it can light up. A
        # block that responds to being pointed at is what says it is a thing you
        # can grab, rather than a marking painted on the clip.
        self._zoom_hover: str | None = None

        # Where the playhead was last painted, so moving it can repaint the two
        # narrow strips it occupies rather than the whole canvas. See
        # `refresh_playhead`.
        self._painted_playhead: tuple[float, float] | None = None
        # The lanes as last drawn, and the geometry it was drawn for. See
        # `_static_layer`.
        self._cache: QPixmap | None = None
        self._cache_key: tuple | None = None

        project.timeline_changed.connect(self._on_model_changed)
        project.playhead_changed.connect(lambda *_: self.refresh_playhead())
        project.selection_changed.connect(self.update)
        project.proxies.peaks_ready.connect(lambda *_: self.update())
        project.proxies.strip_ready.connect(lambda *_: self.update())

    def set_tool(self, tool: Tool) -> None:
        """Hold a different tool. Only Cut behaves differently on the timeline."""
        if tool is self._tool:
            return
        self._tool = tool
        self._cut_frame = None
        if tool is Tool.CUT:
            # Qt has no scissors cursor, so the toolbar's own drawing becomes
            # one — through `icons.cursor`, which is the version built to
            # survive being reduced to a one-bit mask. Drawn once here rather
            # than per mouse move.
            pixmap = icons.cursor("cut")
            self.setCursor(QCursor(pixmap, pixmap.width() // 2, pixmap.height() // 2))
        else:
            self.unsetCursor()
        self.update()

    @property
    def tool(self) -> Tool:
        return self._tool

    # -- convenience -----------------------------------------------------------

    @property
    def timeline(self) -> Timeline:
        return self.project.timeline

    def _on_model_changed(self) -> None:
        self.update()
        self.zoom_changed.emit()

    # -- coordinate mapping ----------------------------------------------------

    def x_of(self, frame: float) -> float:
        return HEADER_WIDTH + frame * self.px_per_frame - self.scroll_x

    def frame_of(self, x: float) -> int:
        return int(round((x - HEADER_WIDTH + self.scroll_x) / self.px_per_frame))

    def visible_frames(self) -> tuple[int, int]:
        first = max(0, self.frame_of(HEADER_WIDTH) - 1)
        last = self.frame_of(self.width()) + 1
        return first, last

    def content_frames(self) -> int:
        """Scrollable length: the timeline plus a screen of room to work past it."""
        lanes = max(1.0, self.width() - HEADER_WIDTH)
        return int(self.timeline.duration + lanes / self.px_per_frame)

    # -- track geometry --------------------------------------------------------

    def lanes(self) -> list[Track]:
        """The lanes shown here, in the order they are drawn.

        Video is listed in reverse so V1 sits closest to the audio lanes and
        higher-numbered tracks stack upward, which is the layout every NLE uses.
        The Audio page passes kinds=("audio",) and gets the audio half of the
        same layout with nothing else to change.
        """
        rows: list[Track] = []
        if "video" in self.kinds:
            rows += list(reversed(self.timeline.video_tracks))
        if "audio" in self.kinds:
            rows += self.timeline.audio_tracks
        return rows

    def track_rows(self) -> list[tuple[Track, int, int]]:
        """(track, y, height) top to bottom."""
        rows: list[tuple[Track, int, int]] = []
        y = RULER_HEIGHT + TRACK_GAP - int(self.scroll_y)
        for track in self.lanes():
            height = self.track_heights[track.kind]
            rows.append((track, y, height))
            y += height + TRACK_GAP
        return rows

    def lanes_height(self) -> int:
        """Total height every lane needs, ignoring how much is on screen."""
        return sum(
            self.track_heights[track.kind] + TRACK_GAP for track in self.lanes()
        ) + TRACK_GAP

    def max_scroll_y(self) -> int:
        return max(0, self.lanes_height() - (self.height() - RULER_HEIGHT))

    def content_height(self) -> int:
        """Bottom of the drawable lane area, clamped to the widget."""
        rows = self.track_rows()
        bottom = (rows[-1][1] + rows[-1][2] + TRACK_GAP) if rows else RULER_HEIGHT + 40
        return min(bottom, self.height())

    def track_at(self, y: int) -> tuple[Track, int, int] | None:
        if y < RULER_HEIGHT:
            return None
        for track, top, height in self.track_rows():
            if top <= y < top + height:
                return track, top, height
        return None

    def clip_rect(self, clip: Clip, top: int, height: int) -> QRectF:
        """Clip body rounded to whole pixels.

        Landing the edges on pixel boundaries keeps the straight sides sharp
        once antialiasing is on, so only the corner arcs get softened — a
        fractional left edge would blur the whole side instead.
        """
        left = round(self.x_of(clip.tl_start))
        right = round(self.x_of(clip.tl_end))
        return QRectF(left, top + 1, max(right - left, 1.0), height - 2)

    def row_for(self, clip: Clip) -> tuple[int, int] | None:
        """(top, height) of the lane a clip is on, if that lane is shown here."""
        for track, top, height in self.track_rows():
            if track.clip_by_id(clip.clip_id) is not None:
                return top, height
        return None

    # -- clip levels -----------------------------------------------------------

    def _gain_band(self, rect: QRectF) -> QRectF:
        """The vertical span the volume line moves in: the clip above its name band."""
        band = min(NAME_BAND_HEIGHT, max(0.0, rect.height() - 8))
        return QRectF(rect.left(), rect.top() + 2, rect.width(), max(1.0, rect.height() - band - 4))

    def _gain_y(self, gain_db: float, rect: QRectF) -> float:
        """Screen y for a gain, through the same curve as the mixer fader.

        Sharing `db_to_fraction` is what makes a clip at unity and a fader at
        unity sit at the same proportional height — the two would otherwise
        disagree about what 0 dB looks like.
        """
        band = self._gain_band(rect)
        return band.bottom() - levels.db_to_fraction(gain_db) * band.height()

    def _gain_db_at(self, y: float, rect: QRectF) -> float:
        band = self._gain_band(rect)
        fraction = (band.bottom() - y) / max(1.0, band.height())
        return levels.fraction_to_db(fraction)

    def _fades_grabbable(self, clip: Clip, rect: QRectF) -> bool:
        """Whether this clip is big enough to offer fade handles.

        The handles live in a square at each top corner, where the trim handle
        already runs the clip's full height. Below these sizes that square would
        swallow most of the trim target, so they are simply not offered — which
        is why the Edit page's short lanes keep behaving exactly as they do now
        and only the Audio page's tall ones grow handles.
        """
        return (
            self.fade_handles
            and clip.kind == "audio"
            and rect.height() >= FADE_GRAB_PX * 4
            and rect.width() >= FADE_HANDLE_PX * 3
        )

    # -- the zoom region -------------------------------------------------------

    def zoom_region_of(self, clip: Clip):
        """The clip's zoom region, or the value a drag on it has reached.

        Everything that draws or measures the band goes through here, so the
        shape follows the mouse for the whole gesture and settles where it is
        let go. Reading `clip.zoom` directly would leave the band pinned to the
        model until the release, which makes a drag feel like it did nothing
        until it suddenly did.
        """
        if self._zoom_clip is not None and self._zoom_clip.clip_id == clip.clip_id:
            return self._zoom_region
        return clip.zoom

    def zoom_band(self, clip: Clip, rect: QRectF) -> QRectF | None:
        """Where a clip's zoom envelope is drawn, or None if it has none.

        The clip's whole picture area, not a strip along the top. A short band
        was legible enough but far too small to aim at: on a 70-pixel lane it
        left a 22-pixel target for four handles, and dragging the edge of a two
        second region became a matter of luck. Filling the body gives every
        handle the clip's full height, which is the same target a trim handle
        gets and is the reason those have never been fiddly.

        The name band along the foot is left out — the words are already there.
        """
        if clip.kind != "video" or self.zoom_region_of(clip) is None:
            return None
        band_height = min(NAME_BAND_HEIGHT, max(0.0, rect.height() - 8))
        body = QRectF(rect)
        body.setBottom(rect.bottom() - band_height)
        if body.height() < 6:
            return None
        return body

    def zoom_marks(self, clip: Clip, rect: QRectF) -> tuple[float, float, float, float] | None:
        """The four x positions of the envelope: start, full, full, end.

        The middle two are where the ramps finish — the corners of the plateau
        — so an instant zoom has all four collapse to two and draws as a square
        block, which is exactly what "no ramp" should look like.
        """
        region = self.zoom_region_of(clip)
        if region is None:
            return None
        left = self.x_of(clip.tl_start + region.start)
        right = self.x_of(clip.tl_start + region.end)
        return (
            left,
            left + region.ramp_in * self.px_per_frame,
            right - region.ramp_out * self.px_per_frame,
            right,
        )

    def _zoom_hit(self, clip: Clip, rect: QRectF, pos: QPoint) -> Zone | None:
        """Which zoom handle, if any, is under the pointer.

        The band is checked before the clip's own body but never before its trim
        edges: a region butting up against the head of a shot must not make the
        shot untrimmable.
        """
        band = self.zoom_band(clip, rect)
        marks = self.zoom_marks(clip, rect)
        if band is None or marks is None or not band.contains(QPointF(pos)):
            return None
        if min(abs(pos.x() - rect.left()), abs(pos.x() - rect.right())) <= TRIM_GRAB_PX:
            return None

        start, ramp_in, ramp_out, end = marks
        # With no ramp its corner sits *exactly* on its edge, so the two cannot
        # be told apart horizontally. Height decides instead, which is what the
        # drawing already says: the plateau corners are the top of the shape and
        # the edges run its full depth. Grabbing high sets how fast the zoom
        # arrives, grabbing low sets when.
        boundary = band.top() + band.height() * ZOOM_CORNER_BAND
        corners = end - start >= ZOOM_NARROW_PX and pos.y() <= boundary
        if corners:
            candidates = [(ramp_in, Zone.ZOOM_RAMP_IN), (ramp_out, Zone.ZOOM_RAMP_OUT)]
        else:
            candidates = [(start, Zone.ZOOM_IN), (end, Zone.ZOOM_OUT)]

        near = [(abs(pos.x() - x), zone) for x, zone in candidates
                if abs(pos.x() - x) <= ZOOM_GRAB_PX]
        if not near:
            return None
        # Keyed on the distance alone. Comparing the tuples lets a tie fall
        # through to the `Zone` members, which have no ordering — and a tie is
        # the *normal* case here, not a corner one, so this raised out of a
        # plain mouse-move as soon as two handles lined up.
        return min(near, key=lambda found: found[0])[1]

    # -- hit testing -----------------------------------------------------------

    def hit_test(self, pos: QPoint) -> Hit | None:
        if pos.x() < HEADER_WIDTH or pos.y() < RULER_HEIGHT:
            return None
        row = self.track_at(pos.y())
        if row is None:
            return None
        track, top, height = row

        for clip in track.clips:
            rect = self.clip_rect(clip, top, height)
            if not (rect.left() <= pos.x() <= rect.right()):
                continue

            # Precedence: fade corner, then trim edge, then volume line, then
            # body. Fade and trim are kept apart by geometry rather than by a
            # modifier — the fade handle owns a small square at the top corner
            # and the trim handle keeps the rest of the edge's height, which on
            # a tall lane is the large majority of it.
            if self._fades_grabbable(clip, rect) and pos.y() - rect.top() <= FADE_GRAB_PX:
                if pos.x() - rect.left() <= FADE_HANDLE_PX:
                    return Hit(track, clip, Zone.FADE_IN)
                if rect.right() - pos.x() <= FADE_HANDLE_PX:
                    return Hit(track, clip, Zone.FADE_OUT)

            # Only offer trim handles when the clip is wide enough that grabbing
            # an edge cannot swallow the whole body.
            if rect.width() > TRIM_GRAB_PX * 3:
                if pos.x() - rect.left() <= TRIM_GRAB_PX:
                    return Hit(track, clip, Zone.IN)
                if rect.right() - pos.x() <= TRIM_GRAB_PX:
                    return Hit(track, clip, Zone.OUT)

            zoom_zone = self._zoom_hit(clip, rect, pos)
            if zoom_zone is not None:
                return Hit(track, clip, zoom_zone)

            # Tested after trim, so a line passing near an edge cannot block one.
            if self.volume_lines and clip.kind == "audio":
                if abs(pos.y() - self._gain_y(clip.gain_db, rect)) <= GAIN_GRAB_PX:
                    return Hit(track, clip, Zone.GAIN)

            return Hit(track, clip, Zone.BODY)
        return None

    # -- snapping --------------------------------------------------------------

    def _snap_targets(self, exclude: set[str]) -> list[int]:
        targets = [0, self.project.playhead]
        for track in self.timeline.tracks:
            for clip in track.clips:
                if clip.clip_id in exclude:
                    continue
                targets.append(clip.tl_start)
                targets.append(clip.tl_end)
        return targets

    def _snap(self, frame: int, exclude: set[str], *, extra_edges: list[int] | None = None) -> tuple[int, int | None]:
        """Pull `frame` to a nearby edge. Returns (frame, the edge snapped to).

        Tolerance is in pixels, not frames, so snapping feels the same at every
        zoom level instead of becoming useless when zoomed in.
        """
        if not self.snapping:
            return frame, None

        tolerance = SNAP_PX / self.px_per_frame
        best_delta = None
        best_target = None

        # Every moving edge is a candidate, so a clip snaps by its tail as well
        # as its head — the usual way you butt one clip against another.
        probes = [0] + list(extra_edges or [])
        for target in self._snap_targets(exclude):
            for probe in probes:
                delta = target - (frame + probe)
                if abs(delta) <= tolerance and (best_delta is None or abs(delta) < abs(best_delta)):
                    best_delta = delta
                    best_target = target

        if best_delta is None:
            return frame, None
        return frame + best_delta, best_target

    # -- painting --------------------------------------------------------------

    def update(self, *args) -> None:
        """Any repaint this widget asks itself for drops the cached picture.

        Blunt on purpose. The cache is only safe if *every* change to what the
        lanes look like invalidates it, and there are twenty-odd places that
        ask for a repaint — remembering to invalidate at each of them is the
        bug waiting to happen. Going through one door means the only way to
        keep a stale cache is to deliberately not use this, which
        `refresh_playhead` does and documents.
        """
        self._cache = None
        super().update(*args)

    def _static_layer(self) -> QPixmap:
        """The lanes, clips and transitions, drawn once and kept.

        The playhead moves thirty or sixty times a second and nothing behind it
        changes, but redrawing it meant laying out every clip, filmstrip and
        waveform on the timeline each time — several milliseconds, taken from
        the thread that also feeds the decoder and the audio device. Keeping
        the picture means a moving playhead costs a blit.
        """
        ratio = self.devicePixelRatioF()
        size = self.size()
        if (
            self._cache is not None
            and self._cache_key == (size.width(), size.height(), ratio)
        ):
            return self._cache

        pixmap = QPixmap(int(size.width() * ratio), int(size.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(theme.BG_DARKEST)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing, False)
        # Everything below the ruler scrolls, so clip it or a lane scrolled up
        # would paint over the timecode strip.
        painter.setClipRect(0, RULER_HEIGHT, size.width(), size.height() - RULER_HEIGHT)
        self._paint_lanes(painter)
        self._paint_clips(painter)
        self._paint_dissolves(painter)
        painter.end()

        self._cache = pixmap
        self._cache_key = (size.width(), size.height(), ratio)
        return pixmap

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.drawPixmap(0, 0, self._static_layer())

        # Live on top of the cached picture: everything that follows the mouse
        # or the playhead. The headers stay here rather than going into the
        # cache because they are painted *over* these — a drop indicator must
        # not run across the track names.
        painter.save()
        painter.setClipRect(0, RULER_HEIGHT, self.width(), self.height() - RULER_HEIGHT)
        self._paint_cut_line(painter)
        self._paint_lane_change(painter)
        self._paint_drop_indicator(painter)
        self._paint_snap_line(painter)
        self._paint_headers(painter)
        painter.restore()

        self._paint_ruler(painter)
        self._paint_playhead(painter)
        painter.end()

    def _paint_lanes(self, painter: QPainter) -> None:
        for _track, top, height in self.track_rows():
            painter.fillRect(
                HEADER_WIDTH, top, self.width() - HEADER_WIDTH, height, theme.BG_DARK
            )

    def _tick_interval(self) -> int:
        """Choose a ruler step that keeps labels roughly 90 px apart."""
        fps = float(self.timeline.timebase.fps)
        seconds_per_90px = 90 / (self.px_per_frame * fps)
        for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600):
            if step >= seconds_per_90px:
                return int(round(step * fps))
        return int(round(3600 * fps))

    def _paint_ruler(self, painter: QPainter) -> None:
        painter.fillRect(0, 0, self.width(), RULER_HEIGHT, theme.BG_DARKEST)
        painter.setPen(theme.BORDER)
        painter.drawLine(0, RULER_HEIGHT - 1, self.width(), RULER_HEIGHT - 1)

        interval = self._tick_interval()
        first, last = self.visible_frames()
        timebase = self.timeline.timebase

        font = QFont(self.font())
        font.setPointSizeF(max(font.pointSizeF() - 1.5, 7.0))
        painter.setFont(font)

        start = (first // interval) * interval
        frame = start
        while frame <= last:
            x = self.x_of(frame)
            if x >= HEADER_WIDTH:
                painter.setPen(theme.TEXT_FAINT)
                painter.drawLine(int(x), RULER_HEIGHT - 7, int(x), RULER_HEIGHT - 1)
                painter.setPen(theme.TEXT_DIM)
                painter.drawText(int(x) + 4, RULER_HEIGHT - 9, timebase.frames_to_timecode(frame))

                # Faint gridline down through the lanes for alignment.
                painter.setPen(QPen(theme.BORDER, 1, Qt.DotLine))
                painter.drawLine(int(x), RULER_HEIGHT, int(x), self.content_height())
            frame += interval

        painter.fillRect(0, 0, HEADER_WIDTH, RULER_HEIGHT, theme.BG_DARKEST)
        painter.setPen(theme.BORDER)
        painter.drawLine(HEADER_WIDTH, 0, HEADER_WIDTH, self.height())

    def _paint_headers(self, painter: QPainter) -> None:
        for track, top, height in self.track_rows():
            painter.fillRect(0, top, HEADER_WIDTH, height, theme.BG_RAISED)
            painter.setPen(theme.BORDER)
            painter.drawLine(0, top + height, HEADER_WIDTH, top + height)

            painter.setPen(theme.TEXT if not track.muted else theme.TEXT_FAINT)
            painter.drawText(11, top + 19, track.name)

            # A faint tint keeps video and audio lanes distinguishable when both
            # are empty, which is the normal state of the spare lanes.
            tint = theme.CLIP_VIDEO if track.kind == "video" else theme.CLIP_AUDIO
            swatch = QColor(tint)
            swatch.setAlpha(70 if not track.muted else 30)
            painter.fillRect(0, top, 3, height, swatch)

            if track.solo:
                # A badge rather than another word in the flag line: solo is the
                # state most likely to explain "why can I not hear that lane",
                # so it has to be findable without reading.
                badge = QRectF(HEADER_WIDTH - 24.0, top + 7.0, 15.0, 14.0)
                painter.setRenderHint(QPainter.Antialiasing, True)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(theme.SOLO))
                painter.drawRoundedRect(badge, 3, 3)
                painter.setRenderHint(QPainter.Antialiasing, False)
                painter.setPen(theme.BG_DARKEST)
                painter.drawText(badge, Qt.AlignCenter, "S")

            flags = []
            if track.muted:
                flags.append("muted")
            if track.locked:
                flags.append("locked")
            if flags:
                painter.setPen(theme.WARN)
                painter.drawText(11, top + 36, " · ".join(flags))

            # The lane's fader position, so the timeline and the mixer cannot
            # appear to disagree about it. Only where there is room for it.
            if track.kind == "audio" and track.gain_db != 0.0 and height >= 44:
                painter.setPen(theme.TEXT_DIM)
                painter.drawText(11, top + height - 8, f"{track.gain_db:+.1f} dB")

    def _clip_colours(self, clip: Clip, selected: bool) -> tuple[QColor, QColor]:
        if clip.is_title:
            # Its own colour, because a title is not a shot: it has no
            # filmstrip, no waveform and no media, and a lane of them should be
            # readable as something else at a glance.
            return (
                (theme.CLIP_TITLE_SEL, theme.CLIP_EDGE_SEL)
                if selected
                else (theme.CLIP_TITLE, theme.CLIP_TITLE_EDGE)
            )
        return self._clip_colours_media(clip, selected)

    def _clip_colours_media(self, clip: Clip, selected: bool) -> tuple[QColor, QColor]:
        if clip.kind == "video":
            fill = theme.CLIP_VIDEO_SEL if selected else theme.CLIP_VIDEO
            edge = theme.CLIP_VIDEO_EDGE
        else:
            fill = theme.CLIP_AUDIO_SEL if selected else theme.CLIP_AUDIO
            edge = theme.CLIP_AUDIO_EDGE
        return fill, theme.CLIP_EDGE_SEL if selected else edge

    @staticmethod
    def _draw_link_icon(painter: QPainter, x: float, y: float, colour: QColor) -> None:
        """The chain mark showing a clip is linked to its A/V partner.

        Hand-drawn rather than a font glyph: at 11 pixels an emoji chain renders
        differently on every system, and half of them are colour bitmaps that
        ignore the pen entirely.
        """
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(colour, 1.4)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        # Two interlocking links, overlapping so they read as a chain.
        painter.drawRoundedRect(QRectF(x, y + 1.5, 7.0, 5.0), 2.5, 2.5)
        painter.drawRoundedRect(QRectF(x + 4.5, y + 1.5, 7.0, 5.0), 2.5, 2.5)
        painter.restore()

    @staticmethod
    def _draw_magnifier(painter: QPainter, x: float, y: float, colour: QColor,
                        size: float = 9.0) -> None:
        """A magnifier, marking the band as a zoom rather than some other span.

        The band is a coloured shape on a busy lane and nothing about a
        trapezoid says "zoom" on its own. Hand-drawn for the same reason the
        link mark is: at this size a font glyph is a lottery.
        """
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(colour, 1.3)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        lens = size * 0.62
        painter.drawEllipse(QRectF(x, y, lens, lens))
        # The handle, out of the lower right at the usual forty-five degrees.
        painter.drawLine(
            QPointF(x + lens * 0.85, y + lens * 0.85),
            QPointF(x + size, y + size),
        )
        painter.restore()

    @staticmethod
    def _draw_mute_icon(painter: QPainter, x: float, y: float, colour: QColor) -> None:
        """A crossed-out speaker, marking a clip that has been silenced.

        Hand-drawn for the same reason as the link mark: at this size a font
        glyph is a lottery across platforms.
        """
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(colour, 1.4)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QBrush(colour))
        # Cone: a small box at the left with a triangle opening to the right.
        cone = QPainterPath()
        cone.moveTo(x + 0.5, y + 3.0)
        cone.lineTo(x + 2.5, y + 3.0)
        cone.lineTo(x + 5.5, y + 0.5)
        cone.lineTo(x + 5.5, y + 8.5)
        cone.lineTo(x + 2.5, y + 6.0)
        cone.lineTo(x + 0.5, y + 6.0)
        cone.closeSubpath()
        painter.fillPath(cone, QBrush(colour))
        # The slash, drawn across the whole mark so it reads as "off" at a glance.
        painter.drawLine(QPointF(x + 7.0, y + 1.5), QPointF(x + 12.0, y + 7.5))
        painter.restore()

    @staticmethod
    def _draw_reverse_icon(painter: QPainter, x: float, y: float, colour: QColor) -> None:
        """Two chevrons pointing back, marking a clip that plays backwards."""
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(colour))
        for offset in (0.0, 5.0):
            arrow = QPainterPath()
            arrow.moveTo(x + offset + 4.5, y + 0.5)
            arrow.lineTo(x + offset + 4.5, y + 8.5)
            arrow.lineTo(x + offset + 0.5, y + 4.5)
            arrow.closeSubpath()
            painter.fillPath(arrow, QBrush(colour))
        painter.restore()

    @staticmethod
    def _draw_framing_icon(
        painter: QPainter, x: float, y: float, colour: QColor, moving: bool = False
    ) -> None:
        """A frame with a smaller frame inside it: this clip has been reframed.

        Drawn rather than a font glyph, like the marks beside it — at eleven
        pixels an emoji renders differently on every machine, and this has to be
        recognisable at a glance or it is not worth the width it costs.
        """
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, False)
        pen = QPen(colour)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(QRectF(x + 0.5, y + 0.5, 10.0, 8.0))
        painter.fillRect(QRectF(x + 3.5, y + 2.5, 4.0, 4.0), QBrush(colour))
        if moving:
            # A second, smaller frame off to one side: the picture is going
            # somewhere. Enough to tell the two apart down a lane of clips.
            painter.drawRect(QRectF(x + 2.5, y + 1.5, 6.0, 5.0))
        painter.restore()

    def _paint_clips(self, painter: QPainter) -> None:
        selected = set(self.project.selected_ids)
        dragging = {clip.clip_id for clip in self._drag_clips}
        metrics = QFontMetrics(self.font())
        visible_first, visible_last = self.visible_frames()

        for track, top, height in self.track_rows():
            for clip in track.clips:
                if clip.tl_end < visible_first or clip.tl_start > visible_last:
                    continue

                rect = self.clip_rect(clip, top, height)
                ghost = False

                # Reflect an in-progress drag without having touched the model.
                if self._mode is Mode.MOVE and clip.clip_id in dragging:
                    moved_lane = (
                        self._drag_target is not None
                        and self._drag_target is not track
                        and clip.kind == self._drag_kind
                    )
                    if moved_lane:
                        # Drawn on the destination lane, so the drop target is
                        # obvious before the mouse is released.
                        continue
                    rect = rect.translated(self._drag_delta * self.px_per_frame, 0)
                    ghost = True
                elif self._mode is Mode.TRIM and self._trim_clip is not None:
                    if clip.clip_id in {c.clip_id for c in self._drag_clips}:
                        if self._trim_edge == "in":
                            rect.setLeft(self.x_of(self._trim_frame))
                        else:
                            rect.setRight(self.x_of(self._trim_frame))
                        ghost = True

                if rect.width() < 1:
                    continue

                is_selected = clip.clip_id in selected
                fill, border = self._clip_colours(clip, is_selected)
                if ghost:
                    fill = QColor(fill)
                    fill.setAlpha(190)

                # A gain or fade drag is shown at its proposed value without the
                # model having been touched, the same as a move or a trim.
                dragging_this = (
                    self._level_clip is not None
                    and self._level_clip.clip_id == clip.clip_id
                )
                gain_db = self._gain_db if (dragging_this and self._mode is Mode.GAIN) else clip.gain_db
                fade_in, fade_out = clip.fade_in, clip.fade_out
                if dragging_this and self._mode is Mode.FADE:
                    if self._fade_edge == "in":
                        fade_in = self._fade_frames
                    else:
                        fade_out = self._fade_frames

                self._paint_clip_body(
                    painter, clip, rect, fill, border, is_selected, metrics,
                    gain_db=gain_db, fade_in=fade_in, fade_out=fade_out,
                )

    def _paint_clip_body(
        self,
        painter: QPainter,
        clip: Clip,
        rect: QRectF,
        fill: QColor,
        border: QColor,
        is_selected: bool,
        metrics: QFontMetrics,
        *,
        gain_db: float = 0.0,
        fade_in: int = 0,
        fade_out: int = 0,
    ) -> None:
        """Content on top, a solid name band along the foot, edge over both.

        The name sits in its own band rather than floating over the picture: it
        stays legible against any frame without needing a scrim, and it gives the
        eye a consistent line to read a lane along.
        """
        # Content gets whatever is left above the band, so a short lane loses
        # thumbnail height rather than losing the name.
        band_height = min(NAME_BAND_HEIGHT, max(0.0, rect.height() - 8))
        content = QRectF(rect)
        content.setBottom(rect.bottom() - band_height)

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
        painter.setRenderHint(QPainter.Antialiasing, False)

        if content.height() > 4 and rect.width() > 4:
            if clip.kind == "audio":
                self._paint_waveform(painter, clip, content)
                # Over the waveform, under the name band: these describe what
                # you are looking at, so they have to sit on top of it.
                self._paint_fades(painter, clip, rect, content, fade_in, fade_out)
                if self.volume_lines:
                    self._paint_gain_line(painter, rect, gain_db, metrics)
            elif clip.is_title:
                # Its words rather than its frames, and unconditionally: the
                # Thumbs setting is about filmstrips, and a title has none.
                self._paint_title_body(painter, clip, content)
            elif self.show_filmstrips:
                self._paint_filmstrip(painter, clip, content)
            if clip.kind == "video":
                # Over the frames, so it describes what it covers.
                self._paint_zoom(painter, clip, rect)

        if band_height > 0:
            # Clipping to the rounded body lets the band keep the bottom corners.
            painter.save()
            path = QPainterPath()
            path.addRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
            painter.setClipPath(path)
            painter.fillRect(
                QRectF(rect.left(), rect.bottom() - band_height, rect.width(), band_height),
                fill,
            )
            painter.restore()

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(border, 2 if is_selected else 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), CLIP_RADIUS, CLIP_RADIUS)
        painter.setRenderHint(QPainter.Antialiasing, False)

        if band_height <= 0:
            return

        text_colour = QColor(255, 255, 255, 240)
        cursor = rect.left() + 6.0
        baseline = rect.bottom() - band_height / 2 + 4.0
        available = rect.width() - 12.0

        if clip.link_id is not None and available > 30:
            self._draw_link_icon(painter, cursor, baseline - 10.0, text_colour)
            cursor += 16.0
            available -= 16.0

        # State marks come before the name, so a lane of clips can be read down
        # its left edge without stopping to parse each name.
        if clip.reversed and available > 30:
            self._draw_reverse_icon(painter, cursor, baseline - 11.0, text_colour)
            cursor += 14.0
            available -= 14.0

        if clip.kind == "audio" and clip.muted and available > 30:
            self._draw_mute_icon(painter, cursor, baseline - 11.0, theme.WARN)
            cursor += 17.0
            available -= 17.0

        if clip.kind == "video" and clip.has_framing and available > 30:
            self._draw_framing_icon(
                painter, cursor, baseline - 10.0, text_colour, clip.has_move
            )
            cursor += 15.0
            available -= 15.0

        # A title's words are already written across its body, so the band
        # would only say the same thing twice — unless the clip is too small
        # for the body, in which case the band is the only place left.
        if available > 12 and not (clip.is_title and self._title_body_fits(rect)):
            painter.setPen(text_colour)
            painter.drawText(
                int(cursor),
                int(baseline),
                metrics.elidedText(clip.name or "clip", Qt.ElideMiddle, int(available)),
            )

    def _paint_zoom(self, painter: QPainter, clip: Clip, rect: QRectF) -> None:
        """The zoom region, drawn as the envelope it actually is.

        A trapezoid: up over the ramp in, flat while the zoom holds, down over
        the ramp out. It is the same picture as the fade wedge on an audio clip
        and it is the same arithmetic underneath, so the shape can be read
        without being explained — a square end is an instant cut to the zoom, a
        sloped one is a glide, and how far the slope reaches is how long it
        takes.
        """
        band = self.zoom_band(clip, rect)
        marks = self.zoom_marks(clip, rect)
        if band is None or marks is None:
            return
        start, ramp_in, ramp_out, end = marks
        if end - start < 1.0:
            return

        painter.save()
        body = QPainterPath()
        body.addRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
        painter.setClipPath(body)
        painter.setRenderHint(QPainter.Antialiasing, True)

        # Inset from the band's full height: the handles want the whole body to
        # aim at, but a shape filling it would bury the frames underneath.
        depth = min(band.height() - 4, max(14.0, band.height() * 0.72))
        bottom = band.center().y() + depth / 2
        top = bottom - depth
        # A dark plate under the span first. Without it the envelope is drawn
        # straight onto the filmstrip, and a thin warm line over a bright frame
        # is invisible — which on real footage means the region is there and
        # cannot be seen, let alone grabbed.
        backing = QColor(theme.BG_DARKEST)
        backing.setAlpha(165)
        painter.fillRect(QRectF(start, top, end - start, bottom - top), backing)

        shape = QPainterPath()
        shape.moveTo(start, bottom)
        shape.lineTo(ramp_in, top)
        shape.lineTo(ramp_out, top)
        shape.lineTo(end, bottom)
        shape.closeSubpath()

        lit = self._zoom_hover == clip.clip_id
        fill = QColor(theme.ZOOM_REGION)
        fill.setAlpha(190 if lit else 150)
        painter.fillPath(shape, fill)
        painter.setPen(QPen(QColor(255, 255, 255) if lit else theme.ZOOM_REGION,
                            2.0 if lit else 1.6))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(shape)

        # A dot on each corner of the shape, which is exactly where the four
        # handles are: the two on the floor move where the zoom starts and
        # stops, the two on the plateau set how fast it gets there. White
        # because the block is amber and a grip has to be findable against it.
        painter.setPen(QPen(QColor(40, 40, 40, 200), 1.0))
        painter.setBrush(QColor(255, 255, 255))
        grips = [(start, bottom), (end, bottom)]
        if end - start >= ZOOM_NARROW_PX:
            grips += [(ramp_in, top), (ramp_out, top)]
        radius = 3.2 if lit else 2.6
        for x, y in grips:
            painter.drawEllipse(QPointF(x, y), radius, radius)

        # The magnifier goes on the plateau, which is the one part of the shape
        # guaranteed to be at full height. Only when it fits without crowding
        # the grips — a mark jammed against a handle reads as decoration on it.
        icon = min(10.0, (bottom - top) - 4.0)
        plateau = ramp_out - ramp_in
        if icon >= 7.0 and plateau >= icon * 2.6:
            self._draw_magnifier(
                painter,
                (ramp_in + ramp_out) / 2 - icon / 2,
                top + ((bottom - top) - icon) / 2,
                QColor(20, 20, 20, 210),
                icon,
            )
        painter.restore()

    def _paint_fades(
        self,
        painter: QPainter,
        clip: Clip,
        rect: QRectF,
        content: QRectF,
        fade_in: int,
        fade_out: int,
    ) -> None:
        """Shade what the fade takes away, and draw the ramp over the waveform.

        Shading the attenuated wedge rather than only drawing a diagonal is what
        makes the length of a fade readable at a glance: a bare line reads as
        decoration, a wedge of dimmed waveform reads as a fade.

        Painted on every canvas, not only the one where the handles can be
        grabbed — a fade set on the Audio page has to be visible on the Edit
        page, or the two views disagree about the edit.
        """
        if fade_in <= 0 and fade_out <= 0:
            return

        shade = QColor(theme.BG_DARKEST)
        shade.setAlpha(140)
        pen = QPen(theme.FADE_CURVE, 1.4)

        painter.save()
        body = QPainterPath()
        body.addRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
        painter.setClipPath(body)
        painter.setRenderHint(QPainter.Antialiasing, True)

        top, bottom = content.top(), content.bottom()
        for frames, edge in ((fade_in, "in"), (fade_out, "out")):
            if frames <= 0:
                continue
            width = frames * self.px_per_frame
            if width < 1.0:
                continue
            if edge == "in":
                corner, inner = rect.left(), rect.left() + width
            else:
                corner, inner = rect.right(), rect.right() - width

            wedge = QPainterPath()
            wedge.moveTo(corner, top)
            wedge.lineTo(inner, top)
            wedge.lineTo(corner, bottom)
            wedge.closeSubpath()
            painter.fillPath(wedge, shade)

            painter.setPen(pen)
            painter.drawLine(QPointF(corner, bottom), QPointF(inner, top))

            if self._fades_grabbable(clip, rect):
                painter.setBrush(QBrush(theme.FADE_CURVE))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(QPointF(inner, top + 1.0), 3.0, 3.0)

        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.restore()

    def _paint_gain_line(
        self, painter: QPainter, rect: QRectF, gain_db: float, metrics: QFontMetrics
    ) -> None:
        """A horizontal line across the clip at its gain, with a unity reference.

        Two lines rather than one when the clip is off unity: the dotted 0 dB
        reference gives the solid line something to be read against, so "quieter
        than it was recorded" is visible without reading a number.
        """
        painter.save()
        body = QPainterPath()
        body.addRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
        painter.setClipPath(body)

        if gain_db != 0.0:
            painter.setPen(QPen(theme.TEXT_FAINT, 1, Qt.DotLine))
            unity = self._gain_y(0.0, rect)
            painter.drawLine(QPointF(rect.left(), unity), QPointF(rect.right(), unity))

        y = self._gain_y(gain_db, rect)
        painter.setPen(QPen(theme.GAIN_LINE, 1.6))
        painter.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))

        if gain_db != 0.0 and rect.width() > 70:
            label = f"{gain_db:+.1f} dB"
            painter.setPen(theme.GAIN_LINE)
            painter.drawText(
                int(rect.right() - metrics.horizontalAdvance(label) - 6),
                int(max(rect.top() + metrics.ascent(), y - 3)),
                label,
            )
        painter.restore()

    def _paint_dissolves(self, painter: QPainter) -> None:
        """A triangle across the head of each clip that dissolves in.

        Drawn over the clips rather than inside one of them, because a
        transition belongs to the join: it is the one mark on the timeline that
        is about two clips at once, and putting it half in each is what makes a
        row of shots readable as a sequence rather than a list.
        """
        for track, top, height in self.track_rows():
            if track.kind != "video" or top + height < RULER_HEIGHT:
                continue
            for clip in track.clips:
                found = track.dissolve_before(clip)
                if found is None:
                    continue
                left = self.x_of(clip.tl_start)
                right = self.x_of(clip.tl_start + found[1])
                if right < HEADER_WIDTH or left > self.width() or right - left < 2:
                    continue

                body = QRectF(left, top + 1.0, right - left, height - 2.0)
                painter.save()
                painter.setClipRect(
                    QRectF(HEADER_WIDTH, RULER_HEIGHT, self.width(), self.height())
                )
                painter.setRenderHint(QPainter.Antialiasing, True)
                wedge = QPainterPath()
                wedge.moveTo(body.left(), body.bottom())
                wedge.lineTo(body.right(), body.top())
                wedge.lineTo(body.right(), body.bottom())
                wedge.closeSubpath()
                painter.fillPath(wedge, QBrush(QColor(255, 255, 255, 42)))
                pen = QPen(QColor(255, 255, 255, 150))
                pen.setWidthF(1.0)
                painter.setPen(pen)
                painter.drawLine(
                    QPointF(body.left(), body.bottom()),
                    QPointF(body.right(), body.top()),
                )
                painter.restore()

    def _paint_lane_change(self, painter: QPainter) -> None:
        """Draw clips that are being dragged onto a different lane."""
        target = self._drag_target
        if self._mode is not Mode.MOVE or target is None or target is self._drag_track:
            return

        rows = {track.track_id: (top, height) for track, top, height in self.track_rows()}
        geometry = rows.get(target.track_id)
        if geometry is None:
            return
        top, height = geometry

        # Tint the destination lane so it reads as the drop target.
        highlight = QColor(theme.ACCENT)
        highlight.setAlpha(28)
        painter.fillRect(HEADER_WIDTH, top, self.width() - HEADER_WIDTH, height, highlight)

        metrics = QFontMetrics(self.font())
        for clip in self._drag_clips:
            if clip.kind != self._drag_kind:
                continue
            rect = self.clip_rect(clip, top, height).translated(
                self._drag_delta * self.px_per_frame, 0
            )
            if rect.width() < 1:
                continue

            fill, _ = self._clip_colours(clip, True)
            fill = QColor(fill)
            fill.setAlpha(200)
            self._paint_clip_body(
                painter, clip, rect, fill, theme.CLIP_EDGE_SEL, True, metrics
            )

    @staticmethod
    def _title_body_fits(rect: QRectF) -> bool:
        """Whether a title clip is big enough to show its words across itself.

        The one predicate, asked in two places: the body draws only when it
        fits, and the name band steps aside only when the body took over.
        """
        return rect.width() >= 46 and rect.height() >= NAME_BAND_HEIGHT + 12

    def _paint_title_body(self, painter: QPainter, clip: Clip, rect: QRectF) -> None:
        """The title's own words across the clip, as its thumbnail.

        A title has no frames to show a filmstrip of, and its name band already
        holds the first line elided to nothing useful. Drawing the actual text —
        centred, in the position it will appear on screen — means a lane of
        titles can be read the way a lane of shots can.
        """
        if not self._title_body_fits(rect):
            return
        body = rect.adjusted(6, 3, -6, -(NAME_BAND_HEIGHT + 1))

        painter.save()
        painter.setClipRect(body)
        font = QFont(painter.font())
        font.setPixelSize(max(9, min(int(body.height() * 0.62), 15)))
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 225))

        text = " · ".join(line for line in clip.title.lines if line.strip())
        metrics = QFontMetrics(font)
        painter.drawText(
            body,
            int(Qt.AlignCenter),
            metrics.elidedText(text or "Title", Qt.ElideRight, int(body.width())),
        )
        painter.restore()

    def _paint_filmstrip(self, painter: QPainter, clip: Clip, rect: QRectF) -> None:
        if clip.is_title:
            return   # nothing to show frames of
        strip = self.filmstrips.get(clip.media_id, self.project.proxies.strip_for(clip.media_id))
        if strip is None:
            return

        fps = float(self.timeline.timebase.fps)
        if fps <= 0 or self.px_per_frame <= 0:
            return

        painter.save()
        # Clip to the rounded body so thumbnails cannot square off the corners.
        path = QPainterPath()
        path.addRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
        painter.setClipPath(path)

        # A retimed clip plays through its source faster or slower, and the strip
        # should show the frames it actually plays.
        seconds_per_pixel = clip.speed / (self.px_per_frame * fps)
        start_seconds = clip.src_in / fps

        # The visible left edge may be scrolled off; start from what is on
        # screen.
        #
        # Deliberately *not* narrowed to the repaint's damaged area, tempting
        # though it is: `draw_filmstrip` tiles from `rect.left()`, so a narrower
        # rect re-phases every thumbnail in it and crops the last one. Under a
        # moving playhead that redraws the strip at a different alignment on
        # every frame, which reads as the thumbnails squashing and jittering as
        # the marker passes over them.
        hidden = max(0.0, HEADER_WIDTH - rect.left())
        visible = QRectF(rect)
        if hidden > 0:
            visible.setLeft(rect.left() + hidden)
            start_seconds += hidden * seconds_per_pixel

        draw_filmstrip(
            painter,
            visible,
            strip,
            start_seconds=start_seconds,
            seconds_per_pixel=seconds_per_pixel,
        )
        painter.restore()

    def _paint_waveform(self, painter: QPainter, clip: Clip, rect: QRectF) -> None:
        peaks = self.waveforms.get(clip.media_id, self.project.proxies.peaks_for(clip.media_id))
        if peaks is None:
            return
        painter.save()
        # Clip to the rounded body, so the waveform cannot square off the corners.
        path = QPainterPath()
        path.addRoundedRect(rect, CLIP_RADIUS, CLIP_RADIUS)
        painter.setClipPath(path)
        draw_waveform(
            painter,
            rect.adjusted(2, 2, -2, -1),
            peaks,
            src_in=clip.src_in,
            src_out=clip.src_out,
            timebase=self.timeline.timebase,
            # A muted clip keeps its waveform — you still need to see what is in
            # there to decide to unmute it — but greyed, so a muted block is
            # obvious across a lane rather than only from its badge.
            colour=theme.TEXT_FAINT if clip.muted else theme.WAVEFORM,
        )
        painter.restore()

    def refresh_playhead(self) -> None:
        """Repaint for a playhead that has moved, and little else.

        During playback this fires at the frame rate, and a whole-canvas repaint
        costs several milliseconds of ruler, clips, filmstrips and waveforms —
        all to move a one-pixel line. Sixty times a second that is a large share
        of the UI thread, and the thread it is taken from is the one feeding the
        decoder and the audio device, so the picture falls behind the sound.

        Only the strip the marker vacated and the one it arrived in need
        redrawing. A scroll invalidates that reasoning, so the auto-scroll during
        playback still takes the full path.
        """
        was = self._painted_playhead
        now = (self.x_of(self.project.playhead), self.scroll_x)
        if was is None or was[1] != now[1]:
            self.update()
            return
        self._painted_playhead = now
        for x in (was[0], now[0]):
            # Wide enough for the marker's arrowhead, which overhangs the line.
            # Deliberately not `self.update`, which would throw the cached lanes
            # away — the whole point here is that nothing behind the marker has
            # changed, so the repaint is a blit and two thin strips.
            super().update(int(x) - 8, 0, 17, self.height())

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self.x_of(self.project.playhead)
        self._painted_playhead = (x, self.scroll_x)
        if x < HEADER_WIDTH:
            return
        painter.setPen(QPen(theme.PLAYHEAD, 1))
        painter.drawLine(int(x), 0, int(x), self.content_height())
        painter.setBrush(QBrush(theme.PLAYHEAD))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(
            QPolygon([QPoint(int(x) - 5, 0), QPoint(int(x) + 5, 0), QPoint(int(x), 8)])
        )

    def _paint_cut_line(self, painter: QPainter) -> None:
        """Where the scissors would land, previewed as you move.

        The point of a cut tool over the X key is that you can see the frame you
        are about to split before committing to it, so this line is most of the
        feature rather than decoration on it.
        """
        if self._cut_frame is None:
            return
        x = self.x_of(self._cut_frame)
        if x < HEADER_WIDTH or x > self.width():
            return
        pen = QPen(theme.WARN)
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.drawLine(int(x), RULER_HEIGHT, int(x), self.content_height())

        # The timecode of the cut, so an exact split does not need the playhead
        # parked on it first.
        label = self.project.timebase.frames_to_timecode(self._cut_frame)
        metrics = QFontMetrics(painter.font())
        width = metrics.horizontalAdvance(label) + 8
        box = QRectF(min(x + 4, self.width() - width - 2), RULER_HEIGHT + 2, width, 15)
        painter.fillRect(box, QBrush(QColor(0, 0, 0, 170)))
        painter.setPen(theme.WARN)
        painter.drawText(box, Qt.AlignCenter, label)

    def _paint_snap_line(self, painter: QPainter) -> None:
        if self._snap_line is None:
            return
        x = self.x_of(self._snap_line)
        if x < HEADER_WIDTH:
            return
        painter.setPen(QPen(theme.WARN, 1, Qt.DashLine))
        painter.drawLine(int(x), RULER_HEIGHT, int(x), self.content_height())

    def _paint_drop_indicator(self, painter: QPainter) -> None:
        if self._drop_frame is None:
            return
        x = self.x_of(self._drop_frame)
        painter.setPen(QPen(theme.ACCENT, 2))
        painter.drawLine(int(x), RULER_HEIGHT, int(x), self.content_height())

    # -- mouse -----------------------------------------------------------------

    def mouseDoubleClickEvent(self, event) -> None:
        """Open a title for editing. Double-click is where people look for it,
        and it is the only clip type with anything to open."""
        hit = self.hit_test(event.position().toPoint())
        if hit is not None and hit.clip.is_title:
            self.edit_title(hit.clip)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _cut_at(self, x: float) -> None:
        """Cut every lane at a point on the ruler, like pressing X does there.

        Every lane rather than only the one under the pointer: a shot and its
        sound are two clips on two tracks, and cutting the picture while
        leaving the sound whole is never what anyone meant.
        """
        frame = max(0, self.frame_of(x))
        if not self.project.edit("Cut", lambda t: ops.razor(t, frame)):
            self.status_message.emit("Nothing to cut there")

    def mousePressEvent(self, event) -> None:
        if (
            self._tool is Tool.CUT
            and event.button() == Qt.LeftButton
            and event.position().x() >= HEADER_WIDTH
            and event.position().y() >= RULER_HEIGHT
        ):
            self._cut_at(event.position().x())
            event.accept()
            return
        self._press_event(event)

    def _press_event(self, event) -> None:
        self.setFocus()
        pos = event.position().toPoint()

        if event.button() == Qt.RightButton:
            # Right-click opens a menu; it must never scrub or begin a drag.
            self._open_context_menu(pos, event.globalPosition().toPoint())
            return

        self._press_pos = pos
        self._snap_line = None

        if pos.x() < HEADER_WIDTH:
            self._toggle_header(pos)
            return

        if pos.y() < RULER_HEIGHT:
            self._mode = Mode.SCRUB
            self._set_playhead_from(pos.x())
            return

        hit = self.hit_test(pos)
        if hit is None:
            self.project.set_selection([])
            self._mode = Mode.SCRUB
            self._set_playhead_from(pos.x())
            return

        additive = bool(event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier))
        self._select(hit.clip, additive=additive)

        if hit.zone is Zone.BODY:
            self._mode = Mode.MOVE
            self._press_frame = self.frame_of(pos.x())
            self._drag_delta = 0
            self._drag_clips = ops.expand_links(self.timeline, self.project.selected_clips())
            self._drag_track = hit.track
            self._drag_target = hit.track
            self._drag_kind = hit.clip.kind
        elif hit.zone is Zone.GAIN:
            self._mode = Mode.GAIN
            self._level_clip = hit.clip
            self._gain_db = hit.clip.gain_db
            self._gain_press_db = hit.clip.gain_db
        elif hit.zone in ZOOM_ZONES:
            self._mode = Mode.ZOOM
            self._zoom_clip = hit.clip
            self._zoom_zone = hit.zone
            self._zoom_region = hit.clip.zoom
        elif hit.zone in (Zone.FADE_IN, Zone.FADE_OUT):
            self._mode = Mode.FADE
            self._level_clip = hit.clip
            self._fade_edge = "in" if hit.zone is Zone.FADE_IN else "out"
            self._fade_frames = (
                hit.clip.fade_in if self._fade_edge == "in" else hit.clip.fade_out
            )
        else:
            self._mode = Mode.TRIM
            self._trim_clip = hit.clip
            self._trim_edge = "in" if hit.zone is Zone.IN else "out"
            self._trim_frame = hit.clip.tl_start if hit.zone is Zone.IN else hit.clip.tl_end
            self._drag_clips = ops.expand_links(self.timeline, [hit.clip])
        self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._tool is Tool.CUT:
            pos = event.position().toPoint()
            inside = pos.x() >= HEADER_WIDTH and pos.y() >= RULER_HEIGHT
            frame = max(0, self.frame_of(pos.x())) if inside else None
            if frame != self._cut_frame:
                self._cut_frame = frame
                self.update()
            return
        self._move_event(event)

    def _move_event(self, event) -> None:
        pos = event.position().toPoint()

        if self._mode is Mode.IDLE:
            self._update_cursor(pos)
            return

        if self._mode is Mode.SCRUB:
            self._set_playhead_from(pos.x())
            return

        if self._mode is Mode.MOVE and self._drag_clips:
            raw = self.frame_of(pos.x()) - self._press_frame
            earliest = min(clip.tl_start for clip in self._drag_clips)
            raw = max(raw, -earliest)

            moving = {clip.clip_id for clip in self._drag_clips}
            widths = [clip.tl_end - earliest for clip in self._drag_clips]
            snapped, target = self._snap(earliest + raw, moving, extra_edges=widths)
            self._drag_delta = snapped - earliest
            self._snap_line = target
            self._drag_target = self._lane_under(pos.y())
            self.update()
            return

        if self._mode is Mode.GAIN and self._level_clip is not None:
            clip = self._level_clip
            row = self.row_for(clip)
            if row is not None:
                rect = self.clip_rect(clip, row[0], row[1])
                if event.modifiers() & Qt.ShiftModifier:
                    # Fine adjust: a tenth of the travel, measured from the press
                    # rather than from the pointer, so it cannot jump on the
                    # frame the modifier goes down.
                    delta = self._gain_db_at(pos.y(), rect) - self._gain_db_at(
                        self._press_pos.y(), rect
                    )
                    wanted = self._gain_press_db + delta * 0.1
                else:
                    wanted = self._gain_db_at(pos.y(), rect)
                # A detent at unity: 0 dB is the value people want most and the
                # hardest to hit by eye.
                if abs(wanted) < 0.4:
                    wanted = 0.0
                self._gain_db = max(MIN_GAIN_DB, min(MAX_GAIN_DB, wanted))
                self.status_message.emit(f"{clip.name or 'Clip'}: {self._gain_db:+.1f} dB")
                self.update()
            return

        if self._mode is Mode.FADE and self._level_clip is not None:
            clip = self._level_clip
            if self._fade_edge == "in":
                wanted = self.frame_of(pos.x()) - clip.tl_start
                other = clip.fade_out
            else:
                wanted = clip.tl_end - self.frame_of(pos.x())
                other = clip.fade_in
            self._fade_frames = max(0, min(wanted, clip.duration - other))
            seconds = float(self.timeline.timebase.frames_to_seconds(self._fade_frames))
            self.status_message.emit(f"Fade {self._fade_edge} {seconds:.2f}s")
            self.update()
            return

        if self._mode is Mode.ZOOM and self._zoom_clip is not None:
            self._drag_zoom(self.frame_of(pos.x()))
            self.update()
            return

        self._track_zoom_hover(pos)

        if self._mode is Mode.TRIM and self._trim_clip is not None:
            low, high = ops.trim_bounds(self.timeline, self._trim_clip, self._trim_edge)
            wanted = self.frame_of(pos.x())
            moving = {clip.clip_id for clip in self._drag_clips}
            wanted, target = self._snap(wanted, moving)
            self._trim_frame = max(low, min(high, wanted))
            self._snap_line = target if target == self._trim_frame else None
            self.update()

    def _drag_zoom(self, frame: int) -> None:
        """One of the four zoom handles, dragged to a timeline frame.

        The edges move where the zoom starts and stops; the corners set how long
        it takes to get there. A corner is measured *inwards* from its own edge,
        which is what makes dragging it back out to the edge mean "instant" —
        the gesture and the value agree, so nothing has to be explained.

        Clamping rather than refusing, for the same reason a trim clamps: the
        mouse goes where it likes and a handle should stop at its limit.
        """
        clip, region = self._zoom_clip, self._zoom_region
        if clip is None or region is None:
            return
        local = max(0, min(frame - clip.tl_start, clip.duration))

        if self._zoom_zone is Zone.ZOOM_IN:
            start = min(local, region.end - 1)
            self._zoom_region = self._fit_ramps(region, start, region.end)
        elif self._zoom_zone is Zone.ZOOM_OUT:
            end = max(local, region.start + 1)
            self._zoom_region = self._fit_ramps(region, region.start, end)
        elif self._zoom_zone is Zone.ZOOM_RAMP_IN:
            room = region.length - region.ramp_out
            self._zoom_region = replace(
                region, ramp_in=max(0, min(local - region.start, room))
            )
        elif self._zoom_zone is Zone.ZOOM_RAMP_OUT:
            room = region.length - region.ramp_in
            self._zoom_region = replace(
                region, ramp_out=max(0, min(region.end - local, room))
            )
        self.status_message.emit(self._zoom_summary(self._zoom_region))

    @staticmethod
    def _fit_ramps(region, start: int, end: int):
        """The region moved to a new span, with both ramps pulled inside it.

        Both, not only the one nearest the handle being dragged: collapsing the
        tail onto the head leaves a region one frame long, and a ramp left at
        its old length there is longer than the zoom it belongs to — which the
        model refuses, mid-drag, as an exception out of a mouse event.
        """
        length = end - start
        ramp_in = max(0, min(region.ramp_in, length))
        ramp_out = max(0, min(region.ramp_out, length - ramp_in))
        return replace(
            region, start=start, end=end, ramp_in=ramp_in, ramp_out=ramp_out
        )

    def _zoom_summary(self, region) -> str:
        fps = float(self.timeline.timebase.fps) or 30.0
        ramps = []
        for frames, label in ((region.ramp_in, "in"), (region.ramp_out, "out")):
            ramps.append("instant" if frames == 0 else f"{frames / fps:.1f}s {label}")
        return (
            f"Zoom {region.framing.zoom:.2f}× for {region.length / fps:.1f}s "
            f"({', '.join(ramps)})"
        ).replace(".00×", "×")

    def leaveEvent(self, event) -> None:
        if self._zoom_hover is not None:
            self._zoom_hover = None
            self.update()
        if self._cut_frame is not None:
            self._cut_frame = None
            self.update()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._tool is Tool.CUT:
            event.accept()
            return
        self._release_event(event)

    def _release_event(self, event) -> None:
        mode, self._mode = self._mode, Mode.IDLE
        self._snap_line = None

        if mode is Mode.MOVE and self._drag_clips:
            clips, delta = list(self._drag_clips), self._drag_delta
            target = self._drag_target
            changed_lane = target is not None and target is not self._drag_track
            if delta != 0 or changed_lane:
                label = "Move clip to " + target.name if changed_lane else "Move clip"
                try:
                    self.project.edit(
                        label,
                        lambda t: ops.move_clips(t, clips, delta, target_track=target),
                    )
                except TimelineError as exc:
                    self.status_message.emit(str(exc))
        elif mode is Mode.TRIM and self._trim_clip is not None:
            clip, edge, frame = self._trim_clip, self._trim_edge, self._trim_frame
            anchor = clip.tl_start if edge == "in" else clip.tl_end
            if frame != anchor:
                self.project.edit(
                    f"Trim {edge}", lambda t: ops.trim(t, clip, edge, frame)
                )
        elif mode is Mode.ZOOM and self._zoom_clip is not None:
            clip, region = self._zoom_clip, self._zoom_region
            self._zoom_clip = self._zoom_zone = self._zoom_region = None
            if region is not None and region != clip.zoom:
                self._run("Adjust zoom", ops.set_zoom_region, [clip], region)
        elif mode is Mode.GAIN and self._level_clip is not None:
            clip, gain = self._level_clip, self._gain_db
            if abs(gain - clip.gain_db) > 1e-6:
                self._run("Clip gain", ops.set_clip_gain, [clip], gain)
        elif mode is Mode.FADE and self._level_clip is not None:
            clip, edge, frames = self._level_clip, self._fade_edge, self._fade_frames
            current = clip.fade_in if edge == "in" else clip.fade_out
            if frames != current:
                self._run(f"Fade {edge}", ops.set_clip_fade, clip, edge, frames)

        self._drag_clips = []
        self._drag_delta = 0
        self._drag_target = None
        self._drag_track = None
        self._trim_clip = None
        self._level_clip = None
        self.update()

    def _lane_under(self, y: int) -> Track | None:
        """The lane a vertical drag would drop onto.

        Only lanes of the grabbed clip's own kind count, so dragging video over
        the audio lanes keeps it on its current video lane rather than silently
        refusing the whole move. A linked partner stays on its own lane and just
        follows horizontally.
        """
        row = self.track_at(y)
        if row is None:
            return self._drag_track
        track = row[0]
        if track.kind != self._drag_kind or track.locked:
            return self._drag_track
        return track

    def _hovered_zoom(self, pos: QPoint) -> str | None:
        """The clip whose zoom block is under the pointer, if any."""
        row = self.track_at(pos.y())
        if row is None:
            return None
        track, top, height = row
        for clip in track.clips:
            band = self.zoom_band(clip, self.clip_rect(clip, top, height))
            marks = self.zoom_marks(clip, self.clip_rect(clip, top, height))
            if band is None or marks is None:
                continue
            if band.top() <= pos.y() <= band.bottom() and marks[0] <= pos.x() <= marks[3]:
                return clip.clip_id
        return None

    def _track_zoom_hover(self, pos: QPoint) -> None:
        hovered = self._hovered_zoom(pos)
        if hovered != self._zoom_hover:
            self._zoom_hover = hovered
            self.update()

    def _update_cursor(self, pos: QPoint) -> None:
        if self._tool is Tool.CUT:
            return   # the scissors stay until the tool is put down
        if pos.x() < HEADER_WIDTH or pos.y() < RULER_HEIGHT:
            self.setCursor(Qt.ArrowCursor)
            return
        hit = self.hit_test(pos)
        if hit is None:
            self.setCursor(Qt.ArrowCursor)
        elif hit.zone is Zone.BODY:
            self.setCursor(Qt.OpenHandCursor)
        elif hit.zone is Zone.GAIN:
            self.setCursor(Qt.SizeVerCursor)
        elif hit.zone is Zone.ZOOM_RAMP_IN:
            # Along the slope it is sitting on: the ramp in climbs left to
            # right, so the corner is dragged diagonally rather than sideways.
            self.setCursor(Qt.SizeBDiagCursor)
        elif hit.zone is Zone.ZOOM_RAMP_OUT:
            self.setCursor(Qt.SizeFDiagCursor)
        else:
            self.setCursor(Qt.SizeHorCursor)

    def _toggle_header(self, pos: QPoint) -> None:
        """Clicking a header toggles mute; shift-clicking toggles lock.

        Through the undo stack like the menu path, so muting from the header and
        muting from the mixer are the same edit rather than two behaviours that
        happen to look alike.
        """
        row = self.track_at(pos.y())
        if row is None:
            return
        track = row[0]
        from PySide6.QtWidgets import QApplication

        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            self._set_track_flag(track, "locked", not track.locked)
        else:
            self._set_track_flag(track, "muted", not track.muted)

    def _set_playhead_from(self, x: float) -> None:
        frame = max(0, self.frame_of(x))
        self.project.set_playhead(frame)
        self.playhead_moved.emit(frame)

    def _select(self, clip: Clip, *, additive: bool) -> None:
        current = self.project.selected_ids
        if additive:
            if clip.clip_id in current:
                current = [cid for cid in current if cid != clip.clip_id]
            else:
                current = current + [clip.clip_id]
        elif clip.clip_id not in current:
            current = [clip.clip_id]
        self.project.set_selection(current)


    # -- context menus ---------------------------------------------------------

    SPEED_PRESETS = ((0.25, "¼×  Slow"), (0.5, "½×"), (1.0, "1×  Normal"),
                     (2.0, "2×"), (4.0, "4×  Fast"))

    def _open_context_menu(self, pos: QPoint, global_pos: QPoint) -> None:
        if pos.x() < HEADER_WIDTH:
            row = self.track_at(pos.y())
            if row is not None:
                self._track_menu(row[0], global_pos)
            return

        # The zoom block gets its own menu, because right-clicking a thing you
        # can see and grab should be about *that* thing. It is also the only way
        # to be rid of a zoom now that the viewer has no Reset button on it.
        hovered = self._hovered_zoom(pos)
        if hovered is not None:
            for track in self.timeline.tracks:
                for clip in track.clips:
                    if clip.clip_id == hovered:
                        self.build_zoom_menu(clip).exec(global_pos)
                        return

        hit = self.hit_test(pos)
        if hit is not None:
            # Right-clicking outside the selection selects that clip first, so
            # the menu always acts on what was actually clicked.
            if hit.clip.clip_id not in self.project.selected_ids:
                self.project.set_selection([hit.clip.clip_id])
            self._clip_menu(hit.clip, global_pos)
        else:
            row = self.track_at(pos.y())
            if row is not None:
                self._track_menu(row[0], global_pos, empty_area=True)

    def _clip_menu(self, clip: Clip, global_pos: QPoint) -> None:
        self.build_clip_menu(clip).exec(global_pos)

    def build_zoom_menu(self, clip: Clip) -> QMenu:
        """The menu for one zoom block, raised by right-clicking it."""
        region = clip.zoom
        fps = float(self.timeline.timebase.fps) or 30.0
        menu = QMenu(self)

        heading = menu.addAction(
            f"Zoom {region.framing.zoom:.2f}\u00d7 for {region.length / fps:.1f}s"
            .replace(".00\u00d7", "\u00d7")
        )
        heading.setEnabled(False)
        menu.addSeparator()

        for label, seconds in (("Instant", 0.0), ("Ease 0.3s", 0.3),
                               ("Ease 0.6s", 0.6), ("Ease 1s", 1.0)):
            frames = int(round(seconds * fps))
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(
                region.ramp_in == frames and region.ramp_out == frames
            )
            action.triggered.connect(
                lambda _=False, f=frames: self._set_zoom_ramps([clip], f)
            )

        menu.addSeparator()
        for factor, label in self.ZOOM_PRESETS:
            action = menu.addAction(f"Zoom {label.replace('Punch In  ', '')}")
            action.setCheckable(True)
            action.setChecked(abs(region.framing.zoom - factor) < 1e-6)
            action.triggered.connect(
                lambda _=False, f=factor: self._set_zoom_level(clip, f)
            )

        menu.addSeparator()
        menu.addAction(
            "Delete Zoom",
            lambda: self._run("Remove zoom", ops.set_zoom_region, [clip], None),
        )
        return menu

    def _set_zoom_level(self, clip: Clip, factor: float) -> None:
        """Change how far a region punches in, keeping where it is and its ramps."""
        if clip.zoom is None:
            return
        self._run(
            "Zoom level",
            ops.set_zoom_region,
            [clip],
            replace(clip.zoom, framing=framing.with_zoom(clip.zoom.framing, factor)),
        )

    def build_clip_menu(self, clip: Clip) -> QMenu:
        menu = QMenu(self)
        selected = self.project.selected_clips() or [clip]

        menu.addAction("Cut at Playhead\tX", self._razor_here)
        menu.addSeparator()

        speed_menu = menu.addMenu("Speed")
        for factor, label in self.SPEED_PRESETS:
            action = speed_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(abs(clip.speed - factor) < 1e-6)
            action.triggered.connect(lambda _=False, f=factor: self._apply_speed(f))
        speed_menu.addSeparator()
        speed_menu.addAction("Custom…", lambda: self._custom_speed(clip))

        if clip.speed != 1.0:
            menu.addAction(f"Reset Speed (now {clip.speed:g}×)", lambda: self._apply_speed(1.0))

        backwards = menu.addAction("Reverse\tR")
        backwards.setCheckable(True)
        backwards.setChecked(clip.reversed)
        backwards.triggered.connect(lambda: self._toggle_reversed(selected))

        menu.addSeparator()
        group = self.timeline.linked_group(clip)
        if clip.link_id is not None and len(group) > 1:
            menu.addAction("Unlink Audio and Video", lambda: self._run("Unlink", ops.unlink, clip))
        elif len(selected) > 1:
            menu.addAction("Link Selected", lambda: self._link(selected))

        toggle = "Disable" if clip.enabled else "Enable"
        menu.addAction(toggle, lambda: self._toggle_enabled(selected))

        # Mute is offered on picture too, because a linked pair is one thing to
        # the person right-clicking it: `set_clip_muted` finds the audio half.
        muted_now = self._muted_state(selected)
        if muted_now is not None:
            menu.addSeparator()
            mute = menu.addAction(("Unmute Clip" if muted_now else "Mute Clip") + "\tM")
            mute.setCheckable(True)
            mute.setChecked(muted_now)
            mute.triggered.connect(lambda: self._toggle_mute(selected))

        if clip.is_title:
            menu.addSeparator()
            menu.addAction("Edit Title…", lambda: self.edit_title(clip))
        elif clip.kind == "video":
            self._add_transition_menu(menu, clip)
            self._add_picture_menu(menu, clip, selected)

        if clip.kind == "audio":
            menu.addAction(
                f"Normalise to {NORMALISE_TARGET_DB:g} dB", lambda: self._normalise(selected)
            )
            if clip.gain_db != 0.0:
                menu.addAction(
                    f"Reset Gain (now {clip.gain_db:+.1f} dB)",
                    lambda: self._run("Clip gain", ops.set_clip_gain, selected, 0.0),
                )
            if clip.has_fades:
                menu.addAction(
                    "Clear Fades",
                    lambda: self._run("Clear fades", ops.clear_clip_fades, selected),
                )

        menu.addSeparator()
        menu.addAction("Delete (leave gap)\tBackspace", lambda: self._delete(selected, ripple=False))
        menu.addAction("Ripple Delete\tDelete", lambda: self._delete(selected, ripple=True))
        return menu

    # Enough to punch in on a face or crop a wobbly edge without hunting for a
    # number. Anything finer is what dragging the picture is for.
    ZOOM_PRESETS = ((1.2, "Punch In  1.2×"), (1.5, "1.5×"), (2.0, "2×"))
    # In seconds. Half a second is the everyday dissolve; the outer two are for
    # a quick soften and a long lazy mix.
    DISSOLVE_PRESETS = (0.25, 0.5, 1.0, 2.0)

    def _add_transition_menu(self, menu: QMenu, clip: Clip) -> None:
        """Cross dissolve into this clip from the one before it."""
        track = self.timeline.track_of(clip)
        current = track.dissolve_before(clip)

        menu.addSeparator()
        transitions = menu.addMenu("Dissolve In")
        timebase = self.project.timebase
        for seconds in self.DISSOLVE_PRESETS:
            frames = max(1, int(round(seconds * float(timebase.fps))))
            action = transitions.addAction(f"{seconds:g}s")
            action.setCheckable(True)
            action.setChecked(current is not None and current[1] == frames)
            action.triggered.connect(
                lambda _=False, f=frames: self._run(
                    "Dissolve", ops.set_dissolve, clip, f
                )
            )
        if current is not None:
            transitions.addSeparator()
            transitions.addAction(
                f"Remove ({timebase.frames_to_timecode(current[1])})",
                lambda: self._run("Remove dissolve", ops.set_dissolve, clip, 0),
            )

    def _add_picture_menu(self, menu: QMenu, clip: Clip, selected: list[Clip]) -> None:
        """The picture section: framing, rotation and flip.

        The first video-only section this menu has had. Kept as a submenu
        because the top level is already long, and because everything in it is
        the same thought — "what does this shot look like".
        """
        menu.addSeparator()
        picture = menu.addMenu("Picture")

        fill = picture.addAction("Fill Frame")
        fill.setToolTip("Zoom until the black bars are gone")
        fill.triggered.connect(lambda: self._fill_frame(clip))

        for factor, label in self.ZOOM_PRESETS:
            action = picture.addAction(label)
            action.setCheckable(True)
            action.setChecked(abs(clip.framing.zoom - factor) < 1e-6)
            action.triggered.connect(lambda _=False, f=factor: self._apply_zoom(selected, f))

        picture.addSeparator()
        if clip.has_move:
            picture.addAction(
                "Remove Move",
                lambda: self._run("Remove move", ops.set_framing_move, selected, None),
            )
        else:
            picture.addAction(
                "Add Move (push in)",
                lambda: self._run(
                    "Move framing",
                    ops.set_framing_move,
                    selected,
                    framing.with_zoom(clip.framing, min(clip.framing.zoom * 1.35, 8.0)),
                ),
            )

        if clip.has_zoom:
            fps = float(self.timeline.timebase.fps) or 30.0
            region = clip.zoom
            picture.addSeparator()
            picture.addAction(
                f"Remove Zoom ({region.length / fps:.1f}s at "
                f"{region.framing.zoom:.2f}\u00d7)".replace(".00\u00d7", "\u00d7"),
                lambda: self._run("Remove zoom", ops.set_zoom_region, selected, None),
            )
            for label, ramp in (("Instant", 0), ("Ease 0.3s", 0.3), ("Ease 1s", 1.0)):
                frames = int(round(ramp * fps))
                action = picture.addAction(f"Zoom Ramp: {label}")
                action.setCheckable(True)
                action.setChecked(region.ramp_in == frames and region.ramp_out == frames)
                action.triggered.connect(
                    lambda _=False, f=frames: self._set_zoom_ramps(selected, f)
                )

        picture.addSeparator()
        picture.addAction(
            "Rotate Right", lambda: self._run("Rotate", ops.rotate_clips, selected, 1)
        )
        picture.addAction(
            "Rotate Left", lambda: self._run("Rotate", ops.rotate_clips, selected, -1)
        )
        flip = picture.addAction("Flip Horizontally")
        flip.setCheckable(True)
        flip.setChecked(clip.flipped)
        flip.triggered.connect(lambda: self._run("Flip", ops.toggle_flipped, selected))

        if clip.has_framing:
            picture.addSeparator()
            picture.addAction(
                f"Reset Picture (now {framing.describe(clip.framing, clip.rotation, clip.flipped)})",
                lambda: self._run("Reset picture", ops.reset_framing, selected),
            )

    def _set_zoom_ramps(self, clips: list[Clip], frames: int) -> None:
        """Both ramps at once — asking for them separately is what the corner
        handles are for, and the menu is the place for the common answer."""
        def both(timeline):
            ops.set_zoom_ramp(timeline, clips, "in", frames)
            ops.set_zoom_ramp(timeline, clips, "out", frames)

        self.project.edit("Zoom ramp", both)

    def _apply_zoom(self, clips: list[Clip], factor: float) -> None:
        """Set the zoom on a selection, each clip keeping its own pan.

        Keeping the pan matters: having framed a shot off-centre, changing how
        far in you are should not throw away where you were looking.
        """
        for clip in clips:
            if clip.kind != "video":
                continue
            self._run(
                "Zoom", ops.set_framing, [clip], framing.with_zoom(clip.framing, factor)
            )

    def _fill_frame(self, clip: Clip) -> None:
        """Zoom until the letterbox bars are gone.

        The one-click fix for a video shot on a phone and dropped on a
        horizontal timeline. Needs the source's real size, so it is the one
        picture action that has to consult the media pool.
        """
        info = self.project.media_for(clip.media_id)
        if info is None or info.video is None:
            self.status_message.emit("That clip's media is not available")
            return
        zoom = framing.fill_zoom(
            info.video.display_size,
            (self.timeline.width, self.timeline.height),
            clip.rotation,
        )
        if abs(zoom - 1.0) < 1e-6:
            self.status_message.emit("That clip already fills the frame")
            return
        self._run(
            "Fill frame", ops.set_framing, [clip], framing.with_zoom(clip.framing, zoom)
        )

    def add_title(self, track: Track | None = None) -> None:
        """Put a new title at the playhead and open it for writing."""
        from vedit.core.ffmpeg import has_filter

        if not has_filter("drawtext"):
            self.status_message.emit(
                "This build of ffmpeg has no drawtext filter, so titles could "
                "not be exported — install an ffmpeg built with libfreetype"
            )
            return

        dialog = self._title_dialog(titles_mod.Title(), self.project.playhead)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        title = dialog.result_title()
        if title.is_empty:
            self.status_message.emit("A title needs some words")
            return
        self._run("Add title", ops.add_title, self.project.playhead, title=title,
                  track=track)

    def edit_title(self, clip: Clip) -> None:
        dialog = self._title_dialog(clip.title, clip.tl_start)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        self._run("Edit title", ops.set_title, clip, dialog.result_title())

    def _title_dialog(self, title, at_frame: int):
        """The dialog, showing the title over whatever is on screen behind it."""
        from vedit.timeline.title_dialog import TitleDialog

        backdrop = None
        surface = getattr(self.window(), "_title_backdrop", None)
        if callable(surface):
            backdrop = surface()
        return TitleDialog(
            title,
            frame=(self.timeline.width, self.timeline.height),
            backdrop=backdrop,
            parent=self,
        )

    def _track_menu(self, track: Track, global_pos: QPoint, *, empty_area: bool = False) -> None:
        self.build_track_menu(track).exec(global_pos)

    def build_track_menu(self, track: Track) -> QMenu:
        menu = QMenu(self)

        if track.kind == "video":
            menu.addAction("Add Title Here…\tCtrl+T", lambda: self.add_title(track))
            menu.addSeparator()

        mute = menu.addAction("Mute" if not track.muted else "Unmute")
        mute.triggered.connect(lambda: self._set_track_flag(track, "muted", not track.muted))
        lock = menu.addAction("Lock" if not track.locked else "Unlock")
        lock.triggered.connect(lambda: self._set_track_flag(track, "locked", not track.locked))

        if track.kind == "audio":
            solo = menu.addAction("Solo")
            solo.setCheckable(True)
            solo.setChecked(track.solo)
            solo.triggered.connect(
                lambda: self._run(
                    ("Unsolo " if track.solo else "Solo ") + track.name,
                    ops.set_track_solo, track, not track.solo,
                )
            )
            reset = menu.addAction(f"Reset Gain ({track.gain_db:+.1f} dB)")
            reset.setEnabled(track.gain_db != 0.0)
            reset.triggered.connect(
                lambda: self._run("Track gain", ops.set_track_gain, track, 0.0)
            )

        menu.addSeparator()
        # No point offering a video lane on a canvas that cannot show one.
        if "video" in self.kinds:
            menu.addAction("Add Video Track", lambda: self._add_track("video"))
        menu.addAction("Add Audio Track", lambda: self._add_track("audio"))

        siblings = self.timeline.video_tracks if track.kind == "video" else self.timeline.audio_tracks
        remove = menu.addAction(f"Delete Track {track.name}")
        remove.triggered.connect(lambda: self._remove_track(track))
        remove.setEnabled(len(siblings) > 1)

        clear = menu.addAction(f"Clear Track {track.name}")
        clear.triggered.connect(lambda: self._run("Clear track", ops.clear_track, track))
        clear.setEnabled(bool(track.clips))
        return menu

    # -- menu actions ----------------------------------------------------------

    def _run(self, label: str, func, *args, **kwargs) -> None:
        """Run an edit, showing its refusal in the status bar rather than raising.

        Keywords are passed through: several ops take them, and leaving them out
        meant `add_title` raised a `TypeError` from inside the one funnel that
        exists to stop errors reaching the user.
        """
        try:
            self.project.edit(label, lambda t: func(t, *args, **kwargs))
        except TimelineError as exc:
            self.status_message.emit(str(exc))

    def _razor_here(self) -> None:
        frame = self.project.playhead
        if not self.project.edit("Razor", lambda t: ops.razor(t, frame)):
            self.status_message.emit("Nothing to cut at the playhead")

    def _apply_speed(self, factor: float) -> None:
        clips = self.project.selected_clips()
        if not clips:
            return
        self._run(f"Speed {factor:g}x", ops.set_speed, clips, factor)

    def _muted_state(self, clips: list[Clip]) -> bool | None:
        """Whether the selection's audio is muted, or None if it has no audio.

        Reported from the link group rather than the clicked clip, so the entry
        appears on the picture half of a linked pair and reads the state its
        sound is actually in.
        """
        audio = [
            member
            for member in ops.expand_links(self.timeline, clips)
            if member.kind == "audio"
        ]
        return audio[0].muted if audio else None

    def _toggle_mute(self, clips: list[Clip]) -> None:
        muted_now = self._muted_state(clips)
        if muted_now is None:
            self.status_message.emit("That clip has no audio to mute")
            return
        self._run("Unmute clip" if muted_now else "Mute clip", ops.toggle_clip_mute, clips)

    def _toggle_reversed(self, clips: list[Clip]) -> None:
        if not clips:
            return
        backwards = not clips[0].reversed
        label = "Reverse clip" if backwards else "Play forwards"
        self._run(label, ops.set_reversed, clips, backwards)

    def _custom_speed(self, clip: Clip) -> None:
        from PySide6.QtWidgets import QInputDialog

        factor, accepted = QInputDialog.getDouble(
            self, "Clip speed", "Speed multiplier:", clip.speed, 0.1, 10.0, 2
        )
        if accepted:
            self._apply_speed(factor)

    def _normalise(self, clips: list[Clip]) -> None:
        """Set each clip's gain so its loudest peak lands at the target.

        Measured from the peak file made at ingest — the same data the waveform
        is drawn from — so it is instant and touches no media. Measuring only
        the clip's own window matters: normalising against the whole source
        would be wrong for a clip trimmed away from the loud part, which is
        exactly the case people reach for this in.
        """
        gains: dict[str, float] = {}
        for clip in clips:
            if clip.kind != "audio":
                continue
            peaks = self.waveforms.get(
                clip.media_id, self.project.proxies.peaks_for(clip.media_id)
            )
            if peaks is None:
                continue
            gains[clip.clip_id] = levels.normalise_gain_db(
                peaks,
                src_in=clip.src_in,
                src_out=clip.src_out,
                timebase=self.timeline.timebase,
                target_db=NORMALISE_TARGET_DB,
            )

        if not gains:
            self.status_message.emit("No peak data for those clips yet")
            return
        self._run("Normalise", ops.normalise_clips, clips, gains)

    def _toggle_enabled(self, clips: list[Clip]) -> None:
        wanted = not clips[0].enabled

        def apply(timeline: Timeline) -> None:
            for clip in ops.expand_links(timeline, clips):
                clip.enabled = wanted

        self.project.edit("Enable clip" if wanted else "Disable clip", apply)

    def _delete(self, clips: list[Clip], *, ripple: bool) -> None:
        action = ops.ripple_delete if ripple else ops.lift
        try:
            self.project.edit("Ripple delete" if ripple else "Delete", lambda t: action(t, clips))
            self.project.set_selection([])
        except TimelineError as exc:
            self.status_message.emit(str(exc))

    def _link(self, clips: list[Clip]) -> None:
        self.project.edit("Link clips", lambda t: ops.link(t, clips))

    def _set_track_flag(self, track: Track, flag: str, value: bool) -> None:
        # Through the undo stack, not a bare setattr: muting is an edit like any
        # other, and doing it directly meant it was neither undoable nor counted
        # as an unsaved change.
        operation = ops.set_track_muted if flag == "muted" else ops.set_track_locked
        label = ("Mute " if value else "Unmute ") if flag == "muted" else (
            "Lock " if value else "Unlock "
        )
        self._run(label + track.name, operation, track, value)
        self.update()

    def _add_track(self, kind: str) -> None:
        self.project.edit(f"Add {kind} track", lambda t: t.add_track(kind))
        self.zoom_changed.emit()

    def _remove_track(self, track: Track) -> None:
        self._run("Delete track", ops.remove_track, track)
        self.zoom_changed.emit()

    # -- wheel / zoom ----------------------------------------------------------

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        self.release_auto_fit()
        if event.modifiers() & Qt.ControlModifier:
            # Zoom about the pointer so the frame under the cursor stays put.
            anchor = event.position().x()
            frame_under = self.frame_of(anchor)
            self.set_zoom(self.px_per_frame * (1.18 if delta > 0 else 1 / 1.18))
            self.scroll_x = frame_under * self.px_per_frame - (anchor - HEADER_WIDTH)
            self.scroll_x = max(0.0, self.scroll_x)
            self.zoom_changed.emit()
        elif event.modifiers() & Qt.ShiftModifier:
            self.scroll_x = max(0.0, self.scroll_x - (delta / 120) * 90)
            self.zoom_changed.emit()
        elif self.max_scroll_y() > 0:
            # With several lanes, a bare wheel scrolls them vertically, which is
            # what the extra tracks make people reach for first.
            self.scroll_y = max(
                0.0, min(float(self.max_scroll_y()), self.scroll_y - (delta / 120) * 40)
            )
            self.zoom_changed.emit()
        else:
            self.scroll_x = max(0.0, self.scroll_x - (delta / 120) * 45)
            self.zoom_changed.emit()
        self.update()
        event.accept()

    def set_zoom(self, px_per_frame: float) -> None:
        self.px_per_frame = max(MIN_PX_PER_FRAME, min(MAX_PX_PER_FRAME, px_per_frame))

    def zoom_to_fit(self) -> None:
        """Fit the whole timeline across the lanes.

        If the widget has no usable width yet — fitting on a page that has not
        been shown, or right after loading a project — the fit is deferred to the
        next resize. Computing it against a placeholder width collapses the zoom
        to its minimum and leaves the timeline apparently empty.

        A fit stays armed until the user changes the zoom themselves, so the
        timeline re-fits as the window and splitter settle rather than sticking
        at whatever width happened to exist on the first layout pass.
        """
        self._auto_fit = True

        lanes = self.width() - HEADER_WIDTH - 20
        if lanes < 50:
            return

        duration = self.timeline.duration
        self.set_zoom(2.0 if duration <= 0 else lanes / duration)
        self.scroll_x = 0.0
        self.zoom_changed.emit()
        self.update()

    def release_auto_fit(self) -> None:
        """Stop re-fitting on resize — the user has taken control of the zoom."""
        self._auto_fit = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._auto_fit:
            self.zoom_to_fit()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._auto_fit:
            self.zoom_to_fit()

    def ensure_visible(self, frame: int) -> None:
        """Scroll so `frame` is on screen — used while playing back."""
        x = self.x_of(frame)
        lanes_left, lanes_right = HEADER_WIDTH, self.width()
        if x < lanes_left + 20:
            self.scroll_x = max(0.0, frame * self.px_per_frame - 40)
            self.zoom_changed.emit()
        elif x > lanes_right - 40:
            # Jump by most of a screen rather than creeping, so playback does not
            # scroll continuously under the pointer.
            span = (lanes_right - lanes_left) * 0.8
            self.scroll_x = max(0.0, frame * self.px_per_frame - span)
            self.zoom_changed.emit()

    # -- drops from the media pool ---------------------------------------------

    def dragEnterEvent(self, event) -> None:
        mime = event.mimeData()
        if MediaPool.ids_from_mime(mime) or mime.hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        mime = event.mimeData()
        if not (MediaPool.ids_from_mime(mime) or mime.hasUrls()):
            return
        frame = max(0, self.frame_of(event.position().x()))
        frame, self._snap_line = self._snap(frame, set())
        self._drop_frame = frame
        self.update()
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._drop_frame = None
        self._snap_line = None
        self.update()

    def dropEvent(self, event) -> None:
        mime = event.mimeData()
        frame = self._drop_frame if self._drop_frame is not None else 0
        self._drop_frame = None
        self._snap_line = None

        media_ids = MediaPool.ids_from_mime(mime)
        if not media_ids and mime.hasUrls():
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            media_ids = [info.media_id for info in self.project.import_paths(paths)]

        row = self.track_at(event.position().toPoint().y())
        target = row[0] if row else None

        placed = 0
        for media_id in media_ids:
            info = self.project.media_for(media_id)
            if info is None:
                continue
            try:
                clips = self.project.edit(
                    f"Add {info.name}",
                    lambda t, i=info, f=frame, tr=target: ops.place_media(t, i, f, track=tr),
                )
            except TimelineError as exc:
                self.status_message.emit(str(exc))
                break
            # Lay multiple dropped items end to end rather than on top of each other.
            frame = max(clip.tl_end for clip in clips)
            placed += 1

            # On an audio-only canvas the picture half of a linked pair lands on
            # a lane that is not on screen. That is the right thing to do, but it
            # is invisible, so it gets said out loud.
            if "video" not in self.kinds:
                elsewhere = [clip for clip in clips if clip.kind == "video"]
                if elsewhere:
                    lane = self.timeline.track_of(elsewhere[0])
                    self.status_message.emit(
                        f"Video from {info.name} went to {lane.name}"
                    )

        if placed:
            event.acceptProposedAction()
        self.update()


class TimelinePanel(QWidget):
    """The canvas plus its horizontal scrollbar."""

    status_message = Signal(str)

    def __init__(self, project: Project, parent=None, **canvas_options) -> None:
        super().__init__(parent)
        self.project = project
        # Options pass straight through: the panel has no opinion about which
        # lanes the canvas shows, it only wraps it in scrollbars.
        self.canvas = TimelineCanvas(project, self, **canvas_options)
        self.canvas.status_message.connect(self.status_message)

        self.scrollbar = QScrollBar(Qt.Horizontal, self)
        self.scrollbar.valueChanged.connect(self._on_scroll)
        self.vscrollbar = QScrollBar(Qt.Vertical, self)
        self.vscrollbar.valueChanged.connect(self._on_vscroll)
        self.canvas.zoom_changed.connect(self._sync_scrollbar)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(0)
        top.addWidget(self.canvas, 1)
        top.addWidget(self.vscrollbar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top, 1)
        layout.addWidget(self.scrollbar)

        self._syncing = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        self._syncing = True
        lanes = max(1, self.canvas.width() - HEADER_WIDTH)
        total = int(self.canvas.content_frames() * self.canvas.px_per_frame)
        self.scrollbar.setRange(0, max(0, total - lanes))
        self.scrollbar.setPageStep(lanes)
        self.scrollbar.setSingleStep(max(1, lanes // 12))
        self.scrollbar.setValue(int(self.canvas.scroll_x))

        span = max(0, self.canvas.height() - RULER_HEIGHT)
        self.vscrollbar.setRange(0, self.canvas.max_scroll_y())
        self.vscrollbar.setPageStep(max(1, span))
        self.vscrollbar.setSingleStep(24)
        self.vscrollbar.setValue(int(self.canvas.scroll_y))
        self.vscrollbar.setVisible(self.canvas.max_scroll_y() > 0)
        self._syncing = False

    def _on_scroll(self, value: int) -> None:
        if self._syncing:
            return
        self.canvas.scroll_x = float(value)
        self.canvas.update()

    def _on_vscroll(self, value: int) -> None:
        if self._syncing:
            return
        self.canvas.scroll_y = float(value)
        self.canvas.update()
