# meeniFX Studios — what the app is and what it does

A short, readable overview of the application, its feature set, the current state of the
Graph Editor, and everything that was repaired in this development pass. For the long
form (exact snapping rules, cache internals, recorder endpoints, measured benchmarks) see
`README.md`.

---

## 1. What it is

meeniFX is a **desktop photomosaic video editor** written in Python: PyQt6 for the whole
interface, OpenCV/numpy for frame processing, imageio-ffmpeg for encoding. It is a
multi-track editor in the Vegas/AE tradition — timeline, transform handles on the picture,
keyframes with easing, effect stack — whose distinguishing feature is a set of
photomosaic and tracker effects that turn footage into tile mosaics, ASCII art, or faces
and hands tracked by small image swatches.

Everything the preview shows is what export writes: one renderer, one effect parameter
table, one transform model.

| | |
|---|---|
| Language / UI | Python 3.10+, PyQt6 |
| Frame pipeline | OpenCV, numpy, Pillow |
| Encoding | imageio + imageio-ffmpeg, NVENC when available |
| Audio | numpy DSP, WASAPI capture, 16-bit WAV |
| Project file | one `.mfxpf` (a ZIP, format v5) |
| Source size | 98 modules, ~32 000 lines in `meenifx/` |
| Tests | 40 `test_*.py` suites plus 9 driver scripts in `tests/`, headless with `QT_QPA_PLATFORM=offscreen` |

## 2. Running it

```powershell
python -m pip install -r meenifx/requirements.txt
python .\main.py
```

Windows is required for the recorder (it captures microphones and system sound through
WASAPI). The editor itself is portable Python. A `.mfxpf` path may be passed on the
command line, so double-clicking a project opens it once the association exists
(`packaging\associate_mfxpf.ps1`).

## 3. The features, by area

**Projects** — a start window with recent-project cards, search and sort; New/Open;
projects are a single `.mfxpf` container (settings, media references, columnar
tracks/clips, effects, keyframes, markers, layout, thumbnails). Atomic saves with one
`.bak`, 3-minute autosave, recovery after a crash, media never embedded. Missing media is
flagged rather than fatal.

**Timeline** — multi-track, frame-based, thumbnails and waveforms, split/razor, trim,
move, Alt-drag time stretch, freeze frames, loop region, markers, per-clip fades with
draggable grips and curve bending, deterministic snapping with named feedback ("Screen 1 ·
end", "3 clip edges aligned"), overlap modes, clipboard, full undo/redo.

**Preview and transform** — fit/percentage zoom, cursor-centred wheel zoom, pan, guides.
A bounding box with eight scale handles, rotation knob, visible anchor, motion path and
warp pins; Shift constrains, arrows nudge by a project pixel, every drag is one undo step.
The overlay is editing-only and never reaches exported frames.

**Effects** — Mosaic, ASCII Art, Face Tracker, Hand Tracker, Blob Tracker, puppet warp,
corner pin, colour, blur, glow, plus audio effects (filter, compressor, limiter, delay,
reverb, pan). Every effect declares which media types it can process, so a video-only
effect cannot be added to audio and an old project that has one is switched off with an
explanation instead of rendering wrongly. Each effect publishes its parameter table, which
the properties panel builds itself from.

**Keyframes and the Graph Editor** — every numeric parameter animates: stopwatch, diamond,
previous/next keys, Auto-Keyframe (on = key at playhead, off = shift the whole curve).
Clip opacity, effect envelopes and audio gain animate too. Text has its own animation
presets.

**Text** — a real text generator (fonts, fill, stroke, shadow, alignment) rendered through
Pillow, animatable, split-able and saveable.

**Audio** — per-clip gain and fades on the waveform, a mixer plan, live mastering meter,
and export that matches the preview.

**Recorder** — webcam, screen, microphone and system audio in any combination; countdown,
always-on-top bar, global hotkeys, per-source gain and meters, pause as segments. On stop
the sources are muxed (AAC) and inserted as a linked video+audio pair.

**Export** — whole project or loop region to video/GIF/image sequences, Save Frame, NVENC
with verification and a fallback writer, off-thread with progress and cancel.

**Performance** — a dedicated render thread with an interactive (newest-wins) queue and a
background prerender queue, a RAM+disk frame cache keyed by a timeline fingerprint,
digest-based invalidation, idle prefetch, automatic proxy media, and a measured GPU
dispatch (OpenCL is benchmarked at startup and only used where it actually wins).

**Robustness and polish** — one dark theme including tooltips, menus and the non-native
file dialogs; no status bar, hints live in tooltips; fading toasts for the few messages
that matter; a crash guard that logs an exception from a Qt slot instead of aborting the
application; docks that persist their layout.

## 4. The Graph Editor today

The panel lives in the **Graph Editor** dock (`G`) and edits the selected clip's curves.

- **Value Graph / Speed Graph** — the value over time, or its derivative, for the same
  animation; switching never changes the animation itself.
- **Left value gutter** — 56 px of right-aligned value labels (`626 px`, `447 px`, …)
  with precision that follows the tick spacing, so labels never overlap the curves and
  never read `625.93px`. The number of grid lines steps down (4 / 2 / 1) as the dock gets
  shorter, so nothing collides on a short canvas.
- **Ruler** — frame numbers centred over their tick with tick marks, on its own plate; the
  time gridlines start at the plot edge instead of running under the labels.
- **Playhead** — the red line sits at the true frame; only the head triangle is nudged
  inside the plot, so it is never sliced by the border at frame 0 or when panned left.
- **Dragging keys** — keys follow the pointer live (every move, not on release), from a
  press-time snapshot so later moves stay correct; snap to the playhead within 7 px; the
  whole gesture is one undo step.
- **Scrubbing** — press anywhere in the ruler band to jump and scrub, or grab the red line
  itself; the cursor changes over the ruler and is restored afterwards.
- **Following playback** — the playhead line tracks the timeline during playback. The
  repaint is limited to the two 13 px columns the playhead left and entered, because a
  full repaint of a five-channel graph costs 36 ms while a playback tick then costs
  **7.7 ms median (9.3 ms p90)**.
- **Also** — channel list with visibility toggles, box select, Ctrl+C/V, Delete, easing
  presets, Bézier handles, continuous/unified handles, wheel zoom anchored under the
  pointer, middle-drag/Ctrl+wheel pan, Fit / Fit selected / Reset view.

## 5. What was fixed in this pass

| Area | Before | Now |
|---|---|---|
| Graph Editor performance | a full repaint took **56–107 ms** on a real 5-channel clip, so dragging felt like it was catching up | **~42 ms** repaint (36 ms measured today), and playhead moves cost only the two columns that changed |
| Graph Editor crash | a channel holding a non-numeric animation (a colour preset stored as a curve) reached `float()` inside `paintEvent`; PyQt6 turned that into `qFatal` — no traceback, no window | non-numeric channels are never plotted; the guard logs instead of aborting |
| Crash reporting | an exception inside a Qt slot killed the process silently | "meeniFX hit a problem" dialog with the traceback, the crash-log path, *Copy details* and *Open log folder*; an unclean session offers recovery, a clean quit clears the marker |
| File dialogs | native Windows dialogs, light and out of place | themed, non-native Open/Save/Export dialogs that match the app |
| Keyframe dragging (timeline and graph) | keys moved only when the button came up | live dragging, snapped to the playhead, one undo step per gesture |
| Playhead dragging (graph) | the "scrub" branch was unreachable; the playhead could only be moved by clicking empty space | ruler press jumps and scrubs, grabbing the line drags it, hover cursor feedback |
| Key drag repaint | a local variable named `time` shadowed the module, so every move raised on its last line — the model moved but the repaint never ran | renamed; the canvas repaints on every move (regression test included) |
| Playhead during playback | the graph's red line froze on the frame where playback started, because playback advances the playhead *silently* and the curve view never listened | subscribes to the low-level move signal and repaints the two affected columns |
| Graph Editor layout | value labels were drawn inside the plot and collided with the ruler (`625.93px` over `30 60 90`); gridlines ran through the labels | separate value gutter, adaptive decimals, centred ruler labels, step-aware line count |
| Playhead head clipping | the triangle was centred on the frame, so half of it fell outside the canvas at frame 0 and the border drew through it | the line keeps the exact frame, the head is clamped inside the plot and drawn above the border |
| Unrelated crash paths found on the way | unguarded callbacks in `effects/base.py`, `project_controller.py`, `undo.py`, and a Qt parent mistake in the crash reporter itself | guarded, and covered by the crash-reporting tests |

## 6. Known limitations

- `build/`, `dist/` and `dist_new/` are **stale PyInstaller output** (built 2 November).
  Rebuild with `pyinstaller "meeniFX Studios.spec"`; they are excluded from git.
- The Graph Editor does not auto-scroll horizontally during playback; when zoomed in past
  the visible range the playhead clamps at the edge instead of dragging the view along.
- OpenCL compositing measures slower than the CPU path on this machine, so *Auto* keeps
  the CPU; hardware **decode** is unavailable in this OpenCV build (the app reports what
  the driver says). NVENC encoding is used after a verified probe encode.
- The recorder is Windows-only (WASAPI). Screen recording is written by the bundled
  ffmpeg (gdigrab) rather than by Qt, because DXGI desktop duplication - what Qt uses -
  delivers a pointer-free image and QScreenCapture has no cursor option. gdigrab draws the
  pointer, so the "Show mouse cursor" switch controls it; the Qt capture keeps running for
  the preview only. There is still no region select.
- Projects (`*.mfxpf`) and their `Cache/` and `Recordings/` folders live outside this
  repository; nothing but source, assets and test fixtures is committed.

## 7. Checking it

```powershell
python -m pytest tests --ignore=tests/test_project_app.py --ignore=tests/test_project_large.py `
  --ignore=tests/test_project_panel.py --ignore=tests/test_start_window.py
```

Four scripts build a real window and call `os._exit`, so they are excluded from collection
and run directly (`python tests\test_project_app.py` and friends). The Graph Editor work
above is pinned by `tests/test_graph_editor.py`, `tests/test_graph_editor_perf.py` and
`tests/test_graph_editor_robustness.py` (real mouse and wheel events, live-drag and
playhead-follow regressions, and a guard against the paint crashes).
