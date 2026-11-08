"""PyQt6 application window, dock composition, and thin model/UI signal bridge."""

import json
import os
import threading
import time

import cv2
import numpy as np
from PyQt6.QtCore import QObject, QPoint, QRunnable, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QActionGroup, QKeySequence
from PyQt6.QtWidgets import (QAbstractButton, QAbstractSpinBox, QDockWidget, QLineEdit, QMainWindow,
                             QMessageBox, QProgressDialog, QPlainTextEdit, QStyle, QTextEdit,
                             QToolBar)

from ..audio.demo import ensure_demo_audio
from ..audio.engine import AudioEngine
from ..audio.library import AudioLibrary
from ..audio.plan import build_segments, missing_audio_paths
from ..audio.waveform_service import WaveformService
from ..config import APP_TITLE, DEFAULT_INPUT, PROJECT_ROOT
from ..engine import EditorEngine
from ..media.probe import has_audio_stream
from ..media.reader import probe_media as probe_media_header
from ..media.reader import read_media
from ..preview import PlaybackMetrics, PlaybackSettings, PreviewCache, PreviewRenderer
from ..preview.settings import WAVEFORM_MODES
from .timeline_settings import TimelineSettings
from ..preview import gpu as render_backend
from ..preview.invalidation import changed_ranges, fingerprint, timeline_digests
from ..preview.metrics import PerfOverlay
from ..theme import configure_theme, resolve_theme
from ..updates.manager import UpdateManager
from ..version import VERSION
from . import layout as dock_layout
from .branding import app_icon
from .dialogs import AboutDialog, MarkerListDialog
from .filedialogs import open_file, open_files, save_file
from .panels import EffectsPanel
from .preview import PreviewPanel
from .project_controller import ProjectController
from .timeline_widget import TimelinePanel, timecode
from .toast import Toast
from .undo import QtCommandStack
from .update_notice import UpdateNotice


class ModelBridge(QObject):
    changed = pyqtSignal()
    playhead_changed = pyqtSignal(int)
    selection_changed = pyqtSignal(list)
    status = pyqtSignal(str)
    preview_ready = pyqtSignal(int, object, object)
    import_ready = pyqtSignal(str, object, object, float)
    import_failed = pyqtSignal(str, str)
    export_progress = pyqtSignal(int, int, object)
    export_done = pyqtSignal(object)
    export_failed = pyqtSignal(str)


class FunctionSignals(QObject):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class FunctionJob(QRunnable):
    def __init__(self, function):
        super().__init__(); self.function=function; self.signals=FunctionSignals()
    def run(self):
        try: self.signals.done.emit(self.function())
        except BaseException as exc: self.signals.failed.emit(str(exc))


class ImportSignals(QObject):
    ready = pyqtSignal(str, object, object, float)
    failed = pyqtSignal(str, str)


class ImportJob(QRunnable):
    def __init__(self, paths):
        super().__init__(); self.paths=paths; self.signals=ImportSignals()
    def run(self):
        for path in self.paths:
            try:
                kind,frames,fps=read_media(path)
                self.signals.ready.emit(path,kind,frames,float(fps))
            except Exception as exc: self.signals.failed.emit(path,str(exc))


class ExportSignals(QObject):
    progress = pyqtSignal(int, int, object)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)


class ExportJob(QRunnable):
    def __init__(self, engine, settings, cancelled):
        super().__init__(); self.engine=engine; self.settings=settings; self.cancelled=cancelled; self.signals=ExportSignals()
    def run(self):
        try:
            result=self.engine.export(self.settings,
                progress=lambda i,n,frame:self.signals.progress.emit(i,n,frame),cancelled=self.cancelled.is_set)
            self.signals.done.emit(result)
        except InterruptedError: self.signals.failed.emit("Export cancelled")
        except Exception as exc: self.signals.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self, app):
        super().__init__()
        self.qt_app=app; configure_theme(app,resolve_theme(app,dock_layout.app_settings().value("ui/theme","light")))
        self.setWindowTitle(APP_TITLE); self.resize(1420,880)
        icon=app_icon(); self.setWindowIcon(icon)
        # Message boxes and dialogs take the application icon; set it here too so any
        # launch path (app.main(), tests, embedding) gets a branded taskbar entry.
        if app.windowIcon().isNull(): app.setWindowIcon(icon)
        self.engine=EditorEngine(30)
        self.audio=AudioEngine(self)
        self.waveforms=WaveformService(AudioLibrary(self), self._waveform_cache_dir(), parent=self)
        self._scrub_mode=False; self._pending_audio={}
        # A scrub or arrow-key snippet is one short burst; throttled so fast moves cannot crackle.
        self._scrub_resume=False; self._audio_while_scrubbing=True; self._last_snippet_at=0.0; self._edit_jump=0
        self._snippet_timer=QTimer(self); self._snippet_timer.setSingleShot(True)
        self._snippet_timer.timeout.connect(self._end_snippet)
        self.qt_undo_stack=QtCommandStack(self.engine.timeline,parent=self)
        self.engine.session.stack=self.qt_undo_stack
        self.engine.session.stack.changed.connect(self.engine.session._on_stack_changed)
        self.bridge=ModelBridge(self); self.pool=QThreadPool.globalInstance()
        # Ctrl+V: clipboard pictures and files become media; in-app clip copies keep working.
        from .smart_paste import SmartPaste
        self.smart_paste=SmartPaste(self)
        self.engine.session.clip_copied.connect(self.smart_paste.note_internal_copy)
        self.projects=ProjectController(self)
        self.projects.project_changed.connect(self._on_project_changed)
        self.preview_busy=False; self.preview_generation=0; self._requested_frame=None; self._preview_dirty=False
        self.last_frame=None; self._play_clock=0.0; self._play_head=0.0; self._playing=False
        self._cancel_export=None; self._export_dialog=None; self._camera=None
        # Remembered for this session only: whole project or the loop region.
        self._export_scope=None
        self._import_context_by_path={}
        self._import_audio_by_path={}
        self._audio_probe_cache={}
        self._demo_import_paths=set()
        # INI file in the user config folder, so the layout survives crashes and is easy to inspect.
        self._settings=dock_layout.app_settings()
        # Updates: checks and downloads run off the GUI thread; the manager also holds the
        # remind/skip state, so the notice and the Updates page always agree.
        self.update_manager=UpdateManager(self)
        self._updates_dialog=None
        # ---- preview pipeline: disk+RAM cache, one render thread, real measurements ----
        # Built before the UI so the menu can read the saved options and the canvas can
        # ask the cache for its state from the very first paint.
        self.playback_settings=PlaybackSettings.load()
        # Timeline preference: automatic overlap transitions can be switched off without
        # changing the overlap mode the user picked in the timeline settings popover.
        self.timeline_settings=TimelineSettings.load()
        self.engine.session.auto_transitions.set(self.timeline_settings.auto_overlap_transitions)
        self.frame_cache=PreviewCache(self.playback_settings.ram_cache_mb,
                                      self.playback_settings.disk_cache_mb,parent=self)
        self.renderer=PreviewRenderer(self._render_preview_frame,self.frame_cache,parent=self)
        self.metrics=PlaybackMetrics()
        self.perf_overlay=PerfOverlay(self._overlay_stats)
        self._clip_digests={}
        # Bumped whenever cached content is invalidated. A render request carries the
        # revision it was started with, so a frame computed from an older state can be
        # recognised and dropped instead of being shown as the current picture.
        self._content_revision=0
        self._render_revisions={}
        self._shown_frame=-1
        self._auto_scale=None
        self._cache_dir=""
        # Composition editing context: the path of composition ids below the main
        # timeline. The undo stack stays on the root timeline, whose snapshot includes
        # every definition, so undo behaves the same at any nesting depth.
        self._composition_path=[]
        self._root_render_state=None
        self._prerender_dialog=None
        self._prerender_failed=False
        self._layout_ready=False; self._pending_default_sizes=False; self._shown=False
        self._layout_touched=False; self._split_drag=None
        self._layout_save_timer=QTimer(self); self._layout_save_timer.setSingleShot(True)
        self._layout_save_timer.setInterval(600); self._layout_save_timer.timeout.connect(self._save_layout_now)
        self._connect_model(); self._build_ui(); self._connect_audio(); self._restore_layout(); self._bind_shortcuts(); self._connect_values()
        self.play_timer=QTimer(self); self.play_timer.setTimerType(Qt.TimerType.PreciseTimer); self.play_timer.timeout.connect(self._tick_playback)
        self.camera_timer=QTimer(self); self.camera_timer.timeout.connect(self._tick_camera)
        # Coalesce panel/preview refreshes so dragging the playhead stays smooth.
        self._playhead_timer=QTimer(self); self._playhead_timer.setSingleShot(True); self._playhead_timer.setInterval(20)
        self._playhead_timer.timeout.connect(self._sync_playhead_views)
        # No status bar: the toolbar already shows tool, snap and loop state. Only the
        # few messages that matter reach the user, through a fading toast.
        self.timeline_panel.canvas.setFocus()
        self.bridge.import_ready.connect(self._on_import_ready)
        self.bridge.import_failed.connect(lambda path,error:self.toast_message(
            f"Could not import {os.path.basename(path)}: {error}",kind="error",duration_ms=4200))
        self.bridge.export_progress.connect(self._on_export_progress); self.bridge.export_done.connect(self._on_export_done)
        self.bridge.export_failed.connect(self._on_export_failed)
        # ---- preview cache wiring: the render thread feeds the cache, the cache feeds
        # the picture. The GUI thread only ever copies an already-rendered frame.
        self.renderer.frame_ready.connect(self._on_frame_rendered)
        self.renderer.frame_failed.connect(self._on_frame_failed)
        self.renderer.prerendered.connect(self._on_prerendered)
        self.renderer.prerender_done.connect(self._on_prerender_done)
        self.frame_cache.changed.connect(self._on_cache_changed)
        self._cache_timer=QTimer(self); self._cache_timer.setSingleShot(True); self._cache_timer.setInterval(120)
        self._cache_timer.timeout.connect(self._repaint_cache_bar)
        # Idle prefetch: after a pause or an edit, quietly render the frames just ahead.
        self._prefetch_timer=QTimer(self); self._prefetch_timer.setSingleShot(True); self._prefetch_timer.setInterval(700)
        self._prefetch_timer.timeout.connect(self._auto_prefetch)
        self.timeline_panel.canvas.cache_provider=self._cache_state_for_frame
        self.preview_panel.view.perf_overlay=self.perf_overlay
        self._apply_playback_settings(self.playback_settings,save=False)
        self.renderer.start()
        # Measure the GPU once, in the background: until it finishes the app stays on the
        # CPU path, and a slow OpenCL driver can never make the first preview slower.
        render_backend.benchmark_async()
        self._sync_transport()
        # Icon-only buttons get a screen-reader name from their tooltip.
        for button in self.findChildren(QAbstractButton):
            if not button.accessibleName() and not button.text() and button.toolTip():
                button.setAccessibleName(button.toolTip().split(" (")[0])

    def _connect_model(self):
        session=self.engine.session
        session.changed.connect(self.bridge.changed.emit); session.playhead_changed.connect(self.bridge.playhead_changed.emit)
        session.clip_selected.connect(self.bridge.selection_changed.emit); session.status.connect(self.bridge.status.emit)
        self.bridge.changed.connect(self._on_model_changed); self.bridge.playhead_changed.connect(self._on_playhead_changed)
        self.bridge.selection_changed.connect(lambda _ids:self._refresh_panels())

    def _build_ui(self):
        self._build_actions(); self._build_toolbar(); self._build_composition_bar()
        self.preview_panel=PreviewPanel(lambda:self.preview_scale()*100.0)
        self.effects_panel=EffectsPanel(self.engine)
        # One browser: its Effects / Animations / Styles tabs cover what used to be a
        # separate Presets panel, so there is a single animation UI.
        self.timeline_panel=TimelinePanel(self.engine,self.waveforms); self.docks={}
        from .graph_editor import GraphEditorPanel
        from .keyframe_editor import KeyframeEditorPanel
        # Two animation editors, one model: timing/values in the Keyframe Editor and
        # curves/interpolation in the Graph Editor. Both are real, independent docks.
        self.keyframe_editor=KeyframeEditorPanel(self.engine)
        self.graph_editor=GraphEditorPanel(self.engine)
        from .project_panel import ProjectPanel
        self.project_panel=ProjectPanel(self)
        # No central widget: an empty one would eat the space the Preview should fill.
        # Every panel is a real dock, so all four stay movable/floatable/tabbable.
        self.setDockNestingEnabled(True)
        self.setDockOptions(QMainWindow.DockOption.AnimatedDocks|QMainWindow.DockOption.AllowNestedDocks|
                            QMainWindow.DockOption.AllowTabbedDocks|QMainWindow.DockOption.GroupedDragging)
        # The inspector is its own dock, so "Effects" and "Properties" can be moved,
        # hidden and tabbed separately and each gets a labelled View entry.
        for key,widget in (("project",self.project_panel),("preview",self.preview_panel),
                           ("effects",self.effects_panel),
                           ("properties",self.effects_panel.props_scroll),
                           ("timeline",self.timeline_panel),
                           ("keyframe",self.keyframe_editor),("graph",self.graph_editor)):
            self._add_dock(key,widget)
        # Interactive editing lives on the picture, but only on screen: the overlay is
        # painted by the preview widget and never touches the rendered frame.
        from .preview_transform import PreviewOverlay
        self.overlay=PreviewOverlay(self.preview_panel.view,self.engine,self.engine.session)
        self.preview_panel.view.overlay=self.overlay
        self.overlay.on_change=self._overlay_changed
        self.engine.session.auto_keyframe.trace_add("write",lambda *_:self._sync_auto_key_buttons())
        self.engine.session.clip_selected.connect(
            lambda _ids:(self.graph_editor.refresh(),self.keyframe_editor.refresh()))
        self.preview_panel.zoom_preset.connect(self.preview_panel.apply_preset)
        self.preview_panel.files_dropped.connect(lambda paths:self.import_paths(paths,self.engine.session.playhead,find_free=True))
        self.preview_panel.media_ids_dropped.connect(
            lambda ids:self._media_ids_dropped(ids,self.engine.session.playhead,None))
        self.preview_panel.quality_changed.connect(self.set_preview_quality)
        self.preview_panel.save_frame_requested.connect(self.save_frame_dialog)
        self.preview_panel.master.volume_changed.connect(self.audio.set_master_volume)
        self.preview_panel.master.set_volume_percent(self.audio.master_volume*100)
        self.set_preview_quality(str(self._settings.value("previewQuality","Auto")),save=False)
        # Export lives in the top toolbar and the File menu; Save Frame in the File menu and by the preview.
        self.preview_panel.play_toggled.connect(self.toggle_playback)
        self.preview_panel.step_requested.connect(self._step_frame)
        self.preview_panel.start_requested.connect(lambda: self._set_playhead(0))
        self.preview_panel.end_requested.connect(lambda: self._set_playhead(self.engine.timeline.duration))
        self.timeline_panel.canvas.files_dropped.connect(self.import_paths)
        self.timeline_panel.canvas.media_dropped.connect(self._media_ids_dropped)
        self.timeline_panel.canvas.media_lookup = self._media_entries_for
        self.timeline_panel.canvas.composition_open_requested.connect(
            self.open_composition_clip)
        self.timeline_panel.canvas.composition_dropped.connect(
            self._composition_dropped)
        # Selecting keys on a clip selects them in the Graph Editor; double-clicking one
        # (or "Open in Graph Editor") shows the curve there. Both read the shared
        # session keyframe selection, so the two views can never disagree.
        self.timeline_panel.canvas.graph_editor_requested.connect(self._open_graph_editor_for)
        self.timeline_panel.canvas.keyframe_editor_requested.connect(self._open_keyframe_editor_for)
        self.keyframe_editor.graph_requested.connect(
            lambda:self._open_graph_editor_for(self.engine.session.selected_key_entries()))
        self.graph_editor.keyframe_requested.connect(
            lambda:self._open_keyframe_editor_for(self.engine.session.selected_key_entries()))
        self.effects_panel.preview_requested.connect(self._preview_clip_range)
        self.effects_panel.properties_requested.connect(self._raise_properties)
        self.timeline_panel.canvas.playhead_requested.connect(self._set_playhead)
        self.bridge.changed.connect(self.timeline_panel.canvas.update)
        self._menu_view.addSeparator(); self._menu_view.addAction("Markers List...",self.show_markers_list)
        # Waveform drawing mode: the source envelope, or the sound after gain/fades/effects.
        wave_menu=self._menu_view.addMenu("Waveform Display"); self._wave_menu=wave_menu
        self._wave_group=QActionGroup(self); self._wave_actions={}
        for mode in WAVEFORM_MODES:
            action=QAction(mode,self); action.setCheckable(True)
            action.setChecked(self.playback_settings.waveform_mode==mode)
            action.triggered.connect(lambda _checked,m=mode:self._set_waveform_mode(m))
            self._wave_group.addAction(action); wave_menu.addAction(action)
            self._wave_actions[mode]=action
        graph_dock=self.docks.get("graph")
        if graph_dock is not None:
            toggle=graph_dock.toggleViewAction(); toggle.setShortcut(QKeySequence("G")); toggle.setText("Graph Editor")
        keyframe_dock=self.docks.get("keyframe")
        if keyframe_dock is not None:
            toggle=keyframe_dock.toggleViewAction(); toggle.setShortcut(QKeySequence("Shift+G"))
            toggle.setText("Keyframe Editor")
        self._menu_view.addAction("Reset Layout",self.reset_layout)
        self._menu_view.addAction("Save Layout As...",self.save_layout_preset)
        self._menu_view.addAction("Load Layout As...",self.load_layout_preset)
        help_menu=self.menuBar().addMenu("Help")
        self._add_action(help_menu,"Getting Started",None,self.show_getting_started)
        self._add_action(help_menu,"Open sample project",None,lambda:self.projects.open_sample())
        self._add_action(help_menu,"Keyboard Shortcuts...",None,self.show_shortcuts)
        self._add_action(help_menu,"Check for Updates...",None,self.show_updates)
        self._add_action(help_menu,"What's New...",None,self.show_whats_new)
        help_menu.addSeparator()
        self._add_action(help_menu,"About meeniFX Studios",None,self.show_about)
        self.toast=Toast(self)
        # Non-modal, so a background check can never interrupt playback, recording or export.
        self.update_notice=UpdateNotice(self)
        self.update_notice.view_requested.connect(self.show_updates)
        self.update_notice.remind_requested.connect(self._remind_update)
        self.update_notice.skip_requested.connect(self._skip_update)
        self.update_manager.update_available.connect(self._on_update_available)
        self.engine.session.tool.trace_add("write",lambda *_:self._update_hints())
        self.engine.session.snap_enabled.trace_add("write",lambda *_:self._update_hints())
        self.engine.session.loop_changed.connect(self._sync_loop_state)
        self._activate_animation_editor("keyframe")
        self._update_hints()

    def _add_dock(self,key,widget):
        """Create a dock with a stable objectName (restoreState depends on it)."""
        title,area,minimum=dock_layout.DOCK_SPECS[key]
        dock=QDockWidget(title,self); dock.setObjectName(f"dock_{key}"); dock.setWidget(widget)
        dock.setMinimumSize(*minimum)
        dock.setAllowedAreas(Qt.DockWidgetArea.AllDockWidgetAreas)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable|QDockWidget.DockWidgetFeature.DockWidgetMovable|QDockWidget.DockWidgetFeature.DockWidgetFloatable)
        self.addDockWidget(area,dock); self.docks[key]=dock; self._menu_view.addAction(dock.toggleViewAction())
        # Debounced save: a crash mid-drag can never leave a stale layout behind.
        dock.dockLocationChanged.connect(lambda _area:self._on_dock_moved())
        dock.topLevelChanged.connect(lambda _floating:self._on_dock_moved())
        dock.visibilityChanged.connect(lambda _visible:self._schedule_layout_save())

    def _raise_properties(self):
        """An effect was added: bring the inspector forward without forcing it open."""
        dock=self.docks.get("properties")
        if dock is not None and dock.isVisible():dock.raise_()

    def _build_actions(self):
        bar=self.menuBar(); file_menu=bar.addMenu("File")
        self._add_action(file_menu,"New Project...","Ctrl+N",self.action_new_project)
        self._add_action(file_menu,"Open...","Ctrl+O",lambda:self.projects.browse_open())
        self._menu_recent=file_menu.addMenu("Open Recent"); self._menu_recent.aboutToShow.connect(self._fill_recent_menu)
        self._add_action(file_menu,"Save","Ctrl+S",lambda:self.projects.save())
        self._add_action(file_menu,"Save As...","Ctrl+Shift+S",lambda:self.projects.save_as())
        file_menu.addSeparator()
        self._add_action(file_menu,"Project Settings...",None,self.show_project_settings)
        self._add_action(file_menu,"Close Project",None,lambda:self.projects.close_project())
        file_menu.addSeparator()
        self._add_action(file_menu,"Import Media...","Ctrl+I",self.browse_input)
        self._add_action(file_menu,"Record...",None,self.open_recorder)
        self._add_action(file_menu,"Export...","Ctrl+R",self.open_export_dialog)
        self._add_action(file_menu,"Save Frame...","Ctrl+Alt+S",self.save_frame_dialog)
        file_menu.addSeparator(); self._add_action(file_menu,"Preferences...",None,self.show_preferences)
        self._add_action(file_menu,"Exit","Alt+F4",self.close)
        edit=bar.addMenu("Edit")
        self._add_action(edit,"Undo","Ctrl+Z",self.engine.session.undo); self._add_action(edit,"Redo","Ctrl+Y",self.engine.session.redo); edit.addSeparator()
        commands=(("Cut","Ctrl+X",self.engine.session.cut_selected),("Copy","Ctrl+C",self.engine.session.copy_selected),
            ("Paste","Ctrl+V",self.smart_paste.paste),("Duplicate","Ctrl+D",self.engine.session.duplicate_selected),
            ("Select All","Ctrl+A",self._select_all_focused),("Split at Playhead","C",self.engine.session.split_at_playhead),
            ("Delete","Delete",self._delete_focused),("Ripple Delete","Shift+Delete",lambda:self._delete_focused(True)),
            ("Add Marker","M",self.engine.session.add_marker_at_playhead))
        for row in commands:self._add_action(edit,*row)
        edit.addSeparator()
        self.link_action = self._add_action(edit,"Link Selected Clips","Ctrl+L",
                                           lambda:self.engine.session.link_selected())
        self.unlink_action = self._add_action(edit,"Unlink Selected Clips","Ctrl+Shift+L",
                                             lambda:self.engine.session.unlink_selected())
        # The timeline toolbar no longer carries a chain button: linking is the explicit
        # command above, while this checkable option (and its shortcut) preserves the
        # separate "follow linked partners when selecting" behaviour.
        self.linked_selection_action = self._add_check_action(
            edit, "Linked Selection", "Ctrl+Shift+K",
            self.engine.session.linked_selection_enabled, self._set_linked_selection)
        self.linked_selection_action.setToolTip(
            "Select, move, trim and split already-linked clips together "
            "(selection behaviour only; use Link Selected Clips, Ctrl+L, to create links)")
        self.engine.session.linked_selection_changed.connect(self._sync_linked_selection_action)
        self.engine.session.clip_selected.connect(lambda _ids:self._refresh_link_actions())
        self.engine.session.changed.connect(self._refresh_link_actions)
        self._refresh_link_actions()
        comp_menu = bar.addMenu("Composition")
        # Ctrl+Shift+C already copies keyframes, so pre-composing gets its own chord.
        self._add_action(comp_menu, "Precompose Selected Clips...", "Ctrl+Alt+C",
                         self.show_precompose_dialog)
        self._add_action(comp_menu, "Back to Parent Composition", None, self.back_to_parent)
        self._add_action(comp_menu, "Composition Settings...", None,
                         self.show_composition_settings)
        comp_menu.addSeparator()
        self._open_comp_menu = comp_menu.addMenu("Open Composition")
        self._open_comp_menu.aboutToShow.connect(self._populate_open_composition_menu)
        comp_menu.addSeparator()
        self._add_action(comp_menu, "Render Composition", None,
                         lambda: self.render_preview(False))
        self._add_action(comp_menu, "Rebuild Composition Cache", None,
                         self.rebuild_composition_cache)
        edit.addSeparator()
        self._add_action(edit,"Delete Gap",None,self.delete_gap)
        self._add_action(edit,"Copy Effects",None,self.copy_effects)
        self._add_action(edit,"Paste Effects",None,self.paste_effects)
        self._add_action(edit,"Paste Attributes...",None,self.paste_attributes)
        self._add_action(edit,"Remove All Effects",None,self.remove_all_effects)
        edit.addSeparator()
        self._add_action(edit,"Rename Selected Marker...","F2",self._rename_marker)
        self._add_action(edit,"Delete Selected Marker(s)",None,self.engine.session.delete_markers)
        self._add_action(edit,"Delete All Markers",None,self.engine.session.delete_all_markers)
        edit.addSeparator()
        # Tools and snapping are also here so the toolbar buttons always have a twin.
        self._add_action(edit,"Select Tool","V",lambda:self.engine.session.tool.set("select"))
        self._add_action(edit,"Razor Tool","S",lambda:self.engine.session.tool.set("razor"))
        tools=bar.addMenu("Tools")
        captions_menu=tools.addMenu("Captions")
        self._add_action(captions_menu,"Generate Auto Captions...",None,self.open_captions_dialog)
        self._add_action(captions_menu,"Transcript Editor...",None,self.open_transcript_editor)
        captions_menu.addSeparator()
        self._add_action(captions_menu,"Import SRT...",None,self.import_captions)
        self._add_action(captions_menu,"Export SRT...",None,lambda:self.export_captions("srt"))
        self._add_action(captions_menu,"Export ASS...",None,lambda:self.export_captions("ass"))
        self._add_action(tools,"Remove Dead Silence...",None,self.open_silence_dialog)
        self._add_action(tools,"Normalize Audio...",None,self.normalize_audio)
        transformation_menu=bar.addMenu("Transform")
        self._add_action(transformation_menu,"Selection Tool","V",lambda:self._set_overlay_mode("select"))
        self._add_action(transformation_menu,"Transform Tool","T",lambda:self._set_overlay_mode("transform"))
        self._add_action(transformation_menu,"Puppet Warp Tool","P",lambda:self._set_overlay_mode("puppet"))
        transformation_menu.addSeparator()
        self.autokey_action=self._add_check_action(transformation_menu,"Auto-Keyframe","K",False,self._set_auto_key)
        self.motion_path_action=self._add_check_action(transformation_menu,"Show Motion Path","Ctrl+Shift+P",True,
                                                       self._set_motion_path)
        self._add_action(transformation_menu,"Fit to Frame","Shift+F7",lambda:self.engine.session.fit_frame(sorted(self.engine.session.selected)))
        self._add_action(transformation_menu,"Fill Frame","Shift+F8",lambda:self.engine.session.fill_frame(sorted(self.engine.session.selected),self._clip_media_size))
        self._add_action(transformation_menu,"Center in Frame","Shift+F9",lambda:self.engine.session.center_in_frame(sorted(self.engine.session.selected)))
        self._add_action(transformation_menu,"Reset Transform","Shift+F10",lambda:self.engine.session.reset_transform(sorted(self.engine.session.selected)))
        self._add_action(transformation_menu,"Flip Horizontal",None,lambda:self.engine.session.flip_selected(sorted(self.engine.session.selected),"h"))
        self._add_action(transformation_menu,"Flip Vertical",None,lambda:self.engine.session.flip_selected(sorted(self.engine.session.selected),"v"))
        # The simple keyframe workflow: one command per operation, each on the shared
        # session selection, so the timeline, inspector and Graph Editor stay in step.
        keyframes_menu=bar.addMenu("Keyframes")
        self._add_action(keyframes_menu,"Add Keyframe at Playhead","Ctrl+K",
                         lambda:self.engine.session.add_keyframes_at_playhead())
        self._add_action(keyframes_menu,"Copy Selected Keyframes","Ctrl+Shift+C",
                         lambda:self.timeline_panel.canvas.copy_selected_keyframes())
        self._add_action(keyframes_menu,"Paste Keyframes at Playhead","Ctrl+Shift+V",
                         lambda:self.timeline_panel.canvas.paste_keyframes())
        self._add_action(keyframes_menu,"Duplicate Selected Keyframes","Ctrl+Shift+D",
                         lambda:self.timeline_panel.canvas.duplicate_selected_keyframes())
        self._add_action(keyframes_menu,"Delete Selected Keyframes",None,
                         lambda:self.timeline_panel.canvas._delete_selected_keyframes())
        keyframes_menu.addSeparator()
        self._add_action(keyframes_menu,"Open Keyframe Editor",None,
                         lambda:self._open_keyframe_editor_for(self.engine.session.selected_key_entries()))
        self._add_action(keyframes_menu,"Open Selection in Graph Editor",None,
                         lambda:self._open_graph_editor_for(self.engine.session.selected_key_entries()))
        keyframes_menu.addSeparator()
        self.show_keys_action=self._add_check_action(keyframes_menu,"Show Keyframes on Clips",None,True,
                                                     lambda on:self.engine.session.show_keyframes.set(bool(on)))
        self.opacity_env_action=self._add_check_action(keyframes_menu,"Opacity Envelope",None,True,
                                                       lambda on:self.engine.session.show_opacity_envelope.set(bool(on)))
        self.gain_env_action=self._add_check_action(keyframes_menu,"Gain Envelope",None,True,
                                                    lambda on:self.engine.session.show_gain_envelope.set(bool(on)))
        self.double_click_graph_action=self._add_check_action(
            keyframes_menu,"Double-click Keyframe Opens Graph Editor",None,True,
            lambda on:self.engine.session.set_double_click_opens_graph(on))
        add_menu=bar.addMenu("Add")
        self._add_action(add_menu,"Adjustment Layer",None,self.add_adjustment_layer)
        from ..model import shape as SHAPE
        shape_menu=add_menu.addMenu("Shape")
        for kind in SHAPE.KINDS:
            self._add_action(shape_menu,SHAPE.LABELS[kind],None,
                             lambda k=kind:self.add_shape(k))
        text_menu=bar.addMenu("Text")
        self._add_action(text_menu,"Add Text Clip","Ctrl+T",self.add_text_clip)
        self._add_action(text_menu,"Add or Remove Text Keyframe at Playhead","Ctrl+Shift+T",
                         lambda:self.toggle_text_key(sorted(self.engine.session.selected)))
        text_menu.addSeparator()
        self._add_action(text_menu,"Edit Text in Properties",None,self._focus_text_editor)
        self.snap_action=self._add_check_action(edit,"Snap to Edits","F8",True,
                                                lambda on:self.engine.session.snap_enabled.set(on))
        playback=bar.addMenu("Playback")
        # One play/pause action for the menu, the Space shortcut and the top toolbar.
        self.play_pause_action=QAction("Play",self)
        self.play_pause_action.setShortcut(QKeySequence("Space")); self.play_pause_action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.play_pause_action.setToolTip("Play / Pause (Space)")
        self.play_pause_action.triggered.connect(lambda _checked=False:self._guarded_action(self.toggle_playback))
        playback.addAction(self.play_pause_action)
        self._add_action(playback,"Previous Frame","Left",lambda:self._step_frame(-1)); self._add_action(playback,"Next Frame","Right",lambda:self._step_frame(1))
        self._add_action(playback,"Back 1 Second","Shift+Left",lambda:self._step_seconds(-1)); self._add_action(playback,"Forward 1 Second","Shift+Right",lambda:self._step_seconds(1))
        self._add_action(playback,"Previous Edit","Ctrl+Left",lambda:self._jump_edit(-1)); self._add_action(playback,"Next Edit","Ctrl+Right",lambda:self._jump_edit(1))
        # Markers: [ and ] walk the ruler without touching the clip-edge shortcuts above.
        previous_marker=self._add_action(playback,"Previous Marker",None,lambda:self.engine.session.jump_to_marker(-1))
        previous_marker.setShortcut(QKeySequence(Qt.Key.Key_BracketLeft))
        previous_marker.setToolTip("Jump to the previous marker ( [ )")
        next_marker=self._add_action(playback,"Next Marker",None,lambda:self.engine.session.jump_to_marker(1))
        next_marker.setShortcut(QKeySequence(Qt.Key.Key_BracketRight))
        next_marker.setToolTip("Jump to the next marker ( ] )")
        self._add_action(playback,"Home","Home",lambda:self._set_playhead(0)); self._add_action(playback,"End","End",lambda:self._set_playhead(self.engine.timeline.duration))
        self.loop_action=self._add_check_action(playback,"Loop Playback","Q",False,self._set_loop_enabled)
        self.loop_end_action=self._add_check_action(playback,"Loop at End",None,True,self._set_loop_at_end)
        self.loop_end_action.setToolTip("Without a loop region, reaching the end jumps back to the start "
                                        "and keeps playing instead of stopping")
        self.scrub_audio_action=self._add_check_action(playback,"Audio While Scrubbing",None,True,self._set_scrub_audio)
        self._add_action(playback,"Set Loop Start at Playhead","I",self.engine.session.set_loop_start_at_playhead)
        self._add_action(playback,"Set Loop End at Playhead","O",self.engine.session.set_loop_end_at_playhead)
        self._add_action(playback,"Add Freeze Frame","Shift+F",self.engine.session.add_freeze_at_playhead)
        cache_menu=bar.addMenu("Preview Cache")
        self._add_action(cache_menu,"Render Preview","Ctrl+Shift+R",lambda:self.render_preview(False))
        self._add_action(cache_menu,"Render Selected Region","Ctrl+Shift+E",lambda:self.render_preview(True))
        self.cancel_prerender_action=self._add_action(cache_menu,"Cancel Preview Rendering",None,
                                                      self.cancel_preview_render)
        cache_menu.addSeparator()
        self._add_action(cache_menu,"Clear Preview Cache",None,self.clear_preview_cache)
        self._add_action(cache_menu,"Preview Cache Settings...",None,self.show_preferences)
        cache_menu.addSeparator()
        self.perf_overlay_action=self._add_check_action(cache_menu,"Performance Overlay","Ctrl+Shift+F",
                                                       bool(self.playback_settings.show_overlay),
                                                       self.set_perf_overlay_visible)
        self._menu_view=bar.addMenu("View")
        self._build_theme_menu(self._menu_view)

    def _build_theme_menu(self,menu):
        from PyQt6.QtGui import QActionGroup
        sub=menu.addMenu("Appearance")
        group=QActionGroup(self); group.setExclusive(True)
        current=dock_layout.app_settings().value("ui/theme","light")
        self._theme_actions={}
        for key,label in (("light","Light"),("dark","Dark"),("system","Match system")):
            action=QAction(label,self); action.setCheckable(True); action.setChecked(key==current)
            action.triggered.connect(lambda _c=False,k=key: self.set_theme_choice(k))
            group.addAction(action); sub.addAction(action); self._theme_actions[key]=action

    def set_theme_choice(self,choice):
        dock_layout.app_settings().setValue("ui/theme",choice)
        configure_theme(self.qt_app,resolve_theme(self.qt_app,choice))

    def _add_action(self,menu,label,shortcut,callback):
        action=QAction(label,self)
        if shortcut: action.setShortcut(QKeySequence(shortcut)); action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.triggered.connect(lambda _checked=False, cb=callback: self._guarded_action(cb))
        menu.addAction(action); return action

    def _add_check_action(self,menu,label,shortcut,checked,callback):
        action=QAction(label,self); action.setCheckable(True); action.setChecked(bool(checked))
        if shortcut: action.setShortcut(QKeySequence(shortcut)); action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        action.toggled.connect(lambda on, cb=callback: self._guarded_action(lambda: cb(on)))
        menu.addAction(action); return action

    def _guarded_action(self, callback):
        from PyQt6.QtWidgets import QApplication
        if self._is_text_entry(QApplication.focusWidget()):
            return
        callback()

    def _focused_animation_editor(self):
        """The Keyframe/Graph panel that owns focus, or None for the rest of the app."""
        from PyQt6.QtWidgets import QApplication
        widget = QApplication.focusWidget()
        if widget is None:
            return None
        for panel in (self.keyframe_editor, self.graph_editor):
            if widget is panel or panel.isAncestorOf(widget):
                return panel
        return None

    def _select_all_focused(self):
        """Ctrl+A follows the focused animation editor; otherwise it selects clips."""
        from PyQt6.QtWidgets import QApplication
        if self._is_text_entry(QApplication.focusWidget()):
            return
        panel = self._focused_animation_editor()
        if panel is not None:
            panel.select_all()
            return
        self.engine.session.select_all()

    def _delete_focused(self, ripple=False):
        """Delete keys while an animation editor has focus; otherwise delete clips."""
        from PyQt6.QtWidgets import QApplication
        if self._is_text_entry(QApplication.focusWidget()):
            return
        panel = self._focused_animation_editor()
        if panel is not None:
            panel.delete_selected()
            return
        self.engine.session.delete_selected(ripple=ripple)

    @staticmethod
    def _is_text_entry(widget):
        """Only real text inputs keep their own keys; read-only fields must not eat shortcuts."""
        if isinstance(widget,QLineEdit): return not widget.isReadOnly()
        if isinstance(widget,QAbstractSpinBox): return True
        if isinstance(widget,(QPlainTextEdit,QTextEdit)): return not widget.isReadOnly()
        return False

    def _build_toolbar(self):
        from PyQt6.QtWidgets import QHBoxLayout,QMenu,QPushButton,QSizePolicy,QToolButton,QWidget
        from .. import tokens
        from ..icons import icon
        toolbar=QToolBar("Main",self); toolbar.setMovable(False); toolbar.setObjectName("mainToolbar")
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.addToolBar(toolbar); self.main_toolbar=toolbar
        # Import is secondary; Export is the only primary (accent) button.
        self.import_button=QPushButton("Import"); self.import_button.setToolTip("Import media (Ctrl+I)")
        self.import_button.setFixedHeight(tokens.CONTROL_H); self.import_button.clicked.connect(lambda _=False:self.browse_input())
        toolbar.addWidget(self.import_button)
        self.record_button=QPushButton("Record"); self.record_button.setIcon(icon("record",tokens.COLORS["red"]))
        self.record_button.setToolTip("Record webcam, screen or microphone"); self.record_button.setFixedHeight(tokens.CONTROL_H)
        self.record_button.clicked.connect(lambda _=False:self._record_button_clicked())
        toolbar.addWidget(self.record_button); toolbar.addSeparator()
        def icon_button(name,tip,callback):
            button=QToolButton(); button.setProperty("role","icon"); button.setIcon(icon(name)); button.setToolTip(tip)
            button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H); button.clicked.connect(lambda _=False:callback()); toolbar.addWidget(button); return button
        self.undo_button=icon_button("undo","Undo (Ctrl+Z)",self.engine.session.undo)
        self.redo_button=icon_button("redo","Redo (Ctrl+Y)",self.engine.session.redo)
        self.qt_undo_stack.changed.connect(self._sync_undo_buttons); self._sync_undo_buttons()
        toolbar.addSeparator()
        def tool_button(name,icon_name,tip,mode):
            button=QToolButton(); button.setProperty("role","icon"); button.setCheckable(True)
            button.setIcon(icon(icon_name)); button.setToolTip(tip); button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
            button.clicked.connect(lambda _=False,m=mode:self._set_overlay_mode(m))
            toolbar.addWidget(button); return button
        self.select_tool_button=tool_button("select","select","Selection tool (V)","select")
        self.transform_tool_button=tool_button("transform","transform","Transform tool: move, scale, rotate (T)","transform")
        self.puppet_tool_button=tool_button("puppet","puppet","Puppet Warp tool: drag pins on the picture (P)","puppet")
        self.text_button=QToolButton(); self.text_button.setProperty("role","icon")
        self.text_button.setIcon(icon("text")); self.text_button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
        self.text_button.setToolTip("Add a text clip at the playhead (Ctrl+T)")
        self.text_button.clicked.connect(lambda _=False:self.add_text_clip())
        toolbar.addWidget(self.text_button)
        # Shape tool: the main button re-arms the last shape, the menu picks another. Both
        # only arm the tool - the shape itself is drawn where the user drags.
        from ..model import shape as SHAPE
        self._shape_kind="rectangle"
        self.shape_button=QToolButton(); self.shape_button.setProperty("role","icon")
        self.shape_button.setIcon(icon(SHAPE.ICONS["rectangle"]))
        self.shape_button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
        self.shape_button.setCheckable(True)
        self.shape_button.setToolTip("Shape tool: pick a shape, then drag in the preview to draw it")
        shape_picker=QMenu(self)
        for kind in SHAPE.KINDS:
            shape_picker.addAction(icon(SHAPE.ICONS[kind]),SHAPE.LABELS[kind],
                                   lambda _checked=False,k=kind:self._start_shape_tool(k))
        # A split button has two glyphs that need their own space: the shape itself and
        # the disclosure arrow. Keeping them as two real controls avoids the menu
        # indicator ever painting over the icon at any DPI or toolbar height.
        self.shape_menu_button=QToolButton(); self.shape_menu_button.setProperty("role","icon")
        self.shape_menu_button.setIcon(icon("chevron_down"))
        self.shape_menu_button.setToolTip("Choose a shape")
        self.shape_menu_button.setFixedSize(tokens.CONTROL_H - 6,tokens.CONTROL_H)
        self.shape_menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.shape_menu_button.setMenu(shape_picker)
        shape_holder=QWidget()
        shape_row=QHBoxLayout(shape_holder); shape_row.setContentsMargins(0,0,0,0); shape_row.setSpacing(0)
        shape_row.addWidget(self.shape_button); shape_row.addWidget(self.shape_menu_button)
        self.shape_button.clicked.connect(lambda _=False:self._start_shape_tool(self._shape_kind))
        toolbar.addWidget(shape_holder)
        self.captions_button=QToolButton(); self.captions_button.setProperty("role","icon")
        self.captions_button.setIcon(icon("captions")); self.captions_button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
        self.captions_button.setToolTip("Generate Auto Captions from the audio")
        self.captions_button.clicked.connect(lambda _=False:self.open_captions_dialog())
        toolbar.addWidget(self.captions_button)
        toolbar.addSeparator()
        self.graph_button=QToolButton(); self.graph_button.setProperty("role","icon"); self.graph_button.setCheckable(True)
        self.graph_button.setIcon(icon("graph")); self.graph_button.setToolTip("Graph Editor (G)")
        self.graph_button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
        self.graph_button.clicked.connect(lambda _=False:self._toggle_graph_dock())
        toolbar.addWidget(self.graph_button)
        self.path_button=QToolButton(); self.path_button.setProperty("role","icon"); self.path_button.setCheckable(True)
        self.path_button.setChecked(True); self.path_button.setIcon(icon("path"))
        self.path_button.setToolTip("Show motion paths in the preview")
        self.path_button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
        self.path_button.toggled.connect(self._set_motion_path)
        toolbar.addWidget(self.path_button)
        self.autokey_button=QToolButton(); self.autokey_button.setProperty("role","icon"); self.autokey_button.setCheckable(True)
        self.autokey_button.setIcon(icon("auto_key"))
        self.autokey_button.setToolTip("Auto-Keyframe: changing an animated value at a new time creates a keyframe (K)")
        self.autokey_button.setFixedSize(tokens.CONTROL_H,tokens.CONTROL_H)
        self.autokey_button.toggled.connect(self._set_auto_key)
        toolbar.addWidget(self.autokey_button)
        spacer=QWidget(); spacer.setSizePolicy(QSizePolicy.Policy.Expanding,QSizePolicy.Policy.Preferred); toolbar.addWidget(spacer)
        self.export_button=QPushButton("Export"); self.export_button.setProperty("primary",True)
        self.export_button.setToolTip("Export the timeline (Ctrl+R)"); self.export_button.setFixedHeight(tokens.CONTROL_H)
        self.export_button.setMinimumWidth(88); self.export_button.clicked.connect(lambda _=False:self.open_export_dialog())
        toolbar.addWidget(self.export_button)
        icon_names=[(self.undo_button,"undo"),(self.redo_button,"redo"),(self.select_tool_button,"select"),
                    (self.transform_tool_button,"transform"),(self.puppet_tool_button,"puppet"),
                    (self.text_button,"text"),(self.captions_button,"captions"),(self.graph_button,"graph"),
                    (self.path_button,"path"),(self.autokey_button,"auto_key")]
        def refresh_icons(_toolbar):
            for button,name in icon_names: button.setIcon(icon(name))
            self.shape_button.setIcon(icon(SHAPE.ICONS.get(self._shape_kind,"shape_rectangle")))
            self.shape_menu_button.setIcon(icon("chevron_down"))
            self.record_button.setIcon(icon("record",tokens.COLORS["red"]))
        from .themed import bind_theme
        bind_theme(toolbar,refresh_icons)

    def _sync_undo_buttons(self,*_):
        stack=self.qt_undo_stack
        self.undo_button.setEnabled(bool(stack.can_undo)); self.redo_button.setEnabled(bool(stack.can_redo))

    def _bind_shortcuts(self):
        pass  # QAction shortcuts are ignored by Qt while a text editor has focus.

    @staticmethod
    def _waveform_cache_dir():
        """Peaks live in a per-user cache folder so project folders stay clean."""
        folder=os.path.join(os.path.expanduser("~"),".meenifx","waveform-cache")
        try: os.makedirs(folder,exist_ok=True)
        except OSError: return os.path.join(os.path.dirname(DEFAULT_INPUT),"waveform-cache")
        return folder

    def _connect_audio(self):
        """Bridge the audio engine and waveform service to the timeline and panels."""
        library=self.waveforms.library
        self.waveforms.ready.connect(self._on_waveform_ready)
        # Processed peaks land asynchronously; repaint so the lane refines in place.
        self.waveforms.processed_ready.connect(self._on_waveform_ready)
        self.waveforms.failed.connect(self._on_audio_failed)
        library.loaded.connect(self._on_audio_loaded)
        library.failed.connect(self._on_audio_failed)
        canvas=self.timeline_panel.canvas
        canvas.waveform_mode_getter=lambda:self.playback_settings.waveform_mode
        self.audio.level_changed.connect(self.preview_panel.master.bar.set_level)
        self.audio.playing_changed.connect(lambda playing:None if playing else self.preview_panel.master.bar.reset())
        canvas.audio_refresh.connect(self._refresh_audio)
        canvas.scrub_started.connect(self._on_scrub_started)
        canvas.scrub_finished.connect(self._on_scrub_finished)
        canvas.loop_changed.connect(self._sync_loop_state)
        if not self.audio.available:
            self.preview_panel.master.set_unavailable(self.audio.error_message or "no audio output device")
        else:
            self.bridge.status.emit(f"Audio output: {self.audio.device_name()}")
        QTimer.singleShot(120,self._refresh_audio)

    def _refresh_audio(self):
        """Rebuild the mixer snapshot from the timeline and start missing decodes."""
        timeline=self.engine.timeline; library=self.waveforms.library
        compositions=getattr(self.engine,"compositions",None)
        for path in missing_audio_paths(timeline,library.cached,compositions):
            if path in self.engine.missing_paths:continue
            # Decode first: cached waveform peaks must never stand in for playable audio.
            library.request(path)
        self.audio.set_segments(build_segments(timeline,library.cached,library.sample_rate,self.engine.runtime_effects,compositions=compositions))

    def _on_waveform_ready(self,_path):
        self.timeline_panel.canvas.update()

    def _set_waveform_mode(self,mode):
        """Switch between the source envelope and the processed one, and persist it."""
        if mode not in WAVEFORM_MODES: return
        self.playback_settings.waveform_mode=mode
        try: self.playback_settings.save()
        except OSError: pass
        action=self._wave_actions.get(mode)
        if action is not None and not action.isChecked(): action.setChecked(True)
        self.timeline_panel.canvas.update()

    def _on_audio_failed(self,path,message):
        pending=self._pending_audio.pop(path,[])
        name=os.path.basename(path) if path else "audio"
        if "audio stream" in str(message).lower():
            if pending:
                pass
        else:
            self.toast_message(f"Could not decode {name}: {message}",kind="error",duration_ms=4200)
        if pending:
            self.bridge.status.emit(f"{name} was not added: {message}")

    def _place_audio(self,path,at_frame,track,find_free):
        """Add one decoded audio clip; the timeline creates a track when none matches."""
        fps=self.engine.timeline.fps
        duration=max(1,int(round(self.waveforms.library.duration_seconds(path)*fps)))
        self.engine.import_records([{"name":os.path.basename(path),"path":path,"duration":duration,
                                     "type":"audio"}],at_frame,track,find_free)
        self.bridge.status.emit(f"Imported audio: {os.path.basename(path)} ({duration/fps:.1f}s)")

    def _on_audio_loaded(self,path):
        """Audio imports are added once their real duration is known."""
        for at_frame,track,find_free in self._pending_audio.pop(path,[]):
            self._place_audio(path,at_frame,track,find_free)
        self._refresh_audio()
        self.timeline_panel.canvas.update()

    def _on_scrub_started(self):
        """Ruler press: suspend playback (remembering it) but never toggle it off for good."""
        if self._camera is not None:
            # A live camera has no timeline position to scrub about: just stop it.
            self.stop_playback()
            return
        self._scrub_resume=self._playing
        if self._playing:
            self._playing=False; self.play_timer.stop(); self.audio.pause()
        self._scrub_mode=True
        self._sync_transport()
        self._refresh_audio()
        self.play_snippet(self.engine.session.playhead)

    def _on_scrub_finished(self):
        if not self._scrub_mode:
            return
        self._scrub_mode=False
        self._snippet_timer.stop()
        self.audio.stop_snippet(self.engine.session.playhead/float(self.engine.timeline.fps))
        if self._scrub_resume:
            # Playback was running when the scrub began: carry on from the new position.
            self._scrub_resume=False
            self._begin_playback(self.engine.session.playhead)

    # ------------------------------------------------------- preview frames/cache
    def _render_preview_frame(self, frame, scale):
        """Render one frame for the preview. Runs on the render thread, never the GUI."""
        _raw,image=self.engine.render_frame(int(frame),float(scale))
        return image

    def _show_preview(self, frame, image, scale=None):
        """Publish a finished frame to the widget and remember it for other panels."""
        self.last_frame=image
        self.engine.current_processed=image
        self.preview_panel.view.set_frame(image,frame_key=self.engine.current_media_path,
                                          logical_size=self.engine.project_size())
        self._shown_frame=int(frame)
        if self._playing:
            self.metrics.note_shown()
        self._preview_dirty=False

    def _on_frame_rendered(self, frame, scale, image):
        """A preview frame finished on the render thread; display it if it is still wanted."""
        self.frame_cache.clear_pending(frame, scale)
        self.metrics.note_render(float(self.renderer.stats().get("last_ms",0.0)))
        # A frame that started rendering before the last edit pictures the old state. It
        # used to land *after* an interactive drag and be displayed as if it were current,
        # which is exactly the "the picture only catches up later" symptom. The revision
        # stamp of the request decides: stale pixels are dropped, never cached.
        stamp = self._render_revisions.pop((int(frame), float(scale)), None)
        if stamp is not None and stamp != self._content_revision:
            return
        if self._playing:
            # Latest wins: a frame older than the picture already shown is dropped - that
            # is the frame-drop strategy, and the audio clock never waits for the renderer.
            if int(frame)<self._shown_frame:
                return
            self._play_head=max(self._play_head,float(frame))
        elif int(frame)!=int(self.engine.session.playhead):
            # The playhead moved again while this frame rendered: drop it, the pending
            # request for the new position is the one that matters.
            self._request_preview()
            return
        self._show_preview(frame, image, scale)

    def _on_frame_failed(self, frame, error):
        self._prerender_failed=True
        self.frame_cache.clear_pending(frame, self.preview_scale())
        self.bridge.status.emit(f"Preview render failed: {error}")

    def _cache_state_for_frame(self, frame):
        """Real cache state for one timeline frame at the current preview quality."""
        return self.frame_cache.state(int(frame), self.preview_scale())

    def _on_cache_changed(self):
        if not self._cache_timer.isActive():
            self._cache_timer.start()

    def _repaint_cache_bar(self):
        self.timeline_panel.canvas.update()

    def _overlay_stats(self):
        """Live numbers for the performance overlay - measured, never estimated."""
        scale=self.preview_scale(); stats=self.frame_cache.stats()
        width,height=self.engine.project_size()
        renderer=self.renderer.stats()
        # Dropped = frames the playback clock asked for that never reached the screen.
        # While paused the counter simply keeps its last value.
        dropped=(max(0,self.metrics.ticks-self.metrics.frames_shown)
                 if self._playing else self.metrics.frames_dropped)
        return {"fps": self.metrics.fps() if self._playing else 0.0,
                "target_fps": float(self.engine.timeline.fps),
                "dropped": dropped,
                "shown": self.metrics.frames_shown,
                "render_ms": self.metrics.render_ms,
                "render_avg_ms": float(renderer.get("average_ms",0.0)),
                "scale": scale,
                "resolution": (int(width*scale),int(height*scale)),
                "cache_frames": stats["ram_frames"]+stats["disk_frames"],
                "cache_mb": stats["ram_bytes"]/(1024*1024),
                "cache_hit_rate": stats["hit_rate"],
                "queue": int(renderer.get("queued",0)),
                "backend": render_backend.summary()}

    def _invalidate_preview_cache(self):
        """Drop only the cached frames of clips whose contribution actually changed."""
        current=timeline_digests(self.engine.timeline,getattr(self.engine,"compositions",None))
        ranges=changed_ranges(self._clip_digests,current)
        self._clip_digests=current
        if not ranges:
            return
        # Frames in flight were computed from the state before this change; bumping the
        # revision is what lets the delivery path recognise and drop them.
        self._content_revision += 1
        if self.frame_cache.invalidate_ranges(ranges):
            self._on_cache_changed()
            if self.playback_settings.background_render and not self._playing:
                self._prefetch_timer.start()

    # ------------------------------------------------------- audio snippets/state
    def play_snippet(self, frame, duration_ms=None):
        """One short burst at `frame`, shared by scrubbing, arrow keys and ruler clicks.

        Cancels any snippet still playing and is throttled so a fast drag retriggers
        instead of piling up buffers. Respects track mute/solo/volume through the mixer
        segments refreshed at scrub start.
        """
        if not self._audio_while_scrubbing or not self.audio.available:
            return False
        now=time.monotonic()
        if now-self._last_snippet_at<0.045:
            return False
        self._last_snippet_at=now
        seconds=max(0.0,frame/float(self.engine.timeline.fps))
        if not self.audio.start_snippet(seconds):
            return False
        self._snippet_timer.start(max(50,int(duration_ms or 90)))
        return True

    def _end_snippet(self):
        """Silence a finished snippet - but never interrupt real playback."""
        if self._playing and not self._scrub_mode:
            return
        self.audio.stop_snippet(self.engine.session.playhead/float(self.engine.timeline.fps))

    def toast_message(self, text, kind="info", duration_ms=None):
        """One short, non-blocking notice at the bottom of the window."""
        self.toast.show_message(text, kind=kind, duration_ms=duration_ms)

    def _update_hints(self):
        """Keep the Snap menu item in step with the button (no status text any more)."""
        session=self.engine.session
        snapping=bool(session.snap_enabled.get())
        if self.snap_action.isChecked()!=snapping:
            self.snap_action.blockSignals(True); self.snap_action.setChecked(snapping); self.snap_action.blockSignals(False)

    def _sync_loop_state(self):
        """Keep the Loop button, the menu item and the status text on the same value."""
        enabled=bool(self.engine.timeline.loop_enabled)
        if self.loop_action.isChecked()!=enabled:
            self.loop_action.blockSignals(True); self.loop_action.setChecked(enabled); self.loop_action.blockSignals(False)
        self.timeline_panel.set_loop_enabled(enabled)
        self._update_hints()

    def _set_loop_enabled(self, enabled):
        self.engine.session.set_loop_enabled(enabled)

    def _set_loop_at_end(self, enabled):
        self.engine.timeline.loop_at_end=bool(enabled)
        self._settings.setValue("loopAtEnd",bool(enabled))
        self.bridge.status.emit("Loop at end "+("on" if enabled else "off"))

    def _set_scrub_audio(self, enabled):
        self._audio_while_scrubbing=bool(enabled)
        self._settings.setValue("audioWhileScrubbing",bool(enabled))
        self.bridge.status.emit("Audio while scrubbing "+("on" if enabled else "off"))

    def _connect_values(self):
        self.engine.preview_quality.trace_add("write",lambda *_:self._request_preview())
        session=self.engine.session
        for flag,name in ((session.show_keyframes,"show_keys_action"),
                          (session.show_opacity_envelope,"opacity_env_action"),
                          (session.show_gain_envelope,"gain_env_action")):
            flag.trace_add("write",lambda *_,f=flag,n=name:self._sync_toggle_action(n,f))

    # ------------------------------------------------------------- transport UI
    def _sync_transport(self):
        """One source of truth for the toolbar toggle and the preview transport bar."""
        playing=bool(self._playing)
        if getattr(self,"play_pause_action",None) is not None:
            self.play_pause_action.setText("Pause" if playing else "Play")
            self.play_pause_action.setToolTip("Pause (Space)" if playing else "Play (Space)")
        self.preview_panel.set_playing(playing)
        self._update_timecode()

    def _update_timecode(self):
        self.preview_panel.set_timecode(timecode(self.engine.session.playhead,self.engine.timeline.fps))

    # ------------------------------------------------------------------ dock layout
    def build_default_layout(self, apply_sizes=False):
        """Controls | Preview | Effects over a full-width Timeline (no central widget)."""
        dock_layout.build_default_layout(self, apply_sizes=apply_sizes)

    def _restore_layout(self):
        """Arrange the default layout, then overlay whatever was saved last session."""
        dock_layout.apply_dock_minimums(self)
        self.build_default_layout(apply_sizes=False)
        if dock_layout.restore_state(self,self._settings)=="restored":
            # Sizes come back with restoreState; do not overwrite them on first show.
            self._pending_default_sizes=False
        else:
            # First run, older layout version or a broken/stale state blob.
            self._pending_default_sizes=True
        self._restore_timeline_view()
        # The loop region and markers belong to the project now; only preferences are global.
        self.engine.timeline.loop_at_end=bool(self._settings.value("loopAtEnd",True,type=bool))
        self.loop_end_action.blockSignals(True); self.loop_end_action.setChecked(self.engine.timeline.loop_at_end)
        self.loop_end_action.blockSignals(False)
        self._audio_while_scrubbing=bool(self._settings.value("audioWhileScrubbing",True,type=bool))
        self.scrub_audio_action.blockSignals(True); self.scrub_audio_action.setChecked(self._audio_while_scrubbing)
        self.scrub_audio_action.blockSignals(False)
        self._sync_loop_state()
        self._layout_ready=True

    def _restore_timeline_view(self):
        """Track height and timeline zoom/scroll are view state, not project data."""
        try:track_height=int(self._settings.value("trackHeight"))
        except (TypeError,ValueError):track_height=0
        if track_height:self.timeline_panel.canvas.set_track_height(track_height)
        try:zoom_value=float(self._settings.value("timelineZoom"))
        except (TypeError,ValueError):zoom_value=0.0
        if zoom_value>0:
            self.timeline_panel.restore_zoom_state(zoom_value,self._settings.value("timelineScroll",0))

    def reset_layout(self):
        """Rebuild the default arrangement and overwrite the saved layout with it."""
        self.build_default_layout(apply_sizes=False)
        self._pending_default_sizes=True
        # Re-docking recomputes the geometry, so the shares are applied on the next
        # turn of the event loop, when there is a real dock area to measure.
        QTimer.singleShot(0,self._force_default_sizes)
        self.bridge.status.emit("Layout reset to the default arrangement")

    def _force_default_sizes(self):
        """Apply the default shares after a reset, overriding the drag guard."""
        self._layout_touched=False
        dock_layout.apply_default_sizes(self)
        self._pending_default_sizes=False
        self._schedule_layout_save()

    def save_layout_preset(self):
        path=save_file(self,"Save layout preset",os.path.join(dock_layout.default_preset_dir(),"Editing.mfxlayout"),
                       "Layout preset (*.mfxlayout);;All files (*)")
        if not path:return
        if not os.path.splitext(path)[1]:path+=".mfxlayout"
        try:
            zoom,scroll=self.timeline_panel.zoom_state()
            dock_layout.save_preset(self,path,{"trackHeight":self.timeline_panel.canvas.track_height_value,
                                                "timelineZoom":zoom,"timelineScroll":scroll})
        except OSError as exc:self._show_error(f"Could not save the layout preset: {exc}"); return
        self.bridge.status.emit(f"Layout preset saved: {os.path.basename(path)}")

    def load_layout_preset(self):
        path=open_file(self,"Load layout preset",dock_layout.default_preset_dir(),
                       "Layout preset (*.mfxlayout);;All files (*)")
        if not path:return
        payload=dock_layout.read_preset(path)
        if payload is None:
            self._show_error("That file is not a layout preset for this version of meeniFX Studios."); return
        if not dock_layout.apply_preset(self,payload):
            self._show_error("The preset could not be applied; the layout has been left as it was."); return
        for key in ("trackHeight","timelineZoom","timelineScroll"):
            if key in payload:
                self._settings.setValue(key,payload[key])
        self._restore_timeline_view()
        self._save_layout_now()
        self.bridge.status.emit(f"Layout preset loaded: {os.path.basename(path)}")

    def _schedule_layout_save(self):
        if getattr(self,"_layout_ready",False):self._layout_save_timer.start()

    def _save_layout_now(self):
        """Persist window geometry, dock state and timeline view in one go."""
        if not getattr(self,"_layout_ready",False):return
        dock_layout.save_state(self,self._settings)
        zoom,scroll=self.timeline_panel.zoom_state()
        self._settings.setValue("trackHeight",self.timeline_panel.canvas.track_height_value)
        self._settings.setValue("timelineZoom",zoom)
        self._settings.setValue("timelineScroll",float(scroll))
        self._settings.sync()

    # ----------------------------------------------------------------- markers
    def _rename_marker(self):
        markers=self.engine.session.selected_markers()
        if not markers:
            self.bridge.status.emit("Select a marker on the ruler first (click it, or press M to add one)")
            return
        self.timeline_panel.canvas.rename_marker(markers[0])

    def tutorial_target(self,key):
        """The real widget a tour page points at, when it is currently on screen."""
        targets={"project":self.project_panel,"timeline":self.timeline_panel,
                 "preview":self.preview_panel,"effects":self.effects_panel,
                 "properties":self.effects_panel.props_scroll,"export":self.export_button}
        widget=targets.get(key)
        return widget if widget is not None and widget.isVisible() else None

    def show_getting_started(self):
        """Open the tour on demand; Help ignores the completed flag."""
        from .onboarding import OnboardingDialog
        dialog=getattr(self,"_onboarding_dialog",None)
        if dialog is None:
            dialog=OnboardingDialog(self)
            self._onboarding_dialog=dialog
        dialog.restart()

    def maybe_show_onboarding(self):
        """Show the first-launch tour once, and only when the editor is usable."""
        if not self.isVisible():
            return
        from .onboarding import has_completed
        if not has_completed():
            self.show_getting_started()

    def show_markers_list(self):
        """View > Markers List: a small non-modal window; double-click jumps to a marker."""
        if getattr(self,"_markers_dialog",None) is None:
            self._markers_dialog=MarkerListDialog(self.engine.session,self)
        self._markers_dialog.refresh()
        self._markers_dialog.show(); self._markers_dialog.raise_(); self._markers_dialog.activateWindow()

    def _decorate_dock_buttons(self):
        """Qt creates the title-bar float/close buttons itself, so they arrive untooltipped."""
        from PyQt6.QtWidgets import QAbstractButton
        tips={"qt_dockwidget_floatbutton":"Float this panel into its own window",
              "qt_dockwidget_closebutton":"Close this panel (View menu brings it back)"}
        for dock in self.docks.values():
            for button in dock.findChildren(QAbstractButton):
                tip=tips.get(button.objectName())
                if tip and not button.toolTip():
                    button.setToolTip(tip)

    def showEvent(self,event):
        super().showEvent(event)
        if self._shown:return
        self._shown=True
        QTimer.singleShot(0,self._decorate_dock_buttons)
        if self._pending_default_sizes:
            # After the real geometry exists, so resizeDocks() is not thrown away.
            QTimer.singleShot(0,lambda:self._apply_default_sizes(final=False))
            # A second, later pass in case the first one ran before the window (and its
            # wide timeline toolbar) reached the final size.
            QTimer.singleShot(220,lambda:self._apply_default_sizes(final=True))

    def _on_dock_moved(self):
        """The user rearranged the docks: stop auto-sizing and save the new layout."""
        if getattr(self,"_pending_default_sizes",False):
            # Showing the window re-docks the panels itself; that is not the user
            # rearranging anything, and it must not cancel the pending default sizes.
            return
        if self._shown and self._layout_ready:
            self._layout_touched=True
        self._schedule_layout_save()

    def _apply_default_sizes(self,final=False):
        if not getattr(self,"_pending_default_sizes",False):return
        if final and getattr(self,"_layout_touched",False):return
        dock_layout.apply_default_sizes(self)
        if final:
            self._pending_default_sizes=False
            self._schedule_layout_save()

    # ------------------------------------------------- Preview/Timeline separator
    def _timeline_splitter_band(self):
        """The y range of the separator between the top row and the Timeline, or None.

        Qt only accepts a drag of that separator that makes the bottom dock *bigger*;
        dragging it back up does nothing at all, so the boundary is handled here: the
        press pins the dock and the moves set its height, both directions included.
        The band stops at the Timeline's top edge so its title bar stays draggable.
        """
        timeline=self.docks.get("timeline")
        if timeline is None or timeline.isFloating() or not timeline.isVisible():return None
        if self.dockWidgetArea(timeline)!=Qt.DockWidgetArea.BottomDockWidgetArea:return None
        boundary=timeline.mapTo(self,QPoint(0,0)).y()
        extent=self.style().pixelMetric(QStyle.PixelMetric.PM_DockWidgetSeparatorExtent,None,self)
        return boundary-max(int(extent)+1,4),boundary

    def _end_split_drag(self):
        if self._split_drag is None:return
        self._split_drag=None; self.unsetCursor()
        dock_layout.release_timeline_height(self); self._schedule_layout_save()

    def mousePressEvent(self,event):
        band=self._timeline_splitter_band()
        if (event.button()==Qt.MouseButton.LeftButton and band is not None
                and band[0]<=event.position().y()<=band[1]):
            self._split_drag=(event.position().y(),self.docks["timeline"].height())
            self._pending_default_sizes=False; self._layout_touched=True
            dock_layout.pin_timeline_height(self,self.docks["timeline"].height())
            self.setCursor(Qt.CursorShape.SplitVCursor)
            event.accept(); return
        super().mousePressEvent(event)

    def mouseMoveEvent(self,event):
        drag=self._split_drag
        if drag is not None:
            if not (event.buttons() & Qt.MouseButton.LeftButton):
                # The release went somewhere else: never leave the dock pinned.
                self._end_split_drag(); event.accept(); return
            start_y,timeline_height=drag
            dock_layout.pin_timeline_height(self,timeline_height-int(event.position().y()-start_y))
            event.accept(); return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self,event):
        if self._split_drag is not None:
            self._end_split_drag(); event.accept(); return
        super().mouseReleaseEvent(event)

    def resizeEvent(self,event):
        super().resizeEvent(event); self._schedule_layout_save()
        if getattr(self,"toast",None) is not None and self.toast.isVisible():
            self.toast.reposition()

    def moveEvent(self,event):
        super().moveEvent(event); self._schedule_layout_save()

    def _on_model_changed(self):
        self.timeline_panel.canvas.update(); self._refresh_panels()
        self._invalidate_preview_cache()
        self._request_preview(); self._refresh_audio()

    def _on_playhead_changed(self,frame):
        if self._playing:self._play_head=float(frame)
        seconds=frame/float(self.engine.timeline.fps)
        if self._scrub_mode:
            # Only a change of position makes a sound; holding still stays silent.
            self.play_snippet(frame)
        elif self._playing and self.audio.playing:
            self.audio.seek(seconds)
        self.timeline_panel.canvas.update()
        self._update_timecode()
        self._playhead_timer.start()

    def _sync_playhead_views(self):
        self._refresh_panels(); self._request_preview(); self._update_timecode()

    def _refresh_panels(self):
        self.effects_panel.refresh_properties(); self.timeline_panel.canvas.update()
        self.keyframe_editor.refresh(); self.graph_editor.refresh()
        self.preview_panel.view.update()

    def _overlay_changed(self):
        """A preview drag changed the model: refresh the panels and re-render live.

        The drag writes straight into the model (that is what keeps it responsive), so it
        bypasses the `changed` signal - and therefore the cache invalidation that signal
        does. Without this call `_request_preview()` found the frame it rendered before
        the drag in the cache and the picture only caught up on mouse release.
        """
        self.effects_panel.sync_playhead(); self.timeline_panel.canvas.update()
        self.graph_editor.canvas.update(); self.preview_panel.view.update()
        self._invalidate_preview_cache()
        self._request_preview()

    def _sync_toggle_action(self,name,flag):
        """Keep a menu checkmark on the session value it controls."""
        action=getattr(self,name,None)
        value=bool(flag.get())
        if action is not None and action.isChecked()!=value:
            action.blockSignals(True); action.setChecked(value); action.blockSignals(False)

    def _sync_auto_key_buttons(self):
        on=bool(self.engine.session.auto_keyframe.get())
        button=getattr(self,"autokey_button",None)
        if button is not None and button.isChecked()!=on:
            button.blockSignals(True); button.setChecked(on); button.blockSignals(False)

    def _set_auto_key(self,on):
        self.engine.session.auto_keyframe.set(bool(on))
        self._sync_auto_key_buttons()
        self.effects_panel.refresh_properties()

    def _set_playhead(self,frame):self.engine.session.set_playhead(max(0,int(frame)))

    def _step_frame(self,delta):
        """Arrow key: one frame, plus a short burst when playback is stopped."""
        self._set_playhead(self.engine.session.playhead+delta)
        self._after_playhead_move()

    def _step_seconds(self,seconds):
        self._set_playhead(self.engine.session.playhead+int(round(seconds*self.engine.timeline.fps)))
        self._after_playhead_move()

    def _jump_edit(self,direction):
        """Ctrl+Left/Right: previous or next edit point, marker or timeline end."""
        frame=self.engine.session.playhead
        candidates={0,self.engine.timeline.duration,*self.engine.timeline.marker_frames()}
        for clip in self.engine.timeline.all_clips():
            candidates.update((clip.timeline_start,clip.end))
        options=[value for value in candidates if value<frame] if direction<0 else [value for value in candidates if value>frame]
        if options:
            target=max(options) if direction<0 else min(options)
        else:
            target=0 if direction<0 else self.engine.timeline.duration
        self._set_playhead(target)
        self._after_playhead_move()

    def _after_playhead_move(self):
        if not self._playing:
            self.play_snippet(self.engine.session.playhead)

    def set_preview_quality(self,label,save=True):
        """Quality dropdown: Full/1/2/1/4/1/8 are fixed render scales, Auto follows the panel size."""
        from .preview import QUALITY_LABELS,QUALITY_PERCENT
        label=label if label in QUALITY_LABELS else "Auto"
        self._quality_label=label
        # A manual choice overrides the adaptive Auto scale; picking Auto re-adapts.
        self._auto_scale=None
        self.playback_settings.quality_mode=label
        self.preview_panel.set_quality_label(label)
        if label in QUALITY_PERCENT: self.engine.preview_quality.set(QUALITY_PERCENT[label])
        if save: self._settings.setValue("previewQuality",label)
        self._request_preview()

    INTERACTIVE_SCALE = 0.5      # preview scale while a transform drag is in progress

    def _interacting(self):
        """True while the user is dragging on the picture (transform, warp, pin, path)."""
        overlay = getattr(self, "overlay", None)
        return bool(overlay is not None and getattr(overlay, "dragging", lambda: False)())

    def preview_scale(self):
        """Render scale for the preview: quality percent (or the panel fit for Auto), never above 1.0.

        Auto starts at the widget fit and then adapts: when frames take too long to render
        the scale drops (half, quarter, eighth) and it climbs back when the renderer keeps
        up again. The project and the export are never touched - only the preview.
        """
        if getattr(self,"_quality_label","Auto")=="Auto":
            if getattr(self,"_auto_scale",None) is not None:
                scale = float(self._auto_scale)
            else:
                pw,ph=self.engine.project_size(); view=self.preview_panel.view
                fit=max(view.width()/max(1,pw),view.height()/max(1,ph))
                scale = max(0.25,min(1.0,round(fit*20)/20.0))
        else:
            scale = max(0.1, min(1.0, float(self.engine.preview_quality.get()) / 100.0))
        # Interactive frames are half-resolution: a drag re-renders every few milliseconds
        # and the release re-renders once at full quality, so the picture keeps up with the
        # pointer without ever showing a soft frame after the gesture ends.
        if self._interacting():
            scale = min(scale, self.INTERACTIVE_SCALE)
        return scale

    def _adapt_preview_quality(self, elapsed_ms):
        """Auto quality: step the preview scale down when a frame is too slow for the fps.

        Only Auto mode adapts, and only between interactive renders - a cached or paused
        frame never changes the scale. Faster than ~half a frame time climbs back up.
        """
        if getattr(self,"_quality_label","Auto")!="Auto" or elapsed_ms<=0:
            return
        if self._interacting():
            # Interactive frames are deliberately half-size: their timing says nothing
            # about the scale the paused preview should use.
            return
        target_ms=1000.0/max(1.0,float(self.engine.playback_fps.get()))
        current=self.preview_scale()
        limits=(1.0,0.75,0.5,0.25,0.125)
        if elapsed_ms>target_ms*2.2 and current>limits[-1]:
            nxt=max(step for step in limits if step<current-1e-6)
            self._auto_scale=nxt
            self._show_quality_hint(f"Preview quality reduced to {int(nxt*100)}% (auto)")
        elif elapsed_ms<target_ms*0.55:
            # Climb back towards full quality. This used to test current<limits[0],
            # which is never true (limits[0] is 1.0), so Auto could only degrade and
            # stayed blurry for the rest of the session.
            larger=[step for step in limits if step>current+1e-6]
            if larger:
                self._auto_scale=min(larger)

    def _show_quality_hint(self, message):
        now=time.monotonic()
        if now-getattr(self,"_quality_hint_at",0.0)<4.0:
            return
        self._quality_hint_at=now
        self.toast_message(message,duration_ms=2600)

    def _request_preview(self):
        """Show the playhead frame: from the cache when possible, otherwise render it.

        The GUI thread never renders here. A hit displays immediately; a miss marks the
        frame pending (the cache bar turns yellow) and hands it to the render thread,
        which keeps only the newest interactive request. Nothing is discarded because
        "the playhead moved on" - the newest finished frame is always shown, which is
        what used to leave the picture frozen on one frame during playback.
        """
        if not self.engine.has_media():
            return
        frame=int(self.engine.session.playhead); scale=self.preview_scale()
        cached=self.frame_cache.get(frame,scale)
        if cached is not None:
            self._show_preview(frame,cached,scale)
            return
        self.frame_cache.mark_pending(frame,scale)
        self.preview_generation+=1; self._requested_frame=frame; self._preview_dirty=True
        self._render_revisions[(frame, float(scale))] = self._content_revision
        self.renderer.request(frame,scale)

    def _on_preview_failed(self,error):
        """A failed render must release the busy flag, or previews stop for good."""
        self.preview_busy=False
        self._preview_dirty=False
        self.bridge.status.emit(f"Preview error: {error}")

    # ------------------------------------------------------------ project hooks
    def collect_ui_state(self):
        """(ui dict, dock/window state bytes) saved inside the project."""
        zoom,scroll=self.timeline_panel.zoom_state()
        ui={"layout_version":dock_layout.LAYOUT_VERSION,"track_height":self.timeline_panel.canvas.track_height_value,
            "timeline_zoom":float(zoom),"timeline_scroll":float(scroll),"playhead":int(self.engine.session.playhead)}
        return ui,bytes(self.saveState().data())

    def restore_ui_state(self,project):
        """Apply the layout saved with a project; an old layout version leaves the current docks alone."""
        ui=project.ui or {}
        if project.window_state and ui.get("layout_version")==dock_layout.LAYOUT_VERSION:
            from PyQt6.QtCore import QByteArray
            self.restoreState(QByteArray(project.window_state))
        try:track_height=int(ui.get("track_height",0))
        except (TypeError,ValueError):track_height=0
        if track_height:self.timeline_panel.canvas.set_track_height(track_height)
        try:zoom=float(ui.get("timeline_zoom",0))
        except (TypeError,ValueError):zoom=0.0
        if zoom>0:self.timeline_panel.restore_zoom_state(zoom,ui.get("timeline_scroll",0))
        self.engine.session.set_playhead(max(0,int(ui.get("playhead",0) or 0)),silent=True)
        self.loop_end_action.blockSignals(True); self.loop_end_action.setChecked(self.engine.timeline.loop_at_end)
        self.loop_end_action.blockSignals(False)

    def load_sample_media(self):
        self._load_demo_project()

    def _fill_recent_menu(self):
        from ..project import storage
        menu=self._menu_recent; menu.clear()
        recents=storage.load_recents()
        for path in recents[:10]:
            label=os.path.splitext(os.path.basename(path))[0]
            action=menu.addAction(label); action.setToolTip(path)
            action.triggered.connect(lambda _c=False,p=path:self._open_recent(p))
        if not recents:menu.addAction("(none)").setEnabled(False)
        else:
            menu.addSeparator(); menu.addAction("Clear list",lambda:[storage.remove_recent(p) for p in recents])

    def _open_recent(self,path):
        from ..project import storage
        if not os.path.isfile(path):
            storage.remove_recent(path); self.toast_message("That project no longer exists",kind="error"); return
        self.projects.open_path(path)

    def action_new_project(self):
        from .project_dialogs import NewProjectDialog
        if not self.projects.maybe_save():return
        dialog=NewProjectDialog(self)
        if dialog.exec():self.projects.create_project(dialog.settings(),dialog.location())

    def show_preferences(self, section=None):
        from .start_window import PreferencesDialog
        PreferencesDialog(self, section=section).exec()

    def _reload_effect_previews(self):
        """A new reference image or quality: rebuild the browser's thumbnails."""
        panel = getattr(self, "effects_panel", None)
        browser = getattr(panel, "browser", None)
        if browser is not None:
            browser.reload_previews()

    def show_project_settings(self):
        from .project_dialogs import ProjectSettingsDialog
        if not self.projects.is_open:return
        dialog=ProjectSettingsDialog(self.projects.project.settings,self)
        if dialog.exec():self.projects.apply_settings(dialog.settings())

    def _load_demo_project(self):
        """Recreate the starter two-video/two-audio layout when the sample media exists."""
        base=os.path.dirname(DEFAULT_INPUT) if DEFAULT_INPUT else ""
        candidates=[]
        if DEFAULT_INPUT and os.path.exists(DEFAULT_INPUT):candidates.append(DEFAULT_INPUT)
        if base and os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                path=os.path.join(base,name)
                if path not in candidates and name.lower().endswith((".gif",".png",".jpg",".jpeg",".mp4")):
                    candidates.append(path)
        videos=[p for p in candidates if p.lower().endswith((".gif",".mp4"))]
        images=[p for p in candidates if p not in videos]
        if not videos and not images:
            self.bridge.status.emit("Ready — import media to begin")
            return
        timeline=self.engine.timeline
        while len(timeline.tracks_of_type("video"))<2:timeline.add_track("video")
        while len(timeline.tracks_of_type("audio"))<2:timeline.add_track("audio")
        audio1,audio2=timeline.tracks_of_type("audio")[:2]
        fps=self.engine.timeline.fps
        demo=ensure_demo_audio(str(PROJECT_ROOT/"demo_audio"))
        if demo.get("bed"):
            self.engine.session.add_media([{"name":"Music bed","path":demo["bed"],"duration":fps*14,
                                            "type":"audio"}],0,audio1)
            self.engine.session.add_media([{"name":"Voice","path":demo["voice"],"duration":fps*4,
                                            "type":"audio"}],fps,audio2)
            self.engine.session.add_media([{"name":"SFX hit","path":demo["hit"],"duration":fps*2,
                                            "type":"audio"}],fps*7,audio2)
        else:
            self.bridge.status.emit("Demo audio could not be created - audio tracks stay empty")
        if videos:
            demo_context=(0,self.engine.timeline.tracks_of_type("video")[-1],True)
            for path in videos[:2]:
                self._import_context_by_path[path]=demo_context; self._demo_import_paths.add(path)
            job=ImportJob(videos[:2]); job.signals.ready.connect(self.bridge.import_ready.emit)
            job.signals.failed.connect(self.bridge.import_failed.emit); self.pool.start(job)
        if images:
            self._demo_image_queue=images[:3]
            self._import_next_demo_image(2*fps,self.engine.timeline.tracks_of_type("video")[0])
        self.bridge.status.emit("Demo project loaded")

    def _import_next_demo_image(self,start,track):
        queue=getattr(self,"_demo_image_queue",[])
        if not queue:return
        path=queue.pop(0)
        self._import_context_by_path[path]=(start,track,False); self._demo_import_paths.add(path)
        job=ImportJob([path]); job.signals.ready.connect(self.bridge.import_ready.emit)
        job.signals.failed.connect(self.bridge.import_failed.emit); self.pool.start(job)
        if queue:QTimer.singleShot(250,lambda:self._import_next_demo_image(start+4*self.engine.timeline.fps,track))

    def browse_input(self):
        paths=open_files(self,"Import media",os.path.dirname(self.engine.input_path.get()),
            "Media (*.png *.jpg *.jpeg *.bmp *.webp *.gif *.mp4 *.avi *.mov *.mkv *.webm *.m4v *.wav *.mp3 *.ogg *.flac *.aac *.m4a);;All files (*)")
        if paths:self.import_paths(paths,self.engine.session.playhead,find_free=True)

    # ---------------------------------------------------- project media -> timeline
    def _has_audio(self,path):
        """Does this file carry sound? Header-only probe, memoised per path."""
        return has_audio_stream(path,self._audio_probe_cache)

    def _media_entry(self,item):
        """Timeline entry for one project media item.

        The library already stores duration/fps from import or probing; the file header is
        read only when that is still unknown. Videos get an `audio` flag so the timeline can
        create the linked audio clip in the same operation.
        """
        fps=max(1,int(self.engine.timeline.fps)); kind=item.type
        if kind=="image":
            return {"name":item.name,"path":item.path,"duration":fps*3,"type":"video","audio":False}
        seconds=float(item.duration or 0.0); source_fps=float(item.fps or fps) or float(fps)
        if seconds<=0:
            try:
                probed_kind,count,probed_fps=probe_media_header(item.path)
                source_fps=float(probed_fps or source_fps) or float(fps)
                seconds=(count/max(1e-9,source_fps)) if probed_kind!="image" else 0.0
                item.duration,item.fps=seconds,source_fps
            except Exception:
                seconds=0.0
        duration=max(1,int(round(seconds*fps))) if seconds>0 else fps*5
        entry={"name":item.name,"path":item.path,"duration":duration,
               "type":"audio" if kind=="audio" else "video","source_fps":source_fps,
               "source_out":max(1,int(round((seconds or duration/float(fps))*source_fps)))}
        if kind!="audio":
            entry["audio"]=self._has_audio(item.path)
        return entry

    def _media_entries_for(self,ids):
        """Entries for a drag preview: the canvas draws them, it never touches the library."""
        project=self.projects.project
        if project is None:return []
        entries=[]
        for mid in ids:
            item=project.library.items.get(mid)
            if item is not None:entries.append(self._media_entry(item))
        return entries

    def insert_media_items(self,items,frame=None,track=None,find_free=False):
        """Put project media on the timeline by id, as one undoable step.

        A video with sound becomes two linked clips; if the pair cannot be placed (locked
        lane) nothing is inserted at all.
        """
        project=self.projects.project
        if project is None:return []
        entries=[];missing=[]
        for item in items:
            if item.path and not os.path.exists(item.path):missing.append(item.name); continue
            entries.append(self._media_entry(item))
        if missing:
            self.toast_message(f"{missing[0]} is missing - use Relink in the Project panel",kind="error")
        if not entries:return []
        ids=self.engine.session.add_media(entries,frame,track,find_free,reveal=frame is not None)
        if not ids:
            self.toast_message("Could not insert: the target track is locked",kind="error",duration_ms=3200)
            return []
        self._refresh_panels(); self._refresh_audio(); self._request_preview()
        self.timeline_panel.canvas.update()
        return ids

    def _media_ids_dropped(self,ids,frame,track):
        """A drop from the Project panel: media ids, already imported, no re-decoding."""
        project=self.projects.project
        if project is None:return
        items=[project.library.items[mid] for mid in ids if mid in project.library.items]
        if items:
            self.insert_media_items(items,frame,track)

    def import_paths(self,paths,at_frame=None,track=None,find_free=False):
        from ..constants import AUDIO_EXT,IMAGE_EXT,VIDEO_EXT
        video=[]; audio=[]; ignored=[]; placed_audio=0
        for path in paths:
            ext=os.path.splitext(path)[1].lower()
            if not os.path.isfile(path) or ext not in VIDEO_EXT+IMAGE_EXT+AUDIO_EXT:ignored.append(os.path.basename(path)); continue
            if ext in AUDIO_EXT:
                if self.waveforms.library.cached(path) is not None:
                    # Already decoded in this session: no decode signal will arrive again,
                    # so place it now instead of leaving it stuck in the pending map.
                    self._place_audio(path,at_frame,track,find_free)
                    placed_audio+=1
                else:
                    # Decode first: the clip length must come from the real file, not a guess.
                    self._pending_audio.setdefault(path,[]).append((at_frame,track,find_free))
                    self.waveforms.request(path)
                    audio.append(path)
            else:video.append(path)
        if placed_audio:
            self._refresh_audio(); self.timeline_panel.canvas.update()
        if video:
            for path in video:
                self._import_context_by_path[path]=(at_frame,track,find_free)
                self._import_audio_by_path[path]=self._has_audio(path)
            job=ImportJob(video); job.signals.ready.connect(self.bridge.import_ready.emit); job.signals.failed.connect(self.bridge.import_failed.emit); self.pool.start(job)
        if ignored:self.bridge.status.emit("Ignored unsupported: "+", ".join(ignored[:3]))
        elif audio and not video:self.bridge.status.emit(f"Decoding {len(audio)} audio file(s)…")

    def _on_import_ready(self,path,kind,frames,fps):
        try:
            at_frame,track,find_free=self._import_context_by_path.pop(path,(self.engine.session.playhead,None,True))
            # Startup demo media must not scroll the timeline away from the playhead.
            reveal=path not in self._demo_import_paths
            if kind=="image":
                self.engine.add_decoded_image(path,kind,frames,fps,at_frame,track,find_free,reveal=reveal)
            else:
                audio=self._import_audio_by_path.pop(path,False)
                self.engine.add_decoded_media(path,kind,frames,fps,at_frame,track,find_free,
                                              reveal=reveal,audio=audio)
            self.bridge.status.emit(f"Imported: {os.path.basename(path)}")
            self._refresh_panels(); self._request_preview()
        except Exception as exc:self._show_error(f"Could not add {path}: {exc}")

    def _record_button_clicked(self):
        """Record opens the panel; while recording, the same button stops the take."""
        recorder = getattr(self, "recorder", None)
        if recorder is not None and recorder.state != "idle":
            recorder.stop()
            return
        self.open_recorder()

    def set_recording_status(self, seconds=None):
        """In-app recording indicator: the button shows the elapsed time and stops the take."""
        button = getattr(self, "record_button", None)
        if button is None:
            return
        if seconds is None:
            button.setText("Record")
            button.setToolTip("Record webcam, screen or microphone")
            return
        minutes, secs = divmod(max(0, int(seconds)), 60)
        button.setText(f"\u25cf {minutes:02d}:{secs:02d}")
        button.setToolTip("Recording - click to stop")

    def open_recorder(self):
        """Open the compact recorder panel (created on first use)."""
        if getattr(self,"recorder",None) is None:
            from .recorder_controller import RecorderController
            self.recorder=RecorderController(self)
        self.recorder.open_panel()

    def toggle_playback(self):
        if self._playing:self.stop_playback(); return
        if self.engine.source_mode.get()=="camera":
            index=self.engine.camera_index.get(); self._camera=cv2.VideoCapture(index)
            if not self._camera.isOpened():self._camera.release(); self._camera=None; self._show_error(f"Unable to open camera index {index}"); return
            self._playing=True; self.camera_timer.start(max(1,int(1000/max(1,self.engine.playback_fps.get())))); self._sync_transport(); return
        self._begin_playback()

    def _play_range(self):
        """Loop region when looping is on and one exists, else the whole project."""
        return self.engine.timeline.play_range()

    def _begin_playback(self,frame=None):
        """Start timeline playback, optionally jumping to `frame` first."""
        if not self.engine.has_media():
            self._sync_transport()
            if not self.engine.timeline.all_clips():
                self.toast_message("Nothing to play - the timeline is empty")
            else:
                self.toast_message("Nothing to play under the playhead - check that a video track is visible")

            return False
        lo,hi=self._play_range()
        if hi<=lo:return False
        if frame is not None:
            self._set_playhead(max(lo,min(hi-1,int(frame))))
        if self.engine.session.playhead>=hi or self.engine.session.playhead<lo:self._set_playhead(lo)
        self._scrub_mode=False
        self._playing=True; self._play_clock=time.perf_counter(); self._play_head=float(self.engine.session.playhead)
        # Playback asks the renderer for interactive frames only: background prerendering
        # yields so it never competes with the picture the user is watching.
        self.metrics.reset(); self._shown_frame=-1
        self.renderer.prepare_for_playback(); self._cancel_prefetch()
        self._sync_transport()
        self._start_audio()
        self.play_timer.start(max(1,int(1000/max(1,self.engine.playback_fps.get())))); self.bridge.status.emit("Playing timeline")
        return True

    def _start_audio(self):
        """Start audio playback; it becomes the master clock when it is running."""
        self._refresh_audio()
        if not self.audio.available:
            self.preview_panel.master.set_unavailable(self.audio.error_message or "no audio output device")
            return False
        if self.engine.reverse_timeline.get() or abs(self.engine.speed_percent.get()-100)>0.5:
            self.bridge.status.emit("Audio muted: reverse and non-100% preview speed play silently")
            return False
        return self.audio.play(self.engine.session.playhead/float(self.engine.timeline.fps))

    def stop_playback(self):
        self._playing=False; self.play_timer.stop(); self.camera_timer.stop(); self._sync_transport()
        self._snippet_timer.stop(); self.audio.pause()
        # Interactive work is over: let background rendering resume and quietly fill the
        # frames just after the playhead for the next play.
        self.renderer.finish_playback(); self.renderer.cancel_interactive()
        # Auto may have reduced the scale to keep playback real-time. That is a playback
        # concession, never the quality of the paused picture: drop back to the panel fit
        # and repaint the current frame at full detail.
        if getattr(self, "_quality_label", "Auto") == "Auto":
            self._auto_scale = None
        # App shutdown calls stop_playback too; there is no point starting a render
        # against widgets that are about to be destroyed.
        if not getattr(self, "_closing", False):
            self._request_preview()
        self._prefetch_timer.start()
        if self._camera is not None:self._camera.release(); self._camera=None
        self.bridge.status.emit("Preview stopped")

    def _tick_camera(self):
        if self._camera is None:return
        ok,frame=self._camera.read()
        if not ok:self.stop_playback(); return
        self.last_frame=frame; self.preview_panel.view.set_frame(frame,frame_key=("camera",self.engine.camera_index.get()))

    def _tick_playback(self):
        fps=self.engine.timeline.fps; lo,hi=self._play_range()
        if hi<=lo:
            self.stop_playback(); return
        direction=-1 if self.engine.reverse_timeline.get() else 1
        # One loop decision: the loop region when one is enabled, otherwise "Loop at End",
        # which makes the playhead jump back to the start and keep playing.
        looping=self.engine.timeline.loop_enabled or self.engine.timeline.loop_at_end
        if self.audio.playing and direction>0 and not self._scrub_mode:
            # Audio is the master clock: video follows the audible position.
            target=self.audio.clock_seconds()*fps
            if target>=hi:
                if looping:
                    self._wrap_playback(lo); target=float(lo)
                else:
                    self.stop_playback(); self._set_playhead(max(lo,hi-1)); return
            self._play_head=target
        else:
            now=time.perf_counter(); dt=now-self._play_clock; self._play_clock=now
            self._play_head+=dt*fps*self.engine.speed_percent.get()/100.0*direction
            if self._play_head>=hi or self._play_head<lo:
                if looping:self._play_head=float(lo if direction>0 else max(lo,hi-1))
                else:self.stop_playback(); self._play_head=float(max(lo,min(hi-1,self._play_head)))
        target=int(self._play_head)
        # Count what the clock asked for; the overlay reports the difference to what was
        # actually shown as dropped frames - both numbers are measured, not estimated.
        self.metrics.note_tick(target)
        self.engine.session.set_playhead(target,silent=True); self.timeline_panel.canvas.update()
        self._update_timecode(); self._request_preview()

    def _wrap_playback(self,lo):
        """Jump back to the loop start by moving the audio origin, not by restarting the sink."""
        self._play_head=float(lo)
        if self.audio.playing:
            self.audio.seek_live(lo/float(self.engine.timeline.fps))

    def open_export_dialog(self):
        if not self.engine.has_media():
            # Not a silent no-op, but still not a modal dialog.
            self.toast_message("Nothing to export - the timeline has no clips")
            return
        from .dialogs import ExportDialog, ExportRangeDialog
        timeline=self.engine.timeline
        region=timeline.loop_range()
        export_range=None
        if region is not None and timeline.loop_enabled:
            # A loop region is active: ask once, then remember the choice for this session.
            if self._export_scope is None:
                chooser=ExportRangeDialog(region[0],region[1],timeline.fps,self)
                if not chooser.exec():return
                self._export_scope=chooser.scope
            if self._export_scope=="region":export_range=region
        dialog=ExportDialog(self.engine,self,export_range=export_range)
        if dialog.exec():self._start_export(dialog.settings())

    def _start_export(self,settings):
        self.stop_playback(); self._cancel_export=threading.Event()
        library=self.waveforms.library
        settings=dict(settings,audio_segments=build_segments(self.engine.timeline,library.cached,library.sample_rate,self.engine.runtime_effects,compositions=getattr(self.engine,"compositions",None)),
                      audio_rate=library.sample_rate)
        self._export_dialog=QProgressDialog("Preparing export...","Cancel",0,100,self); self._export_dialog.setWindowTitle("Export")
        self._export_dialog.setWindowModality(Qt.WindowModality.ApplicationModal); self._export_dialog.canceled.connect(self._cancel_export.set); self._export_dialog.show()
        job=ExportJob(self.engine,settings,self._cancel_export); job.signals.progress.connect(self.bridge.export_progress.emit)
        job.signals.done.connect(self.bridge.export_done.emit); job.signals.failed.connect(self.bridge.export_failed.emit); self.pool.start(job)

    def _on_export_progress(self,index,total,frame):
        if self._export_dialog:self._export_dialog.setMaximum(total); self._export_dialog.setValue(index); self._export_dialog.setLabelText(f"Rendering {index}/{total} frames")
    def _on_export_done(self,result):
        if self._export_dialog:self._export_dialog.close(); self._export_dialog=None
        path,count,fps,size=result
        self.toast_message(f"Exported {count} frames to {os.path.basename(path)}",duration_ms=3200)
    def _on_export_failed(self,error):
        if self._export_dialog:self._export_dialog.close(); self._export_dialog=None
        if error!="Export cancelled":self.toast_message(f"Export failed: {error}",kind="error",duration_ms=4200)

    def save_frame_dialog(self):
        if self.last_frame is None:self.toast_message("No frame to save yet"); return
        path=save_file(self,"Save Frame",self.engine.frame_output_path.get(),
                       "Images (*.png *.jpg *.jpeg *.bmp *.tiff)")
        if path:
            if not os.path.splitext(path)[1]:path+=".png"
            # Re-render at full quality: the preview frame may be a low-res proxy.
            _raw,frame=self.engine.render_frame(self.engine.session.playhead,1.0)
            if not cv2.imwrite(path,frame):self.toast_message("Could not write the frame",kind="error")
            else:self.engine.frame_output_path.set(path)

    def _show_error(self,message):QMessageBox.critical(self,"meeniFX Studios",str(message))
    def show_about(self):AboutDialog(self).exec()

    # ------------------------------------------------------------------ updates
    def show_updates(self,manual=True):
        """Open the Updates page; a manual open also asks GitHub straight away."""
        from .updates_dialog import UpdatesDialog
        if self._updates_dialog is None:
            self._updates_dialog=UpdatesDialog(self.update_manager,self)
            self._updates_dialog.finished.connect(self._forget_updates_dialog)
        self._updates_dialog.refresh()
        self._updates_dialog.show()
        self._updates_dialog.raise_()
        self._updates_dialog.activateWindow()
        if manual:
            self._updates_dialog.check_now()

    def _forget_updates_dialog(self,_result):
        self._updates_dialog=None

    def _on_update_available(self,update):
        """A background check found a newer build: a small notice, never a modal dialog."""
        if self._updates_dialog is not None and self._updates_dialog.isVisible():
            return
        self.update_notice.show_update(update)

    def _remind_update(self):
        self.update_manager.remind_later()

    def _skip_update(self):
        self.update_manager.skip()

    def _acknowledge_whats_new(self,version_text):
        self.update_manager.settings.acknowledge_whats_new(version_text)
        self.update_manager.settings.save(self.update_manager.store)

    def show_whats_new(self,version_text=None,notes=None,date_text=""):
        """Open the release notes; Help shows the last acknowledged version."""
        from ..updates import whats_new as notes_module
        from .whats_new import WhatsNewDialog
        from .updates_dialog import friendly_date
        settings=self.update_manager.settings
        version_text=version_text or settings.pending_whats_new or settings.last_ack_version or VERSION
        update=self.update_manager.pending_update
        release_url=None
        if update is not None and update.version_text==version_text:
            notes=notes or update.notes
            date_text=date_text or friendly_date(update.published_at)
            release_url=update.html_url
        dialog=WhatsNewDialog(version_text,notes_module.notes_text(version_text,notes),self,
                              date_text=date_text,
                              release_url=release_url,
                              acknowledge=self._acknowledge_whats_new)
        dialog.exec()

    # ------------------------------------------------------- proxy media
    def _ensure_proxies(self,delay_ms=0):
        """Queue a background proxy encode for heavy sources missing one.

        Proxies are preview-only (the exporter ignores them) and live in the project's
        disposable ``Cache/proxy`` folder, so deleting them costs only regeneration.
        """
        if not self.playback_settings.auto_proxy or not self.engine.has_media():
            return 0
        from ..media.proxy import is_proxyable, make_proxy, proxy_folder, proxy_path
        folder=proxy_folder(getattr(self.engine,"project_folder","") or "")
        if not folder:
            return 0
        width=int(self.playback_settings.proxy_width)
        pending={}
        for clip in self.engine.timeline.all_clips():
            path=getattr(clip,"source_path","") or ""
            if path in pending or not is_proxyable(path):
                continue
            target=proxy_path(path,folder,width)
            if target and not os.path.exists(target):
                pending[path]=target
        if not pending:
            return 0
        self.bridge.status.emit(f"Generating {len(pending)} preview proxy file(s) in the background")
        for path,target in list(pending.items())[:4]:
            job=FunctionJob(lambda p=path,t=target: make_proxy(p,t,width))
            job.signals.done.connect(lambda _result:self._on_proxy_ready())
            self.pool.start(job)
        return len(pending)

    def _on_proxy_ready(self):
        """A proxy appeared: let MediaCache re-resolve and show the new preview frame."""
        self.engine.media._resolved.clear()
        self.frame_cache.clear(disk=False)
        self._request_preview()
        self._on_cache_changed()

    # ---------------------------------------------------- preview cache controls
    def _range_to_render(self, region):
        """Frames to prerender: the loop region or the selection, else the whole timeline."""
        lo,hi=self._play_range()
        if not region:
            return int(lo),int(hi)
        timeline=self.engine.timeline
        if (bool(timeline.loop_enabled) and timeline.loop_in is not None
                and timeline.loop_out is not None):
            start,end=sorted((int(timeline.loop_in),int(timeline.loop_out)))
            if end>start:
                return start,end
        spans=[(int(clip.timeline_start),int(clip.end)) for clip in
               (timeline.find_clip(cid) for cid in sorted(self.engine.session.selected))
               if clip is not None]
        if spans:
            return min(s[0] for s in spans),max(s[1] for s in spans)
        return None

    def render_preview(self, region=False):
        """Prerender a range into the preview cache, in the background, with progress."""
        if not self.engine.has_media():
            self.toast_message("Nothing to render - import media first")
            return 0
        span=self._range_to_render(region)
        if span is None:
            self.toast_message("Set a loop region or select clips to render a region")
            return 0
        lo,hi=span
        if hi<=lo:
            self.toast_message("Nothing to render in that range")
            return 0
        scale=self.preview_scale()
        frames=[frame for frame in range(lo,hi) if not self.frame_cache.has(frame,scale)]
        if not frames:
            self.toast_message("That range is already cached")
            return 0
        self._prerender_failed=False
        self.renderer.set_background_enabled(True)
        # Yellow means "queued or rendering", so the bar shows this work immediately.
        self.frame_cache.mark_pending_many(frames,scale)
        total=self.renderer.queue_range(frames,scale)
        if not total:
            self.toast_message("Could not start preview rendering")
            return 0
        from PyQt6.QtWidgets import QProgressDialog
        dialog=QProgressDialog(f"Rendering preview ({len(frames)} frames)...","Cancel",0,len(frames),self)
        dialog.setWindowTitle("Render Preview"); dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setMinimumDuration(0); dialog.setAutoClose(False); dialog.setAutoReset(False)
        dialog.canceled.connect(self.cancel_preview_render)
        self._prerender_total=len(frames); self._prerender_done_count=0
        self._prerender_dialog=dialog
        dialog.show()
        self.bridge.status.emit(f"Rendering {len(frames)} preview frames")
        return len(frames)

    def cancel_preview_render(self):
        """Stop background prerendering immediately; finished frames stay cached."""
        self.renderer.cancel_background()
        self.frame_cache.clear_pending()
        dialog=getattr(self,"_prerender_dialog",None)
        if dialog is not None:
            dialog.close(); self._prerender_dialog=None
        self.toast_message("Preview rendering cancelled",duration_ms=1800)

    def clear_preview_cache(self):
        """Drop every cached preview frame; the disk Cache folder is emptied too."""
        self.renderer.cancel_background(); self.renderer.cancel_interactive()
        self.frame_cache.clear(disk=True)
        self._on_cache_changed()
        self.toast_message("Preview cache cleared",duration_ms=1800)

    def _on_prerendered(self, frame, scale):
        self.frame_cache.clear_pending(frame,scale)
        if self._prerender_dialog is None:
            return
        self._prerender_done_count=getattr(self,"_prerender_done_count",0)+1
        self._prerender_dialog.setValue(min(self._prerender_done_count,self._prerender_total))
        self._prerender_dialog.setLabelText(
            f"Rendering preview... {self._prerender_done_count}/{self._prerender_total}")

    def _on_prerender_done(self, rendered, total):
        self.frame_cache.clear_pending()
        dialog=getattr(self,"_prerender_dialog",None)
        if dialog is not None:
            dialog.close(); self._prerender_dialog=None
        # Silence by default: the timeline's cache indicator already says the frames are
        # there, and a popup on every background batch interrupts editing. Users who want
        # the confirmation can turn it on in Preferences > Playback & Performance.
        if rendered and getattr(self.playback_settings,"cache_notifications",False):
            self.toast_message(f"Preview cache ready: {rendered} frames",duration_ms=2200)
        if self._prerender_failed:
            self._prerender_failed=False

    def _cancel_prefetch(self):
        if getattr(self,"_prefetch_timer",None) is not None:
            self._prefetch_timer.stop()

    def _auto_prefetch(self):
        """Quietly cache the next few seconds after the playhead while the editor idles."""
        if self._playing or not self.playback_settings.background_render:
            return
        if not self.engine.has_media() or self.renderer.background_pending:
            return
        lo,hi=self._play_range(); scale=self.preview_scale()
        ahead=int(max(0,self.playback_settings.prefetch_seconds)*self.engine.timeline.fps)
        start=max(lo,int(self.engine.session.playhead))
        frames=[frame for frame in range(start,min(hi,start+ahead))
                if not self.frame_cache.has(frame,scale)]
        if frames:
            self.frame_cache.mark_pending_many(frames,scale)
            self.renderer.queue_range(frames,scale)

    def set_perf_overlay_visible(self, on):
        on=bool(on)
        self.playback_settings.show_overlay=on
        # The overlay paints from this flag; the menu, the shortcut and the saved
        # preference all changed while the picture stayed exactly as it was before.
        self.perf_overlay.visible=on
        action=getattr(self,"perf_overlay_action",None)
        if action is not None and action.isChecked()!=on:
            action.blockSignals(True); action.setChecked(on); action.blockSignals(False)
        self.preview_panel.view.update()
        self.playback_settings.save()
        if on:
            self.toast_message("Performance overlay on: real FPS, drops, cache, backend",duration_ms=2400)

    # ------------------------------------------------- playback/performance settings
    def _apply_playback_settings(self, settings, save=True):
        """Push a PlaybackSettings record into the live renderer, cache and backend."""
        self.playback_settings=settings
        render_backend.set_mode(settings.gpu_mode)
        self.frame_cache.set_budgets(settings.ram_cache_mb,settings.disk_cache_mb)
        self.renderer.set_background_enabled(bool(settings.background_render))
        self.perf_overlay.visible=bool(settings.show_overlay)
        if getattr(self,"preview_panel",None) is not None:
            if getattr(self,"_quality_label",None)!=settings.quality_mode:
                self.set_preview_quality(settings.quality_mode,save=False)
            self.preview_panel.view.update()
        if getattr(self,"perf_overlay_action",None) is not None:
            action=self.perf_overlay_action
            if action.isChecked()!=bool(settings.show_overlay):
                action.blockSignals(True); action.setChecked(bool(settings.show_overlay)); action.blockSignals(False)
        self.engine.set_proxy_settings(settings.auto_proxy,settings.proxy_width,
                                       settings.hardware_decode)
        # Hardware encoding is verified lazily by the exporter itself (it falls back to
        # the OpenCV writer when NVENC is not usable), so only the preference is set here.
        self.engine.hardware_encode=settings.hardware_encode
        # The audio gain ceiling is a real clamp used by the mixer and the envelopes.
        from ..model import animation as animation_model
        animation_model.set_gain_ceiling(settings.gain_headroom_db)
        if getattr(self, "effects_panel", None) is not None:
            # Force the audio card's slider range and the envelope scale to rebuild.
            self.effects_panel._built_key = None
        # A changed waveform mode must show immediately, not after the next repaint causes.
        if getattr(self, "timeline_panel", None) is not None:
            self.timeline_panel.canvas.update()
        actions = getattr(self, "_wave_actions", None)
        if actions:
            action = actions.get(settings.waveform_mode)
            if action is not None and not action.isChecked():
                action.setChecked(True)
        self._open_cache_folder()
        if save:
            settings.save()

    # --------------------------------------------------------- composition editing
    @property
    def editing_composition(self):
        """The composition currently open for editing, or None at the main timeline."""
        if not self._composition_path:
            return None
        return self.engine.compositions.get(self._composition_path[-1])

    def _composition_chain(self):
        return [comp for comp in (self.engine.compositions.get(cid)
                                  for cid in self._composition_path) if comp is not None]

    def _remember_root_state(self):
        """Capture what the main timeline renders with before entering a composition."""
        if self._composition_path:
            return
        self._root_render_state = {
            "source_width": self.engine.source_width,
            "source_height": self.engine.source_height,
            "source_fps": self.engine.source_fps,
            "background_bgr": self.engine.background_bgr,
            "project_size": self.engine.session.project_size,
        }

    def _build_composition_bar(self):
        """A breadcrumb above the panels: the user always knows what is being edited."""
        from PyQt6.QtWidgets import QLabel, QToolBar
        bar = QToolBar("Composition", self)
        bar.setObjectName("compositionBar")
        bar.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)
        self.composition_bar = bar
        self.frame_cache.changed.connect(self._update_composition_cache_label)
        self._sync_composition_bar()

    def _sync_composition_bar(self):
        from PyQt6.QtWidgets import QLabel
        bar = getattr(self, "composition_bar", None)
        if bar is None:
            return
        bar.clear()
        chain = self._composition_chain()
        # `clear()` drops widget actions, so the labels are rebuilt with the breadcrumb.
        label = QLabel("Editing: " + (chain[-1].name if chain else "Main Timeline"))
        label.setContentsMargins(6, 0, 6, 0)
        label.setToolTip("The timeline every panel, the preview and the export use right now")
        bar.addWidget(label)
        self.composition_context_label = label
        bar.addSeparator()
        root = bar.addAction("Main Timeline")
        root.setToolTip("Go back to the main timeline")
        root.triggered.connect(self._go_to_root)
        for index, comp in enumerate(chain):
            bar.addAction(" / ")
            crumb = bar.addAction(comp.name)
            crumb.setToolTip(f"Editing {comp.name} ({comp.width}x{comp.height}, {comp.fps} fps)")
            crumb.triggered.connect(lambda _checked=False, i=index:
                                    self._go_to_composition_index(i))
        if chain:
            bar.addSeparator()
            back = bar.addAction("Back to Parent")
            back.setToolTip("Leave this composition and return to the one that contains it")
            back.triggered.connect(self.back_to_parent)
            settings_action = bar.addAction("Settings...")
            settings_action.triggered.connect(self.show_composition_settings)
        bar.addSeparator()
        cache_label = QLabel("")
        cache_label.setProperty("quiet", True)
        cache_label.setToolTip("Preview frames cached for this timeline")
        bar.addWidget(cache_label)
        self.composition_cache_label = cache_label
        self._update_composition_cache_label()
        self.setWindowTitle(f"{APP_TITLE} - {chain[-1].name}" if chain else APP_TITLE)

    def _update_composition_cache_label(self):
        label = getattr(self, "composition_cache_label", None)
        if label is None:
            return
        stats = self.frame_cache.stats()
        cached = int(stats.get("ram_frames", 0)) + int(stats.get("disk_frames", 0))
        pending = int(stats.get("pending", 0))
        label.setText(f"{cached} cached" + (f" - {pending} rendering" if pending else ""))

    def open_composition(self, comp_id):
        """Open a composition for editing, keeping the path back to the parent."""
        comp = self.engine.compositions.get(comp_id)
        if comp is None:
            self.toast_message("That composition no longer exists", kind="error")
            return False
        if str(comp.id) in self._composition_path:
            self.toast_message("That composition is already open", duration_ms=1600)
            return False
        self._remember_root_state()
        self._composition_path.append(comp.id)
        self._apply_edit_context()
        self.toast_message(f"Editing composition: {comp.name}", duration_ms=1600)
        return True

    def open_composition_clip(self, clip_id):
        clip = self.engine.timeline.find_clip(clip_id)
        if clip is None or not getattr(clip, "is_composition", False):
            return False
        return self.open_composition(clip.composition_id)

    def back_to_parent(self):
        if not self._composition_path:
            return False
        self._composition_path.pop()
        self._apply_edit_context()
        return True

    def _go_to_root(self):
        if not self._composition_path:
            return False
        self._composition_path = []
        self._apply_edit_context()
        return True

    def _go_to_composition_index(self, index):
        if index < 0 or index >= len(self._composition_path):
            return False
        self._composition_path = self._composition_path[:index + 1]
        self._apply_edit_context()
        return True

    def _apply_edit_context(self):
        """Point every panel, the preview and the audio at the timeline being edited."""
        session = self.engine.session
        self.stop_playback()
        self.renderer.cancel_interactive()
        self.renderer.cancel_background()
        comp = self.editing_composition
        if comp is None:
            timeline = self.engine.root_timeline
            state = self._root_render_state or {}
            if state:
                self.engine.source_width = state["source_width"]
                self.engine.source_height = state["source_height"]
                self.engine.source_fps = state["source_fps"]
                self.engine.background_bgr = state["background_bgr"]
                session.project_size = state["project_size"]
        else:
            timeline = comp.timeline
            self.engine.source_width, self.engine.source_height = comp.width, comp.height
            self.engine.source_fps = float(comp.fps)
            self.engine.background_bgr = tuple(comp.background)
            session.project_size = (comp.width, comp.height)
        # Wait for any in-flight nested render to restore its context before pointing the
        # engine at another timeline, and hold the lock while every panel follows.
        with self.engine.render_lock:
            self.engine.timeline = timeline
            session.timeline = timeline
            session.root_timeline = self.engine.root_timeline
            session.compositions = self.engine.compositions
            # The canvas caches the timeline it was built with; without this it would keep
            # drawing the main timeline while the breadcrumb said otherwise.
            canvas = self.timeline_panel.canvas
            canvas.timeline = timeline
            canvas.session = session
            canvas._hover_clip = None
            canvas._drag = None
            canvas._ghost = None
            canvas._band = None
        session.set_selection(set())
        session.clear_key_selection()
        session.set_playhead(0)
        # Frame numbers now belong to another timeline: cached pictures must not leak
        # across the switch, and in-flight renders are already stale.
        self._content_revision += 1
        self._shown_frame = -1
        self.frame_cache.clear(disk=False)
        self._clip_digests = timeline_digests(timeline, self.engine.compositions)
        self._open_cache_folder()
        self._on_cache_changed()
        self.project_panel.rebuild(force=True)
        self.effects_panel.refresh_properties()
        self.keyframe_editor.refresh()
        self.graph_editor.refresh()
        session.changed.emit()
        self._sync_composition_bar()
        self._request_preview()
        self._refresh_audio()

    def _composition_dropped(self, composition_ids, frame, track):
        """A composition dragged from the Project panel becomes one nested clip."""
        placed = 0
        at = max(0, int(frame or 0))
        # `engine.timeline` is briefly a composition while a nested preview renders;
        # the session's timeline is the one the user is editing.
        timeline = self.engine.session.timeline
        for comp_id in composition_ids:
            clip_id = self.engine.session.insert_composition(comp_id, at, track)
            if clip_id is None:
                continue
            clip = timeline.find_clip(clip_id)
            if clip is not None:
                at = int(clip.end)
            placed += 1
        if placed:
            self._request_preview()
            self._refresh_audio()
        return placed

    def _populate_open_composition_menu(self):
        menu = getattr(self, "_open_comp_menu", None)
        if menu is None:
            return
        menu.clear()
        comps = sorted(self.engine.compositions.all(), key=lambda comp: comp.name.lower())
        if not comps:
            entry = menu.addAction("No compositions yet - use Precompose")
            entry.setEnabled(False)
            return
        for comp in comps:
            entry = menu.addAction(comp.name)
            entry.setCheckable(True)
            entry.setChecked(comp.id in self._composition_path)
            entry.triggered.connect(lambda _checked=False, cid=comp.id:
                                    self.open_composition(cid))

    def show_precompose_dialog(self):
        """Name the new composition, then move the selected clips into it."""
        session = self.engine.session
        problem = session.precompose_problem()
        if problem:
            self.toast_message(problem, kind="error", duration_ms=3000)
            return None
        clips = session._clips_for(None)
        start = min(int(clip.timeline_start) for clip in clips)
        end = max(int(clip.end) for clip in clips)
        from .composition_dialogs import PrecomposeDialog
        dialog = PrecomposeDialog(self, name="Composition", clip_count=len(clips),
                                  range_text=f"frames {start}-{end}")
        if dialog.exec():
            nested = session.precompose(dialog.values()["name"])
            if nested is not None:
                self._request_preview()
                self._refresh_audio()
            return nested
        return None

    def show_composition_settings(self):
        """Edit the open composition, or the one referenced by the selected clip."""
        target = self.editing_composition
        if target is None:
            for clip_id in self.engine.session.selected:
                clip = self.engine.timeline.find_clip(clip_id)
                if clip is not None and getattr(clip, "is_composition", False):
                    target = self.engine.compositions.get(clip.composition_id)
                    break
        if target is None:
            self.toast_message("Open a composition or select a composition clip first",
                               kind="error", duration_ms=2600)
            return None
        from .composition_dialogs import CompositionSettingsDialog
        dialog = CompositionSettingsDialog(self, target)
        if dialog.exec():
            self.engine.session.set_composition_settings(target.id, **dialog.values())
            self._apply_edit_context()
            return True
        return None

    def rebuild_composition_cache(self):
        """Throw the cached frames away and render this timeline again from scratch."""
        self.clear_preview_cache()
        return self.render_preview(False)

    def _cache_folder(self):
        """<project>/Cache/preview, or the configured folder when one is set."""
        project=getattr(self.projects,"project",None)
        folder=self.playback_settings.cache_folder
        if folder:
            base=os.path.expandvars(os.path.expanduser(folder))
            name=(project.name if project is not None else "preview") or "preview"
            return os.path.join(base,name)
        if project is not None and getattr(project,"folder",""):
            base=os.path.join(project.folder,"Cache","preview")
            # Each editing context gets its own folder: frame numbers belong to one
            # timeline, so a composition's cache can never be mistaken for the root's.
            if self._composition_path:
                return os.path.join(base,self._composition_path[-1])
            return base
        return ""

    def _open_cache_folder(self):
        folder=self._cache_folder()
        if folder==self.frame_cache.folder:
            return
        self._cache_dir=folder
        self.frame_cache.open_folder(folder,fingerprint(
            self.engine.timeline,self.engine.project_size(),self.engine.background_bgr,
            getattr(self.engine,"compositions",None)))
        self._on_cache_changed()

    def _on_project_changed(self):
        """A project was opened, saved or closed: re-point and validate the disk cache."""
        # Opening or closing a project always returns to the main timeline: the old
        # composition objects are gone with the previous project.
        if self._composition_path:
            self._composition_path=[]
        self._root_render_state=None
        self.engine.session.root_timeline=self.engine.root_timeline
        self.engine.session.timeline=self.engine.timeline
        self.engine.session.compositions=getattr(self.engine,"compositions",None)
        # A loaded project replaces the timeline's contents; the canvas must follow.
        self.timeline_panel.canvas.timeline=self.engine.timeline
        self._clip_digests=timeline_digests(self.engine.timeline,getattr(self.engine,"compositions",None))
        self._sync_composition_bar()
        self._open_cache_folder()
        self._on_cache_changed()
        self._prefetch_timer.start()
        # Proxies for heavy sources are generated after the window has settled, so
        # opening a project never waits for an encode.
        QTimer.singleShot(1500,self._ensure_proxies)

    def _on_project_saved(self):
        """Stamp the cache folder so a later open can reuse these frames."""
        if self.frame_cache.folder:
            self.frame_cache.write_manifest(fingerprint(
                self.engine.timeline,self.engine.project_size(),self.engine.background_bgr,
                getattr(self.engine,"compositions",None)),
                {"fps":self.engine.timeline.fps})

    # ------------------------------------------------------------ transform tools
    # ------------------------------------------------------------------- text clips
    def add_adjustment_layer(self):
        """Add an adjustment layer above the picture and select it."""
        clip_id = self.engine.session.add_adjustment_clip()
        if clip_id is None:
            return None
        self._refresh_panels()
        self._request_preview()
        return clip_id

    def _start_shape_tool(self, kind="rectangle"):
        """Arm the shape tool: the next drag in the preview draws that shape."""
        from ..model import shape as SHAPE
        from ..icons import icon
        if kind not in SHAPE.KINDS:
            kind="rectangle"
        self._shape_kind=kind
        if hasattr(self,"shape_button"):
            self.shape_button.setIcon(icon(SHAPE.ICONS[kind]))
        self.overlay.shape_kind=kind
        self._set_overlay_mode("shape")
        self.preview_panel.view.setFocus()
        self.bridge.status.emit(
            f"Shape tool: drag in the preview for a {SHAPE.LABELS[kind].lower()} "
            f"(Shift constrains, Alt draws from the centre, Esc cancels)")

    def add_shape(self, kind="rectangle"):
        """Add a shape at its default size in the middle of the frame (Add menu)."""
        clip_id=self.engine.session.add_shape_clip(kind)
        if clip_id is None:
            return None
        self._set_overlay_mode("transform")
        self._refresh_panels(); self._request_preview()
        return clip_id

    def add_text_clip(self):
        """Add a generated text clip at the playhead and select it for editing."""
        from ..model import text as TXT
        text="Text"
        selected=[self.engine.timeline.find_clip(cid) for cid in sorted(self.engine.session.selected)]
        source=next((clip for clip in selected if clip is not None and clip.is_text),None)
        clip_id=self.engine.session.add_text_clip(text)
        if clip_id is None:
            return None
        clip=self.engine.timeline.find_clip(clip_id)
        if source is not None and clip is not None:
            # A new text clip inherits the selected one's look, so building titles is quick.
            TXT.clone_settings(source,clip)
        self._set_overlay_mode("transform")
        self._refresh_panels(); self._request_preview()
        return clip_id

    # ------------------------------------------------------------ effects & gaps
    def delete_gap(self):
        if not self.engine.session.delete_gap():
            self.toast_message("Select a clip with empty space before it")

    def copy_effects(self):
        if not self.engine.session.copy_effects():
            self.toast_message("Select a clip to copy effects from")
            return
        self.toast_message("Effects copied — Paste Effects or Paste Attributes onto other clips")

    def paste_effects(self):
        if not self.engine.session.selected:
            self.toast_message("Select the clips to paste effects onto")
            return
        count = self.engine.session.paste_effects(resolver=self.engine.runtime_effects)
        if count:
            self._refresh_panels(); self._request_preview()
            self.timeline_panel.canvas.update()
            self.toast_message(f"Effects pasted onto {count} clip(s)")

    def paste_attributes(self):
        from ..model.session import TimelineSession
        from .dialogs import PasteAttributesDialog

        source = TimelineSession._attributes
        if not source:
            self.toast_message("Select a clip and use Copy Effects or Copy Attributes first")
            return
        if not self.engine.session.selected:
            self.toast_message("Select the clips to paste attributes onto")
            return
        available = set(source)
        dialog = PasteAttributesDialog(self, available=available)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        parts = dialog.parts()
        if not parts:
            return
        count = self.engine.session.paste_attributes(parts, resolver=self.engine.runtime_effects)
        if count:
            self._refresh_panels(); self._request_preview()
            self.timeline_panel.canvas.update()
            self.toast_message(f"Attributes pasted onto {count} clip(s)")

    def remove_all_effects(self):
        count = self.engine.session.remove_all_effects()
        if not count:
            self.toast_message("Select clips with effects to clear")
            return
        self._refresh_panels(); self._request_preview()
        self.timeline_panel.canvas.update()

    def show_shortcuts(self):
        from .shortcuts_dialog import ShortcutsDialog
        ShortcutsDialog(self).exec()

    # ------------------------------------------------------------------ audio tools
    def open_silence_dialog(self, clip_id=None):
        """Open Remove Dead Silence for the selection, or for a clip a menu pointed at."""
        from .audio_tools import SilenceDialog
        SilenceDialog(self, clip_id=clip_id).exec()

    def _set_linked_selection(self, on):
        """Edit > Linked Selection: follows links; it never creates or removes one."""
        self.engine.session.set_linked_selection(bool(on))

    def _sync_linked_selection_action(self, on=None):
        """Keep the menu checkmark in step with shortcuts and any other caller."""
        action = getattr(self, "linked_selection_action", None)
        if action is None:
            return
        on = self.engine.session.linked_selection_enabled if on is None else bool(on)
        if action.isChecked() != on:
            action.blockSignals(True)
            action.setChecked(on)
            action.blockSignals(False)

    def _refresh_link_actions(self, *_args):
        """Enable the Edit > Link/Unlink commands only when they can do something."""
        session = self.engine.session
        ids = sorted(session.selected)
        if getattr(self, "link_action", None) is not None:
            self.link_action.setEnabled(session.can_link(ids))
        if getattr(self, "unlink_action", None) is not None:
            self.unlink_action.setEnabled(session.can_unlink(ids))

    def normalize_audio(self):
        from .audio_tools import NormalizeDialog
        NormalizeDialog(self).exec()

    # ------------------------------------------------------------------ captions
    def open_captions_dialog(self):
        from .caption_dialog import CaptionDialog
        dialog = getattr(self, "_caption_dialog", None)
        if dialog is None:
            dialog = CaptionDialog(self)
            dialog.setModal(False)
            self._caption_dialog = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def open_transcript_editor(self):
        from .caption_dialog import TranscriptDialog
        TranscriptDialog(self).exec()

    def _caption_size(self):
        width, height = self.engine.project_size() or (0, 0)
        return (int(width) or 1920), (int(height) or 1080)

    def import_captions(self):
        """Import an SRT file as editable caption clips at the playhead."""
        from ..captions import model as caption_model
        from ..captions import styles as caption_styles
        from ..captions import subtitles

        path = open_file(self, "Import subtitles", "", "Subtitles (*.srt);;All files (*)")
        if not path:
            return
        try:
            payload = open(path, "r", encoding="utf-8-sig", errors="replace").read()
        except OSError as exc:
            self.toast_message(f"Could not read {os.path.basename(path)}: {exc}", kind="error")
            return
        rows = subtitles.parse_srt(payload)
        if not rows:
            self.toast_message("That file has no readable subtitle entries", kind="error")
            return
        width, height = self._caption_size()
        style = caption_styles.style("classic")
        ids = self.engine.session.generate_captions(
            caption_model.cues_from_srt(rows), style_id="classic",
            animation=style["animation"], start_frame=self.engine.session.playhead,
            width=width, height=height, label="Import subtitles")
        if ids:
            self.toast_message(f"Imported {len(ids)} captions from {os.path.basename(path)}")
        self._refresh_panels(); self._request_preview(); self.timeline_panel.canvas.update()

    def export_captions(self, fmt="srt"):
        """Write the caption track as SRT or ASS (timings come from the clips themselves)."""
        from ..captions import model as caption_model
        from ..captions import styles as caption_styles
        from ..captions import subtitles

        timeline = self.engine.timeline
        clips = caption_model.caption_clips(timeline)
        if not clips:
            self.toast_message("There are no captions to export")
            return
        cues = caption_model.export_cues(clips, timeline.fps)
        project = self.projects.project
        name = (project.name if project is not None else "Captions") + "." + fmt
        path = save_file(self, "Export subtitles", name,
                         "SRT subtitles (*.srt);;ASS subtitles (*.ass)")
        if not path:
            return
        if not path.lower().endswith((".srt", ".ass")):
            path += "." + fmt
        width, height = self._caption_size()
        try:
            if fmt == "ass":
                style_id = caption_styles.style_of(clips[0])
                settings = caption_styles.style_settings(style_id, width, height)
                payload = subtitles.format_ass(
                    cues, width, height, font=settings.get("font", "Segoe UI"),
                    size=settings.get("size", 54.0), color=settings.get("color", "#FFFFFF"),
                    outline_color=settings.get("stroke_color", "#000000"),
                    outline=settings.get("stroke_width", 0.0),
                    shadow=settings.get("shadow_opacity", 0.0) / 40.0)
            else:
                payload = subtitles.format_srt(cues)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError as exc:
            self.toast_message(f"Could not write {os.path.basename(path)}: {exc}", kind="error")
            return
        self.toast_message(f"Exported {len(cues)} caption{'s' if len(cues) != 1 else ''} to "
                           f"{os.path.basename(path)}")

    def toggle_text_key(self, clip_ids=None):
        """Key the words (and size) of the selected text clips at the playhead."""
        from ..model import text as TXT
        from ..model import channels as CH
        ids=set(clip_ids or self.engine.session.selected)
        clips=[c for c in (self.engine.timeline.find_clip(cid) for cid in sorted(ids)) if c is not None and c.is_text]
        if not clips:
            self.toast_message("Select a text clip first")
            return
        for clip in clips:
            frame=max(0,int(self.engine.session.playhead)-int(clip.timeline_start))
            self.engine.session.stack.run("Text keyframe",
                                          lambda c=clip,f=frame: CH.toggle_key(c,CH.text_content_channel(),f))
        self.engine.session.changed.emit()
        self._refresh_panels(); self._request_preview()

    def _focus_text_editor(self):
        editor=getattr(self,"effects_panel",None)
        for row in getattr(editor,"_rows",[]) or ():
            if row.get("kind")=="text" and row.get("editor") is not None:
                row["editor"].setFocus(); row["editor"].selectAll(); return
        self.toast_message("Select a text clip to edit its words")

    def _set_overlay_mode(self,mode):
        self.overlay.mode=mode
        for button,name in ((getattr(self,"select_tool_button",None),"select"),
                            (getattr(self,"transform_tool_button",None),"transform"),
                            (getattr(self,"puppet_tool_button",None),"puppet"),
                            (getattr(self,"shape_button",None),"shape")):
            if button is not None:
                button.blockSignals(True); button.setChecked(name==mode); button.blockSignals(False)
        if mode=="puppet": self._ensure_puppet_effect()
        if mode in ("select","transform"):
            self.engine.session.tool.set("select")
        self.preview_panel.view.update()
        self.effects_panel.refresh_properties()

    def _ensure_puppet_effect(self):
        """The Puppet tool needs its effect: add it to the selection when it is missing."""
        clip=self.overlay.primary()
        if clip is None or any(entry.get("effect_id")=="puppet_warp" for entry in clip.effects):
            return
        runtime=self.engine.runtime_effects.get("puppet_warp")
        params=dict(runtime.capture_params()) if runtime is not None else {}
        def action():
            target=self.engine.timeline.find_clip(clip.id)
            if target is None or target.track.locked: return
            if not any(entry.get("effect_id")=="puppet_warp" for entry in target.effects):
                target.effects.append({"effect_id":"puppet_warp","enabled":True,"params":params,"pins":[]})
        self.engine.session.stack.run("Add Puppet Warp",action)
        self.effects_panel.refresh_properties()

    def _set_motion_path(self,on):
        self.overlay.show_motion_path=bool(on)
        button=getattr(self,"path_button",None)
        if button is not None and button.isChecked()!=bool(on):
            button.blockSignals(True); button.setChecked(bool(on)); button.blockSignals(False)
        action=getattr(self,"motion_path_action",None)
        if action is not None and action.isChecked()!=bool(on):
            action.blockSignals(True); action.setChecked(bool(on)); action.blockSignals(False)
        self.preview_panel.view.update()

    def _activate_animation_editor(self, mode):
        """Keep the two mode switches and their docks showing the same active editor."""
        graph = mode == "graph"
        self.graph_editor.set_active(graph)
        self.keyframe_editor.set_active(not graph)

    def _open_graph_editor_for(self, entries=None):
        """Show the Graph Editor dock and select the given keys' curves in it."""
        dock=self.docks.get("graph")
        if dock is not None:
            dock.setVisible(True)
            dock.raise_()
        if entries:
            self.graph_editor.focus_entries(entries)
        else:
            self.graph_editor.refresh()
        self._activate_animation_editor("graph")
        if getattr(self,"graph_button",None) is not None:
            self.graph_button.blockSignals(True); self.graph_button.setChecked(True); self.graph_button.blockSignals(False)

    def _open_keyframe_editor_for(self, entries=None):
        """Show the Keyframe Editor dock and select the requested timing keys."""
        dock=self.docks.get("keyframe")
        if dock is not None:
            dock.setVisible(True)
            dock.raise_()
        self.keyframe_editor.refresh()
        if entries:
            self.keyframe_editor.focus_entries(entries)
        self._activate_animation_editor("keyframe")

    def _preview_clip_range(self, start, end):
        """Play one clip so an animation preset can be checked without hunting for it."""
        start, end = max(0,int(start)), max(1,int(end))
        # Previewing is not an edit: set the loop range directly instead of through the
        # undoable loop-region command.
        timeline=self.engine.timeline
        timeline.loop_in, timeline.loop_out = start, min(end, max(start+1, timeline.duration))
        self.engine.session.set_loop_enabled(True)
        self.engine.session.loop_changed.emit()
        self.engine.session.set_playhead(start)
        self._refresh_panels()
        if not self._playing:
            self.toggle_playback()

    def _toggle_graph_dock(self):
        dock=self.docks.get("graph")
        if dock is None: return
        visible = not dock.isVisible()
        dock.setVisible(visible)
        if visible:
            dock.raise_()
            self.graph_editor.refresh()
        if getattr(self,"graph_button",None) is not None:
            self.graph_button.blockSignals(True); self.graph_button.setChecked(visible); self.graph_button.blockSignals(False)

    def _clip_media_size(self,clip):
        size=self.engine.media.source_size(clip.source_path) if clip.source_path else None
        if size:
            return size
        return (self.engine.project_size() or (1920,1080))

    def closeEvent(self,event):
        self._closing=True
        if not self.projects.maybe_save():
            self._closing=False
            event.ignore(); return
        self.stop_playback(); self.audio.stop()
        # Finish or drop the browser's thumbnail jobs before the widgets go away.
        self.effects_panel.browser.shutdown()
        # No debounced save may fire after this point and overwrite the final state.
        self._layout_save_timer.stop()
        self._save_layout_now()
        self._settings.setValue("loopAtEnd",bool(self.engine.timeline.loop_at_end))
        self._settings.setValue("audioWhileScrubbing",bool(self._audio_while_scrubbing))
        if self._cancel_export:self._cancel_export.set()
        # Stop the render thread before the cache goes away, then stamp the manifest so
        # the frames that are already rendered survive a reopen of this project.
        self._prefetch_timer.stop(); self._cache_timer.stop()
        self.renderer.stop()
        self._on_project_saved()
        self.frame_cache.clear(disk=False)
        self.playback_settings.save()
        self.projects.shutdown()
        self.update_manager.cancel_download()
        super().closeEvent(event)
