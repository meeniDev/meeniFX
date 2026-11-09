"""Qt timeline canvas. Frame positions remain integers; pixels use one ppf scale."""

import math
import time
from types import SimpleNamespace

import cv2
import numpy as np
from PyQt6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (QBrush, QColor, QCursor, QFont, QIcon, QImage, QKeySequence,
                         QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QPolygonF)
from PyQt6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QInputDialog,
                             QLabel, QMenu, QMessageBox, QSlider, QToolButton, QVBoxLayout,
                             QWidget, QWidgetAction)

from .. import tokens
from ..audio import processed_waveform
from ..audio.waveform import envelope
from ..constants import COMPOSITION_MIME, EFFECT_MIME, MEDIA_MIME, PRESET_MIME
from ..icons import icon
from ..model import animation as ANIM
from ..model import channels as CH
from ..model import presets as PM
from ..model import transition as TR
from ..model.keyframes import AnimatedValue, Keyframe, clip_animations, clip_keyframe_frames
from ..model.timeline import FADE_CURVES, MARKER_COLORS, fade_curve_name, fade_gain
from ..theme import COLORS
from .track_header import header_hit, header_layout, paint_header, TOOLTIPS
from .themed import on_theme_change
from .widgets import ScrubField


class ZoomSlider(QSlider):
    """Compact logarithmic zoom slider in pixels per frame."""
    edited = pyqtSignal(float)

    def __init__(self, value, minimum, maximum, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._min, self._max = float(minimum), float(maximum)
        self.setRange(0, 1000)
        self.setToolTip("Timeline zoom (Ctrl + wheel over the canvas)")
        self.set_value(value)
        self.valueChanged.connect(lambda pos: self.edited.emit(self._ppf(pos)))

    def _ppf(self, pos):
        return self._min * (self._max / self._min) ** (pos / 1000.0)

    def set_value(self, ppf):
        ratio = math.log(max(self._min, min(self._max, float(ppf))) / self._min) / math.log(self._max / self._min)
        self.blockSignals(True)
        self.setValue(int(round(ratio * 1000)))
        self.blockSignals(False)


TIMELINE_COLORS = {
    "bg": "#0e131b", "lane_a": "#121821", "lane_b": "#141b26", "lane_audio": "#111a1b",
    "sep": "#1f2735", "header": "#161d29", "header_text": "#dbe6f7", "muted": "#7e8da3",
    "ruler": "#151c27", "tick": "#4a586d", "tick_major": "#8ea0b8",
    # The loop region and the selection band share the accent blue; red belongs to the playhead.
    "playhead": "#ff3b4a", "snap": "#ffd166",
    "marker": "#ffa94d", "marker_line": "#ffc266", "snap_glow": "#ffffff",
    "select": "#ffffff", "band": "#7c9cff", "accent": COLORS["accent"],
    "ghost": "#8fb0ff", "invalid": "#ff5c6c", "wave": "#7ef0c0", "wave_dim": "#3f8f74",
    "loop_strip": "#0b1017", "loop_band": "#5f7bff", "loop_band_off": "#3a4457",
    "video": ("#3C6A8E", "#7FB2D6"), "image": ("#9A5545", "#E39B86"), "audio": ("#37755C", "#7FC4A6"),
}

# Lanes, ruler and headers follow the theme; clip colours stay deep enough for white labels in both.
_TIMELINE_SURFACES = {
    "light": {"bg": "#E9E4DA", "lane_a": "#F4F0E7", "lane_b": "#EFEADF", "lane_audio": "#EAF0E7",
              "sep": "#D6CFC1", "header": "#F0ECE3", "header_text": "#333A40", "muted": "#5D645E",
              "ruler": "#ECE7DC", "tick": "#A39B8C", "tick_major": "#6B645A",
              "loop_strip": "#E2DCCF", "loop_band_off": "#C9C1B2", "playhead": "#D6453A",
              "band": "#4F7FA8", "ghost": "#4F7FA8", "snap": "#B8860B", "loop_band": "#4F7FA8"},
    "dark": {"bg": "#1B1D1F", "lane_a": "#222527", "lane_b": "#26292B", "lane_audio": "#222927",
             "sep": "#34383A", "header": "#2A2D2F", "header_text": "#EDEAE3", "muted": "#A8A399",
             "ruler": "#26292B", "tick": "#6E7270", "tick_major": "#A8ABA6",
             "loop_strip": "#1A1C1D", "loop_band_off": "#4A4E50", "playhead": "#FF6B5E",
             "band": "#7FB2D6", "ghost": "#8FC0E0", "snap": "#F4D98C", "loop_band": "#7FB2D6"},
}


def _retheme_timeline():
    TIMELINE_COLORS.update(_TIMELINE_SURFACES["dark" if tokens.is_dark() else "light"])
    TIMELINE_COLORS["accent"] = COLORS["accent"]


on_theme_change(_retheme_timeline)

HIST = 14          # pixels of lane hysteresis when changing lanes mid-drag


def shaped_envelope(minimums, maximums, gain):
    """Scale an envelope by per-column gain and keep the result inside the lane.

    The gain itself is only floored at zero: a boost has to be able to draw *larger* than
    the source peaks, otherwise raising the gain would leave the waveform unchanged. The
    bound is applied to the scaled amplitude, so the drawing can never leave its lane.
    """
    factors = np.maximum(0.0, np.asarray(gain, np.float32))
    return (np.clip(np.asarray(minimums, np.float32) * factors, -1.0, 1.0),
            np.clip(np.asarray(maximums, np.float32) * factors, -1.0, 1.0))


def timecode(frame, fps):
    fps = max(1, int(round(fps)))
    seconds, frames = divmod(max(0, int(frame)), fps)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def _shade(color, factor):
    values = [int(color[i:i + 2], 16) for i in (1, 3, 5)]
    base = (18, 24, 33)
    values = [int(a * factor + b * (1 - factor)) for a, b in zip(values, base)]
    return "#%02x%02x%02x" % tuple(values)


def clip_source_position(clip, timeline_frame):
    """Float source-frame position for a timeline frame (speed, reverse, loop, freeze)."""
    if clip.source_out <= clip.source_in:
        return float(clip.source_in)
    span = clip.source_out - clip.source_in
    if clip.freeze:
        return float(min(clip.source_out - 1, clip.source_in))
    elapsed = max(0.0, float(timeline_frame) - clip.timeline_start)
    delta = clip.source_offset + elapsed * clip.source_fps / max(1.0, clip.timeline_fps) * clip.speed
    if clip.loop:
        delta = delta % span
    else:
        delta = min(delta, max(0.0, span - 1))
    return (clip.source_out - 1 - delta) if clip.reverse else (clip.source_in + delta)


class TimelineCanvas(QWidget):
    playhead_requested = pyqtSignal(int)
    selection_changed = pyqtSignal(list)
    files_dropped = pyqtSignal(list, int, object)
    media_dropped = pyqtSignal(list, int, object)     # media-library ids, frame, lane
    scrub_started = pyqtSignal()
    scrub_finished = pyqtSignal()
    audio_refresh = pyqtSignal()
    track_height_changed = pyqtSignal(int)
    loop_changed = pyqtSignal()
    tool_changed = pyqtSignal(str)
    snap_changed = pyqtSignal(bool)
    zoom_changed = pyqtSignal(float)
    # Double-clicking a keyframe (or "Open in Graph Editor") asks the window to show the
    # curve of these [(clip_id, channel, time)] keys.
    graph_editor_requested = pyqtSignal(list)
    # The compact animation badge asks for the dedicated Keyframe Editor instead of
    # expanding a dense set of property rows inside the clip.
    keyframe_editor_requested = pyqtSignal(list)
    # Double-clicking a composition clip asks the window to open that composition.
    composition_open_requested = pyqtSignal(object)
    # A composition dragged from the Project panel becomes one nested clip.
    composition_dropped = pyqtSignal(list, int, object)

    HEADER_W = 156
    TOP_H = 0           # the old "Timeline | timecode" row is gone; the timecode lives in the preview
    LOOP_H = 12         # loop region strip
    RULER_H = 26        # ruler ticks, labels, markers and the playhead handle
    CACHE_H = 5         # preview cache state band (green/yellow/red), above the ruler
    # Everything above the lanes is fixed: it never scrolls vertically.
    TRACKS_TOP = TOP_H + LOOP_H + CACHE_H + RULER_H
    BAND_ALPHA = 51     # ~20% accent fill; the band border stays fully opaque
    SCRUB_TICK_MS = 30
    SCRUB_EDGE_PX = 28
    LOOP_EDGE_PX = 6
    MIN_PPF, MAX_PPF = 0.02, 48.0
    MIN_TRACK_H, MAX_TRACK_H = 24, 160
    DEFAULT_TRACK_H = 64
    TRACK_H_STEP = 8.0          # pixels per wheel notch with Alt held
    EDGE_PX = 7
    MARKER_HIT_PX = 5.0         # +/- 5 px: a 10 px grab area around each marker flag
    SNAP_IN_PX = 8.0            # snap engages inside 8 px ...
    SNAP_OUT_PX = 12.0          # ... and only releases past 12 px, so it cannot flicker
    SNAP_FADE_MS = 150
    HANDLE_PX = 10              # fade grip size
    FADE_HIT_PX = 8.0           # grab radius around a fade grip
    FADE_CURVE_PX = 70.0        # vertical drag that spans the whole curve range
    KEYFRAME_ROW_H = 14         # one animated parameter per row under the clip
    KEYFRAME_HIT_PX = 7.0       # grab radius around a keyframe diamond
    KEYFRAME_SNAP_PX = 7.0      # a dragged keyframe sticks to the playhead
    ENVELOPE_HIT_PX = 3.0       # grab radius around an opacity/gain line; small on
    # purpose: the line runs through the clip body, so a wider band would steal the
    # press that drags the clip itself (a linked audio clip at 0 dB puts its line in
    # the middle of the clip).
    ENVELOPE_INSET = 4.0        # keeps the 100 % / +ceiling line inside the clip

    def is_missing(self, path):
        return path in self.engine.missing_paths

    def __init__(self, engine, waveforms=None, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.session = engine.session
        self.timeline = engine.timeline
        self.media = engine.media
        self.waveforms = waveforms
        # Filled in by MainWindow: frame number -> preview cache state. The bar is drawn
        # from this callable, so it can only ever show what the cache actually holds.
        self.cache_provider = None
        self.ppf, self.scroll_x, self.scroll_y = 3.0, 0.0, 0.0
        # One global height drives every track; the dict only holds overrides created by
        # dragging a header's bottom border, and those are rescaled with the global value.
        self.track_height_value = self.DEFAULT_TRACK_H
        self.track_heights = {}
        self._height_residual = 0.0
        self.overlap_mode = "Crossfade overlap"
        self._transition_sel = None     # (out_id, in_id) of the selected transition
        self._drag = None
        self._loop_drag = None
        self._loop_preview = None
        self._ghost = None
        self._band = None
        self._band_end = (0, 0)
        self._band_ids = set()
        self._band_base = set()
        self._marker_drag = None        # {"ids", "anchor", "orig", "snapshot", "frame", "moved"}
        self._marker_marquee = None     # {"start", "end", "base"} while Shift-dragging the ruler
        self._fade_drag = None          # {"clip_id", "edge", "snapshot", ...} while dragging a fade
        self._preset_drag = None        # the same, for a preset bar's inner edge
        self._expanded_clips = set()    # clips whose animation rows are open
        # The keyframe selection itself lives in the session, shared with the Graph
        # Editor: this canvas only ever reads and asks it to change.
        self._kf_drag = None            # {"clip_id", ...} while dragging a keyframe
        self._kf_band = None            # Ctrl+drag box that selects keyframes
        self._env_drag = None           # opacity/gain envelope drag
        self._env_hint = ""             # value chip shown while dragging an envelope
        self._hover_clip = None         # clip under the pointer, so its fade grips can appear
        self._snap_hint = None          # {"frame", "kind", "rect"} drawn white on top of clips
        self._snap_cached = None        # candidate the pointer is currently stuck to
        self._snap_fade = None          # time.monotonic() when the glow started fading
        self._snap_timer = QTimer(self)
        self._snap_timer.setInterval(30)
        self._snap_timer.timeout.connect(self._tick_snap_fade)
        self._fx_drop_highlight = None
        self._drop_highlight = False
        self._media_drop = None         # preview of a project-media drop (ids, frame, lanes)
        # Filled in by the window: media id -> {name, type, duration, audio}. The canvas must
        # not reach into the project library itself, it only needs enough to draw the preview.
        self.media_lookup = None
        self._header_hover = None
        self._header_press = None
        self._header_selected = None
        self._volume_drag = None
        self._height_drag = None
        self.setMinimumSize(320, 150)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.session.changed.connect(self.update)
        self.session.changed.connect(self._prune_keyframe_selection)
        self.session.redraw_requested.connect(self.update)
        self.session.playhead_changed.connect(lambda _f: self.update())
        self.session.clip_selected.connect(lambda _ids: self.update())
        # Follow the playhead while it is driven by playback, imports or keyboard steps.
        # (Drag scrubbing scrolls itself, see _scrub_tick.)
        self.session.playhead_moved.connect(self._on_playhead_moved)
        self.session.reveal_requested.connect(self.ensure_visible)
        self.session.loop_changed.connect(self._on_loop_changed)
        self.session.marker_selection_changed.connect(lambda _ids: self.update())
        self.session.key_selection_changed.connect(self.update)
        for flag in (self.session.show_keyframes, self.session.show_opacity_envelope,
                     self.session.show_gain_envelope):
            flag.trace_add("write", lambda *_: self.update())
        # Snap and tool live in the session so menus, buttons and this canvas cannot disagree.
        self.session.snap_enabled.trace_add("write", lambda *_: self._on_snap_changed())
        self.session.tool.trace_add("write", lambda *_: self._on_tool_changed())
        self._scrub_pos = (0, Qt.KeyboardModifier.NoModifier)
        self._scrub_timer = QTimer(self)
        self._scrub_timer.setInterval(self.SCRUB_TICK_MS)
        self._scrub_timer.timeout.connect(self._scrub_tick)

    # ---------------------------------------------------------------- geometry
    @property
    def tracks_top(self):
        """Screen y where the scrolling lanes begin (work bar + loop strip + ruler above)."""
        return self.TRACKS_TOP

    @property
    def loop_strip_top(self):
        return self.TOP_H

    @property
    def cache_bar_top(self):
        return self.TOP_H + self.LOOP_H

    @property
    def ruler_top(self):
        return self.TOP_H + self.LOOP_H + self.CACHE_H

    @property
    def snap_enabled(self):
        return bool(self.session.snap_enabled.get())

    @snap_enabled.setter
    def snap_enabled(self, value):
        self.session.snap_enabled.set(bool(value))

    @property
    def tool(self):
        return self.session.tool.get()

    @tool.setter
    def tool(self, value):
        self.session.tool.set(value)

    def track_height(self, track):
        return int(self.track_heights.get(track.id, self.track_height_value))

    def _content_track_top(self, track):
        """Unscrolled y of a track's top edge (the lanes start under the ruler)."""
        before = self.timeline.tracks[:self.timeline.tracks.index(track)]
        return self.TRACKS_TOP + sum(self.track_height(t) for t in before)

    def set_track_height(self, value, anchor_y=None):
        """Set the height shared by all tracks (wheel, menu and panel buttons use this)."""
        value = int(round(max(self.MIN_TRACK_H, min(self.MAX_TRACK_H, float(value)))))
        old = self.track_height_value
        if value == old:
            return
        anchor = self.track_at(int(anchor_y)) if anchor_y is not None else None
        offset = (float(anchor_y) - self.header_y(anchor)) if anchor is not None else 0.0
        scale = value / float(old)
        for track_id, height in list(self.track_heights.items()):
            fitted = max(self.MIN_TRACK_H, min(self.MAX_TRACK_H, int(round(height * scale))))
            if fitted == value:
                self.track_heights.pop(track_id, None)   # follows the global value again
            else:
                self.track_heights[track_id] = fitted
        self.track_height_value = value
        if anchor is not None and anchor in self.timeline.tracks:
            # Keep the lane under the pointer pinned to the same screen row.
            self.scroll_y = max(0.0, self._content_track_top(anchor) - (float(anchor_y) - offset))
        self.track_height_changed.emit(value)
        self.update()

    def reset_track_heights(self):
        """Drop every per-track override and return to the default height."""
        self.track_heights.clear()
        self.set_track_height(self.DEFAULT_TRACK_H)
        self.track_height_changed.emit(self.track_height_value)
        self.update()

    def header_y(self, track):
        """Screen y of a track's top edge; accounts for the fixed band and the scroll."""
        return (self.TRACKS_TOP
                + sum(self.track_height(t) for t in self.timeline.tracks[:self.timeline.tracks.index(track)])
                - self.scroll_y)

    def track_at(self, y):
        if y < self.TRACKS_TOP:
            return None
        for track in self.timeline.tracks:
            top = self.header_y(track)
            if top <= y < top + self.track_height(track):
                return track
        return None

    def frame_at(self, x):
        return max(0, int(round((x - self.HEADER_W + self.scroll_x) / max(self.ppf, 0.001))))

    def frame_at_exact(self, x):
        return max(0.0, (x - self.HEADER_W + self.scroll_x) / max(self.ppf, 0.001))

    def x_at(self, frame):
        return self.HEADER_W + int(round(frame * self.ppf - self.scroll_x))

    def fit_to_window(self):
        end = max(self.timeline.duration, self.timeline.min_duration)
        self.ppf = max(self.MIN_PPF, min(8.0, (self.width() - self.HEADER_W - 30) / max(1, end)))
        self.scroll_x = 0
        self.zoom_changed.emit(float(self.ppf))
        self.update()

    def _max_scroll_x(self):
        end = max(self.timeline.duration, self.timeline.min_duration)
        return max(0.0, end * self.ppf - (self.width() - self.HEADER_W) + 120)

    def ensure_visible(self, frame):
        """Scroll horizontally so frame sits inside the visible timeline area."""
        margin = 24
        x = self.x_at(frame)
        if x < self.HEADER_W + margin:
            self.scroll_x = max(0.0, frame * self.ppf - margin)
        elif x > self.width() - margin:
            self.scroll_x = max(0.0, frame * self.ppf - (self.width() - self.HEADER_W - margin))
        else:
            return
        self.scroll_x = min(self.scroll_x, self._max_scroll_x())
        self.update()

    def _on_playhead_moved(self, frame, follow):
        if follow:
            self.ensure_visible(frame)
        self.update()

    def set_drop_highlight(self, active):
        self._drop_highlight = active
        self.update()

    def drop_target_at(self, x, y):
        return self.frame_at(x), self.track_at(y)

    def _visible_frame_range(self):
        return self.frame_at(self.HEADER_W), self.frame_at(self.width()) + 1

    # ------------------------------------------------------------------ paint
    def paintEvent(self, _event):
        """Four independent regions, each with its own clip on all four sides.

        1. the lane viewport (lane stripes, clips, waveforms, thumbnails);
        2. the clip viewport (drag ghost, playhead line, selection band);
        3. the fixed header column (scrolls vertically, never horizontally);
        4. the fixed top band (timecode row, loop strip, ruler and playhead handle).

        Because every region starts with a full setClipRect, no track, clip, ghost or
        playhead pixel can ever be painted outside the region that owns it.
        """
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.rect(), QColor(TIMELINE_COLORS["bg"]))
        p.save()
        p.setClipRect(self._lane_viewport())
        self._paint_tracks(p)
        self._paint_transitions(p)
        self._paint_empty_state(p)
        p.restore()
        p.save()
        p.setClipRect(self._clip_area())
        if self._ghost:
            self._paint_ghost(p, *self._ghost)
        if self._media_drop:
            self._paint_media_drop(p, self._media_drop)
        self._paint_marker_lines(p)
        self._paint_playhead_line(p)
        self._paint_band(p)
        self._paint_keyframe_band(p)
        self._paint_snap_glow(p)
        p.restore()
        p.save()
        p.setClipRect(self._header_area())
        self._paint_header_column(p)
        p.restore()
        p.save()
        p.setClipRect(self._fixed_strip_rect())
        self._paint_toolbar_header(p)
        self._paint_fixed_header(p)
        p.restore()
        if self._drop_highlight:
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(TIMELINE_COLORS["accent"]), 2))
            p.drawRect(self.rect().adjusted(1, 1, -2, -2))
        p.end()

    def _lane_viewport(self):
        """Rect owned by the scrolling lanes (all four sides clipped)."""
        return QRectF(0, self.TRACKS_TOP, max(0, self.width()),
                      max(0, self.height() - self.TRACKS_TOP))

    def _header_area(self):
        """Rect owned by the fixed header column: vertical scroll only."""
        return QRectF(0, self.TRACKS_TOP, self.HEADER_W,
                      max(0, self.height() - self.TRACKS_TOP))

    def _fixed_strip_rect(self):
        """Rect owned by the fixed band: timecode row, loop strip and ruler."""
        return QRectF(0, 0, max(0, self.width()), self.TRACKS_TOP)

    def _paint_band(self, p):
        """Translucent accent selection rectangle with a crisp, opaque 1 px border."""
        if not self._band:
            return
        rect = QRectF(QPointF(*self._band), QPointF(*self._band_end)).normalized()
        rect = rect.intersected(self._clip_area())
        if rect.isEmpty():
            return
        fill = QColor(TIMELINE_COLORS["band"])
        fill.setAlpha(self.BAND_ALPHA)
        p.save()
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.fillRect(rect, fill)
        p.setBrush(Qt.BrushStyle.NoBrush)          # never fill with a leftover brush
        p.setPen(QPen(QColor(TIMELINE_COLORS["band"]), 1, Qt.PenStyle.SolidLine))
        p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        p.restore()

    def _paint_keyframe_band(self, p):
        """Ctrl+drag box that selects keyframes instead of clips."""
        if not self._kf_band:
            return
        rect = QRectF(QPointF(*self._kf_band[0]), QPointF(*self._kf_band[1])).normalized()
        rect = rect.intersected(self._clip_area())
        if rect.isEmpty():
            return
        fill = QColor(TIMELINE_COLORS["snap"])
        fill.setAlpha(40)
        p.save()
        p.fillRect(rect, fill)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(TIMELINE_COLORS["snap"]), 1, Qt.PenStyle.DashLine))
        p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))
        p.restore()

    def _select_keyframes_in_band(self):
        band = self._kf_band
        self._kf_band = None
        if not band:
            return
        rect = QRectF(QPointF(*band[0]), QPointF(*band[1])).normalized()
        chosen = []
        for track in self.timeline.tracks:
            for clip in track.sorted_clips():
                _full, entries = self._keyframe_row_geometry(clip)
                for row, mid in entries:
                    for key in row["value"].keyframes:
                        point = QPointF(self.x_at(clip.timeline_start + key.time), mid)
                        if rect.contains(point):
                            chosen.append((clip.id, row["channel"], int(key.time)))
                if self.session.envelope_visible(clip):
                    channel = self.session.envelope_channel(clip)
                    content = self._clip_rect(clip)
                    for time in CH.key_times(clip, channel):
                        value = CH.value_at(clip, channel, time)
                        if value is None:
                            continue
                        point = QPointF(self.x_at(clip.timeline_start + time),
                                        self._envelope_y(clip, value, content))
                        if rect.contains(point):
                            chosen.append((clip.id, channel, int(time)))
        self.session.set_key_selection(chosen)
        self.session.status.emit(f"{len(chosen)} keyframe(s) selected" if chosen
                                 else "No keyframe in that box")
        self.update()

    def _paint_toolbar_header(self, p):
        return

    def ruler_label(self, frame, step):
        """Label for a ruler tick: only as much precision as the current step needs."""
        fps = max(1, int(round(self.timeline.fps)))
        seconds, frames = divmod(max(0, int(frame)), fps)
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if step < fps:
            base = f"{minutes}:{secs:02d}" if not hours else f"{hours}:{minutes:02d}:{secs:02d}"
            return f"{base}.{frames:02d}" if frames else base
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def ruler_step(self):
        """Frames between ruler labels: the smallest step that keeps labels >=80 px apart."""
        fps = max(1, int(self.timeline.fps))
        target = max(1.0, 80.0 / max(self.ppf, 0.001))
        if target <= fps:
            for step in (1, 2, 5, 10, 15, fps):
                if step >= target:
                    return step
            return fps
        for seconds in (2, 5, 10, 15, 30, 60, 120, 300, 600, 1800, 3600):
            if seconds * fps >= target:
                return seconds * fps
        return int(math.ceil(target / fps)) * fps

    def _paint_fixed_header(self, p):
        """Loop strip, ruler and playhead handle: one fixed band above the lanes."""
        width = self.width()
        # Corner above the header column: fixed, so scrolled content can never show here.
        p.fillRect(0, self.TOP_H, self.HEADER_W, self.tracks_top - self.TOP_H,
                   QColor(TIMELINE_COLORS["header"]))
        # ---- loop strip (Vegas style band with draggable edges)
        strip_top = self.loop_strip_top
        p.fillRect(self.HEADER_W, strip_top, width - self.HEADER_W, self.LOOP_H,
                   QColor(TIMELINE_COLORS["loop_strip"]))
        region = self._loop_preview or self.timeline.loop_range()
        if region is not None:
            enabled = self.timeline.loop_enabled
            band = QColor(TIMELINE_COLORS["loop_band"] if enabled else TIMELINE_COLORS["loop_band_off"])
            band.setAlpha(200 if enabled else 120)
            x1 = max(self.HEADER_W, self.x_at(region[0]))
            x2 = min(width, self.x_at(region[1]))
            if x2 > x1:
                p.fillRect(x1, strip_top + 1, x2 - x1, self.LOOP_H - 2, band)
                p.setPen(QPen(QColor("#ffffff" if enabled else TIMELINE_COLORS["muted"]), 1))
                p.drawLine(x1, strip_top + 1, x1, strip_top + self.LOOP_H - 2)
                p.drawLine(x2 - 1, strip_top + 1, x2 - 1, strip_top + self.LOOP_H - 2)
        p.setPen(QPen(QColor(TIMELINE_COLORS["sep"]), 1))
        p.drawLine(0, strip_top, width, strip_top)
        p.drawLine(0, strip_top + self.LOOP_H - 1, width, strip_top + self.LOOP_H - 1)
        # ---- preview cache band (real frame states, see paint_cache_bar)
        self._paint_cache_bar(p, self.cache_bar_top)
        # ---- ruler (starts at the column edge; the corner stays header-coloured)
        top = self.ruler_top
        p.fillRect(self.HEADER_W, top, max(0, width - self.HEADER_W), self.RULER_H,
                   QColor(TIMELINE_COLORS["ruler"]))
        p.setPen(QPen(QColor(TIMELINE_COLORS["tick"]), 1))
        p.setFont(QFont("Segoe UI", 8))
        start, end = self._visible_frame_range()
        step = self.ruler_step()
        first = (start // step) * step
        for frame in range(first, end, step):
            x = self.x_at(frame)
            if x < self.HEADER_W:
                continue
            p.drawLine(x, top, x, top + self.RULER_H - 1)
            p.setPen(QColor(TIMELINE_COLORS["tick_major"]))
            p.drawText(x + 3, top + self.RULER_H - 4, self.ruler_label(frame, step))
            p.setPen(QPen(QColor(TIMELINE_COLORS["tick"]), 1))
        self._paint_markers(p, top)
        self._paint_marker_marquee(p)
        self._paint_playhead_handle(p, top)
        self._paint_snap_flag_glow(p, top)
        # The column edge rule closes the fixed band's corner row.
        p.setPen(QPen(QColor(TIMELINE_COLORS["sep"]), 1))
        p.drawLine(0, self.tracks_top - 1, self.HEADER_W, self.tracks_top - 1)
        p.drawLine(self.HEADER_W, self.TOP_H, self.HEADER_W, self.height())

    def _paint_cache_bar(self, p, top):
        """Thin band above the ruler: green cached, yellow rendering, red missing.

        Drawn per visible pixel column from the cache's own state (never an estimate), so
        a scroll or a quality change immediately repaints the truth. Columns are batched
        into runs because a per-frame fillRect would be far too slow while scrolling.
        """
        width = self.width()
        if width <= self.HEADER_W:
            return
        provider = self.cache_provider
        p.fillRect(0, top, width, self.CACHE_H, QColor(TIMELINE_COLORS["ruler"]))
        p.fillRect(0, top, self.HEADER_W, self.CACHE_H, QColor(TIMELINE_COLORS["header"]))
        if provider is None:
            return
        colours = {"cached": QColor("#22C55E"), "pending": QColor("#EAB308"),
                   "missing": QColor("#EF4444")}
        start_x = max(self.HEADER_W, 1)
        run_state = None
        run_start = start_x
        # One sample every 2 px: the band is 5 px tall, so this is visually exact and
        # keeps a full-width repaint around a few hundred dict lookups.
        step = 2
        samples = range(start_x, width, step)
        for x in samples:
            state = provider(self.frame_at(x + step / 2.0))
            if state not in colours:
                state = "missing"
            if state != run_state:
                if run_state is not None:
                    p.fillRect(run_start, top, max(1, x - run_start), self.CACHE_H,
                               colours[run_state])
                run_state, run_start = state, x
        if run_state is not None:
            p.fillRect(run_start, top, max(1, width - run_start), self.CACHE_H, colours[run_state])
        p.setPen(QPen(QColor(TIMELINE_COLORS["sep"]), 1))
        p.drawLine(0, top + self.CACHE_H - 1, width, top + self.CACHE_H - 1)

    def _clip_area(self):
        """Rect owned by the scrolling clip content: everything right of the headers."""
        return QRectF(self.HEADER_W, self.TRACKS_TOP,
                      max(0.0, self.width() - self.HEADER_W),
                      max(0.0, self.height() - self.TRACKS_TOP))

    def _paint_tracks(self, p):
        start, end = self._visible_frame_range()
        visible = []
        for index, track in enumerate(self.timeline.tracks):
            y = int(round(self.header_y(track)))
            height = self.track_height(track)
            if y + height < self.TRACKS_TOP or y > self.height():
                continue
            visible.append((index, track, y, height))
            lane_key = "lane_audio" if track.type == "audio" else ("lane_a" if index % 2 == 0 else "lane_b")
            p.fillRect(0, y, self.width(), height, QColor(TIMELINE_COLORS[lane_key]))
            p.setPen(QPen(QColor(TIMELINE_COLORS["sep"]), 1))
            p.drawLine(0, y + height - 1, self.width(), y + height - 1)
        # Clips live in the clip viewport and are additionally cut to their own lane, so
        # a clip, thumbnail or waveform can never leave the lane it belongs to (or the
        # scrolling area) even when that lane is scrolled under the fixed header.
        p.save()
        p.setClipRect(self._clip_area(), Qt.ClipOperation.IntersectClip)
        for _index, track, y, height in visible:
            lane = QRectF(self.HEADER_W, y, max(0, self.width() - self.HEADER_W), height)
            p.save()
            p.setClipRect(lane, Qt.ClipOperation.IntersectClip)
            for clip in track.sorted_clips():
                if clip.end < start or clip.timeline_start > end:
                    continue
                self._paint_clip(p, track, clip, y, height)
            p.restore()
        p.restore()

    def _paint_header_column(self, p):
        """The fixed name column, drawn after the clips so nothing can cover it.

        It scrolls vertically with the lanes (same header_y) but never horizontally:
        every header is painted at x = 0.
        """
        top = self.TRACKS_TOP
        p.save()
        p.setClipRect(self._header_area(), Qt.ClipOperation.IntersectClip)
        p.fillRect(0, top, self.HEADER_W, max(0, self.height() - top), QColor(TIMELINE_COLORS["header"]))
        for track in self.timeline.tracks:
            y = int(round(self.header_y(track)))
            height = self.track_height(track)
            if y + height < top or y > self.height():
                continue
            p.fillRect(0, y, self.HEADER_W, height, QColor(TIMELINE_COLORS["header"]))
            hover = self._header_hover[1] if self._header_hover and self._header_hover[0] == track.id else None
            press = self._header_press[1] if self._header_press and self._header_press[0] == track.id else None
            paint_header(p, track, QRectF(0, y, self.HEADER_W, height), hover=hover, pressed=press,
                         selected=self._header_selected == track.id)
            p.setPen(QPen(QColor(TIMELINE_COLORS["sep"]), 1))
            p.drawLine(0, y + height - 1, self.HEADER_W, y + height - 1)
            p.drawLine(self.HEADER_W - 1, y, self.HEADER_W - 1, y + height)
        p.restore()

    def _paint_transitions(self, p):
        """Draw every overlap transition inside its region: tint, border, crossing curves."""
        if not getattr(self.timeline, "transitions", None):
            return
        p.save()
        p.setClipRect(self._clip_area(), Qt.ClipOperation.IntersectClip)
        for transition in self.timeline.transitions:
            window = self.timeline.transition_window(transition)
            outgoing = self.timeline.find_clip(transition.out_id)
            if window is None or outgoing is None or outgoing.track is None:
                continue
            track = outgoing.track
            if track.type != "video":
                # Audio pair records mirror the picture transition; drawing them again on
                # the audio lane would suggest a second, separately editable transition.
                continue
            track_y = int(round(self.header_y(track)))
            height = self.track_height(track)
            if track_y + height < self.TRACKS_TOP or track_y > self.height():
                continue
            left, right = self.x_at(window[0]), self.x_at(window[1])
            rect = QRectF(left, track_y + 3, max(2, right - left), height - 6)
            if rect.right() < self.HEADER_W or rect.left() > self.width():
                continue
            selected = getattr(self, "_transition_sel", None) == (transition.out_id,
                                                                  transition.in_id)
            fill = QColor("#ffb454")
            fill.setAlpha(82 if selected else 44)
            p.fillRect(rect, fill)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor("#ffd166" if selected else "#e0a33d"), 2 if selected else 1))
            p.drawRect(rect)
            # Compact crossing curves: picture weight for the outgoing and incoming clip.
            steps = max(6, min(48, int(rect.width() / 4)))
            outgoing_path, incoming_path = QPainterPath(), QPainterPath()
            for index in range(steps + 1):
                progress = index / steps
                x = rect.left() + rect.width() * progress
                weight = TR.video_weight(progress, transition.curve)
                point_out = QPointF(x, rect.bottom() - (1.0 - weight) * rect.height())
                point_in = QPointF(x, rect.bottom() - weight * rect.height())
                if index == 0:
                    outgoing_path.moveTo(point_out)
                    incoming_path.moveTo(point_in)
                else:
                    outgoing_path.lineTo(point_out)
                    incoming_path.lineTo(point_in)
            p.setPen(QPen(QColor("#ffe6b0"), 1.2))
            p.drawPath(outgoing_path)
            p.setPen(QPen(QColor("#ffffff"), 1.2))
            p.drawPath(incoming_path)
            if selected and rect.width() >= 70 and rect.height() >= 22:
                label = f"{timecode(window[1] - window[0], self.timeline.fps)}  crossfade"
                p.setFont(QFont("Segoe UI", 8))
                p.setPen(QColor("#ffffff"))
                p.drawText(rect.adjusted(4, 1, -4, -1),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
        p.restore()

    def _transition_at(self, x, y):
        """Transition under the pointer, if the point is inside its overlap region."""
        track = self.track_at(y)
        if track is None or track.type != "video" or x < self.HEADER_W:
            return None
        frame_no = self.frame_at(x)
        for transition in getattr(self.timeline, "transitions", []) or ():
            window = self.timeline.transition_window(transition)
            outgoing = self.timeline.find_clip(transition.out_id)
            if (window is None or outgoing is None or outgoing.track is not track
                    or not (window[0] <= frame_no < window[1])):
                continue
            left, right = self.x_at(window[0]), self.x_at(window[1])
            if left - 2 <= x <= right + 2:
                return transition
        return None

    def _edit_transition(self, transition):
        """Double-click/Edit Transition: curve selectors for one overlap."""
        from .transition_dialog import TransitionDialog
        current = self.timeline.transition_between(transition.out_id, transition.in_id)
        if current is None:
            return
        dialog = TransitionDialog(current, self.timeline.transition_window(current), self)
        if dialog.exec():
            self.session.update_transition(current.out_id, current.in_id, **dialog.values())

    def _transition_menu(self, transition, point):
        self._transition_sel = (transition.out_id, transition.in_id)
        self.update()
        menu = QMenu(self)
        menu.addAction("Edit Transition...", lambda: self._edit_transition(transition))
        types = menu.addMenu("Change Transition Type")
        for key, label in TR.KIND_LABELS.items():
            action = types.addAction(label)
            action.setCheckable(True)
            action.setChecked(transition.kind == key)
            action.setEnabled(key == "crossfade")
            action.triggered.connect(
                lambda _checked=False, value=key:
                self.session.update_transition(transition.out_id, transition.in_id, kind=value))
        future = types.addMenu("More (not implemented)")
        for label in ("Dip to Black", "Dip to White", "Wipe", "Slide", "Blur Dissolve"):
            action = future.addAction(label)
            action.setEnabled(False)
        menu.addAction("Remove Transition",
                       lambda: self.session.remove_transition(transition.out_id,
                                                              transition.in_id))
        menu.addAction("Reset Transition Curve",
                       lambda: self.session.reset_transition_curve(transition.out_id,
                                                                   transition.in_id))
        menu.exec(point)

    def _paint_empty_state(self, p):
        """One faint line while the timeline has no clips at all, and nothing after that."""
        if self.timeline.all_clips():
            return
        p.save()
        p.setClipRect(self._clip_area(), Qt.ClipOperation.IntersectClip)
        p.setFont(QFont("Segoe UI", 10))
        p.setPen(QColor(126, 141, 163, 140))
        p.drawText(self._clip_area(), int(Qt.AlignmentFlag.AlignCenter), "Drop files here")
        p.restore()

    def _paint_clip(self, p, track, clip, track_y, track_h):
        x1, x2 = self.x_at(clip.timeline_start), self.x_at(clip.end)
        rect = QRectF(x1 + 1, track_y + 3, max(2, x2 - x1 - 2), track_h - 6)
        rows = self.animation_rows(clip)
        # The clip stays a clip.  Animation is advertised by one compact badge and edited
        # in the dedicated Keyframe Editor; no property rows are packed inside the lane.
        content = QRectF(rect)
        palette = TIMELINE_COLORS["audio" if track.type == "audio" else ("image" if clip.kind == "image" else "video")]
        selected = clip.id in self.session.selected or clip.id in self._band_ids
        p.setPen(QPen(QColor("#ffffff" if selected else _shade(palette[0], 1.25)), 2 if selected else 1))
        p.setBrush(QColor(palette[0]))
        p.drawRoundedRect(rect, 3, 3)
        if track.type == "video" and clip.source_path and rect.width() > 18 and content.height() >= 34:
            # Thumbnails scale with the lane and disappear below the readable threshold.
            tile_h = max(12, int(content.height()) - 8)
            count = max(1, int(rect.width() / max(1, tile_h)))
            tile_width = max(1, int(rect.width() / count))
            for i in range(count):
                offset = min(max(0, clip.duration - 1), int((i * tile_width / max(self.ppf, 1e-5))))
                thumb = self.media.thumbnail(clip, offset, self.timeline.fps, tile_h, tile_width)
                if thumb is not None:
                    image = self._array_image(thumb)
                    p.save()
                    p.setClipRect(QRectF(x1 + 2 + i * tile_width, content.top() + 1,
                                         tile_width - 1, content.height() - 2),
                                  Qt.ClipOperation.IntersectClip)
                    p.drawImage(QRectF(x1 + 2 + i * tile_width, content.top() + 1,
                                       tile_width, content.height() - 2), image)
                    p.restore()
        if track.type == "audio":
            self._paint_waveform(p, track, clip, content)
        self._paint_fades(p, track, clip, content)
        if content.height() >= 28:
            p.save()
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont("Segoe UI", 8))
            label = clip.name + (f"  {clip.speed*100:.0f}%" if abs(clip.speed - 1) > 0.001 else "")
            if clip.effects:
                label += "  fx"
            p.setClipRect(content.adjusted(1, 1, -1, -1), Qt.ClipOperation.IntersectClip)
            p.drawText(content.adjusted(5, 3, -3, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
            p.restore()
        if clip.loop and clip.source_duration_timeline > 0:
            period = max(1, clip.source_duration_timeline)
            repeat = period
            p.setPen(QPen(QColor("#ffffff"), 1))
            while repeat < clip.duration:
                rx = self.x_at(clip.timeline_start + repeat)
                p.drawLine(rx, int(rect.top()), rx, int(rect.top() + 5))
                repeat += period
        if track.locked:
            p.fillRect(rect, QColor(10, 14, 22, 110))
        missing = clip.source_path and self.is_missing(clip.source_path)
        if missing and rect.width() > 14:
            self._paint_missing_warning(p, rect)
        if clip.link_id and not missing:
            self._paint_link_badge(p, clip, content)
        self._paint_presets(p, clip, content)
        self._paint_fade_handles(p, clip, content)
        self._paint_fade_chip(p, clip, content)
        self._paint_envelope(p, clip, content)
        if rows:
            self._paint_keyframe_controls(p, clip, rect, content, rows)

    # ------------------------------------------------------------------ presets
    PRESET_COLORS = {"in": "#4fbf87", "out": "#e0913f", "loop": "#6aa9ff"}
    PRESET_BAR_H = 4.0
    PRESET_GRIP = 4          # pixels around an inner edge that start a drag

    def preset_bar_rects(self, clip):
        """[(index, instance, QRectF bar, left_qrect, right_qrect)] for the clip's presets."""
        rows = PM.instances(clip)
        if not rows:
            return []
        content = self._clip_rect(clip)
        width = max(2.0, content.width() - 8.0)
        x0 = content.left() + 4.0
        bars = []
        base = content.bottom() - 4.0
        for index, instance in enumerate(rows):
            start, length = PM.window(instance, clip.duration)
            left = x0 + width * (start / max(1, clip.duration))
            right = x0 + width * ((start + length) / max(1, clip.duration))
            top = base - (index + 1) * (self.PRESET_BAR_H + 2.0)
            if top < content.top() + 1:
                # Too many presets for the lane: the extras share the top bar.
                top = content.top() + 1
            bar = QRectF(left, top, max(3.0, right - left), self.PRESET_BAR_H)
            grip = self.PRESET_GRIP
            bars.append((index, instance, bar, QRectF(bar.left() - grip, bar.top() - 2,
                                                      2 * grip, bar.height() + 4),
                         QRectF(bar.right() - grip, bar.top() - 2, 2 * grip, bar.height() + 4)))
        return bars

    def _paint_presets(self, p, clip, content):
        """Thin coloured bars: In at the start, Out at the end, Loop over its range."""
        if content.height() < 14:
            return
        for _index, instance, bar, _left, _right in self.preset_bar_rects(clip):
            colour = QColor(self.PRESET_COLORS.get(instance.get("category"), "#8a8f9a"))
            p.save()
            p.setPen(Qt.PenStyle.NoPen)
            fill = QColor(colour)
            fill.setAlpha(215)
            p.setBrush(fill)
            p.drawRoundedRect(bar, 1.5, 1.5)
            # The draggable inner edge gets a lighter notch so it can be found.
            p.setBrush(QColor(255, 255, 255, 170))
            if instance.get("category") == "out":
                p.drawRect(QRectF(bar.left(), bar.top(), 1.5, bar.height()))
            elif instance.get("category") == "in":
                p.drawRect(QRectF(bar.right() - 1.5, bar.top(), 1.5, bar.height()))
            else:
                p.drawRect(QRectF(bar.left(), bar.top(), 1.5, bar.height()))
                p.drawRect(QRectF(bar.right() - 1.5, bar.top(), 1.5, bar.height()))
                self._paint_loop_ticks(p, instance, clip, bar)
            p.restore()

    def _paint_loop_ticks(self, p, instance, clip, bar):
        """Faint marks where each cycle of a loop starts, so its rhythm stays visible."""
        _start, span = PM.window(instance, clip.duration)
        period = max(1, PM.loop_period(instance))
        if span <= 0:
            return
        step = bar.width() * period / float(span)
        if step < 4.0:
            return
        p.setBrush(QColor(255, 255, 255, 90))
        offset = step
        while offset < bar.width() - 2.0:
            p.drawRect(QRectF(bar.left() + offset, bar.top(), 1.0, bar.height()))
            offset += step

    def _preset_hit(self, x, y):
        """(clip, index, edge) under the pointer; edge is 'start'/'end' or None."""
        for track in self.timeline.tracks:
            if track.type != "video":
                continue
            for clip in track.clips:
                for index, _instance, bar, left_grip, right_grip in self.preset_bar_rects(clip):
                    if not bar.adjusted(-2, -3, 2, 3).contains(x, y):
                        continue
                    if right_grip.contains(x, y):
                        return clip, index, "end"
                    if left_grip.contains(x, y):
                        return clip, index, "start"
                    return clip, index, None
        return None, None, None

    def _begin_preset_drag(self, clip, index, edge, x, y):
        instance = PM.instances(clip)[index]
        self._preset_drag = {
            "clip_id": clip.id, "index": index, "edge": edge,
            "snapshot": self.timeline.snapshot(), "start_x": x,
            "start": int(instance.get("start", 0)),
            "duration": int(instance.get("duration", 1)), "moved": False,
        }
        self._release_snap(animate=False)
        self.setCursor(Qt.CursorShape.SizeHorCursor)

    def _preset_move(self, x, y, modifiers):
        drag = self._preset_drag
        clip = self.timeline.find_clip(drag["clip_id"]) if drag else None
        if clip is None or not 0 <= drag["index"] < len(PM.instances(clip)):
            return
        instance = PM.instances(clip)[drag["index"]]
        start, length = PM.window(instance, clip.duration)
        boundary = self._fade_boundary_frame(clip, x)
        if self.snap_enabled and not (modifiers & Qt.KeyboardModifier.ShiftModifier):
            boundary = self._snap_frame(boundary, exclude=(clip.id,), modifiers=modifiers)
        else:
            self._release_snap(animate=False)
        local = int(boundary) - int(clip.timeline_start)
        category = instance.get("category", "in")
        if category == "out":
            length = max(1, clip.duration - local)
            start = clip.duration - length
            PM.set_range(instance, start, length, clip)
        elif category == "in":
            PM.set_range(instance, 0, max(1, local), clip)
        else:
            # A loop's bar is its coverage; `duration` is the length of one cycle, so
            # dragging an edge never changes the cycle.
            if drag["edge"] == "start":
                new_start = max(0, min(local, start + length - 1))
                PM.set_range(instance, new_start, clip=clip)
                PM.set_loop_coverage(instance, start + length - new_start)
            else:
                PM.set_loop_coverage(instance, max(1, local - start))
        drag["moved"] = True
        self.session.changed.emit()
        self.update()

    def _preset_release(self):
        drag, self._preset_drag = self._preset_drag, None
        self.unsetCursor()
        self._release_snap()
        if drag is None:
            return
        if drag.get("moved"):
            self.session.stack.commit("Preset timing", drag["snapshot"])
        self.session.changed.emit()
        self.update()

    def _preset_menu(self, clip, index, point):
        instance = PM.instances(clip)[index]
        menu = QMenu(self)
        title = menu.addAction(PM.summary(instance))
        title.setEnabled(False)
        menu.addSeparator()
        bake = menu.addAction("Bake to Keyframes")
        bake.setEnabled(PM.bakeable(instance, clip))
        bake.triggered.connect(lambda: self.session.bake_preset(clip.id, index))
        menu.addAction("Remove Preset", lambda: self.session.remove_preset(clip.id, index))
        menu.exec(point)

    def _paint_missing_warning(self, p, rect):
        """Amber warning triangle: the clip's media file cannot be found."""
        p.save()
        p.fillRect(rect, QColor(30, 10, 10, 120))
        size = min(18.0, max(10.0, rect.height() - 8))
        x, y = rect.right() - size - 4, rect.top() + 4
        triangle = QPolygonF([QPointF(x + size / 2, y), QPointF(x + size, y + size), QPointF(x, y + size)])
        p.setPen(QPen(QColor("#3a2a00"), 1))
        p.setBrush(QColor("#ffb020"))
        p.drawPolygon(triangle)
        p.setPen(QColor("#3a2a00"))
        p.setFont(QFont("Segoe UI", max(6, int(size * 0.5)), QFont.Weight.Bold))
        p.drawText(QRectF(x, y + size * 0.25, size, size * 0.75), Qt.AlignmentFlag.AlignCenter, "!")
        p.restore()

    def _paint_link_badge(self, p, clip, content):
        """Small chain link marking a video/audio pair, amber and with its offset when slipped."""
        if content.width() < 26 or content.height() < 14:
            return
        synced = self.timeline.link_is_synced(clip)
        color = QColor(TIMELINE_COLORS["snap"] if synced else TIMELINE_COLORS["invalid"])
        x = content.right() - 9
        y = content.top() + 9
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(color, 1.3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(x - 2.4, y), 2.6, 2.0)
        p.drawEllipse(QPointF(x + 2.4, y), 2.6, 2.0)
        if not synced and content.width() > 84:
            offset = self.timeline.link_offset(clip)
            p.setPen(color)
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(QRectF(content.left() + 4, content.top() + 1, content.width() - 26, 14),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop), f"{offset:+.0f}f")
        p.restore()

    # ---------------------------------------------------------------- keyframes
    def animation_rows(self, clip):
        """One entry per animated property, in pipeline order (painting + hit tests).

        Transform channels come first, then effect parameters and puppet pins, so the
        timeline shows the same properties the inspector and the graph editor do.
        """
        rows = []
        for row in CH.list_channels(clip, self.engine.runtime_effects):
            if not row["animated"]:
                continue
            value = CH.stored(clip, row["id"])
            if not hasattr(value, "keyframes"):
                # Held text channel: expose its keys in the same shape the row painter and
                # the key drag handlers use, so text keys behave like every other keyframe.
                value = SimpleNamespace(keyframes=[
                    Keyframe(time, CH.value_at(clip, row["id"], time))
                    for time in CH.key_times(clip, row["id"])])
            rows.append({"channel": row["id"], "effect_name": row["group"],
                         "label": row["label"], "value": value})
        return rows

    @property
    def keyframes_visible(self):
        """The toolbar toggle: keyframes drawn on clips (envelopes stay separately on)."""
        return bool(self.session.show_keyframes.get())

    def _visible_keyframe_rows(self, clip, full_rect):
        """Kept for callers that still ask: the timeline no longer expands property rows."""
        return []

    def _keyframe_row_geometry(self, clip):
        """(full clip rect, no expanded rows); timing lives in the Keyframe Editor."""
        return self._clip_full_rect(clip), []

    def _keyframe_summary_rect(self, clip):
        """The compact animation badge at the clip's lower-right corner."""
        content = self._clip_full_rect(clip)
        width = min(76.0, max(20.0, content.width() - 16.0))
        return QRectF(content.right() - width - 5.0, content.bottom() - 20.0, width, 16.0)

    def _keyframe_toggle_rect(self, clip):
        return self._keyframe_summary_rect(clip)

    def _paint_keyframe_controls(self, p, clip, rect, content, rows):
        """A quiet ``◆ 12 keys`` badge, never dozens of diamonds inside the clip."""
        if not self.keyframes_visible or not rows:
            return
        nkeys = sum(len(CH.key_times(clip, row["channel"])) for row in rows)
        if nkeys <= 0 or content.width() < 22 or content.height() < 14:
            return
        box = self._keyframe_summary_rect(clip)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setPen(QPen(QColor(TIMELINE_COLORS["snap"]), 1.0))
        p.setBrush(QColor(8, 12, 18, 205))
        p.drawRoundedRect(box, 7, 7)
        cx, cy = box.left() + 9.0, box.center().y()
        self._paint_keyframe_diamond(p, cx, cy, False, small=True)
        if box.width() >= 46:
            p.setPen(QColor("#eaf2ff"))
            p.setFont(QFont("Segoe UI", 7))
            text = f"{nkeys} key" if nkeys == 1 else f"{nkeys} keys"
            p.drawText(box.adjusted(18, 0, -4, 0),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
        p.restore()

    @staticmethod
    def _paint_keyframe_diamond(p, x, y, selected, small=False):
        size = 3.0 if small else (5.0 if selected else 4.0)
        poly = QPolygonF([QPointF(x, y - size), QPointF(x + size, y),
                          QPointF(x, y + size), QPointF(x - size, y)])
        p.setPen(QPen(QColor(TIMELINE_COLORS["snap"]), 1.4))
        p.setBrush(QColor(TIMELINE_COLORS["snap"] if selected else TIMELINE_COLORS["bg"]))
        p.drawPolygon(poly)

    def _keyframe_hit(self, x, y):
        """No keyframe diamonds live on the timeline any more."""
        return None, None, None

    def _keyframe_row_hit(self, x, y):
        """No property lanes live on the timeline any more."""
        return None, None, None

    def _keyframe_toggle_hit(self, x, y):
        if not self.keyframes_visible:
            return None
        for track in self.timeline.tracks:
            for clip in track.sorted_clips():
                if not self.animation_rows(clip):
                    continue
                if self._keyframe_toggle_rect(clip).contains(QPointF(x, y)):
                    return clip
        return None

    def _toggle_keyframe_rows(self, clip):
        """Open the dedicated editor; the clip itself stays compact."""
        if not self.animation_rows(clip):
            return
        if clip.id not in self.session.selected:
            self.session.set_selection({clip.id})
        self._expanded_clips.discard(clip.id)
        self.keyframe_editor_requested.emit([])

    def _begin_keyframe_drag(self, clip, row, time, modifiers):
        """Select (Ctrl/Shift-aware) and arm a drag of every selected keyframe."""
        entry = (clip.id, row["channel"], int(time))
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if ctrl:
            self.session.toggle_key_selection(*entry)
        elif shift:
            self.session.set_key_selection({entry}, additive=True)
        elif entry not in self.session.key_selection:
            self.session.set_key_selection({entry})
        entries = self.session.selected_key_entries()
        if not entries:
            self._kf_drag = None
            self.update()
            return
        self._kf_drag = {"clip_id": clip.id, "channel": row["channel"], "time": int(time),
                         "anchor": clip.timeline_start + int(time),
                         "snapshot": self.timeline.snapshot(),
                         "bases": self.session.key_bases(entries), "delta": 0, "moved": False}
        self._release_snap(animate=False)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.update()

    def _keyframe_snap_adjustment(self, drag, delta):
        """Pixel-accurate snapping of a group move to the playhead, markers and keys."""
        if not self.snap_enabled:
            return 0, None
        candidates = [self.session.playhead]
        candidates.extend(self.timeline.marker_frames())
        selected = {(cid, channel, time) for cid, channel, time in self.session.key_selection}
        for clip in self.timeline.all_clips():
            for channel, time in CH.keyframe_entries(clip):
                if (clip.id, channel, int(time)) in selected:
                    continue
                candidates.append(clip.timeline_start + int(time))
        if not candidates:
            return 0, None
        best = None
        for (clip_id, _channel), bucket in drag["bases"].items():
            clip = self.timeline.find_clip(clip_id)
            if clip is None:
                continue
            for time in bucket["times"]:
                proposed = clip.timeline_start + int(time) + delta
                for candidate in candidates:
                    gap = abs(self.x_at(candidate) - self.x_at(proposed))
                    if gap <= self.KEYFRAME_SNAP_PX and (best is None or gap < best[0]):
                        best = (gap, candidate - proposed)
        if best is None:
            return 0, None
        return best[1], (drag["anchor"] + delta + best[1])

    def _keyframe_move(self, x):
        drag = self._kf_drag
        clip = self.timeline.find_clip(drag["clip_id"])
        if clip is None:
            return
        delta = self.frame_at(x) - int(drag["anchor"])
        adjust, snap_frame = self._keyframe_snap_adjustment(drag, delta)
        delta += adjust
        low, high = self.session.key_shift_limits(drag["bases"])
        delta = max(low, min(high, delta))
        if snap_frame is not None and adjust:
            self._set_snap_hint(snap_frame)
        if delta != drag["delta"]:
            self.session.shift_keyframes(drag["bases"], delta)
            drag["delta"] = delta
            drag["moved"] = True
        self.update()

    def _keyframe_release(self):
        drag = self._kf_drag
        self._kf_drag = None
        self.unsetCursor()
        self._release_snap()
        if drag and drag.get("moved"):
            delta = int(drag.get("delta", 0))
            label = "Move keyframe" if len(self.session.key_selection) <= 1 else \
                f"Move {len(self.session.key_selection)} keyframes"
            self.session.commit_keyframes(label, drag["snapshot"])
            # The selection follows the keys it just moved (a drag must not deselect).
            moved = []
            for (clip_id, channel), bucket in drag["bases"].items():
                clip = self.timeline.find_clip(clip_id)
                if clip is None:
                    continue
                for time in bucket["times"]:
                    moved.append((clip_id, channel,
                                  max(0, min(clip.duration - 1, int(time) + delta))))
            self.session.set_key_selection(moved)
        self.update()

    def _cancel_keyframe_drag(self):
        drag = self._kf_drag
        self._kf_drag = None
        self.unsetCursor()
        self._release_snap(animate=False)
        if drag is not None:
            self.timeline.restore(drag["snapshot"])
            self.session.changed.emit()
        self.update()

    def _delete_selected_keyframes(self):
        entries = self.session.selected_key_entries()
        if not entries:
            return False
        removed = self.session.delete_keyframes(entries)
        self.session.clear_key_selection()
        if removed:
            self.session.status.emit("Keyframe deleted" if removed == 1 else f"{removed} keyframes deleted")
        self.update()
        return True

    def _prune_keyframe_selection(self):
        # The session owns the selection and prunes dead keys itself.
        self.session.selected_key_entries()

    def open_keyframes_in_graph(self, entries):
        """Select the keys in the shared selection and ask for the Graph Editor."""
        entries = [(int(cid), str(channel), int(time)) for cid, channel, time in entries or ()]
        if not entries:
            return
        self.session.set_key_selection(entries)
        self.graph_editor_requested.emit(entries)

    def copy_selected_keyframes(self):
        count = self.session.copy_keyframes(self.session.selected_key_entries())
        self.session.status.emit(f"Copied {count} keyframe(s)" if count else "Select keyframes to copy")
        return count

    def paste_keyframes(self):
        count = self.session.paste_keyframes_at_playhead()
        self.update()
        return count

    def duplicate_selected_keyframes(self):
        count = self.session.duplicate_keyframes(self.session.selected_key_entries())
        self.update()
        return count

    def apply_interpolation_spec(self, spec, entries):
        """Apply one Advanced Interpolation spec to keyframes (shared with the graph)."""
        if not entries:
            return
        if spec == "bezier":
            # Custom Bezier: start from a gentle S curve and hand the handles to the user.
            self.session.set_key_interpolation(entries, "bezier",
                                               handles=(0.42, 0.0, 0.58, 1.0))
            self.open_keyframes_in_graph(entries)
        else:
            self.session.set_key_interpolation(entries, spec)
        self.session.changed.emit()
        self.update()

    def _apply_menu_easing(self, label, entries):
        if not entries:
            return
        # The friendly presets are real curves (fn:...) or stored Bezier handles; the
        # technical interpolation types arrive through the Advanced submenu instead.
        self.session.apply_easing(entries, label)
        self.session.changed.emit()
        self.update()

    def _add_easing_actions(self, menu, entries):
        """The easing block of the keyframe menu, shared with the envelope menu.

        Normal users meet the named families (Sine, Quadratic, Bounce, ...) first. The
        interpolation types - Hold, Auto/Continuous/Broken Bezier - live one level down in
        *Advanced Interpolation*, where they cannot be mistaken for easing presets.
        """
        from ..model import easing as EASING
        from .graph_editor import curve_icon
        curve_entries = self.session.easing_entries(entries)
        for label in EASING.PRESET_ORDER:
            action = menu.addAction(label)
            action.setIcon(curve_icon(EASING.preset_curve(label)))
            action.setEnabled(bool(curve_entries))
            action.triggered.connect(lambda _=False, name=label, rows=curve_entries:
                                     self._apply_menu_easing(name, rows))
        custom = [name for name in EASING.load_custom_presets()]
        if custom:
            menu.addSeparator()
            for label in custom:
                action = menu.addAction(label)
                action.setIcon(curve_icon(EASING.preset_curve(label)))
                action.setEnabled(bool(curve_entries))
                action.triggered.connect(lambda _=False, name=label, rows=curve_entries:
                                         self._apply_menu_easing(name, rows))
        menu.addSeparator()
        advanced = menu.addMenu("Advanced Interpolation")
        for item_label, spec, tip in EASING.ADVANCED_INTERPOLATION:
            action = advanced.addAction(item_label)
            action.setToolTip(tip)
            action.setEnabled(bool(curve_entries))
            action.triggered.connect(lambda _=False, value=spec, rows=curve_entries:
                                     self.apply_interpolation_spec(value, rows))

    def _build_keyframe_menu(self, clip, channel, time):
        """The right-click menu for a keyframe, with only the operations that apply."""
        entries = self.session.selected_key_entries() or [(clip.id, channel, int(time))]
        menu = QMenu(self)
        self._add_easing_actions(menu, entries)
        menu.addSeparator()
        menu.addAction("Open in Graph Editor",
                       lambda: self.open_keyframes_in_graph(entries))
        menu.addSeparator()
        menu.addAction("Copy", self.copy_selected_keyframes)
        paste = menu.addAction("Paste", self.paste_keyframes)
        paste.setEnabled(self.session.has_key_clipboard())
        menu.addAction("Duplicate", self.duplicate_selected_keyframes)
        delete = menu.addAction("Delete", self._delete_selected_keyframes)
        delete.setEnabled(not clip.track.locked)
        return menu

    def _keyframe_menu(self, clip, channel, time, point):
        """Right-click a keyframe: easing, curve view, clipboard, delete."""
        menu = self._build_keyframe_menu(clip, channel, time)
        menu.exec(point)

    # ------------------------------------------------------------- envelopes
    @staticmethod
    def _envelope_colour(channel):
        return QColor("#7ef0c0") if channel == CH.gain_channel() else QColor("#cfe0ff")

    def _envelope_geometry(self, clip, content=None):
        content = content if content is not None else self._clip_rect(clip)
        top = content.top() + self.ENVELOPE_INSET
        height = max(1.0, content.height() - 2 * self.ENVELOPE_INSET)
        return content, top, height

    def _envelope_y(self, clip, value, content=None):
        channel = self.session.envelope_channel(clip)
        _content, top, height = self._envelope_geometry(clip, content)
        return top + (1.0 - ANIM.value_to_fraction(value, channel)) * height

    def _envelope_value_at(self, clip, y, content=None):
        channel = self.session.envelope_channel(clip)
        _content, top, height = self._envelope_geometry(clip, content)
        return ANIM.fraction_to_value((top + height - y) / height, channel)

    def _paint_envelope(self, p, clip, content):
        """The subtle opacity (visual) or gain (audio) line, with its own keyframes."""
        if not self.session.envelope_visible(clip) or content.height() < 10 or content.width() < 6:
            return
        channel = self.session.envelope_channel(clip)
        colour = self._envelope_colour(channel)
        left = max(content.left() + 1.0, self.HEADER_W + 1.0)
        right = min(content.right() - 1.0, self.width() - 1.0)
        if right - left < 4:
            return
        default = 0.0 if channel == CH.gain_channel() else 100.0
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setClipRect(content.adjusted(1, 1, -1, -1), Qt.ClipOperation.IntersectClip)
        reference = self._envelope_y(clip, default, content)
        p.setPen(QPen(QColor(255, 255, 255, 55), 1, Qt.PenStyle.DotLine))
        p.drawLine(QPointF(left, reference), QPointF(right, reference))
        path = QPainterPath()
        px = left
        while px <= right:
            frame = max(0, min(clip.duration - 1, self.frame_at(px) - clip.timeline_start))
            value = CH.value_at(clip, channel, frame)
            point = QPointF(px, self._envelope_y(clip, default if value is None else value, content))
            if px <= left + 0.01:
                path.moveTo(point)
            else:
                path.lineTo(point)
            px += 3.0
        p.setPen(QPen(QColor(colour.red(), colour.green(), colour.blue(), 190), 1.4))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)
        if self.keyframes_visible:
            for time in CH.key_times(clip, channel):
                value = CH.value_at(clip, channel, time)
                if value is None:
                    continue
                selected = (clip.id, channel, int(time)) in self.session.key_selection
                self._paint_envelope_key(p, self.x_at(clip.timeline_start + time),
                                         self._envelope_y(clip, value, content), selected, colour)
        drag = self._env_drag
        if drag and drag.get("clip_id") == clip.id and drag.get("value") is not None:
            text = ANIM.envelope_text(drag["value"], channel)
            y = self._envelope_y(clip, drag["value"], content)
            box = QRectF(left + 5, max(content.top() + 1.0, min(content.bottom() - 15.0, y - 16.0)), 86, 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(8, 12, 18, 220))
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QColor("#eaf2ff"))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), text)
        p.restore()

    @staticmethod
    def _paint_envelope_key(p, x, y, selected, colour):
        size = 4.5 if selected else 3.4
        poly = QPolygonF([QPointF(x, y - size), QPointF(x + size, y),
                          QPointF(x, y + size), QPointF(x - size, y)])
        p.setPen(QPen(QColor("#ffffff" if selected else colour), 1.3))
        p.setBrush(QColor(colour if selected else TIMELINE_COLORS["bg"]))
        p.drawPolygon(poly)

    def _envelope_hit(self, x, y):
        """(clip, channel, 'key'|'line', frame) for an envelope under the pointer."""
        if y < self.TRACKS_TOP or x < self.HEADER_W:
            return None, None, None, None
        track = self.track_at(y)
        if track is None:
            return None, None, None, None
        for clip in track.sorted_clips():
            content = self._clip_rect(clip)
            if not (content.left() - 1 <= x <= content.right() + 1
                    and content.top() - 1 <= y <= content.bottom() + 1):
                continue
            if not self.session.envelope_visible(clip):
                continue
            channel = self.session.envelope_channel(clip)
            if self.keyframes_visible:
                for time in CH.key_times(clip, channel):
                    value = CH.value_at(clip, channel, time)
                    if value is None:
                        continue
                    kx = self.x_at(clip.timeline_start + time)
                    ky = self._envelope_y(clip, value, content)
                    if abs(x - kx) <= self.KEYFRAME_HIT_PX and abs(y - ky) <= self.KEYFRAME_HIT_PX:
                        return clip, channel, "key", int(time)
            frame = max(0, min(clip.duration - 1, self.frame_at(x) - clip.timeline_start))
            value = CH.value_at(clip, channel, frame)
            if value is None:
                value = 0.0 if channel == CH.gain_channel() else 100.0
            if abs(y - self._envelope_y(clip, value, content)) <= self.ENVELOPE_HIT_PX:
                return clip, channel, "line", frame
        return None, None, None, None

    def _begin_envelope_drag(self, clip, channel, kind, frame, modifiers):
        self._env_drag = {"clip_id": clip.id, "channel": channel, "kind": kind,
                          "time": int(frame), "snapshot": self.timeline.snapshot(),
                          "moved": False,
                          "value": CH.value_at(clip, channel, frame)}
        if kind == "key":
            self.session.set_key_selection({(clip.id, channel, int(frame))})
        self._release_snap(animate=False)
        self.setCursor(Qt.CursorShape.SizeVerCursor if kind == "line"
                       else Qt.CursorShape.SizeAllCursor)
        self.update()

    def _envelope_move(self, x, y):
        drag = self._env_drag
        clip = self.timeline.find_clip(drag["clip_id"]) if drag else None
        if clip is None or clip.track.locked:
            return
        content = self._clip_rect(clip)
        value = self._envelope_value_at(clip, y, content)
        channel = drag["channel"]
        frame = max(0, min(clip.duration - 1, self.frame_at(x) - clip.timeline_start))
        if drag["kind"] == "line":
            self.session.set_envelope_value(clip.id, channel, value, frame)
        else:
            if self.snap_enabled and abs(x - self.x_at(self.session.playhead)) <= self.KEYFRAME_SNAP_PX:
                frame = max(0, min(clip.duration - 1,
                                  self.session.playhead - clip.timeline_start))
                self._set_snap_hint(self.session.playhead)
            landed = CH.set_key_time(clip, channel, drag["time"], frame)
            if landed is not None:
                drag["time"] = landed
                raw = CH.stored(clip, channel)
                key = raw.key_at(landed) if isinstance(raw, AnimatedValue) else None
                if key is not None and isinstance(key.value, (int, float)):
                    key.value = float(value)
        drag["value"] = value
        drag["moved"] = True
        self.session.changed.emit()
        self.update()

    def _envelope_release(self):
        drag, self._env_drag = self._env_drag, None
        self.unsetCursor()
        self._release_snap()
        if drag is not None and drag.get("moved"):
            label = "Gain envelope" if drag["channel"] == CH.gain_channel() else "Opacity envelope"
            self.session.stack.commit(label, drag["snapshot"])
        self.session.changed.emit()
        self.update()

    def _cancel_envelope_drag(self):
        drag, self._env_drag = self._env_drag, None
        self.unsetCursor()
        self._release_snap(animate=False)
        if drag is not None:
            self.timeline.restore(drag["snapshot"])
            self.session.changed.emit()
        self.update()

    def _envelope_menu(self, clip, channel, kind, frame, point):
        audio = channel == CH.gain_channel()
        menu = QMenu(self)
        menu.addAction("Add Keyframe Here",
                       lambda: self.session.add_envelope_key(clip.id, channel, frame))
        if kind == "key":
            entry = [(clip.id, channel, int(frame))]
            menu.addAction("Remove Keyframe", lambda: self.session.delete_keyframes(entry))
            menu.addAction("Open in Graph Editor", lambda: self.open_keyframes_in_graph(entry))
            easing_menu = menu.addMenu("Easing")
            self._add_easing_actions(easing_menu, entry)
        menu.addSeparator()
        flag = self.session.show_gain_envelope if audio else self.session.show_opacity_envelope
        toggle = menu.addAction("Show Gain Envelope" if audio else "Show Opacity Envelope")
        toggle.setCheckable(True)
        toggle.setChecked(bool(flag.get()))
        toggle.triggered.connect(lambda on, f=flag: f.set(bool(on)))
        menu.addAction("Reset to 0 dB" if audio else "Reset to 100%",
                       lambda: self.session.reset_envelope(clip.id, channel))
        menu.exec(point)

    def _paint_waveform(self, p, track, clip, rect):
        """Peaks mapped through the clip's own source mapping (trim, speed, volume)."""
        left = int(max(rect.left() + 1, self.HEADER_W + 1))
        right = int(min(rect.right() - 1, self.width() - 1))
        if right - left < 2 or not clip.source_path:
            return
        p.save()
        p.setClipRect(rect.adjusted(1, 1, -1, -1), Qt.ClipOperation.IntersectClip)
        mid = rect.center().y() + (2 if rect.height() > 34 else 0)
        half = max(3.0, (rect.height() - (14 if rect.height() > 34 else 6)) / 2.0)
        p.setPen(QPen(QColor(TIMELINE_COLORS["wave_dim"]), 1))
        p.drawLine(int(rect.left()) + 1, int(mid), int(rect.right()) - 1, int(mid))
        levels = self.waveforms.peaks(clip.source_path) if self.waveforms is not None else None
        if rect.height() < 14:
            # Too short for peaks: the centre line above already reads as audio.
            p.restore()
            return
        processed_levels = self._processed_levels(clip)
        if processed_levels is not None:
            # The drawn envelope is the sound the clip actually plays: after gain, fades
            # and the audio effect chain. Falls back to the source while it is computed.
            levels = processed_levels
        if levels is None:
            if self.waveforms is not None:
                self.waveforms.request(clip.source_path)
            p.setPen(QColor(TIMELINE_COLORS["muted"]))
            p.setFont(QFont("Segoe UI", 7))
            p.drawText(rect.adjusted(6, 2, -4, -2), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                       "waveform…" if self.waveforms is not None else "no audio engine")
            p.restore()
            return
        columns = max(1, right - left)
        first_frame = clip_source_position(clip, self.frame_at_exact(left))
        last_frame = clip_source_position(clip, self.frame_at_exact(left + 1))
        source_fps = float(clip.source_fps or self.timeline.fps) or float(self.timeline.fps)
        sample_rate = float(getattr(self.waveforms.library, "sample_rate", 48000))
        samples_per_frame = sample_rate / max(1e-6, source_fps)
        first_sample = first_frame * samples_per_frame
        step = (last_frame - first_frame) * samples_per_frame
        if abs(step) < 1e-3:
            p.restore()
            return
        mins, maxs = envelope(levels, first_sample, abs(step), columns)
        # Gain is a per-sample multiplier, so it maps onto the envelope exactly: the
        # keyframed dB gain channel, the fade curve and the track level are applied
        # column by column. The *scaled amplitude* is what gets bounded to the lane, so a
        # boost really draws larger; clamping the gain itself at 1.0 would freeze it.
        column_gain = self._wave_gain(clip, left, columns)
        level = min(1.0, max(0.0, float(track.volume)))
        if track.muted:
            level *= 0.35
        mins, maxs = shaped_envelope(mins, maxs, column_gain * level)
        color = QColor(TIMELINE_COLORS["wave"] if not track.muted else TIMELINE_COLORS["muted"])
        p.setPen(QPen(color, 1))
        for index in range(columns):
            x = left + index
            top = mid - maxs[index] * half
            bottom = mid - mins[index] * half
            if abs(bottom - top) < 1:
                bottom = top + 1
            p.drawLine(x, int(round(top)), x, int(round(bottom)))
        p.restore()

    def _wave_gain(self, clip, left, columns):
        """Per-column linear gain: the keyframed dB channel times the fade envelope."""
        frames = np.array([self.frame_at_exact(left + index) for index in range(columns)],
                          np.float64)
        gains = processed_waveform.gain_curve(clip, frames - float(clip.timeline_start))
        gains = gains * max(0.0, float(getattr(clip, "volume", 1.0)))
        if clip.fade_in > 0 or clip.fade_out > 0:
            gains = gains * np.array([clip.fade_gain_at(frame) for frame in frames], np.float32)
        return gains.astype(np.float32)

    def _processed_levels(self, clip):
        """Processed peaks for this clip state, requesting them when missing.

        Returns None when the envelope must come from the source peaks instead: the clip
        has no shaping effects, the mode is Source, or the worker has not finished yet.
        """
        if self.waveforms is None or not clip.source_path:
            return None
        if self.waveform_mode() != "Processed":
            return None
        resolver = getattr(self, "effect_resolver", None)
        if not processed_waveform.has_shaping_effects(clip, resolver):
            return None
        rate = int(getattr(self.waveforms.library, "sample_rate", 48000) or 48000)
        signature = processed_waveform.clip_signature(clip, resolver, self.timeline.fps)
        levels = self.waveforms.processed(clip.source_path, signature, rate)
        if levels is None:
            self.waveforms.request_processed(clip.source_path, signature,
                                             getattr(clip, "effects", None) or (),
                                             rate, self.timeline.fps)
        return levels

    def waveform_mode(self):
        """Which envelope to draw: "Processed" (the sound that plays) or "Source"."""
        getter = getattr(self, "waveform_mode_getter", None)
        return getter() if callable(getter) else "Processed"

    # ----------------------------------------------------------------- fades
    def _clip_full_rect(self, clip):
        """The whole clip rectangle, animation rows included."""
        track = clip.track
        return QRectF(self.x_at(clip.timeline_start) + 1, self.header_y(track) + 3,
                      max(2, self.x_at(clip.end) - self.x_at(clip.timeline_start) - 2),
                      self.track_height(track) - 6)

    def _clip_rect(self, clip):
        """The content part of the clip (fades and grips live here), for hit tests."""
        return self._clip_full_rect(clip)

    def _fade_handle_x(self, clip, edge):
        """x of a fade grip: it sits where the fade ends (and follows the pointer while dragging)."""
        frames = clip.fade_in if edge == "in" else clip.fade_out
        if edge == "in":
            return self.x_at(clip.timeline_start + frames)
        return self.x_at(clip.end - frames)

    def _fade_edge_at(self, clip, x, y):
        """'in' / 'out' when the pointer is on a fade grip, else None."""
        rect = self._clip_rect(clip)
        if y > rect.top() + self.HANDLE_PX + 6:
            return None
        for edge in ("in", "out"):
            if abs(x - self._fade_handle_x(clip, edge)) <= self.FADE_HIT_PX:
                return edge
        return None

    def _fade_hit(self, x, y):
        """(clip, edge) for a grip under the pointer.

        Uses the clip's drawn rectangle rather than the frame lookup, so the grip on
        the right corner is still grabbable although its frame sits at clip.end.
        """
        track = self.track_at(y)
        if track is None:
            return None, None
        for clip in track.clips:
            if track.locked:
                # A locked track refuses every edit, fades included.
                continue
            rect = self._clip_rect(clip)
            if rect.left() - 2 <= x <= rect.right() + 2:
                edge = self._fade_edge_at(clip, x, y)
                if edge is not None:
                    return clip, edge
        return None, None

    def _fade_shape_points(self, clip, rect, edge, width, curve, steps=None):
        """Sample a fade from silent (0) to full (1): the drawn curve is the mix curve."""
        steps = steps or max(6, min(48, int(width / 3) + 6))
        points = []
        for index in range(steps + 1):
            t = index / steps
            x = rect.left() + width * t if edge == "in" else rect.right() - width * t
            points.append(QPointF(x, rect.bottom() - fade_gain(t, curve) * rect.height()))
        return points

    def _paint_video_fade(self, p, clip, rect, edge, width, curve):
        """Vegas style: the darkened area sits above the curve - the whole height at the
        fade's silent end, nothing at its full end."""
        points = self._fade_shape_points(clip, rect, edge, width, curve)
        wedge = QPainterPath(QPointF(rect.left() if edge == "in" else rect.right(), rect.top()))
        for point in points:
            wedge.lineTo(point)
        wedge.closeSubpath()
        line = QPainterPath(points[0])
        for point in points[1:]:
            line.lineTo(point)
        p.save()
        p.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        p.fillPath(wedge, QColor(4, 7, 11, 170))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(235, 242, 255, 195), 1))
        p.drawPath(line)
        p.restore()

    def _paint_audio_fade(self, p, clip, rect, edge, width, curve):
        """The waveform is already scaled by the fade; this is the curve line over it."""
        points = self._fade_shape_points(clip, rect, edge, width, curve)
        line = QPainterPath(points[0])
        for point in points[1:]:
            line.lineTo(point)
        p.save()
        p.setClipRect(rect, Qt.ClipOperation.IntersectClip)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(TIMELINE_COLORS["wave"]).lighter(145), 1))
        p.drawPath(line)
        p.restore()

    def _paint_fades(self, p, track, clip, rect):
        for edge in ("in", "out"):
            frames = clip.fade_in if edge == "in" else clip.fade_out
            curve = clip.fade_in_curve if edge == "in" else clip.fade_out_curve
            width = frames * max(self.ppf, 1e-6)
            if frames <= 0 or width < 1.0:
                continue
            if track.type == "audio":
                self._paint_audio_fade(p, clip, rect, edge, width, curve)
            else:
                self._paint_video_fade(p, clip, rect, edge, width, curve)

    def _fade_handles_visible(self, clip):
        """Grips only on a hovered, selected or currently dragged clip: a clean timeline."""
        drag = self._fade_drag
        return (clip.id in self.session.selected or clip.id == self._hover_clip
                or (drag is not None and drag["clip_id"] == clip.id))

    def _paint_fade_handles(self, p, clip, rect):
        if not self._fade_handles_visible(clip):
            return
        drag = self._fade_drag
        half = self.HANDLE_PX / 2.0
        for edge in ("in", "out"):
            x = max(rect.left() + half, min(rect.right() - half, self._fade_handle_x(clip, edge)))
            grip = QRectF(x - half, rect.top() + 1, self.HANDLE_PX, self.HANDLE_PX)
            hot = drag is not None and drag["clip_id"] == clip.id and drag["edge"] == edge
            # A light grip with a dark notch reads on light thumbnails and dark audio alike.
            p.setPen(QPen(QColor("#ffd166" if hot else "#0b1017"), 1))
            p.setBrush(QColor("#ffd166" if hot else "#e8edf7"))
            p.drawRoundedRect(grip, 2, 2)
            p.setPen(QPen(QColor("#10151d"), 1))
            p.drawLine(QPointF(grip.center().x(), grip.top() + 2),
                       QPointF(grip.center().x(), grip.bottom() - 2))

    def _paint_fade_chip(self, p, clip, rect):
        """Length + curve readout while a grip is being dragged."""
        drag = self._fade_drag
        if drag is None or drag["clip_id"] != clip.id or not drag.get("moved"):
            return
        frames = clip.fade_in if drag["edge"] == "in" else clip.fade_out
        curve = clip.fade_in_curve if drag["edge"] == "in" else clip.fade_out_curve
        fps = max(1, self.timeline.fps)
        label = f"{frames / fps:.2f}s  {frames}f  {fade_curve_name(curve)}"
        p.setFont(QFont("Segoe UI", 8))
        metrics = p.fontMetrics()
        width = metrics.horizontalAdvance(label) + 14
        x = self._fade_handle_x(clip, drag["edge"])
        left = min(max(self.HEADER_W + 2.0, x - width / 2.0), max(self.HEADER_W + 2.0, self.width() - width - 2))
        box = QRectF(left, rect.top() + self.HANDLE_PX + 4, width, 16)
        p.setPen(QPen(QColor(TIMELINE_COLORS["accent"]), 1))
        p.setBrush(QColor(9, 13, 19, 238))
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QColor("#ffffff"))
        p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    @staticmethod
    def _array_image(frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        return QImage(rgb.data, w, h, rgb.strides[0], QImage.Format.Format_RGB888).copy()

    def _paint_ghost(self, p, placements, valid):
        """Preview of where a drag will land; red when the spot is not allowed."""
        color = QColor(TIMELINE_COLORS["ghost"] if valid else TIMELINE_COLORS["invalid"])
        fill = QColor(color)
        fill.setAlpha(58)
        for clip, target, start in placements:
            track_y = int(round(self.header_y(target)))
            height = self.track_height(target)
            if track_y + height < self.TRACKS_TOP or track_y > self.height():
                continue
            p.fillRect(self.HEADER_W, track_y, self.width() - self.HEADER_W, height,
                       QColor(124, 156, 255, 26))
            x1, x2 = self.x_at(start), self.x_at(start + clip.duration)
            rect = QRectF(x1, track_y + 3, max(3, x2 - x1), height - 6)
            p.fillRect(rect, fill)
            p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            p.drawRect(rect)
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(QPolygonF([QPointF(x1, rect.top()), QPointF(x1 + 5, rect.top()),
                                     QPointF(x1, rect.top() + 5)]))
            p.setPen(QColor("#ffffff"))
            p.setFont(QFont("Segoe UI", 8))
            p.drawText(rect.adjusted(5, 2, -4, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop,
                       f"{clip.name} -> {target.name}  {timecode(start, self.timeline.fps)}")
            if valid and self.session.overlap_mode == "crossfade":
                # The proposed overlap region and its duration, before the drop happens.
                moving = {item.id for item, _lane, _start in placements}
                for other in target.clips:
                    if other.id in moving:
                        continue
                    overlap_start = max(start, other.timeline_start)
                    overlap_end = min(start + clip.duration, other.end)
                    if overlap_end <= overlap_start:
                        continue
                    ox1, ox2 = self.x_at(overlap_start), self.x_at(overlap_end)
                    overlap = QRectF(ox1, rect.top(), max(2, ox2 - ox1), rect.height())
                    colour = QColor("#ffd166")
                    colour.setAlpha(96)
                    p.fillRect(overlap, colour)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                    p.setPen(QPen(QColor("#ffd166"), 1, Qt.PenStyle.SolidLine))
                    p.drawRect(overlap)
                    if overlap.width() >= 44:
                        p.setPen(QColor("#10151d"))
                        p.drawText(overlap, Qt.AlignmentFlag.AlignCenter,
                                   f"{overlap_end - overlap_start}f")

    # ------------------------------------------------------------ media drops
    def _paint_media_drop(self, p, drop):
        """Semi-transparent preview of a project-media drop: video and audio together.

        Both rectangles come from `Timeline.plan_media`, the same call the drop uses, so the
        preview can never promise a different position or lane than the insertion produces.
        """
        placements = drop.get("placements") or []
        color = QColor(TIMELINE_COLORS["ghost"] if drop.get("valid") else TIMELINE_COLORS["invalid"])
        fill = QColor(color)
        fill.setAlpha(70)
        for placement in placements:
            track = placement.get("track")
            top = self._drop_lane_top(track, placement.get("stream"))
            height = self.track_height(track) if track is not None else self.DEFAULT_TRACK_H
            if top + height < self.TRACKS_TOP or top > self.height():
                continue
            x1 = self.x_at(placement["start"])
            x2 = self.x_at(placement["start"] + max(1, placement["duration"]))
            rect = QRectF(x1, top + 3, max(4, x2 - x1), height - 6)
            p.fillRect(rect, fill)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
            p.drawRect(rect)
            if rect.height() >= 16 and rect.width() >= 40:
                p.setPen(QColor("#ffffff"))
                p.setFont(QFont("Segoe UI", 8))
                kind = "Audio" if placement.get("stream") == "audio" else "Video"
                label = placement.get("name") or ""
                if track is not None:
                    label = f"{label}  ·  {track.name}"
                else:
                    label = f"{label}  ·  new {kind.lower()} track"
                p.save()
                p.setClipRect(rect.adjusted(1, 1, -1, -1), Qt.ClipOperation.IntersectClip)
                p.drawText(rect.adjusted(5, 2, -4, -2),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, label)
                p.restore()

    def _drop_lane_top(self, track, stream):
        """Screen y of a drop lane; a lane that does not exist yet is predicted below its kind."""
        if track is not None and track in self.timeline.tracks:
            return int(round(self.header_y(track)))
        kind = "audio" if stream == "audio" else "video"
        lanes = self.timeline.tracks_of_type(kind)
        if lanes:
            last = lanes[-1]
            return int(round(self.header_y(last))) + self.track_height(last)
        if self.timeline.tracks:
            last = self.timeline.tracks[-1]
            return int(round(self.header_y(last))) + self.track_height(last)
        return self.TRACKS_TOP

    def _media_drop_ids(self, mime):
        if not mime.hasFormat(MEDIA_MIME):
            return []
        try:
            raw = bytes(mime.data(MEDIA_MIME)).decode("utf-8")
        except UnicodeDecodeError:
            return []
        return [item for item in raw.splitlines() if item]

    def _media_entries(self, ids):
        lookup = self.media_lookup
        if lookup is None or not ids:
            return []
        try:
            return [entry for entry in (lookup(ids) or []) if entry]
        except Exception:
            return []

    def _update_media_drop(self, event):
        ids = self._media_drop_ids(event.mimeData())
        frame, track = self.drop_target_at(int(event.position().x()), int(event.position().y()))
        entries = self._media_entries(ids)
        if not ids or not entries:
            self._media_drop = {"ids": ids, "frame": frame, "track": track, "placements": [],
                                "valid": False, "reason": "missing media"}
            self.update()
            return
        placements, valid, reason = self.timeline.plan_media(entries, frame, track, False, create=False)
        self._media_drop = {"ids": ids, "frame": frame, "track": track, "placements": placements,
                            "valid": valid, "reason": reason}
        self.update()

    def clear_media_drop(self):
        if self._media_drop is not None:
            self._media_drop = None
            self.update()

    def _paint_playhead_line(self, p):
        """The thin line through the lanes only; the handle lives in the fixed ruler."""
        x = self.x_at(self.session.playhead)
        if x < self.HEADER_W:
            return
        p.setPen(QPen(QColor(TIMELINE_COLORS["playhead"]), 1))
        p.drawLine(x, self.TRACKS_TOP, x, self.height())

    def _paint_playhead_handle(self, p, ruler_top):
        """Red handle, drawn inside the ruler so it never scrolls away."""
        x = self.x_at(self.session.playhead)
        bottom = ruler_top + self.RULER_H
        if x < self.HEADER_W:
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(TIMELINE_COLORS["playhead"]))
        p.drawPolygon(QPolygonF([QPointF(x - 6, ruler_top + 2), QPointF(x + 6, ruler_top + 2),
                                 QPointF(x + 6, bottom - 10), QPointF(x, bottom - 2),
                                 QPointF(x - 6, bottom - 10)]))

    # ------------------------------------------------------------------ markers
    def _marker_flag(self, ruler_top, x):
        """Small pin: a squared head that tapers to a point on the frame."""
        return QPolygonF([QPointF(x - 4.5, ruler_top + 1), QPointF(x + 4.5, ruler_top + 1),
                          QPointF(x + 4.5, ruler_top + 8), QPointF(x, ruler_top + 12),
                          QPointF(x - 4.5, ruler_top + 8)])

    def _marker_at_x(self, x):
        """Nearest marker inside the 10 px grab area; generous enough when zoomed out."""
        best, best_dx = None, self.MARKER_HIT_PX + 0.5
        for marker in self.timeline.markers:
            dx = abs(self.x_at(marker.frame) - x)
            if dx < best_dx:
                best, best_dx = marker, dx
        return best

    def _paint_marker_lines(self, p):
        """Thin translucent guide through the lanes; clipped to the track area by the caller."""
        if not self.timeline.markers:
            return
        selected = self.session.marker_selected
        for marker in self.timeline.markers:
            x = self.x_at(marker.frame)
            if x < self.HEADER_W - 1 or x > self.width():
                continue
            color = QColor(marker.color)
            color.setAlpha(140 if marker.id in selected else 60)
            p.setPen(QPen(color, 1, Qt.PenStyle.SolidLine))
            p.drawLine(QPointF(x, self.TRACKS_TOP), QPointF(x, self.height()))

    def _paint_markers(self, p, ruler_top):
        """Flags with an elided name, plus the live timecode chip while dragging."""
        if not self.timeline.markers and not self._marker_drag:
            return
        metrics = p.fontMetrics()
        p.setFont(QFont("Segoe UI", 8))
        selected = self.session.marker_selected
        flag_xs = [self.x_at(m.frame) for m in self.timeline.markers]
        for index, marker in enumerate(self.timeline.markers):
            x = flag_xs[index]
            if x < self.HEADER_W - 6 or x > self.width() + 6:
                continue
            color = QColor(marker.color)
            poly = self._marker_flag(ruler_top, x)
            p.setBrush(color)
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(poly)
            if marker.id in selected:
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.setPen(QPen(QColor("#ffffff"), 1))
                p.drawPolygon(poly)
            if not marker.name:
                continue
            # The label may run until the next flag (or the viewport edge) and is elided.
            limit = self.width() - 4
            for other in flag_xs:
                if other > x + 8:
                    limit = min(limit, other - 6)
            available = int(limit - (x + 8))
            if available < 20:
                continue
            text = metrics.elidedText(marker.name, Qt.TextElideMode.ElideRight, available)
            box = QRectF(x + 7, ruler_top + 1, metrics.horizontalAdvance(text) + 8, 12)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(9, 13, 19, 215))
            p.drawRoundedRect(box, 2, 2)
            p.setPen(color.lighter(135))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        drag = self._marker_drag
        if drag and drag.get("frame") is not None:
            label = timecode(drag["frame"], self.timeline.fps)
            if len(drag.get("ids", ())) > 1:
                label += f"  +{len(drag['ids']) - 1}"
            width = metrics.horizontalAdvance(label) + 14
            left = min(max(self.HEADER_W + 2.0, self.x_at(drag["frame"]) - width / 2),
                       max(self.HEADER_W + 2.0, self.width() - width - 2))
            # The readout lives in the loop strip row so it never fights the ruler ticks.
            box = QRectF(left, self.loop_strip_top + 1, width, self.LOOP_H - 2)
            p.setPen(QPen(QColor(TIMELINE_COLORS["accent"]), 1))
            p.setBrush(QColor(9, 13, 19, 238))
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_marker_marquee(self, p):
        """Shift-drag rectangle in the ruler: it only selects markers, never clips."""
        marquee = self._marker_marquee
        if not marquee:
            return
        rect = QRectF(QPointF(*marquee["start"]), QPointF(*marquee["end"])).normalized()
        rect = rect.intersected(QRectF(self.HEADER_W, self.ruler_top, self.width() - self.HEADER_W,
                                       self.RULER_H))
        if rect.isEmpty():
            return
        fill = QColor(TIMELINE_COLORS["band"])
        fill.setAlpha(38)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.fillRect(rect, fill)
        p.setPen(QPen(QColor(TIMELINE_COLORS["band"]), 1))
        p.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))

    # -------------------------------------------------------------------- snap
    def _snap_alpha(self):
        """1.0 while engaged, then a quick 150 ms fade so release never pops."""
        if self._snap_fade is None:
            return 1.0
        elapsed = (time.monotonic() - self._snap_fade) * 1000.0
        return max(0.0, 1.0 - elapsed / float(self.SNAP_FADE_MS))

    EDGE_GLOW_PX = 30.0        # how far the white gradient reaches into the clip

    def _snap_rect_for(self, hint):
        """Dirty rect of the feedback: only this small area is repainted on show/hide."""
        if not hint:
            return QRectF()
        x = self.x_at(hint["frame"])
        rect = QRectF(x - 10, self.TRACKS_TOP, 20, max(0, self.height() - self.TRACKS_TOP))
        for _clip_id, edge, clip_rect in hint.get("targets", ()):
            reach = self.EDGE_GLOW_PX
            if edge == "start":
                rect = rect.united(QRectF(clip_rect.left() - 2, clip_rect.top(),
                                          reach + 4, clip_rect.height()))
            else:
                rect = rect.united(QRectF(clip_rect.right() - reach - 4, clip_rect.top(),
                                          reach + 6, clip_rect.height()))
        if hint.get("kind") in ("marker", "playhead"):
            rect = rect.united(QRectF(x - 10, self.ruler_top, 20, self.RULER_H))
        if hint.get("caption"):
            rect = rect.united(self._snap_caption_rect(hint, x))
        return rect.adjusted(-2, -2, 2, 2)

    def _snap_caption_rect(self, hint, x):
        caption = hint.get("caption") or ""
        width = min(220.0, 7.0 * len(caption) + 14.0)
        left = min(max(self.HEADER_W + 4.0, x + 6.0), max(self.HEADER_W + 4.0,
                                                          self.width() - width - 4.0))
        return QRectF(left, self.TRACKS_TOP + 3.0, width, 17.0)

    def _paint_snap_glow(self, p):
        """The snap feedback: exact guide, glowing target edge(s) and a small caption."""
        hint = self._snap_hint
        if not hint:
            return
        alpha = self._snap_alpha()
        if alpha <= 0:
            return
        x = self.x_at(hint["frame"])
        if x < self.HEADER_W:
            return
        # One thin, quiet guide at the resolved snap position: it is the same frame the
        # clips actually landed on, never an approximation of it.
        p.setPen(QPen(QColor(255, 255, 255, int(78 * alpha)), 1))
        p.drawLine(QPointF(x, self.TRACKS_TOP), QPointF(x, self.height()))
        for _clip_id, edge, clip_rect in hint.get("targets", ()):
            self._paint_edge_glow(p, clip_rect, edge, alpha)
        self._paint_moving_edge(p, hint, alpha)
        self._paint_snap_caption(p, hint, x, alpha)

    def _paint_edge_glow(self, p, clip_rect, edge, alpha):
        """A bright white edge with a short gradient fading inward from that edge.

        Only the relevant edge of only the relevant clip lights up: a solid outline or a
        full-height band would misrepresent which clip the drag is actually snapping to.
        """
        top = clip_rect.top() + 1.0
        bottom = clip_rect.bottom() - 1.0
        if bottom <= top or clip_rect.width() <= 0:
            return
        x = clip_rect.left() if edge == "start" else clip_rect.right()
        reach = min(self.EDGE_GLOW_PX, max(6.0, clip_rect.width()))
        if edge == "start":
            band = QRectF(x, top, reach, bottom - top)
            gradient = QLinearGradient(x, 0.0, x + reach, 0.0)
        else:
            band = QRectF(x - reach, top, reach, bottom - top)
            gradient = QLinearGradient(x, 0.0, x - reach, 0.0)
        gradient.setColorAt(0.0, QColor(255, 255, 255, int(118 * alpha)))
        gradient.setColorAt(0.30, QColor(255, 255, 255, int(46 * alpha)))
        gradient.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(gradient))
        p.drawRect(band)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, int(240 * alpha)), 1.6))
        p.drawLine(QPointF(x, top), QPointF(x, bottom))

    def _paint_moving_edge(self, p, hint, alpha):
        """Two small ticks on the clips being dragged: this is the edge that snapped."""
        drag = self._drag or {}
        ids = list(drag.get("ids") or ())
        if not ids and drag.get("clip_id"):
            ids = [drag["clip_id"]]
        if not ids:
            return
        x = self.x_at(hint["frame"])
        p.setPen(QPen(QColor(255, 255, 255, int(215 * alpha)), 2.0))
        for clip_id in ids:
            clip = self.timeline.find_clip(clip_id)
            if clip is None:
                continue
            rect = self._clip_rect(clip)
            top, bottom = rect.top() + 2.0, rect.bottom() - 2.0
            if bottom <= top:
                continue
            p.drawLine(QPointF(x - 4.0, top), QPointF(x + 4.0, top))
            p.drawLine(QPointF(x - 4.0, bottom), QPointF(x + 4.0, bottom))

    def _paint_snap_caption(self, p, hint, x, alpha):
        """A small caption naming the target, so the feedback is never a mystery."""
        caption = hint.get("caption")
        if not caption:
            return
        box = self._snap_caption_rect(hint, x)
        font = QFont(self.font())
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.0))
        p.setFont(font)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(18, 22, 30, int(215 * alpha)))
        p.drawRoundedRect(box, 4.0, 4.0)
        p.setPen(QColor(255, 255, 255, int(238 * alpha)))
        p.drawText(box, int(Qt.AlignmentFlag.AlignCenter), caption)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setFont(self.font())

    def _paint_snap_flag_glow(self, p, ruler_top):
        """Marker/playhead snaps also light up the flag in the fixed ruler."""
        hint = self._snap_hint
        if not hint or hint.get("kind") not in ("marker", "playhead", "loop", "edge"):
            return
        alpha = self._snap_alpha()
        if alpha <= 0:
            return
        x = self.x_at(hint["frame"])
        if x < self.HEADER_W:
            return
        if hint.get("kind") not in ("marker", "playhead"):
            return
        glow = QColor(255, 255, 255, int(90 * alpha))
        p.setPen(QPen(glow, 5))
        p.drawLine(QPointF(x, ruler_top + 1), QPointF(x, self.TRACKS_TOP - 1))
        p.setPen(QPen(QColor(255, 255, 255, int(255 * alpha)), 1))
        p.drawLine(QPointF(x, ruler_top), QPointF(x, self.TRACKS_TOP))

    # Which kind of target wins when two candidates are exactly the same distance away.
    # Clip edges come first (that is what a drag is usually aligning to), then the
    # playhead, markers, the loop region and finally frame 0. The order is fixed, so the
    # same pointer position always resolves to the same target.
    SNAP_PRIORITY = {"edge": 0, "playhead": 1, "marker": 2, "loop": 3, "origin": 4}

    def _snap_candidates(self, exclude=(), include_playhead=True):
        """Every snap target as (frame, kind, clip_id, edge)."""
        candidates = [(0, "origin", None, None)]
        if include_playhead:
            candidates.append((int(self.session.playhead), "playhead", None, None))
        for clip in self.timeline.all_clips():
            if clip.id in exclude:
                continue
            candidates.append((int(clip.timeline_start), "edge", clip.id, "start"))
            candidates.append((int(clip.end), "edge", clip.id, "end"))
        candidates.extend((int(m.frame), "marker", None, None) for m in self.timeline.markers)
        region = self.timeline.loop_range()
        if region is not None:
            candidates.append((int(region[0]), "loop", None, "start"))
            candidates.append((int(region[1]), "loop", None, "end"))
        return candidates

    def _snap_target(self, frame, exclude=(), modifiers=None, include_playhead=True, exclude_frames=()):
        """Snap in at 8 px, release at 12 px, so a pointer hovering the edge cannot flicker."""
        frame = max(0, int(frame))
        if not self.snap_enabled or (modifiers is not None
                                     and modifiers & Qt.KeyboardModifier.ShiftModifier):
            return frame, None
        x = self.x_at(frame)
        cached = self._snap_cached
        best = None
        for value, kind, _clip_id, _edge in self._snap_candidates(exclude, include_playhead):
            if value in exclude_frames:
                continue
            dx = abs(self.x_at(value) - x)
            limit = self.SNAP_OUT_PX if cached == value else self.SNAP_IN_PX
            if dx > limit:
                continue
            # Closest wins; on a tie the kind decides, then the earlier frame. Nothing
            # depends on the order clips happen to be stored in, so the same pointer
            # position cannot flip between two targets while the mouse is held still.
            key = (dx, self.SNAP_PRIORITY.get(kind, 9), value)
            if best is None or key < best[0]:
                best = (key, int(value))
        return (frame, None) if best is None else (best[1], best[1])

    def _snap_hint_for(self, frame):
        """Everything needed to show a snap: target, glowing edges and a caption.

        Every clip whose start or end sits on the resolved frame is collected, so several
        aligned boundaries all light up and the caption can say so.
        """
        frame = int(frame)
        region = self.timeline.loop_range()
        if frame == self.session.playhead:
            kind = "playhead"
        elif any(int(m.frame) == frame for m in self.timeline.markers):
            kind = "marker"
        elif region is not None and frame in (int(region[0]), int(region[1])):
            kind = "loop"
        elif frame == 0:
            kind = "origin"
        else:
            kind = "edge"
        targets = []
        for clip in self.timeline.all_clips():
            for edge, value in (("start", int(clip.timeline_start)), ("end", int(clip.end))):
                if value != frame:
                    continue
                rect = self._clip_rect(clip)
                if rect.width() > 0:
                    targets.append((clip.id, edge, rect))
        caption = self._snap_caption(frame, kind, targets, region)
        return {"frame": frame, "kind": kind, "targets": targets, "caption": caption}

    def _snap_caption(self, frame, kind, targets, region):
        """Human wording for the active snap target."""
        if kind == "edge":
            if len(targets) == 1:
                clip = self.timeline.find_clip(targets[0][0])
                edge = "start" if targets[0][1] == "start" else "end"
                name = (clip.name if clip is not None else "Clip")
                return f"{name} \u00b7 {edge}"
            if len(targets) > 1:
                return f"{len(targets)} clip edges aligned"
            return ""
        if kind == "playhead":
            return "Playhead"
        if kind == "marker":
            marker = next((m for m in self.timeline.markers if int(m.frame) == frame), None)
            name = getattr(marker, "name", "") if marker is not None else ""
            return f"Marker \u00b7 {name}" if name else "Marker"
        if kind == "loop" and region is not None:
            return "Loop in" if frame == int(region[0]) else "Loop out"
        if kind == "origin":
            return "Timeline start"
        return ""

    def _set_snap_hint(self, frame):
        """Show (or clear) the glow; repaints only the old and new dirty rects."""
        hint = None if frame is None else self._snap_hint_for(frame)
        if hint is not None and self._snap_hint == hint:
            if self._snap_fade is not None:
                self._snap_fade = None
                self._snap_timer.stop()
            return
        dirty = self._snap_rect_for(self._snap_hint)
        self._snap_fade = None
        self._snap_timer.stop()
        self._snap_hint = hint
        self._snap_cached = None if hint is None else hint["frame"]
        if hint is not None:
            dirty = dirty.united(self._snap_rect_for(hint))
        if not dirty.isEmpty():
            self.update(dirty.toRect())

    def _release_snap(self, animate=True):
        """Let go of the snap: fade the glow out over 150 ms (or instantly)."""
        self._snap_cached = None
        if self._snap_hint is None:
            return
        if not animate:
            hint, self._snap_hint = self._snap_hint, None
            self._snap_fade = None
            self._snap_timer.stop()
            dirty = self._snap_rect_for(hint)
            if not dirty.isEmpty():
                self.update(dirty.toRect())
            return
        self._snap_fade = time.monotonic()
        self._snap_timer.start()

    def _tick_snap_fade(self):
        hint = self._snap_hint
        if hint is None or self._snap_fade is None:
            self._snap_timer.stop()
            return
        if (time.monotonic() - self._snap_fade) * 1000.0 >= self.SNAP_FADE_MS:
            self._snap_hint = None
            self._snap_fade = None
            self._snap_timer.stop()
        self.update(self._snap_rect_for(hint).toRect())

    def _clip_at(self, x, y):
        track = self.track_at(y)
        if track is None:
            return track, None
        frame = self.frame_at(x)
        clip = next((c for c in track.clips if c.timeline_start <= frame < c.end), None)
        return track, clip

    def _band_clip_ids(self):
        """Clips the rubber band currently overlaps (fully or partially)."""
        if not self._band:
            return set()
        x1, y1 = self._band
        x2, y2 = self._band_end
        left, right = min(x1, x2), max(x1, x2)
        top, bottom = min(y1, y2), max(y1, y2)
        ids = set()
        for track in self.timeline.tracks:
            track_top = self.header_y(track)
            if track_top > bottom or track_top + self.track_height(track) < top:
                continue
            for clip in track.clips:
                if self.x_at(clip.end) >= left and self.x_at(clip.timeline_start) <= right:
                    ids.add(clip.id)
        return ids

    def _snap_frame(self, frame, exclude=(), modifiers=None, include_playhead=True,
                    exclude_frames=()):
        """Snap a frame and publish the glow hint for whatever it stuck to."""
        target, hit = self._snap_target(frame, exclude, modifiers, include_playhead, exclude_frames)
        self._set_snap_hint(hit)
        return target

    # ------------------------------------------------------------------ scrubbing
    def _scrub(self, x, modifiers=Qt.KeyboardModifier.NoModifier):
        """Move the playhead to the frame under x, snapping to edits like a NLE ruler."""
        end = max(self.timeline.duration, self.timeline.min_duration)
        frame = max(0, min(end, self.frame_at(x)))
        if self.snap_enabled and not (modifiers & Qt.KeyboardModifier.ShiftModifier):
            # The playhead is the thing being moved, so it never snaps to itself.
            frame = self._snap_frame(frame, modifiers=modifiers, include_playhead=False)
        else:
            self._release_snap(animate=False)
        self.session.set_playhead(frame, follow=False)

    def _scrub_tick(self):
        """Autoscroll while the pointer is held near either end of the visible range."""
        if not self._drag or self._drag.get("kind") != "scrub":
            self._scrub_timer.stop()
            return
        x, modifiers = self._scrub_pos
        edge = self.SCRUB_EDGE_PX
        speed = 0.0
        if x > self.width() - edge:
            speed = min(60.0, 6.0 + (x - (self.width() - edge)) * 0.6)
        elif x < self.HEADER_W + edge:
            speed = -min(60.0, 6.0 + (self.HEADER_W + edge - x) * 0.6)
        if speed:
            before = self.scroll_x
            self.scroll_x = min(self._max_scroll_x(), max(0.0, self.scroll_x + speed))
            if self.scroll_x != before:
                self.update()
            self._scrub(x, modifiers)

    # ------------------------------------------------------- loop strip / ruler
    def _snap_loop_frame(self, frame, modifiers=Qt.KeyboardModifier.NoModifier, exclude_frames=()):
        """Loop edges snap to 0, the playhead, markers, clip edges and the loop itself."""
        frame = max(0, int(frame))
        if not self.snap_enabled or modifiers & Qt.KeyboardModifier.ShiftModifier:
            self._release_snap(animate=False)
            return frame
        return self._snap_frame(frame, modifiers=modifiers, exclude_frames=set(exclude_frames))

    def _press_fixed_strip(self, x, y, modifiers):
        """The loop strip edits the loop region; the ruler selects/drags markers,
        scrubs otherwise, and Shift+drag there draws a marker marquee."""
        if y >= self.ruler_top:
            marker = self._marker_at_x(x)
            if marker is not None:
                return self._marker_press(marker, modifiers)
            if modifiers & Qt.KeyboardModifier.ShiftModifier:
                return self._marker_marquee_press(x, y, modifiers)
            if self.session.marker_selected:
                self.session.clear_marker_selection()
        if y < self.ruler_top or modifiers & Qt.KeyboardModifier.ControlModifier:
            self._loop_press(x, modifiers)
            return True
        # The window owns playback state: it decides whether to resume on release.
        self._drag = {"kind": "scrub", "moved": False}
        self._scrub_pos = (x, modifiers)
        self.scrub_started.emit()
        self._scrub(x, modifiers)
        self._scrub_timer.start()
        return True

    def _loop_press(self, x, modifiers):
        region = self.timeline.loop_range()
        frame = self._snap_loop_frame(self.frame_at(x), modifiers, exclude_frames=region or ())
        mode, grab = "new", 0
        if region is not None:
            lo_x, hi_x = self.x_at(region[0]), self.x_at(region[1])
            if abs(x - lo_x) <= self.LOOP_EDGE_PX:
                mode = "left"
            elif abs(x - hi_x) <= self.LOOP_EDGE_PX:
                mode = "right"
            elif lo_x < x < hi_x:
                mode = "move"
                grab = frame - region[0]
        self._loop_drag = {"mode": mode, "anchor": frame, "grab": grab, "orig": region,
                           "moved": False}
        self._loop_preview = region if mode != "new" else None
        self.setCursor(Qt.CursorShape.SizeHorCursor if mode in ("left", "right")
                       else Qt.CursorShape.OpenHandCursor)
        self.update()

    def _loop_move(self, x, modifiers):
        drag = self._loop_drag
        if drag is None:
            return
        frame = self._snap_loop_frame(self.frame_at(x), modifiers,
                                      exclude_frames=drag["orig"] or ())
        orig = drag["orig"]
        if drag["mode"] == "new":
            lo, hi = min(drag["anchor"], frame), max(drag["anchor"], frame)
            hi = max(hi, lo + 1)
        elif drag["mode"] == "left":
            lo, hi = max(0, min(frame, orig[1] - 1)), orig[1]
        elif drag["mode"] == "right":
            lo, hi = orig[0], max(frame, orig[0] + 1)
        else:
            span = orig[1] - orig[0]
            lo = max(0, frame - drag["grab"])
            hi = lo + span
        drag["moved"] = True
        self._loop_preview = (lo, hi)
        self.update()

    def _loop_release(self, x=None):
        drag, preview = self._loop_drag, self._loop_preview
        self._loop_drag, self._loop_preview = None, None
        self.unsetCursor()
        self._release_snap()
        if drag is None:
            return
        if not drag["moved"]:
            # A plain click in the strip just parks the playhead; it never clears the region.
            if x is not None:
                self.session.set_playhead(self.frame_at(x))
            self.update()
            return
        if preview is None or preview[1] <= preview[0]:
            self.update()
            return
        self.session.set_loop_region(preview[0], preview[1])
        self._release_snap()
        self.session.status.emit(
            f"Loop region {timecode(preview[0], self.timeline.fps)} - {timecode(preview[1], self.timeline.fps)}")
        self.update()

    def _strip_cursor(self, x, y):
        """Hand/size cursors over the loop band, plain over bare strip/ruler."""
        if y >= self.ruler_top:
            return Qt.CursorShape.SizeHorCursor
        region = self.timeline.loop_range()
        if region is None:
            return Qt.CursorShape.IBeamCursor
        lo_x, hi_x = self.x_at(region[0]), self.x_at(region[1])
        if abs(x - lo_x) <= self.LOOP_EDGE_PX or abs(x - hi_x) <= self.LOOP_EDGE_PX:
            return Qt.CursorShape.SizeHorCursor
        if lo_x < x < hi_x:
            return Qt.CursorShape.OpenHandCursor
        return Qt.CursorShape.IBeamCursor

    # -------------------------------------------------------------- marker edit
    def _marker_action_ids(self, marker):
        """A marker that is part of the selection acts on the whole selection."""
        selected = set(self.session.marker_selected)
        return selected if marker.id in selected else {marker.id}

    def _marker_press(self, marker, modifiers):
        additive = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if additive:
            selection = set(self.session.marker_selected) | {marker.id}
        elif marker.id in self.session.marker_selected:
            selection = set(self.session.marker_selected)
        else:
            selection = {marker.id}
        self.session.set_marker_selection(selection)
        self.session.set_selection(set())
        self._header_selected = None
        self._marker_drag = {"anchor": marker.id, "grab_frame": marker.frame, "frame": marker.frame,
                             "ids": sorted(selection),
                             "orig": {m.id: m.frame for m in self.session.selected_markers()},
                             "snapshot": self.timeline.snapshot(), "moved": False}
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.update()
        return True

    def _marker_move(self, x, modifiers):
        drag = self._marker_drag
        if drag is None:
            return
        target = self._snap_frame(self.frame_at(x), modifiers=modifiers)
        delta = target - drag["grab_frame"]
        drag["moved"] = drag["moved"] or delta != 0
        # Frames are assigned directly so a drag stays perfectly reversible; two markers
        # may pass over one another and are merged once on release.
        for marker_id, original in drag["orig"].items():
            marker = self.timeline.marker_by_id(marker_id)
            if marker is not None:
                marker.frame = max(0, original + delta)
        self.timeline.markers.sort(key=lambda m: (m.frame, m.id))
        anchor = self.timeline.marker_by_id(drag["anchor"])
        drag["frame"] = anchor.frame if anchor is not None else None
        self.update()

    def _marker_release(self):
        drag = self._marker_drag
        self._marker_drag = None
        self.unsetCursor()
        self._release_snap()
        if drag is None:
            return
        if not drag["moved"]:
            self.update()
            return
        timeline, ids = self.timeline, set(drag["ids"])
        seen = set()
        for marker in list(timeline.markers):
            if marker.frame in seen:
                timeline.remove_marker(marker.id)
            else:
                seen.add(marker.frame)
        label = "Move marker" if len(ids) == 1 else f"Move {len(ids)} markers"
        self.session.stack.commit(label, drag["snapshot"])
        remaining = {mid for mid in ids if timeline.marker_by_id(mid) is not None}
        self.session.set_marker_selection(remaining)
        anchor = timeline.marker_by_id(drag["anchor"])
        if anchor is not None:
            self.session.status.emit(f"Marker at {timecode(anchor.frame, timeline.fps)}"
                                     + (f" - {anchor.name}" if anchor.name else ""))
        self.update()

    def _cancel_marker_drag(self):
        drag = self._marker_drag
        self._marker_drag = None
        self.unsetCursor()
        self._release_snap(animate=False)
        if drag is not None:
            self.timeline.restore(drag["snapshot"])
            self.session.changed.emit()
        self.update()

    def _marker_marquee_press(self, x, y, modifiers):
        self.session.set_selection(set())
        self._marker_marquee = {"start": (x, self.ruler_top), "end": (x, self.TRACKS_TOP - 1),
                                "base": set(self.session.marker_selected)
                                if modifiers & Qt.KeyboardModifier.ShiftModifier else set()}
        self._update_marker_marquee(x)
        return True

    def _update_marker_marquee(self, x):
        marquee = self._marker_marquee
        if marquee is None:
            return
        marquee["end"] = (x, self.TRACKS_TOP - 1)
        left, right = sorted((marquee["start"][0], x))
        ids = {m.id for m in self.timeline.markers if left - 1 <= self.x_at(m.frame) <= right + 1}
        self.session.set_marker_selection(marquee["base"] | ids)
        self.update(0, self.ruler_top, self.width(), self.RULER_H)

    def rename_marker(self, marker):
        """Public entry point for the Edit menu (F2) and the Markers list."""
        self._rename_marker(marker)

    def _rename_marker(self, marker):
        name, ok = QInputDialog.getText(self, "Rename marker",
                                        f"Name for {timecode(marker.frame, self.timeline.fps)}:",
                                        text=marker.name)
        if ok:
            self.session.rename_marker(marker.id, name.strip())

    def _marker_menu(self, marker, point):
        ids = self._marker_action_ids(marker)
        menu = QMenu(self)
        menu.addAction("Go to Marker", lambda: self.session.go_to_marker(marker.id))
        menu.addAction("Rename...", lambda: self._rename_marker(marker))
        colors = menu.addMenu("Change Color")
        names = ("Orange", "Red", "Yellow", "Green", "Blue", "Purple", "White", "Grey")
        for color, name in zip(MARKER_COLORS, names):
            swatch = QPixmap(12, 12)
            swatch.fill(QColor(color))
            action = colors.addAction(QIcon(swatch), name)
            action.setCheckable(True)
            action.setChecked(marker.color == color)
            action.triggered.connect(
                lambda _checked=False, value=color, group=frozenset(ids):
                self.session.set_marker_color(value, group))
        menu.addSeparator()
        menu.addAction("Delete", lambda: self.session.delete_markers(ids))
        menu.addAction("Delete All Markers", self.session.delete_all_markers)
        menu.exec(point)

    def _strip_menu(self, point, frame=None):
        menu = QMenu(self)
        if frame is not None:
            menu.addAction(f"Add Marker Here  ({timecode(frame, self.timeline.fps)})",
                           lambda: self.session.add_marker(frame))
            menu.addSeparator()
        loop_action = menu.addAction("Loop Playback")
        loop_action.setCheckable(True)
        loop_action.setChecked(self.timeline.loop_enabled)
        loop_action.setShortcut(QKeySequence("Q"))
        loop_action.triggered.connect(self.session.set_loop_enabled)
        menu.addSeparator()
        menu.addAction("Set Loop Start at Playhead  (I)", self.session.set_loop_start_at_playhead)
        menu.addAction("Set Loop End at Playhead  (O)", self.session.set_loop_end_at_playhead)
        menu.addSeparator()
        menu.addAction("Clear Loop Region", self.session.clear_loop_region)
        menu.addAction("Set Loop to Selected Clips", self.session.set_loop_to_selection)
        menu.addAction("Set Loop to Whole Timeline", self.session.set_loop_to_timeline)
        menu.exec(point)

    # ------------------------------------------------------------------- mouse
    def mousePressEvent(self, event):
        self.setFocus()
        x, y = int(event.position().x()), int(event.position().y())
        if event.button() == Qt.MouseButton.RightButton:
            if x >= self.HEADER_W and self.ruler_top <= y < self.TRACKS_TOP:
                marker = self._marker_at_x(x)
                if marker is not None:
                    if marker.id not in self.session.marker_selected:
                        self.session.set_marker_selection({marker.id})
                    self._marker_menu(marker, event.globalPosition().toPoint())
                    return
                self._strip_menu(event.globalPosition().toPoint(), frame=self.frame_at(x))
                return
            if x >= self.HEADER_W and self.TOP_H <= y < self.ruler_top:
                self._strip_menu(event.globalPosition().toPoint())
                return
            if x < self.HEADER_W and self.track_at(y) is not None:
                self._track_menu(self.track_at(y), event.globalPosition().toPoint())
                return
            transition = self._transition_at(x, y)
            if transition is not None:
                self._transition_menu(transition, event.globalPosition().toPoint())
                return
            preset_clip, preset_index, _preset_edge = self._preset_hit(x, y)
            if preset_clip is not None:
                self.session.set_selection({preset_clip.id})
                self._preset_menu(preset_clip, preset_index, event.globalPosition().toPoint())
                return
            fade_clip, fade_edge = self._fade_hit(x, y)
            if fade_clip is not None:
                self.session.set_selection({fade_clip.id})
                self._fade_menu(fade_clip, fade_edge, event.globalPosition().toPoint())
                return
            env_clip, env_channel, env_kind, env_frame = self._envelope_hit(x, y)
            if env_clip is not None:
                if self.session.marker_selected:
                    self.session.clear_marker_selection()
                if env_kind == "key":
                    self.session.set_key_selection({(env_clip.id, env_channel, env_frame)})
                self._envelope_menu(env_clip, env_channel, env_kind, env_frame,
                                    event.globalPosition().toPoint())
                return
            kf_clip, kf_row, kf_time = self._keyframe_hit(x, y)
            if kf_clip is not None:
                if (kf_clip.id, kf_row["channel"], kf_time) not in self.session.key_selection:
                    self.session.set_key_selection({(kf_clip.id, kf_row["channel"], kf_time)})
                self._keyframe_menu(kf_clip, kf_row["channel"], kf_time,
                                    event.globalPosition().toPoint())
                return
            track, clip = self._clip_at(x, y)
            if clip:
                # Right-clicking a clip that is already part of a multi-selection must
                # keep that selection: Link Selected Clips acts on the whole selection.
                if clip.id not in self.session.selected:
                    self.session.set_selection({clip.id})
                self._clip_menu(clip, event.globalPosition().toPoint())
            else:
                self._timeline_menu(event.globalPosition().toPoint())
            return
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        if x < self.HEADER_W and y >= self.TRACKS_TOP:
            # Track headers occupy the same rows as the lanes but start *below* the
            # fixed ruler band. Bounding this to y < TRACKS_TOP meant no header press
            # ever reached _press_header: the click fell through to the lane hit test,
            # which found the clip whose start sits under the header column and dragged
            # it instead of moving the volume slider or toggling an icon.
            if self._press_header(x, y):
                return
            if self._header_selected is not None or self.session.selected:
                self._header_selected = None
                self.session.set_selection(set())
            self.update()
            return
        if y < self.TRACKS_TOP:
            if x >= self.HEADER_W and y >= self.TOP_H:
                self._press_fixed_strip(x, y, event.modifiers())
            else:
                super().mousePressEvent(event)
            return
        toggle_clip = self._keyframe_toggle_hit(x, y)
        if toggle_clip is not None:
            if toggle_clip.id not in self.session.selected:
                self.session.set_selection({toggle_clip.id})
            self._toggle_keyframe_rows(toggle_clip)
            return
        kf_clip, kf_row, kf_time = self._keyframe_hit(x, y)
        if kf_clip is not None:
            if self.session.marker_selected:
                self.session.clear_marker_selection()
            self._begin_keyframe_drag(kf_clip, kf_row, kf_time, event.modifiers())
            return
        preset_clip, preset_index, preset_edge = self._preset_hit(x, y)
        if preset_clip is not None and preset_edge is not None:
            self._header_selected = None
            if preset_clip.id not in self.session.selected:
                self.session.set_selection({preset_clip.id})
            self._begin_preset_drag(preset_clip, preset_index, preset_edge, x, y)
            return
        fade_clip, fade_edge = self._fade_hit(x, y)
        if fade_clip is not None:
            self._header_selected = None
            if self.session.marker_selected:
                self.session.clear_marker_selection()
            if fade_clip.id not in self.session.selected:
                adding = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self.session.set_selection(self.session.selected | {fade_clip.id} if adding else {fade_clip.id})
            self._begin_fade_drag(fade_clip, fade_edge, x, y, event.modifiers())
            return
        env_clip, env_channel, env_kind, env_frame = self._envelope_hit(x, y)
        if env_clip is not None:
            self._header_selected = None
            if self.session.marker_selected:
                self.session.clear_marker_selection()
            if env_clip.id not in self.session.selected:
                self.session.set_selection({env_clip.id})
            self._begin_envelope_drag(env_clip, env_channel, env_kind, env_frame, event.modifiers())
            return
        transition = self._transition_at(x, y)
        if transition is not None:
            # Selecting the region shows its duration; dragging the clips still works
            # from their non-overlapping edges.
            self._header_selected = None
            self._transition_sel = (transition.out_id, transition.in_id)
            self.session.set_selection({transition.out_id, transition.in_id})
            self.update()
            return
        track, clip = self._clip_at(x, y)
        if clip is not None:
            self._header_selected = None
            if (self.keyframes_visible and clip.id in self._expanded_clips
                    and y > self._clip_rect(clip).bottom()
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                # Ctrl+drag inside an expanded channel lane boxes keyframes.
                self._kf_band = ((x, y), (x, y))
                self.update()
                return
            if self.session.marker_selected:
                self.session.clear_marker_selection()
            if self.tool == "razor":
                self.session.set_playhead(self.frame_at(x))
                self.session.split_clip_at(clip, self.frame_at(x))
                return
            if clip.id not in self.session.selected:
                adding = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                self.session.set_selection(self.session.selected | {clip.id} if adding else {clip.id})
            if track.locked:
                self.session.status.emit(f"{track.name} is locked. Unlock the track to edit it")
                return
            self._release_snap(animate=False)
            ids = [cid for cid in self.session.selected
                   if (c := self.timeline.find_clip(cid)) is not None and not c.track.locked] or [clip.id]
            edge = "left" if abs(x - self.x_at(clip.timeline_start)) < self.EDGE_PX else \
                "right" if abs(x - self.x_at(clip.end)) < self.EDGE_PX else None
            operation = "stretch" if edge and event.modifiers() & Qt.KeyboardModifier.AltModifier else \
                "trim" if edge else "move"
            self._drag = {"kind": operation, "clip_id": clip.id, "ids": ids,
                          "start_x": x, "start_y": y, "start_frame": self.frame_at(x),
                          "grab_offset": self.frame_at(x) - clip.timeline_start,
                          "timeline_snapshot": self.timeline.snapshot(), "edge": edge,
                          "lane_shift": 0, "delta": 0, "moved": False,
                          "anchor_track": clip.track,
                          "followers": self._link_followers(clip, ids)}
            if operation == "move":
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if x >= self.HEADER_W:
            self._transition_sel = None
            if self.session.marker_selected:
                self.session.clear_marker_selection()
            if self.keyframes_visible and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                # Ctrl+drag in the lanes boxes keyframes (plain drag boxes clips).
                self._kf_band = ((x, y), (x, y))
                self.update()
                return
            self._band = (x, y)
            self._band_end = (x, y)
            # Shift keeps the previous selection and adds everything the band touches.
            self._band_base = set(self.session.selected) if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else set()
            self._band_ids = set()
            self._header_selected = None
            self.update()
            return
        super().mousePressEvent(event)

    # ------------------------------------------------------- header interaction
    def _header_track_at(self, y):
        """Track whose header covers y; a lane's bottom border belongs to that lane."""
        for track in self.timeline.tracks:
            top = self.header_y(track)
            bottom = top + self.track_height(track)
            if top <= y < bottom or abs(y - bottom) <= 2:
                return track
        return None

    def _press_header(self, x, y):
        track = self._header_track_at(y)
        if track is None:
            return False
        top = self.header_y(track)
        height = self.track_height(track)
        if abs(y - (top + height)) <= 3:
            self._height_drag = {"track_id": track.id, "start_y": y, "start_h": height}
            self.setCursor(Qt.CursorShape.SizeVerCursor)
            return True
        self._header_selected = track.id
        self.session.set_selection(set())
        action, rect = header_hit(track, self.HEADER_W, height, x, y, origin_y=top)
        if action in ("visible", "mute", "solo", "lock"):
            self._header_press = (track.id, action)
            self._toggle_header_action(track, action)
            return True
        if action == "volume":
            self._volume_drag = {"track_id": track.id, "snapshot": self.timeline.snapshot()}
            self._set_header_volume(track, x, rect)
            return True
        self.update()
        return True

    def _toggle_header_action(self, track, action):
        attr = {"visible": "visible", "mute": "muted", "solo": "solo", "lock": "locked"}[action]
        value = not getattr(track, attr)
        self.session.set_track_attr(track.id, **{attr: value})
        label = {"visible": ("shown", "hidden"), "mute": ("unmuted", "muted"), "solo": ("soloed", "unsoloed"),
                 "lock": ("unlocked", "locked")}[action][0 if value else 1]
        self.session.status.emit(f"{track.name} {label}")
        self.update()

    def _set_header_volume(self, track, x, rect):
        value = max(0.0, min(1.5, (x - rect.left()) / max(1.0, rect.width()) * 1.5))
        if abs(value - track.volume) < 1e-4:
            return
        track.volume = value
        self.audio_refresh.emit()
        self.update()

    def _finish_header_drags(self):
        if self._volume_drag:
            drag = self._volume_drag
            track = self.session.find_track(drag["track_id"])
            self._volume_drag = None
            if track is not None:
                self.session.stack.commit("Track volume", drag["snapshot"])
                self.session.status.emit(f"{track.name} volume {track.volume*100:.0f}%")
            self.audio_refresh.emit()
        if self._height_drag:
            self._height_drag = None
        self._header_press = None
        self.unsetCursor()

    # ------------------------------------------------------------- fade dragging
    def _begin_fade_drag(self, clip, edge, x, y, modifiers):
        """Grab a fade grip: sideways sets the length, up/down bends the curve."""
        linked = (self.session.linked_clip_ids(clip)
                  if modifiers & Qt.KeyboardModifier.ShiftModifier else [])
        self._fade_drag = {
            "clip_id": clip.id, "edge": edge, "snapshot": self.timeline.snapshot(),
            "start_x": x, "start_y": y,
            "frames": clip.fade_in if edge == "in" else clip.fade_out,
            "curve": clip.fade_in_curve if edge == "in" else clip.fade_out_curve,
            "linked_ids": [cid for cid in linked
                           if (target := self.timeline.find_clip(cid)) is not None
                           and not target.track.locked],
            "mode": "curve" if modifiers & Qt.KeyboardModifier.AltModifier else None,
            "moved": False,
        }
        self._release_snap(animate=False)
        self.setCursor(Qt.CursorShape.SizeHorCursor)
        self.update()

    def _fade_boundary_frame(self, clip, x):
        """Timeline frame under the pointer, kept inside this clip."""
        raw = (x - self.HEADER_W + self.scroll_x) / max(self.ppf, 1e-6)
        return max(clip.timeline_start, min(clip.end, int(round(raw))))

    def _fade_move(self, x, y, modifiers):
        drag = self._fade_drag
        clip = self.timeline.find_clip(drag["clip_id"]) if drag else None
        if clip is None:
            return
        dx, dy = x - drag["start_x"], y - drag["start_y"]
        if drag["mode"] is None and (abs(dx) >= 3 or abs(dy) >= 3):
            # Whichever axis moved first wins the gesture, so a length drag never wobbles the curve.
            drag["mode"] = "curve" if abs(dy) > abs(dx) else "length"
        if drag["mode"] == "curve":
            curve = max(-1.0, min(1.0, drag["curve"] + dy / self.FADE_CURVE_PX))
            self.timeline.set_fade_curve(clip.id, drag["edge"], curve)
            for cid in drag["linked_ids"]:
                self.timeline.set_fade_curve(cid, drag["edge"], curve)
        elif drag["mode"] == "length":
            boundary = self._fade_boundary_frame(clip, x)
            if self.snap_enabled and not (modifiers & Qt.KeyboardModifier.ShiftModifier):
                boundary = self._snap_frame(boundary, exclude=(clip.id,), modifiers=modifiers)
            else:
                self._release_snap(animate=False)
            frames = (boundary - clip.timeline_start) if drag["edge"] == "in" else (clip.end - boundary)
            frames = max(0, int(frames))
            self.timeline.set_fade(clip.id, drag["edge"], frames)
            for cid in drag["linked_ids"]:
                self.timeline.set_fade(cid, drag["edge"], frames)
        else:
            return
        drag["moved"] = True
        self.session.changed.emit()
        self.update()

    def _fade_release(self):
        drag, self._fade_drag = self._fade_drag, None
        self.unsetCursor()
        self._release_snap()
        if drag is None:
            return
        if drag.get("moved"):
            self.session.stack.commit("Fade in" if drag["edge"] == "in" else "Fade out", drag["snapshot"])
        self.session.changed.emit()
        self.update()

    def _cancel_fade_drag(self):
        drag, self._fade_drag = self._fade_drag, None
        self.unsetCursor()
        self._release_snap(animate=False)
        if drag is not None:
            self.timeline.restore(drag["snapshot"])
            self.session.changed.emit()
        self.update()

    def _fade_menu(self, clip, edge, point):
        """Right-click a fade grip: curve presets, then clearing it."""
        menu = QMenu(self)
        current = clip.fade_in_curve if edge == "in" else clip.fade_out_curve
        for name, value in FADE_CURVES:
            action = menu.addAction(name, lambda _checked=False, v=value, c=clip, e=edge:
                                    self.session.set_clip_fade(c.id, e, curve=v))
            action.setCheckable(True)
            action.setChecked(abs(current - value) <= 0.02)
        menu.addSeparator()
        menu.addAction("Clear This Fade", lambda: self.session.set_clip_fade(clip.id, edge, 0))
        menu.addAction("Clear Both Fades", lambda: self.session.reset_clip_fades(clip.id))
        menu.exec(point)

    # ------------------------------------------------------------- clip dragging
    def _link_followers(self, clip, ids):
        """Linked partners of a dragged clip: they slide in time but keep their track.

        Honours the Linked Selection toggle (`session.link_partners` is empty when it is
        off) and permanent unlinking (`link_id` groups), and never includes a clip whose
        track is locked or a clip that is already part of the drag.
        """
        found = []
        for cid in [clip.id, *ids]:
            other = self.timeline.find_clip(cid)
            if other is None:
                continue
            for partner_id in self.session.link_partners(other):
                partner = self.timeline.find_clip(partner_id)
                # A partner that is also selected still counts: Linked Selection puts it
                # in the selection, and it has to slide in time without changing lane.
                if partner is None or partner.id == clip.id or partner.track.locked:
                    continue
                if partner.id not in found:
                    found.append(partner.id)
        return found

    def _lane_shift_for(self, y, clip, current_shift):
        """Lane offset for the pointer, with hysteresis so borders cannot flicker."""
        lanes = self.timeline.tracks_of_type(clip.track.type)
        if len(lanes) <= 1:
            return 0
        base = self.timeline.lane_index(clip.track)
        if base < 0:
            return 0
        target = None
        for index, lane in enumerate(lanes):
            top = self.header_y(lane)
            if top <= y < top + self.track_height(lane):
                target = index
                break
        if target is None:
            first, last = lanes[0], lanes[-1]
            if y < self.header_y(first):
                target = 0
            elif y >= self.header_y(last) + self.track_height(last):
                target = len(lanes) - 1
            else:
                return current_shift
        shift = max(-base, min(len(lanes) - 1 - base, target - base))
        if shift == current_shift:
            return shift
        lane = lanes[base + shift]
        top = self.header_y(lane)
        height = self.track_height(lane)
        margin = min(10.0, height / 3.0)
        if top + margin <= y <= top + height - margin:
            return shift
        return current_shift

    def _drag_move(self, x, y):
        drag = self._drag
        clip = self.timeline.find_clip(drag["clip_id"])
        if clip is None:
            return
        mode = self.session.overlap_mode
        if drag["kind"] in ("trim", "stretch"):
            delta = self.frame_at(x) - drag["start_frame"]
            delta = self._snap_frame(self.frame_at(drag["start_x"]) + delta, exclude=(clip.id,),
                                     modifiers=drag.get("modifiers")) - drag["start_frame"]
            if drag["kind"] == "trim":
                # Linked partners take the same delta, so the pair cannot drift apart.
                self.timeline.apply_trim(drag["timeline_snapshot"], clip.id, drag["edge"], delta,
                                         also=[cid for cid in drag["ids"] if cid != clip.id])
            else:
                # A stretched pair has to change length together, so the linked members
                # take the same edge and speed in the same undo step.
                self.timeline.apply_stretch(drag["timeline_snapshot"], clip.id, drag["edge"],
                                            self.frame_at(x), also=drag["followers"])
            drag["moved"] = True
            self.update()
            return
        # Move: everything is derived from the pointer and the stored grab offset,
        # never from the clip's own (unchanged) position, so there is no feedback loop.
        raw = self.frame_at(x) - drag["grab_offset"]
        target_start = self._snap_frame(max(0, raw), exclude=tuple(drag["ids"]),
                                        modifiers=drag.get("modifiers"))
        delta = target_start - clip.timeline_start
        shift = self._lane_shift_for(y, clip, drag["lane_shift"])
        # Linked partners follow the pointer in time only: their lane never changes.
        drag["move_ids"] = list(dict.fromkeys([*drag["ids"], *drag["followers"]]))
        placements, valid = self.timeline.move_preview(drag["move_ids"], delta, shift, mode,
                                                       fixed_tracks=drag["followers"])
        drag["lane_shift"] = shift
        drag["moved"] = True
        if valid:
            drag["delta"] = delta
            drag["last_valid"] = (delta, shift)
        self._ghost = (placements, valid)
        self.update()

    def _commit_drag(self):
        drag, ghost = self._drag, self._ghost
        self._drag, self._ghost = None, None
        self.unsetCursor()
        if drag is None:
            return
        self._release_snap()
        if drag["kind"] in ("trim", "stretch"):
            if drag.get("moved"):
                label = "Trim clip" if drag["kind"] == "trim" else "Stretch clip"
                self.session.stack.commit(label, drag["timeline_snapshot"])
            if drag["kind"] == "stretch":
                self._report_stretch_audio(drag)
            self.session.request_redraw()
            return
        target = drag.get("last_valid")
        if not target or (target[0] == 0 and target[1] == 0):
            self.update()
            return
        delta, shift = target
        mode = self.session.overlap_mode
        ids = list(dict.fromkeys([*drag["ids"], *drag["followers"]]))
        pinned = list(drag["followers"])

        def action():
            fresh, valid = self.timeline.move_preview(ids, delta, shift, mode, fixed_tracks=pinned)
            if valid:
                self.timeline.apply_placements(fresh, mode)
                if mode == "crossfade":
                    # Same undo step as the move: undoing a drop removes its crossfade too.
                    self.timeline.create_auto_transitions(ids)

        first = self.timeline.find_clip(drag["clip_id"])
        moved_to = first.track.name if first is not None else ""
        self.session.stack.run("Move clip" if len(ids) == 1 else f"Move {len(ids)} clips", action)
        if len(ids) == 1 and first is not None:
            self.session.status.emit(f"Moved to {moved_to}, {timecode(first.timeline_start, self.timeline.fps)}")
        self.update()

    def _report_stretch_audio(self, drag):
        """Say how a stretched linked audio clip behaves: speed changes its pitch."""
        audio = []
        for cid in drag.get("followers") or ():
            clip = self.timeline.find_clip(cid)
            if clip is not None and clip.track is not None and clip.track.type == "audio":
                audio.append(clip)
        if not audio:
            return
        speed = audio[0].speed * 100.0
        self.session.status.emit(
            f"Linked audio stretched to {speed:.0f}% - playback follows the clip speed, "
            "so its pitch shifts (no time-stretch)")

    def _cancel_drag(self):
        drag = self._drag
        self._drag, self._ghost = None, None
        self._scrub_timer.stop()
        self._release_snap(animate=False)
        if drag is not None and drag["kind"] in ("trim", "stretch"):
            self.timeline.restore(drag["timeline_snapshot"])
            self.session.changed.emit()
        self.unsetCursor()
        self.update()

    # ------------------------------------------------------------ mouse movement
    def mouseMoveEvent(self, event):
        x, y = int(event.position().x()), int(event.position().y())
        if self._kf_drag:
            self._keyframe_move(x)
            return
        if self._env_drag:
            self._envelope_move(x, y)
            return
        if self._fade_drag:
            if self._preset_drag is not None:
                self._preset_move(x, y, event.modifiers())
                return
            self._fade_move(x, y, event.modifiers())
            return
        if self._marker_drag:
            self._marker_move(x, event.modifiers())
            return
        if self._marker_marquee:
            self._update_marker_marquee(x)
            return
        if self._loop_drag:
            self._loop_move(x, event.modifiers())
            return
        if self._height_drag:
            track = self.session.find_track(self._height_drag["track_id"])
            if track is not None:
                value = max(self.MIN_TRACK_H, min(self.MAX_TRACK_H,
                    int(self._height_drag["start_h"] + (y - self._height_drag["start_y"]))))
                if value == self.track_height_value:
                    self.track_heights.pop(track.id, None)   # back to following the global height
                else:
                    self.track_heights[track.id] = value
                self.update()
            return
        if self._volume_drag:
            track = self.session.find_track(self._volume_drag["track_id"])
            if track is not None:
                top = self.header_y(track)
                layout = header_layout(track, self.HEADER_W, self.track_height(track), origin_y=top)
                self._set_header_volume(track, x, layout["volume"])
            return
        if self._drag and self._drag.get("kind") == "scrub":
            self._drag["moved"] = True
            self._scrub_pos = (x, event.modifiers())
            self._scrub(x, event.modifiers())
            return
        if self._drag:
            self._drag["modifiers"] = event.modifiers()
            self._drag_move(x, y)
            return
        if self._kf_band:
            self._kf_band = (self._kf_band[0], (x, y))
            self.update()
            return
        if self._band:
            self._band_end = (x, y)
            self._band_ids = self._band_clip_ids()
            self.update()
            return
        if x < self.HEADER_W and y >= self.TRACKS_TOP:
            self._update_header_hover(x, y)
            return
        if y < self.TRACKS_TOP and x >= self.HEADER_W:
            marker = self._marker_at_x(x) if y >= self.ruler_top else None
            if marker is not None:
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                self.setToolTip(f"Marker {timecode(marker.frame, self.timeline.fps)}"
                                + (f" - {marker.name}" if marker.name else "")
                                + "  (drag to move, double-click to rename, right-click for more)")
                return
            self.setToolTip(TOOLTIPS["loop_strip" if y < self.ruler_top else "ruler"])
            self.setCursor(self._strip_cursor(x, y))
            return
        self.setToolTip("")
        hover_clip, hover_edge = self._fade_hit(x, y)
        track, clip = ((hover_clip.track, hover_clip) if hover_clip is not None else self._clip_at(x, y))
        if (clip.id if clip is not None else None) != self._hover_clip:
            # Grips appear on hover, so a hover change is a repaint of that lane only.
            previous = self._hover_clip
            self._hover_clip = clip.id if clip is not None else None
            for cid in (previous, self._hover_clip):
                other = self.timeline.find_clip(cid) if cid is not None else None
                if other is not None:
                    self.update(self._clip_rect(other).adjusted(0, -2, 0, 2).toRect())
        if self.tool == "razor":
            self.setCursor(self._razor_cursor())
            super().mouseMoveEvent(event)
            return
        if clip is not None:
            edge = hover_edge if clip is hover_clip else self._fade_edge_at(clip, x, y)
            if edge is not None:
                frames = clip.fade_in if edge == "in" else clip.fade_out
                curve = clip.fade_in_curve if edge == "in" else clip.fade_out_curve
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                self.setToolTip(f"Fade {'in' if edge == 'in' else 'out'}: {frames} f "
                                f"({frames / max(1, self.timeline.fps):.2f} s) - {fade_curve_name(curve)}. "
                                "Drag sideways for length, up/down to bend, right-click for presets, "
                                "double-click to clear")
                super().mouseMoveEvent(event)
                return
        env_clip, env_channel, env_kind, env_frame = self._envelope_hit(x, y)
        if env_clip is not None:
            self.setCursor(Qt.CursorShape.SizeVerCursor if env_kind == "line"
                           else Qt.CursorShape.SizeAllCursor)
            value = CH.value_at(env_clip, env_channel, env_frame)
            label = ANIM.envelope_text(0.0 if value is None else value, env_channel)
            self.setToolTip(f"{'Gain' if env_channel == CH.gain_channel() else 'Clip Opacity'}: {label}. "
                            "Drag to change, double-click to add a keyframe, right-click for more")
            super().mouseMoveEvent(event)
            return
        if clip and (abs(x - self.x_at(clip.end)) < self.EDGE_PX or abs(x - self.x_at(clip.timeline_start)) < self.EDGE_PX):
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif clip:
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.unsetCursor()
        super().mouseMoveEvent(event)

    def _update_header_hover(self, x, y):
        track = self._header_track_at(y)
        if track is None:
            if self._header_hover:
                self._header_hover = None
                self.update()
            self.unsetCursor()
            return
        top = self.header_y(track)
        height = self.track_height(track)
        action, _rect = header_hit(track, self.HEADER_W, height, x, y, origin_y=top)
        if abs(y - (top + height)) <= 3:
            action = "height"
        hover = (track.id, action)
        if hover != self._header_hover:
            self._header_hover = hover
            # setToolTip lets Qt show the hint itself; no popup is created while moving.
            self.setToolTip(TOOLTIPS.get(action, "") if action else "")
            self.update()
        if action == "height":
            self.setCursor(Qt.CursorShape.SizeVerCursor)
        elif action == "volume":
            self.setCursor(Qt.CursorShape.SizeHorCursor)
        elif action in ("visible", "mute", "solo", "lock", "name"):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        else:
            self.unsetCursor()


    def mouseReleaseEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._kf_drag:
            self._keyframe_release()
            return
        if self._env_drag:
            self._envelope_release()
            return
        if self._kf_band:
            self._select_keyframes_in_band()
            return
        if self._fade_drag:
            if self._preset_drag is not None:
                self._preset_release()
                return
            self._fade_release()
            return
        if self._marker_drag:
            self._marker_release()
            return
        if self._marker_marquee:
            self._marker_marquee = None
            count = len(self.session.marker_selected)
            self.session.status.emit(f"{count} marker(s) selected" if count else "No marker selected")
            self.update(0, self.ruler_top, self.width(), self.RULER_H)
            return
        if self._loop_drag:
            self._loop_release(int(event.position().x()))
            return
        if self._height_drag or self._volume_drag:
            self._finish_header_drags()
            self.update()
            return
        if self._drag and self._drag.get("kind") == "scrub":
            self._scrub_timer.stop()
            self._drag = None
            # A final move guarantees the preview matches the released position.
            self._scrub(int(event.position().x()), event.modifiers())
            self._release_snap()
            self.scrub_finished.emit()
            self.update()
            return
        if self._drag:
            self._commit_drag()
        if self._band:
            ids = self._band_clip_ids()
            base = set(self._band_base)
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                base |= set(self.session.selected)
            self.session.set_selection(base | ids)
            if not ids and not base:
                self.session.status.emit("Selection cleared")
            self._band = None
            self._band_ids = set()
            self._band_base = set()
            self.update()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        x, y = int(event.position().x()), int(event.position().y())
        if event.button() == Qt.MouseButton.LeftButton and x >= self.HEADER_W and y >= self.TRACKS_TOP:
            transition = self._transition_at(x, y)
            if transition is not None:
                self._edit_transition(transition)
                return
            kf_clip, kf_row, kf_time = self._keyframe_hit(x, y)
            if kf_clip is not None:
                entries = [(kf_clip.id, kf_row["channel"], int(kf_time))]
                if bool(self.session.double_click_opens_graph.get()):
                    # Optional: take the curve to the Graph Editor (View > Preferences).
                    self.open_keyframes_in_graph(entries)
                else:
                    self.session.delete_keyframes(entries)
                    self.session.clear_key_selection()
                self.update()
                return
            env_clip, env_channel, env_kind, env_frame = self._envelope_hit(x, y)
            if env_clip is not None:
                entry = [(env_clip.id, env_channel, int(env_frame))]
                if env_kind == "key" and bool(self.session.double_click_opens_graph.get()):
                    self.open_keyframes_in_graph(entry)
                elif env_kind == "key":
                    self.session.delete_keyframes(entry)
                    self.session.clear_key_selection()
                else:
                    # Double-click the line: key it here (that is how a fade is started).
                    self.session.add_envelope_key(env_clip.id, env_channel, env_frame)
                self.update()
                return
            lane_clip, lane_row, lane_frame = self._keyframe_row_hit(x, y)
            if lane_clip is not None:
                # Double-click a property lane: add a keyframe at that frame.
                self.session.add_keyframe(lane_clip.id, lane_row["channel"], lane_frame)
                self.update()
                return
            clip, edge = self._fade_hit(x, y)
            if clip is not None:
                # Double-clicking a grip clears that fade, like Vegas.
                self.session.reset_clip_fades(clip.id, edge)
                return
            _track, nested = self._clip_at(x, y)
            if nested is not None and getattr(nested, "is_composition", False):
                # Double-clicking the body of a nested clip opens it for editing.
                self.composition_open_requested.emit(nested.id)
                return
        if event.button() == Qt.MouseButton.LeftButton and x < self.HEADER_W and y >= self.TRACKS_TOP:
            track = self._header_track_at(y)
            if track is not None:
                # Double-clicking the border resets that lane to the shared height.
                border = self.header_y(track) + self.track_height(track)
                if abs(y - border) <= 4:
                    self._height_drag = None
                    self.track_heights.pop(track.id, None)
                    self.track_height_changed.emit(self.track_height_value)
                    self.session.status.emit(f"{track.name} height reset")
                    self.update()
                    return
                self._rename_track(track)
                return
        if event.button() == Qt.MouseButton.LeftButton and x >= self.HEADER_W and \
                self.ruler_top <= y < self.TRACKS_TOP:
            marker = self._marker_at_x(x)
            if marker is not None:
                self._rename_marker(marker)
                return
        if event.button() == Qt.MouseButton.LeftButton and self.loop_strip_top <= y < self.ruler_top:
            # Vegas habit: double-clicking the loop strip clears the region.
            if self.timeline.loop_range() is not None:
                self.session.clear_loop_region()
                self.session.status.emit("Loop region cleared")
            return
        if event.button() == Qt.MouseButton.LeftButton and self.TOP_H <= y < self.TRACKS_TOP:
            self.fit_to_window()
        super().mouseDoubleClickEvent(event)

    def leaveEvent(self, event):
        self._header_hover = None
        self._hover_clip = None
        self.unsetCursor()
        self.update()
        super().leaveEvent(event)

    # ------------------------------------------------------------ tool / state
    @staticmethod
    def _razor_cursor():
        """Small blade cursor so the razor tool is obvious before the first cut."""
        cursor = getattr(TimelineCanvas, "_razor_cursor_cache", None)
        if cursor is None:
            pix = QPixmap(20, 24)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setBrush(QColor("#eaf2ff"))
            painter.setPen(QPen(QColor("#0b0d10"), 1))
            painter.drawPolygon(QPolygonF([QPointF(10, 1), QPointF(16, 11), QPointF(10, 15), QPointF(4, 11)]))
            painter.setPen(QPen(QColor("#eaf2ff"), 2))
            painter.drawLine(QPointF(10, 15), QPointF(10, 22))
            painter.end()
            cursor = QCursor(pix, 10, 1)
            TimelineCanvas._razor_cursor_cache = cursor
        return cursor

    def _on_tool_changed(self):
        self.tool_changed.emit(self.tool)
        if self.tool == "razor" and self.underMouse():
            self.setCursor(self._razor_cursor())
        elif self.tool != "razor" and self.underMouse() and not self._drag:
            self.unsetCursor()
        self.update()

    def _on_snap_changed(self):
        self.snap_changed.emit(self.snap_enabled)
        self.update()

    def _on_loop_changed(self):
        self.loop_changed.emit()
        self.update()

    @staticmethod
    def _wheel_axes(event):
        """Wheel movement as (horizontal, vertical) in native units.

        Windows/Qt can swap the wheel axes while Alt is held: the rotation then arrives
        in angleDelta().x() and .y() is zero. Take whichever axis actually moved, using
        pixelDelta (touchpads) when it carries the movement and angleDelta otherwise.
        """
        pixel, angle = event.pixelDelta(), event.angleDelta()
        source = pixel if (pixel.x() or pixel.y()) else angle
        dx, dy = source.x(), source.y()
        if abs(dx) > abs(dy):
            return float(dx), 0.0
        return 0.0, float(dy)

    def wheelEvent(self, event):
        """The only wheel handler: Alt resizes every track, Ctrl zooms, Shift scrolls X."""
        horizontal, vertical = self._wheel_axes(event)
        delta = horizontal + vertical
        smooth = bool(event.pixelDelta().x() or event.pixelDelta().y())
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.AltModifier:
            self._wheel_track_height(delta, event.position().y(), smooth)
        elif modifiers & Qt.KeyboardModifier.ControlModifier:
            self._zoom_at(event.position().x(), delta, smooth)
        elif modifiers & Qt.KeyboardModifier.ShiftModifier or horizontal:
            step = delta / 6.0 if smooth else delta / 120.0 * 90.0
            self.scroll_x = max(0.0, min(self._max_scroll_x(), self.scroll_x - step))
        else:
            end = self.TRACKS_TOP + sum(self.track_height(t) for t in self.timeline.tracks)
            step = delta / 6.0 if smooth else delta / 120.0 * 48.0
            self.scroll_y = max(0.0, min(max(0.0, end - self.height() + 20), self.scroll_y - step))
        self.update()
        event.accept()

    def _wheel_track_height(self, delta, pointer_y, smooth):
        """Grow/shrink the shared track height, anchored on the lane under the pointer."""
        if not delta:
            return
        step = (delta / 6.0) if smooth else (delta / 120.0 * self.TRACK_H_STEP)
        self._height_residual += step
        whole = int(self._height_residual)
        if not whole:
            return
        self._height_residual -= whole
        self.set_track_height(self.track_height_value + whole, anchor_y=pointer_y)

    def _zoom_at(self, x, delta, smooth=False):
        if not delta:
            return
        # The frame under the pointer must be read at the old scale: reading it after
        # changing ppf gives the frame at the *new* scale and makes the view drift.
        pointer_frame = self.frame_at(x)
        factor = 1.15 ** (delta / 6.0) if smooth else 1.15 ** (delta / 120.0)
        self.ppf = max(self.MIN_PPF, min(self.MAX_PPF, self.ppf * factor))
        self.scroll_x = max(0.0, min(self._max_scroll_x(), pointer_frame * self.ppf - (x - self.HEADER_W)))
        self.zoom_changed.emit(float(self.ppf))

    def event(self, event):
        """Claim Delete for the selected track header before the window shortcut eats it."""
        if (event.type() == QEvent.Type.ShortcutOverride and self.hasFocus()
                and (self._header_selected or self.session.marker_selected
                     or self.session.key_selection)
                and event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace)):
            event.accept()
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key.Key_Escape and self._kf_drag:
            self._cancel_keyframe_drag()
            return
        if key == Qt.Key.Key_Escape and self._env_drag:
            self._cancel_envelope_drag()
            return
        if key == Qt.Key.Key_Escape and self._kf_band:
            self._kf_band = None
            self.update()
            return
        if key == Qt.Key.Key_Escape and self._fade_drag:
            if self._preset_drag is not None:
                drag, self._preset_drag = self._preset_drag, None
                self.unsetCursor()
                self.timeline.restore(drag["snapshot"])
                self.session.changed.emit()
                return
            self._cancel_fade_drag()
            return
        if key == Qt.Key.Key_Escape and self._marker_drag:
            self._cancel_marker_drag()
            return
        if key == Qt.Key.Key_Escape and self._marker_marquee:
            self._marker_marquee = None
            self.session.clear_marker_selection()
            self.update(0, self.ruler_top, self.width(), self.RULER_H)
            return
        if key == Qt.Key.Key_Escape and self._drag:
            self._cancel_drag()
            return
        if key == Qt.Key.Key_Escape and self._band:
            self._band = None
            self._band_ids = set()
            self._band_base = set()
            self.update()
            return
        if key == Qt.Key.Key_C:
            self.session.split_at_playhead()
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if self.session.key_selection:
                self._delete_selected_keyframes()
            elif self._header_selected is not None:
                track = self.session.find_track(self._header_selected)
                if track is not None:
                    self._delete_track(track)
                else:
                    self._header_selected = None
                    self.update()
            else:
                # delete_selected() removes selected markers first, then clips.
                self.session.delete_selected(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
        elif key == Qt.Key.Key_Escape:
            self.tool = "select"
            self.session.set_selection(set())
            self.session.clear_marker_selection()
            self._header_selected = None
            self.update()
        else:
            super().keyPressEvent(event)

    # -------------------------------------------------------------------- menus
    def _timeline_menu(self, point, clip=None):
        """Empty-timeline/track-lane menu; `clip` is only set when the press hit one."""
        menu = QMenu(self)
        menu.addAction("Add Video Track", lambda: self.session.add_track("video"))
        menu.addAction("Add Audio Track", lambda: self.session.add_track("audio"))
        menu.addSeparator()
        menu.addAction("Split at Playhead", self.session.split_at_playhead)
        if clip is not None and clip.is_text:
            split_menu = menu.addMenu("Split Text")
            for mode, label in (("letter", "Into Letters"), ("word", "Into Words"),
                                ("line", "Into Lines")):
                action = split_menu.addAction(label)
                action.triggered.connect(
                    lambda _checked=False, c=clip, m=mode: self.split_text(c, m))
        menu.addAction("Add Freeze Frame", self.session.add_freeze_at_playhead)
        menu.addSeparator()
        keys = menu.addMenu("Keyframes")
        keys.addAction("Add at Playhead (Ctrl+K)", self.session.add_keyframes_at_playhead)
        keys.addAction("Copy Selected (Ctrl+Shift+C)", self.copy_selected_keyframes)
        paste = keys.addAction("Paste at Playhead (Ctrl+Shift+V)", self.paste_keyframes)
        paste.setEnabled(self.session.has_key_clipboard())
        keys.addAction("Duplicate Selected (Ctrl+Shift+D)", self.duplicate_selected_keyframes)
        keys.addAction("Delete Selected", self._delete_selected_keyframes)
        keys.addSeparator()
        keys.addAction("Open Keyframe Editor",
                       lambda: self.keyframe_editor_requested.emit([]))
        open_graph = keys.addAction("Open Selection in Graph Editor",
                                    lambda: self.open_keyframes_in_graph(
                                        self.session.selected_key_entries()))
        open_graph.setEnabled(bool(self.session.key_selection))
        keys.addSeparator()
        for label, flag in (("Show Keyframes on Clips", self.session.show_keyframes),
                            ("Opacity Envelope", self.session.show_opacity_envelope),
                            ("Gain Envelope", self.session.show_gain_envelope)):
            action = keys.addAction(label)
            action.setCheckable(True)
            action.setChecked(bool(flag.get()))
            action.triggered.connect(lambda on, f=flag: f.set(bool(on)))
        menu.exec(point)

    def _clip_menu(self, clip, point):
        menu = QMenu(self)
        menu.addAction("Split at Playhead", self.session.split_at_playhead)
        menu.addAction("Freeze frame", lambda: self.session.freeze_selected_clip(clip.id))
        menu.addAction("Duplicate", self.session.duplicate_selected)
        precompose = menu.addAction("Pre-compose...", self._precompose_selected)
        precompose.setEnabled(not self.session.precompose_problem())
        menu.addSeparator()
        if self.animation_rows(clip):
            menu.addAction("Open Keyframe Editor",
                           lambda _checked=False, c=clip: self._toggle_keyframe_rows(c))
            menu.addAction("Open in Graph Editor",
                           lambda _checked=False, c=clip: self.open_keyframes_in_graph(
                               [(c.id, row["channel"], key.time) for row in self.animation_rows(c)
                                for key in row["value"].keyframes]))
            menu.addSeparator()
        selection = sorted(self.session.selected) or [clip.id]
        link = menu.addAction("Link Selected Clips",
                              lambda _checked=False: self.session.link_selected())
        link.setEnabled(self.session.can_link(selection))
        unlink = menu.addAction("Unlink Selected Clips",
                                lambda _checked=False: self.session.unlink_selected())
        unlink.setEnabled(self.session.can_unlink(selection))
        menu.addSeparator()
        if self._clip_has_audio(clip):
            menu.addAction("Remove Dead Silence...",
                           lambda _checked=False, c=clip: self._remove_dead_silence(c))
            menu.addSeparator()
        menu.addAction("Copy", self.session.copy_selected)
        menu.addAction("Cut", self.session.cut_selected)
        menu.addAction("Delete", self.session.delete_selected)
        menu.addAction("Ripple Delete", lambda: self.session.delete_selected(True))
        menu.exec(point)

    def _precompose_selected(self):
        problem = self.session.precompose_problem()
        if problem:
            self.session.status.emit(problem)
            return
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Pre-compose", "Composition name:",
                                        text="Composition")
        if ok:
            self.session.precompose(name)

    def _clip_has_audio(self, clip):
        """True when this clip, or a clip linked to it, can supply the analysed audio."""
        if clip.carries_audio:
            return True
        if not clip.link_id:
            return False
        return any(member.carries_audio for member in self.timeline.link_group(clip.link_id))

    def _remove_dead_silence(self, clip):
        opener = getattr(self.window(), "open_silence_dialog", None)
        if opener is not None:
            opener(clip.id)

    def split_text(self, clip, mode="letter"):
        """Split a text clip into one clip per unit, keeping every piece where it was.

        The layout lives in the renderer (font metrics), so it produces the offsets and
        the session performs the model edit as one undo step.
        """
        from ..model import text as TXT
        from ..processing import text_units

        width, height = self.engine.project_size()
        spec = TXT.resolve(clip, 0)
        units = text_units.layout_units(spec, width, height, 1.0).get(mode) or []
        if len(units) < 2:
            self.session.status.emit("This text has nothing to split at that unit")
            return []
        centre_x, centre_y = width / 2.0, height / 2.0
        pieces = [(str(unit[2]), float(unit[0]) - centre_x, float(unit[1]) - centre_y)
                  for unit in units]
        return self.session.split_text_clip(clip.id, pieces, mode)

    def _track_menu(self, track, point):
        self._header_selected = track.id
        self.session.set_selection(set())
        self.update()
        menu = QMenu(self)
        menu.addAction("Rename…", lambda: self._rename_track(track))
        # Insert a track of either type directly above or below this header.
        insert = menu.addMenu("Add Track")
        insert.addAction("Video Track Above", lambda: self.session.add_track_near(track, "video", True))
        insert.addAction("Video Track Below", lambda: self.session.add_track_near(track, "video", False))
        insert.addAction("Audio Track Above", lambda: self.session.add_track_near(track, "audio", True))
        insert.addAction("Audio Track Below", lambda: self.session.add_track_near(track, "audio", False))
        height_menu = menu.addMenu("Track Height (all tracks)")
        for label, value in (("Compact (32)", 32), ("Small (48)", 48), ("Normal (64)", 64),
                             ("Large (96)", 96), ("Huge (128)", 128)):
            height_menu.addAction(label, lambda _=False, v=value: self.set_track_height(v))
        height_menu.addSeparator()
        height_menu.addAction("Reset Track Height", self.reset_track_heights)
        menu.addSeparator()
        if track.type == "video":
            toggle = menu.addAction("Hidden", lambda: self._toggle_header_action(track, "visible"))
            toggle.setCheckable(True)
            toggle.setChecked(not track.visible)
        else:
            mute = menu.addAction("Muted", lambda: self._toggle_header_action(track, "mute"))
            mute.setCheckable(True)
            mute.setChecked(track.muted)
            solo = menu.addAction("Solo", lambda: self._toggle_header_action(track, "solo"))
            solo.setCheckable(True)
            solo.setChecked(track.solo)
        lock = menu.addAction("Locked", lambda: self._toggle_header_action(track, "lock"))
        lock.setCheckable(True)
        lock.setChecked(track.locked)
        menu.addSeparator()
        menu.addAction("Delete Track", lambda: self._delete_track(track))
        menu.exec(point)

    def _set_track_height(self, track, value):
        """Single-lane height (kept for header-border drags and per-lane overrides)."""
        value = max(self.MIN_TRACK_H, min(self.MAX_TRACK_H, int(value)))
        if value == self.track_height_value:
            self.track_heights.pop(track.id, None)
        else:
            self.track_heights[track.id] = value
        self.track_height_changed.emit(self.track_height_value)
        self.update()

    def _rename_track(self, track):
        name, ok = QInputDialog.getText(self, "Rename track", "Track name:", text=track.name)
        if ok:
            self.session.rename_track(track.id, name)

    def _delete_track(self, track):
        """Remove a track (any track, including the last of its type).

        Only a track that holds clips asks for confirmation; the whole removal - track
        order included - is one undo entry through the session stack.
        """
        count = len(track.clips)
        if count:
            answer = QMessageBox.question(self, "Delete track",
                                          f'Delete "{track.name}" and its {count} clip(s)?',
                                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                          QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return False
        name, track_type = track.name, track.type
        if self.session.remove_track(track.id):
            self.track_heights.pop(track.id, None)
            if self._header_selected == track.id:
                self._header_selected = None
            if self._header_hover and self._header_hover[0] == track.id:
                self._header_hover = None
            if self.timeline.tracks_of_type(track_type):
                note = f"Deleted {name} (Ctrl+Z to undo)"
            else:
                note = f"Deleted {name} - no {track_type} tracks left (Ctrl+Z to undo)"
            self.session.status.emit(note)
            self.update()
            return True
        return False

    # -------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(MEDIA_MIME):
            # A drag from the Project panel carries media ids; it never re-imports the file.
            self._drop_highlight = True
            self._update_media_drop(event)
            event.acceptProposedAction()
            return
        if mime.hasFormat(EFFECT_MIME) or mime.hasFormat(PRESET_MIME) or mime.hasUrls():
            self._drop_highlight = True
            self.update()
            event.acceptProposedAction()
            return
        if mime.hasFormat(COMPOSITION_MIME):
            self._drop_highlight = True
            self.update()
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(MEDIA_MIME):
            self._update_media_drop(event)
            event.acceptProposedAction()
            return
        if mime.hasFormat(EFFECT_MIME) or mime.hasFormat(PRESET_MIME):
            self._fx_drop_highlight = self._clip_at(event.position().x(), event.position().y())[1]
            self.update()
            event.acceptProposedAction()
            return
        if mime.hasUrls() or mime.hasFormat(COMPOSITION_MIME):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_highlight = False
        self._fx_drop_highlight = None
        self.clear_media_drop()
        self.update()
        event.accept()

    def dropEvent(self, event):
        mime = event.mimeData()
        frame, track = self.drop_target_at(int(event.position().x()), int(event.position().y()))
        media_ids = self._media_drop_ids(mime)
        if media_ids:
            self.clear_media_drop()
            self._drop_highlight = False
            self.update()
            if track is not None and track.locked:
                self.session.status.emit(f"{track.name} is locked - nothing was inserted")
                event.ignore()
                return
            self.media_dropped.emit(media_ids, frame, track)
            event.acceptProposedAction()
            return
        if mime.hasFormat(COMPOSITION_MIME):
            composition_ids = [line.strip() for line in
                               bytes(mime.data(COMPOSITION_MIME)).decode("utf-8").splitlines()
                               if line.strip()]
            self._drop_highlight = False
            self.update()
            if composition_ids:
                if track is not None and track.locked:
                    self.session.status.emit(f"{track.name} is locked - nothing was inserted")
                    event.ignore()
                    return
                self.composition_dropped.emit(composition_ids, frame, track)
                event.acceptProposedAction()
            else:
                event.ignore()
            return
        if mime.hasFormat(PRESET_MIME):
            payload = bytes(mime.data(PRESET_MIME)).decode("utf-8")
            preset_id, _sep, unit = payload.partition("|")
            _track, clip = self._clip_at(event.position().x(), event.position().y())
            if clip is not None:
                from ..model import preset_library as LIB
                preset = LIB.load().get(preset_id)
                if preset is not None:
                    self.session.apply_preset(preset, unit or "whole", ids=[clip.id])
        elif mime.hasFormat(EFFECT_MIME):
            key = bytes(mime.data(EFFECT_MIME)).decode("utf-8")
            _track, clip = self._clip_at(event.position().x(), event.position().y())
            if clip is not None:
                self._add_effect_to_clip(clip, key)
        else:
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            self.files_dropped.emit(paths, frame, track)
        self._drop_highlight = False
        self._fx_drop_highlight = None
        event.acceptProposedAction()
        self.update()

    def _add_effect_to_clip(self, clip, key):
        if str(key).startswith("look:"):
            self.session.apply_look(str(key).split(":", 1)[1], ids=[clip.id])
            self.session.set_selection({clip.id})
            return
        effect = next((item for item in self.engine.effects if item.key == key), None)
        if effect is None:
            return
        if not self.engine.clip_accepts_effect(clip, key):
            kind = self.engine.track_type_of(clip)
            self.session.status.emit(
                f"{effect.name} is a {effect.media_label()}-only effect - it cannot be added "
                f"to {'an' if kind == 'audio' else 'a'} {kind} clip")
            return

        def add(target):
            if not any(entry["effect_id"] == key for entry in target.effects):
                import copy
                target.effects.append({"effect_id": key, "enabled": True,
                                       "params": copy.deepcopy(effect.defaults)})

        self.session.apply_to_clips([clip.id], f"Add {effect.name}", add)
        self.session.set_selection({clip.id})


class TimelinePanel(QWidget):
    """Timeline canvas plus its toolbar; tool toggles mirror the session, never their own state."""

    def __init__(self, engine, waveforms=None, parent=None):
        super().__init__(parent)
        self.setObjectName("timelinePanel")
        self.canvas = TimelineCanvas(engine, waveforms, self)
        # Edit actions drive the session directly, so menus and shortcuts stay in sync.
        self.split = self._tool_button("Split", "Split every clip under the playhead", shortcut="C", icon_name="split")
        self.split.clicked.connect(self.canvas.session.split_at_playhead)
        self.add_track = QToolButton()
        self.add_track.setProperty("role", "tool")
        self.add_track.setIcon(icon("plus"))
        self.add_track.setText("Track")
        self.add_track.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.add_track.setToolTip("Add track: video or audio")
        self.add_track.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.add_track.setCursor(Qt.CursorShape.PointingHandCursor)
        track_menu = QMenu(self.add_track)
        self.add_video = track_menu.addAction("Video track")
        self.add_video.setToolTip("Add a video track (the newest sits on top)")
        self.add_video.triggered.connect(lambda: self.canvas.session.add_track("video"))
        self.add_audio = track_menu.addAction("Audio track")
        self.add_audio.setToolTip("Add an audio track")
        self.add_audio.triggered.connect(lambda: self.canvas.session.add_track("audio"))
        self.add_track.setMenu(track_menu)
        self.snap = self._tool_button("Snap", "Magnetise edits to clip edges, markers, the playhead "
                                      "and the loop edges", checkable=True, shortcut="F8", icon_name="snap")
        self.snap.toggled.connect(self._set_snap)
        # Razor and Select are one exclusive tool group; Snap and Loop are independent.
        self.select = self._tool_button("Select", "Selection tool: move, trim and stretch clips",
                                        checkable=True, shortcut="V", icon_name="select")
        self.razor = self._tool_button("Razor", "Razor tool: click a clip to cut it", checkable=True,
                                       shortcut="S", icon_name="razor")
        self.tool_group = QButtonGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_group.addButton(self.select, 0)
        self.tool_group.addButton(self.razor, 1)
        self.select.setChecked(self.canvas.tool != "razor")
        self.razor.setChecked(self.canvas.tool == "razor")
        self.razor.toggled.connect(lambda on: self._set_tool("razor") if on else None)
        self.select.toggled.connect(lambda on: self._set_tool("select") if on else None)
        self.loop = self._tool_button("Loop", "Loop playback over the loop region", checkable=True,
                                      shortcut="Q", icon_name="loop")
        self.loop.toggled.connect(self._set_loop)
        # Linked Selection lives in Edit as a checkable command now; the timeline no longer
        # carries a second chain button next to the explicit Link/Unlink commands.
        # Simple keyframe workflow: one toggle for the diamonds, one button that keys the
        # animated properties at the playhead. Nothing here is a second animation model.
        self.keyframes = self._tool_button("Keyframes", "Show or hide keyframes on clips",
                                           checkable=True, icon_name="key")
        self.keyframes.setChecked(bool(self.canvas.session.show_keyframes.get()))
        self.keyframes.toggled.connect(lambda on: self.canvas.session.show_keyframes.set(bool(on)))
        self.canvas.session.show_keyframes.trace_add("write", lambda *_: self._sync_keyframes_button())
        self.add_key = self._tool_button("Key", "Add a keyframe at the playhead on the selected "
                                        "clips (Ctrl+K)", shortcut="Ctrl+K", icon_name="key_filled")
        self.add_key.clicked.connect(lambda _=False: self.canvas.session.add_keyframes_at_playhead())
        self.overlap = QComboBox()
        self.overlap.setToolTip("What happens when a clip is dropped on top of another: cut a "
                               "crossfade in the overlap, block the move, or push neighbours along")
        self.overlap.addItems(["Crossfade overlap", "Block overlaps", "Push neighbours"])
        current = str(self.canvas.session.overlap_label.get())
        self.overlap.setCurrentText(current if self.overlap.findText(current) >= 0
                                    else "Crossfade overlap")
        def _set_overlap(text):
            self.canvas.overlap_mode = text
            self.canvas.session.overlap_label.set(text)
        self.overlap.currentTextChanged.connect(_set_overlap)
        self.zoom = ZoomSlider(self.canvas.ppf, self.canvas.MIN_PPF, self.canvas.MAX_PPF)
        self.zoom.setFixedWidth(140)
        self.zoom.edited.connect(self._set_zoom)
        self.canvas.zoom_changed.connect(self.zoom.set_value)
        self.fit = self._tool_button("Fit", "Fit the whole timeline into the view (double-click the ruler "
                                     "does the same)", icon_name="fit")
        self.fit.clicked.connect(self.canvas.fit_to_window)
        # Track height lives in the settings popover; Alt + wheel drives the same single value.
        self.height = ScrubField("Track height", self.canvas.track_height_value, self.canvas.MIN_TRACK_H,
                                  self.canvas.MAX_TRACK_H, default=self.canvas.DEFAULT_TRACK_H,
                                  step=1, unit=" px", label_width=84,
                                  tooltip="Height of every track (Alt + wheel over the timeline)")
        self.height.edited.connect(lambda value: self.canvas.set_track_height(int(value)))
        self.settings_button = QToolButton()
        self.settings_button.setProperty("role", "icon")
        self.settings_button.setIcon(icon("gear"))
        self.settings_button.setToolTip("Timeline settings")
        self.settings_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.settings_button.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        settings_menu = QMenu(self.settings_button)
        popover = QWidget()
        form = QVBoxLayout(popover)
        form.setContentsMargins(tokens.SP3, tokens.SP2, tokens.SP3, tokens.SP2)
        form.setSpacing(tokens.SP2)
        title = QLabel("Timeline settings")
        title.setStyleSheet(f"font-weight: bold; font-size: {tokens.FONT_TITLE}px;")
        form.addWidget(title)
        form.addWidget(self.overlap)
        form.addWidget(self.height)
        form.addWidget(self._separator_line())
        self.show_keys_box = QCheckBox("Show keyframes on clips")
        self.show_keys_box.setToolTip("Diamonds on the clips, the expand arrow and the keyframe lanes")
        self.opacity_box = QCheckBox("Opacity envelope on clips")
        self.opacity_box.setToolTip("The Clip Opacity line drawn across every visual clip")
        self.gain_box = QCheckBox("Gain envelope on audio clips")
        self.gain_box.setToolTip("The audio gain line drawn across every audio clip (dB)")
        session = self.canvas.session
        self.show_keys_box.setChecked(bool(session.show_keyframes.get()))
        self.opacity_box.setChecked(bool(session.show_opacity_envelope.get()))
        self.gain_box.setChecked(bool(session.show_gain_envelope.get()))
        self.show_keys_box.toggled.connect(lambda on: session.show_keyframes.set(bool(on)))
        self.opacity_box.toggled.connect(lambda on: session.show_opacity_envelope.set(bool(on)))
        self.gain_box.toggled.connect(lambda on: session.show_gain_envelope.set(bool(on)))
        for box, flag in ((self.show_keys_box, session.show_keyframes),
                          (self.opacity_box, session.show_opacity_envelope),
                          (self.gain_box, session.show_gain_envelope)):
            flag.trace_add("write", lambda *_, b=box, f=flag: self._sync_box(b, f))
        for box in (self.show_keys_box, self.opacity_box, self.gain_box):
            form.addWidget(box)
        popover.setMinimumWidth(260)
        widget_action = QWidgetAction(settings_menu)
        widget_action.setDefaultWidget(popover)
        settings_menu.addAction(widget_action)
        self.settings_button.setMenu(settings_menu)
        self.canvas.track_height_changed.connect(self._on_track_height)
        self._had_clips = False
        self._zoom_restored = False
        # Buttons follow the model (shortcuts, menus and the context menu all change it).
        self.canvas.tool_changed.connect(self._sync_tool_buttons)
        self.canvas.snap_changed.connect(self._sync_snap_button)
        self.canvas.loop_changed.connect(self._sync_loop_button)
        self._sync_tool_buttons(self.canvas.tool)
        self._sync_snap_button(self.canvas.snap_enabled)
        self._sync_loop_button()
        self.canvas.session.changed.connect(self._maybe_autofit)
        row = QHBoxLayout()
        row.setContentsMargins(tokens.SP2, tokens.SP1, tokens.SP2, tokens.SP1)
        row.setSpacing(tokens.SP1)
        for cluster in ((self.select, self.razor), (self.split,), (self.keyframes, self.add_key),
                        (self.snap, self.loop), (self.add_track,)):
            for widget in cluster:
                row.addWidget(widget)
            row.addSpacing(tokens.SP1)
            row.addWidget(self._separator())
            row.addSpacing(tokens.SP1)
        row.addStretch(1)
        for widget in (self.zoom, self.fit, self._separator(), self.settings_button):
            row.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(row)
        layout.addWidget(self.canvas, 1)

    def _maybe_autofit(self):
        """Fit the zoom to the content when the first clip arrives (import or project load)."""
        has = bool(self.canvas.timeline.all_clips())
        first = has and not self._had_clips
        self._had_clips = has
        if first:
            self._zoom_restored = False
            QTimer.singleShot(0, self._autofit_now)

    def _autofit_now(self):
        # A zoom saved with the project wins over the automatic fit.
        if not self._zoom_restored:
            self.canvas.fit_to_window()

    @staticmethod
    def _tool_button(label, tooltip, checkable=False, shortcut=None, icon_name=None):
        """Icon button that turns accent-coloured while it is on; the tooltip names the shortcut."""
        button = QToolButton()
        button.setAccessibleName(label)
        if icon_name:
            button.setIcon(icon(icon_name))
            button.setIconSize(QSize(tokens.ICON, tokens.ICON))
            button.setFixedSize(tokens.CONTROL_H, tokens.CONTROL_H)
        else:
            button.setText(label)
        button.setProperty("tip", tooltip)
        # "shortcut" is a real QAbstractButton property; keep the label separate.
        button.setProperty("hotkey", shortcut or "")
        button.setCheckable(checkable)
        button.setProperty("role", "tool")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        TimelinePanel._tip(button, label if checkable else "")
        return button

    @staticmethod
    def _separator():
        """Thin vertical rule between toolbar clusters."""
        line = QFrame()
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedWidth(1)
        line.setFixedHeight(tokens.CONTROL_H - 8)
        line.setStyleSheet(f"color: {tokens.COLORS['border']}; background: {tokens.COLORS['border']};")
        return line


    def _set_tool(self, tool):
        self.canvas.tool = tool

    def _set_snap(self, on):
        self.canvas.snap_enabled = on
        self.canvas.snap_changed.emit(self.canvas.snap_enabled)

    def _set_loop(self, on):
        """Loop toggle from the toolbar; the session keeps it and the menu/buttons in sync."""
        self.canvas.session.set_loop_enabled(on)
        if on and self.canvas.timeline.loop_range() is None:
            self.canvas.session.status.emit(
                "Loop playback on - drag in the loop strip to set a region, or double-click it to reset")

    def set_loop_enabled(self, on):
        self.loop.setChecked(bool(on))

    def _sync_tool_buttons(self, tool):
        self.razor.setChecked(tool == "razor")
        self.select.setChecked(tool != "razor")
        self._restyle(self.select, self.razor)
        for button, name in ((self.select, "Select"), (self.razor, "Razor")):
            self._tip(button, f"Tool: {name}" if tool == name.lower() else f"Switch to {name}")

    def _sync_snap_button(self, on):
        self.snap.setChecked(bool(on))
        self._restyle(self.snap)
        self._tip(self.snap, f"Snap: {'on' if on else 'off'}")

    def _sync_loop_button(self):
        on = bool(self.canvas.timeline.loop_enabled)
        self.loop.setChecked(on)
        self._restyle(self.loop)
        self._tip(self.loop, f"Loop: {'on' if on else 'off'}")

    def _sync_keyframes_button(self):
        on = bool(self.canvas.session.show_keyframes.get())
        if self.keyframes.isChecked() != on:
            self.keyframes.setChecked(on)
        self._restyle(self.keyframes)
        self._tip(self.keyframes, f"Keyframes: {'shown' if on else 'hidden'}")

    @staticmethod
    def _sync_box(box, flag):
        """Keep a settings checkbox on the session value without feeding a loop back."""
        value = bool(flag.get())
        if box.isChecked() != value:
            box.blockSignals(True)
            box.setChecked(value)
            box.blockSignals(False)

    @staticmethod
    def _separator_line():
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(1)
        line.setStyleSheet(f"color: {tokens.COLORS['border']}; background: {tokens.COLORS['border']};")
        return line

    @staticmethod
    def _tip(button, state):
        """Tooltip = state + shortcut + the button's own description, so hover never lies."""
        base = button.property("tip") or button.text()
        shortcut = button.property("hotkey") or ""
        head = f"{state} ({shortcut})" if state and shortcut else (state or (f"({shortcut})" if shortcut else ""))
        button.setToolTip(f"{head} - {base}".strip(" -"))

    @staticmethod
    def _restyle(*buttons):
        """Qt does not re-evaluate :checked when the property is set in code."""
        for button in buttons:
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()

    def zoom_state(self):
        """Zoom in pixels per frame plus horizontal scroll offset, for layout persistence."""
        return float(self.canvas.ppf), float(self.canvas.scroll_x)

    def restore_zoom_state(self, value, scroll_x=0.0):
        """Apply a saved zoom without emitting a second, half-built zoom change."""
        try:
            ppf = float(value)
        except (TypeError, ValueError):
            ppf = self.canvas.ppf
        self.canvas.ppf = max(self.canvas.MIN_PPF, min(self.canvas.MAX_PPF, ppf))
        self._zoom_restored = True
        self.zoom.set_value(self.canvas.ppf)
        try: self.canvas.scroll_x = max(0.0, float(scroll_x))
        except (TypeError, ValueError): self.canvas.scroll_x = 0.0
        self.canvas.update()

    def _nudge_height(self, delta):
        self.canvas.set_track_height(self.canvas.track_height_value + delta)

    def _on_track_height(self, value):
        self.height.set_value(value)

    def _set_zoom(self, value):
        """Zoom from the timeline slider: the frame under the middle of the panel stays put."""
        old_center = self.canvas.frame_at(self.canvas.width() / 2)
        self.canvas.ppf = max(self.canvas.MIN_PPF, min(self.canvas.MAX_PPF, float(value)))
        self.canvas.scroll_x = max(0, old_center * self.canvas.ppf - (self.canvas.width() - self.canvas.HEADER_W) / 2)
        self.canvas.update()

    def drop_target_at(self, x, y):
        return self.canvas.drop_target_at(x, y)

    def fit_to_window(self):
        self.canvas.fit_to_window()
