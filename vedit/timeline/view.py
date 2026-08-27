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

from dataclasses import dataclass
from enum import Enum, auto

from PySide6.QtCore import QPoint, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPolygon
from PySide6.QtWidgets import QScrollBar, QVBoxLayout, QWidget

from vedit import theme
from vedit.core.project import Project
from vedit.media.pool import MediaPool
from vedit.timeline import ops
from vedit.timeline.model import Clip, Timeline, TimelineError, Track
from vedit.timeline.waveform import WaveformCache, draw_waveform

RULER_HEIGHT = 26
HEADER_WIDTH = 108
VIDEO_TRACK_HEIGHT = 70
AUDIO_TRACK_HEIGHT = 56
TRACK_GAP = 2
TRIM_GRAB_PX = 7          # how close to an edge counts as grabbing it
SNAP_PX = 9               # snapping tolerance, in screen pixels
MIN_PX_PER_FRAME = 0.002
MAX_PX_PER_FRAME = 40.0


class Zone(Enum):
    BODY = auto()
    IN = auto()
    OUT = auto()


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


class TimelineCanvas(QWidget):
    """Ruler, track headers and clip lanes, all painted here."""

    playhead_moved = Signal(int)
    zoom_changed = Signal()
    status_message = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.waveforms = WaveformCache()

        self.px_per_frame = 2.0
        self.scroll_x = 0.0

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setMinimumHeight(RULER_HEIGHT + VIDEO_TRACK_HEIGHT + AUDIO_TRACK_HEIGHT + 24)

        self._mode = Mode.IDLE
        self._press_pos = QPoint()
        self._press_frame = 0
        self._drag_clips: list[Clip] = []
        self._drag_delta = 0
        self._drag_track: Track | None = None
        self._trim_clip: Clip | None = None
        self._trim_edge: str = "out"
        self._trim_frame = 0
        self._drop_frame: int | None = None
        self._snap_line: int | None = None
        self.snapping = True

        project.timeline_changed.connect(self._on_model_changed)
        project.playhead_changed.connect(lambda *_: self.update())
        project.selection_changed.connect(self.update)
        project.proxies.peaks_ready.connect(lambda *_: self.update())

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

    def track_rows(self) -> list[tuple[Track, int, int]]:
        """(track, y, height) top to bottom: video lanes above audio lanes.

        Video is listed in reverse so V1 sits closest to the audio lanes and
        higher-numbered tracks stack upward, which is the layout every NLE uses.
        """
        rows: list[tuple[Track, int, int]] = []
        y = RULER_HEIGHT + TRACK_GAP
        for track in reversed(self.timeline.video_tracks):
            rows.append((track, y, VIDEO_TRACK_HEIGHT))
            y += VIDEO_TRACK_HEIGHT + TRACK_GAP
        for track in self.timeline.audio_tracks:
            rows.append((track, y, AUDIO_TRACK_HEIGHT))
            y += AUDIO_TRACK_HEIGHT + TRACK_GAP
        return rows

    def content_height(self) -> int:
        rows = self.track_rows()
        return (rows[-1][1] + rows[-1][2] + TRACK_GAP) if rows else RULER_HEIGHT + 40

    def track_at(self, y: int) -> tuple[Track, int, int] | None:
        for track, top, height in self.track_rows():
            if top <= y < top + height:
                return track, top, height
        return None

    def clip_rect(self, clip: Clip, top: int, height: int) -> QRectF:
        left = self.x_of(clip.tl_start)
        right = self.x_of(clip.tl_end)
        return QRectF(left, top + 1, max(right - left, 1.0), height - 2)

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
            # Only offer trim handles when the clip is wide enough that grabbing
            # an edge cannot swallow the whole body.
            if rect.width() > TRIM_GRAB_PX * 3:
                if pos.x() - rect.left() <= TRIM_GRAB_PX:
                    return Hit(track, clip, Zone.IN)
                if rect.right() - pos.x() <= TRIM_GRAB_PX:
                    return Hit(track, clip, Zone.OUT)
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

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), theme.BG_DARKEST)

        self._paint_lanes(painter)
        self._paint_clips(painter)
        self._paint_drop_indicator(painter)
        self._paint_snap_line(painter)
        self._paint_headers(painter)
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

            flags = []
            if track.muted:
                flags.append("muted")
            if track.locked:
                flags.append("locked")
            if flags:
                painter.setPen(theme.WARN)
                painter.drawText(11, top + 36, " · ".join(flags))

    def _clip_colours(self, clip: Clip, selected: bool) -> tuple[QColor, QColor]:
        if clip.kind == "video":
            fill = theme.CLIP_VIDEO_SEL if selected else theme.CLIP_VIDEO
        else:
            fill = theme.CLIP_AUDIO_SEL if selected else theme.CLIP_AUDIO
        return fill, theme.ACCENT if selected else theme.CLIP_BORDER

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

                painter.setPen(Qt.NoPen)
                painter.setBrush(QBrush(fill))
                painter.drawRoundedRect(rect, 3, 3)

                if clip.kind == "audio" and rect.width() > 4:
                    self._paint_waveform(painter, clip, rect)

                painter.setPen(QPen(border, 2 if is_selected else 1))
                painter.setBrush(Qt.NoBrush)
                painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)

                if rect.width() > 34:
                    label = metrics.elidedText(
                        clip.name or "clip", Qt.ElideMiddle, int(rect.width()) - 10
                    )
                    painter.setPen(QColor(255, 255, 255, 225))
                    painter.drawText(int(rect.left()) + 5, int(rect.top()) + 14, label)

    def _paint_waveform(self, painter: QPainter, clip: Clip, rect: QRectF) -> None:
        peaks = self.waveforms.get(clip.media_id, self.project.proxies.peaks_for(clip.media_id))
        if peaks is None:
            return
        painter.save()
        painter.setClipRect(rect)
        draw_waveform(
            painter,
            rect.adjusted(1, 15, -1, -3),
            peaks,
            src_in=clip.src_in,
            src_out=clip.src_out,
            timebase=self.timeline.timebase,
            colour=theme.WAVEFORM,
        )
        painter.restore()

    def _paint_playhead(self, painter: QPainter) -> None:
        x = self.x_of(self.project.playhead)
        if x < HEADER_WIDTH:
            return
        painter.setPen(QPen(theme.PLAYHEAD, 1))
        painter.drawLine(int(x), 0, int(x), self.content_height())
        painter.setBrush(QBrush(theme.PLAYHEAD))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(
            QPolygon([QPoint(int(x) - 5, 0), QPoint(int(x) + 5, 0), QPoint(int(x), 8)])
        )

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

    def mousePressEvent(self, event) -> None:
        self.setFocus()
        pos = event.position().toPoint()
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
        else:
            self._mode = Mode.TRIM
            self._trim_clip = hit.clip
            self._trim_edge = "in" if hit.zone is Zone.IN else "out"
            self._trim_frame = hit.clip.tl_start if hit.zone is Zone.IN else hit.clip.tl_end
            self._drag_clips = ops.expand_links(self.timeline, [hit.clip])
        self.update()

    def mouseMoveEvent(self, event) -> None:
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
            self.update()
            return

        if self._mode is Mode.TRIM and self._trim_clip is not None:
            low, high = ops.trim_bounds(self.timeline, self._trim_clip, self._trim_edge)
            wanted = self.frame_of(pos.x())
            moving = {clip.clip_id for clip in self._drag_clips}
            wanted, target = self._snap(wanted, moving)
            self._trim_frame = max(low, min(high, wanted))
            self._snap_line = target if target == self._trim_frame else None
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        mode, self._mode = self._mode, Mode.IDLE
        self._snap_line = None

        if mode is Mode.MOVE and self._drag_clips and self._drag_delta != 0:
            clips, delta = list(self._drag_clips), self._drag_delta
            try:
                self.project.edit(
                    "Move clip", lambda t: ops.move_clips(t, clips, delta)
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

        self._drag_clips = []
        self._drag_delta = 0
        self._trim_clip = None
        self.update()

    def _update_cursor(self, pos: QPoint) -> None:
        if pos.x() < HEADER_WIDTH or pos.y() < RULER_HEIGHT:
            self.setCursor(Qt.ArrowCursor)
            return
        hit = self.hit_test(pos)
        if hit is None:
            self.setCursor(Qt.ArrowCursor)
        elif hit.zone is Zone.BODY:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.setCursor(Qt.SizeHorCursor)

    def _toggle_header(self, pos: QPoint) -> None:
        """Clicking a header toggles mute; shift-clicking toggles lock."""
        row = self.track_at(pos.y())
        if row is None:
            return
        track = row[0]
        from PySide6.QtWidgets import QApplication

        if QApplication.keyboardModifiers() & Qt.ShiftModifier:
            track.locked = not track.locked
        else:
            track.muted = not track.muted
        self.update()

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

    # -- wheel / zoom ----------------------------------------------------------

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if event.modifiers() & Qt.ControlModifier:
            # Zoom about the pointer so the frame under the cursor stays put.
            anchor = event.position().x()
            frame_under = self.frame_of(anchor)
            self.set_zoom(self.px_per_frame * (1.18 if delta > 0 else 1 / 1.18))
            self.scroll_x = frame_under * self.px_per_frame - (anchor - HEADER_WIDTH)
            self.scroll_x = max(0.0, self.scroll_x)
            self.zoom_changed.emit()
        else:
            step = 90 if event.modifiers() & Qt.ShiftModifier else 45
            self.scroll_x = max(0.0, self.scroll_x - (delta / 120) * step)
            self.zoom_changed.emit()
        self.update()
        event.accept()

    def set_zoom(self, px_per_frame: float) -> None:
        self.px_per_frame = max(MIN_PX_PER_FRAME, min(MAX_PX_PER_FRAME, px_per_frame))

    def zoom_to_fit(self) -> None:
        duration = self.timeline.duration
        lanes = max(1, self.width() - HEADER_WIDTH - 20)
        if duration <= 0:
            self.set_zoom(2.0)
        else:
            self.set_zoom(lanes / duration)
        self.scroll_x = 0.0
        self.zoom_changed.emit()
        self.update()

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

        if placed:
            event.acceptProposedAction()
        self.update()


class TimelinePanel(QWidget):
    """The canvas plus its horizontal scrollbar."""

    status_message = Signal(str)

    def __init__(self, project: Project, parent=None) -> None:
        super().__init__(parent)
        self.project = project
        self.canvas = TimelineCanvas(project, self)
        self.canvas.status_message.connect(self.status_message)

        self.scrollbar = QScrollBar(Qt.Horizontal, self)
        self.scrollbar.valueChanged.connect(self._on_scroll)
        self.canvas.zoom_changed.connect(self._sync_scrollbar)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.canvas, 1)
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
        self._syncing = False

    def _on_scroll(self, value: int) -> None:
        if self._syncing:
            return
        self.canvas.scroll_x = float(value)
        self.canvas.update()
