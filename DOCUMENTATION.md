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
flagged rather than fatal. The recent list is tidied from the start window's Clean up button
(entries whose file is gone, or projects sitting in the system temp folder - with an optional
move to the Recycle Bin) and pruned at startup unless the preference is turned off;
`--no-recent` / `MEENIFX_NO_RECENT=1` keeps a run (tests, scripts, agents) off the list.

**Timeline** — multi-track, frame-based, thumbnails and waveforms, split/razor, trim,
move, Alt-drag time stretch, freeze frames, loop region, markers, per-clip fades with
draggable grips and curve bending, deterministic snapping with named feedback ("Screen 1 ·
end", "3 clip edges aligned"), overlap modes, clipboard, full undo/redo.

**Manual linking** — select two or more clips (Ctrl+click adds to the selection) and use
**Link Selected Clips** (Ctrl+L, Edit menu, or the clip right-click menu) to give them one
persistent group id; **Unlink Selected Clips** (Ctrl+Shift+L) removes only the selected
members. Clips do not need to come from the same file, so separately recorded video and
audio can be linked. Linking never moves, trims or retimes anything; a split gives the left
pair the original id and every new pair its own. **Linked Selection** (Edit menu, or
Ctrl+Shift+K) is only about selection: with it on, selecting/moving/trimming/splitting one
member also acts on its partners; with it off they are edited independently while the link
metadata stays, so turning it back on restores group behaviour. It is deliberately no longer
a second chain button on the timeline, where it looked like the way to create a link.
Linked clips carry a small badge on the clip.

**Smart Paste** — Ctrl+V imports clipboard pictures and files: pixels are written as real
PNGs in the project's `Media` folder (alpha preserved), Explorer files are linked, text and
numeric fields keep their text paste, the Media panel imports without touching the timeline
and everywhere else the picture lands at the playhead. Preferences set duration, placement,
selection and fit; a clipboard picture wins over an older in-app clip copy; the whole paste
is one undo step.

**Auto Captions** — optional local speech recognition (`faster-whisper`, CPU/int8, no
account or key; a model is downloaded only after consent, kept under
`~/.meenifx/models/whisper`, deletable in Preferences). The result is ordinary **text clips
on a `Captions` track** carrying their style, word timings and animation in
`clip.text["caption"]`, which means selection, trimming, undo, `.mfxpf` save/load and the
burned-in render all use the existing paths. Eight styles are plain text-model values scaled
by project height; entrance animations are the shipped text presets. A transcript editor
edits times and words (one undo step), a range can be regenerated without touching captions
outside it, and SRT import plus SRT/ASS export are supported. When the selected clip is
linked, the picture and its sound are treated as one subject: a silent video member resolves
to the linked audio for analysis, while a video with embedded sound can be transcribed
directly. The captured range follows the audible members, so a longer picture does not add
silence around the words. Karaoke colouring *inside* a phrase is not implemented;
word-by-word grouping gives per-word timing instead.

**Remove Dead Silence and Normalize** — `meenifx/audio/silence.py` finds quiet regions from
short-window RMS levels (threshold in dBFS, minimum silence/speech, padding before/after,
keep-pause and a minimum retained segment), the dialog previews the cuts on the waveform and
lists every clip it will affect before Apply. Three scopes are offered: **Audio and linked
video** (the analysed clip's link group), **Matching source video and audio** (same file,
speed, direction and sync key even when not linked) and **Audio only**. Cuts are converted
to absolute timeline frames once and every included clip is split on those same frames;
only the span all included clips cover is edited, so no member is cut where a partner is
missing. Survivors are re-linked per retained segment with fresh ids, a locked member blocks
the whole pass instead of cutting its partners, and the whole edit is one undo step. With
ripple on, gaps close on the included tracks only; other tracks never move. Source media is
never modified. Normalize applies peak gain (not LUFS). The WASAPI capture thread pads
skipped endpoint time with silence using the device position, so leading silence no longer
slides later speech towards frame 0.

**Timeline and effect polish** — Delete Gap, exact Duration field, Copy/Paste Effects,
Paste Attributes (transform/effects/animations/audio/text, incompatible effects skipped),
Remove All Effects, independent link ids for the pieces of a split pair, media-type filter
and sorting in the Project panel, a searchable Keyboard Shortcuts page built from the live
menu bar, and an in-app recording status (`● mm:ss`, click to stop) with the always-on-top
recording bar now opt-in under Preferences.

**Preview and transform** — fit/percentage zoom, cursor-centred wheel zoom, pan, guides.
A bounding box with eight scale handles, rotation knob, visible anchor, motion path and
warp pins. A corner drag is a free transform: the dragged corner follows the pointer on
both axes while the opposite corner stays fixed in composition space, including on
rotated, non-uniformly scaled, re-anchored or non-square clips and at any preview zoom.
Ctrl keeps the aspect ratio captured when the drag (or the current modifier state)
began, Shift resizes from the centre, Ctrl+Shift does both; side handles change one
local axis; a resize stops at a three-pixel minimum and never flips. Shift still
constrains a move to one axis and snaps rotation to 15 degrees. The overlay is
editing-only and never reaches exported frames.

**Effects** — Mosaic, ASCII Art, Face Tracker, Hand Tracker, Blob Tracker, puppet warp,
corner pin, colour, blur, glow, plus audio effects (filter, compressor, limiter, delay,
reverb, pan). Every effect declares which media types it can process, so a video-only
effect cannot be added to audio and an old project that has one is switched off with an
explanation instead of rendering wrongly. Each effect publishes its parameter table, which
the properties panel builds itself from: a parameter may name a group (`ParamSpec.group`),
and the card renders those as collapsible **Detection**, **Visualization**, **Labels**,
**Connectors** and **Face Effects** sections instead of one long list. Detection and
Visualization start open, the rest stay folded, the open/closed state is remembered per
effect, and a group is only created when the effect declares members for it - so no card
shows an empty heading and effects without groups are unchanged. Advanced technical values
sit in the card's *Advanced* fold at the end. Face, Hand and Blob Tracker share one overlay
language: box, corner brackets or ellipse, contour/hull, sampled points, labels, tracking
numbers, opacity, glow and connectors (nearest, all within range, or in track order, with
a range and link cap). Named looks cover common setups; choosing one copies its values
into the ordinary controls, so every checkbox still works independently and the first
manual change returns the Look dropdown to Custom. Old projects'
draw-box/hull/mode/label values are mapped onto the new controls. Face
Tracker can blur a face with a feathered elliptical mask; labels are off by default,
and the Numbers control draws track IDs without appending them to labels. Clearing
Label text removes the label; new effects start with a useful tracker name. Each
tracker has Fast/Balanced/Precise detection quality. That setting runs detection on a smaller working
image and scales the result back, and detector results are cached for repeated preview or
export frames. Hand Tracker remains a skin-colour/morphology detector, not a hand-pose
model.

**Keyframes, Keyframe Editor and Graph Editor** — every numeric parameter animates:
stopwatch, diamond, previous/next keys, Auto-Keyframe (on = key at playhead, off = shift the
whole curve). Clip opacity, effect envelopes and audio gain animate too. The timeline keeps
clips compact with one key-count badge; timing and values live in the dockable **Keyframe
Editor**, while interpolation and Bezier curves live in the **Graph Editor**. Both are
named modes over one animation model and share the selection immediately. Text has its own
animation presets. Bake samples every channel the preset actually moves, including a
vertical-only motion such as **Bounce** (its Y curve remains visible after baking), then
removes the procedural instance so preview and export do not apply the motion twice.

**Animation presets** — an Animations tab with the 81 shipped presets folded into 22
families (one card per behaviour, with a Mode In/Out/Loop/Both and a Direction selector
above the grid), plus search, favourites, recently used and grid/list views. A preset is an
instance on the clip (In anchored to the start, Out to the end, Loop repeating its cycle
across the clip) with Cycle, Intensity, Easing, Direction, Stagger, Unit and Seed knobs, and
a Loop also has Span, Cycles, Repeat/Ping-Pong, Gap and Phase. The Properties *Animation*
card switches Mode and Direction in place - the same instance changes, it is never
duplicated - and loop instances keep repeating when the clip is trimmed or extended;
splitting carries the cycle phase onto both halves. Several
stack and compose on top of the clip's transform and keyframes instead of replacing them
(position/rotation/blur/hue/light add, scale/opacity multiply). Text presets animate per
letter, word or line with a staggered, directional delay around each unit's own centre,
and fall back to the static kerning-exact layout at rest. Instances draw as coloured bars
on the clip (drag the inner edge to retime, snapping like every other drag; right-click to
bake or remove), appear in the Properties *Animation* card, bake to editable keyframes in
one undo step, and live as plain JSON in `presets/` plus the user folder. *Save as preset*
and Import/Export write versioned `.mfxpreset` files. Right-click > *Split Text* turns a
text clip into one clip per letter/word/line at the same positions, one undo step.
Measured on a 60-character title at 1920x1080 (median of 5 renders): 48 ms a frame at Full
quality, 31 ms at 75 % and 15 ms at 50 %, with the glyph cache warm and the composite
restricted to the text's own band.

**Text** — a real text generator (fonts, fill, stroke, shadow, alignment) rendered through
Pillow, animatable per letter/word/line, split-able and saveable.

**Audio** — per-clip gain and fades on the waveform, a mixer plan, live mastering meter,
and export that matches the preview.

**Recorder** — webcam, screen, microphone and system audio in any combination; countdown,
always-on-top bar, global hotkeys, per-source gain and meters, pause as segments. On stop
the sources are muxed (AAC) and inserted as a linked video+audio pair. Recorder settings
carry Performance / Balanced / High Quality presets, which change bitrate headroom and
encoder effort without changing capture timing; the screen path uses a verified NVENC
encoder when available and switches to software libx264 if it produces no data, so a
driver refusal does not lose the take. The settings dialog reports the encoder it will use.

**Project folders** — the Project panel is organised with virtual folders (New Folder,
rename, nested folders, drag and drop, multi-item moves, a Back button, a Project Root
drop target, item counts and search across the whole project). Folder rows use a folder
icon, hover/selection styling and a highlighted drop target; grid and list views both
work. Moving an item changes only its project metadata: the file on disk, the stable
media id and the existing thumbnail stay put. Long media names are elided in the middle
with the full name and path in the tooltip. Deleting a non-empty folder asks whether to move its contents
to the parent, remove the folder and its media references, or cancel; files on disk are
never deleted by that action. **Remove from Project** only removes references, while
**Delete File from Computer...** shows the exact paths, warns about timeline use and moves
the selected files to the operating system's Recycle Bin after confirmation. Physical
deletion is outside the editor's Undo history and is refused when no safe trash mechanism
exists.

**Export** — whole project or loop region to video/GIF/image sequences, Save Frame, NVENC
with verification and a fallback writer, off-thread with progress and cancel.

**Compositing** — 12 blend modes per clip (Normal, Add, Screen, Multiply, Overlay, Soft
Light, Hard Light, Difference, Darken, Lighten, Color Dodge, Color Burn), one shared
compositor for preview, export and thumbnails, alpha-correct by construction. *Add >
Adjustment Layer* makes a picture-less clip on a video track whose effects run over
everything composited below it, for exactly its time range, with its own opacity, fades,
keyframes and blend mode. Every effect card has an animatable **Mix** value (0-100 %, dragged or typed like
every other number; the engine blends the untouched layer with the result, alpha included).

**Effects browser** — one browser with three tabs (Effects, Animations, Styles). Animation
cards carry a genuine hover preview from the same preset evaluator: after a short delay
they play six evaluated frames over a short loop, then return to the static thumbnail;
leaving the card stops them immediately, previews are skipped during timeline playback or
while the panel is hidden, and a render failure keeps the static image. Each tile is a real
render of the reference picture through the same compositor the export uses, cached
by effect/parameters/picture/quality and produced on background workers, so scrolling never
blocks the UI. Effects with no meaningful still result (audio, time effects, and trackers
without a face/hand in the reference) show a readable effect-specific label rather than an
empty tile; no detection is faked. The reference is `figure_cafe.png` beside `main.py`; Preferences > Appearance
> Effect Previews replaces it with a custom image and sets the background and preview
quality (global, never stored in a project). Double-click applies, double-clicking an
applied effect removes it, and a Style opens in Properties as one collapsible group whose
member effects keep their own cards.

**Effects and looks** — the Glitch & Distortion pack (RGB Split, Glitch, Wave Warp,
Bulge/Pinch, Twirl, Mirror/Kaleidoscope), Stylize (VHS/CRT, Film Grain, Halftone, Posterize,
Edge Outline, Duotone, Invert, Vignette), Light (Flash/Strobe, Exposure Flicker, Light Leak,
Bloom Streaks), Time (Posterize Time, Stutter, Echo/Trail) and a LUT effect that reads
`.cube` tables (plus three built-in ones) with an intensity value. Parameters follow the
existing table (two or three visible, the rest under *Advanced*), pixel sizes scale with the
preview quality, and randomness is seeded from the seed and the frame. Ten shipped
**Looks** in `looks/` apply a tuned stack plus one Strength card; saving, importing and
exporting use versioned `.mfxlook` JSON and a per-user folder. `.mfxpf` format 7 adds the
blend/adjustment columns and migrates v1-6 projects.

**Performance** — a dedicated render thread with an interactive (newest-wins) queue and a
background prerender queue, a RAM+disk frame cache keyed by a timeline fingerprint,
digest-based invalidation, idle prefetch, automatic proxy media, and a measured GPU
dispatch (OpenCL is benchmarked at startup and only used where it actually wins).

**Robustness and polish** — one dark theme including tooltips, menus and the non-native
file dialogs; no status bar, hints live in tooltips; fading toasts for the few messages
that matter; a crash guard that logs an exception from a Qt slot instead of aborting the
application; docks that persist their layout.

## Shape generator

Shapes are the second kind of generated clip (the first is text) and follow exactly the same
rules: a shape clip carries a `shape` settings dict instead of a source file, and the engine draws
it into a transparent full-frame layer whenever it is needed. The transform, handles, keyframes,
blend modes, effects, fades, split, clipboard and export therefore treat a shape as an ordinary
picture with no special cases.

The toolbar's shape dropdown offers Rectangle, Rounded Rectangle, Ellipse, Polygon, Star and Line;
*Add > Shape* offers the same six. Dragging in the preview draws the shape live (Shift = circle or
square, Alt = from the centre, Esc = cancel), and releasing creates a five-second clip at the
playhead on the topmost clear video track. Properties then shows a Shape card with the fill and
stroke paint, the geometry for that kind (radius, sides, star points/inner radius, line caps and
arrowheads) and the shared transform values, so position, rotation and opacity are the same
numbers as everywhere else.

Nothing is rasterised at creation time. `meenifx/processing/shapes.py` builds a tile around the
shape's bounds at 2x supersampling, draws the path and its stroke there (center, inside and outside
alignment are measured from the outline, so the ring really is the requested width), downsamples
with area averaging and blits it into the frame, cached by the parameter set. A small shape costs
a small tile even on a 4K frame, and a 35 % preview draws the same geometry as the export.

`tests/test_shapes.py` covers every kind, circles/squares, transparency, stroke alignment, both
render scales, keyframes, colour keys, split, copy/paste, effects, undo and a save/reopen;
`tests/test_shape_ui.py` drives the toolbar, the drag, the modifiers, Escape, the inspector and a
corner resize in the real window.

## 4. The Keyframe Editor and Graph Editor today

Animation has two clearly labelled views over the same channels. The **Keyframe Editor**
dock (`Shift+G`, tabbed next to the Timeline) handles timing and values; the **Graph
Editor** dock (`G`) handles interpolation and curves. The mode buttons at the top of either
panel raise the other one, and selection is shared through `session.key_selection`.

### Keyframe Editor

- **Readable rows** — Transform, Compositing, each effect by name, Text and Audio are
  collapsible groups; every numeric property gets a label lane, a live value at the
  playhead and its own diamonds. Non-numeric properties are not plotted or offered.
- **Timing** — click to select, Ctrl/Shift-click for a multi-selection, and drag any
  selected key to move the whole group. The drag is rebuilt from the press-time snapshot
  on every mouse move, so the old key disappears instead of leaving a trail of duplicates,
  and the release commits exactly one undo step. The playhead within 7 px is a snap target;
  project frames are always whole numbers. Clip ends and locked tracks are respected.
- **Values** — the toolbar field edits every selected key; with no selection it keys the
  active property at the playhead. Double-clicking a key focuses the same field. Edits are
  undoable and immediately update the preview and Graph Editor.
- **Navigation and view** — previous/next on the active property, fit all or fit selection,
  wheel zoom anchored at the pointer, middle-drag pan, playhead scrub, collapsible groups,
  Add/Delete/Copy/Paste/Duplicate, and *Open in Graph Editor*.
- **Docking** — a normal `QDockWidget` beside the Timeline: movable, floatable, tabbable,
  closable, resizable, reopenable from View and independent of the Graph Editor's geometry.
  Layout v9 adds it to saved workspaces; older saved layouts fall back to the default.

### Graph Editor

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
| Timeline animation display | every keyframe was packed into dense purple/yellow rows inside the clip | one compact `◆ N keys` badge opens the dedicated Keyframe Editor |
| Keyframe dragging (Keyframe Editor and Graph Editor) | keys moved only when the button came up; the Keyframe Editor initially read live records, leaving a trail of intermediate keys | live dragging from the press-time snapshot; the old key is replaced, no duplicates, snapped to the playhead, one undo step per gesture |
| Playhead dragging (graph) | the "scrub" branch was unreachable; the playhead could only be moved by clicking empty space | ruler press jumps and scrubs, grabbing the line drags it, hover cursor feedback |
| Key drag repaint | a local variable named `time` shadowed the module, so every move raised on its last line — the model moved but the repaint never ran | renamed; the canvas repaints on every move (regression test included) |
| Playhead during playback | the graph's red line froze on the frame where playback started, because playback advances the playhead *silently* and the curve view never listened | subscribes to the low-level move signal and repaints the two affected columns |
| Graph Editor layout | value labels were drawn inside the plot and collided with the ruler (`625.93px` over `30 60 90`); gridlines ran through the labels | separate value gutter, adaptive decimals, centred ruler labels, step-aware line count |
| Playhead head clipping | the triangle was centred on the frame, so half of it fell outside the canvas at frame 0 and the border drew through it | the line keeps the exact frame, the head is clamped inside the plot and drawn above the border |
| Unrelated crash paths found on the way | unguarded callbacks in `effects/base.py`, `project_controller.py`, `undo.py`, and a Qt parent mistake in the crash reporter itself | guarded, and covered by the crash-reporting tests |

### This pass: render cost, captions and dialogs

- **Opaque blur fast path** (`meenifx/effects/blur.py`): Gaussian and Box Blur used to
  premultiply every layer into float32 and unpremultiply the result. For a fully opaque
  layer - the ordinary full-frame clip - that round trip is an identity and cost about
  90 % of the frame time, so it is skipped: the 8-bit layer is blurred directly. Measured
  at 1080p on a 6-core Intel machine: Gaussian Blur (radius 8) 134 -> 27 ms, Box Blur
  (radius 6) 110 -> 27 ms; at 720p 75 -> 12 ms. Layers with real alpha keep the
  premultiplied path, so text, PNG characters and letterboxed pictures still blur with a
  soft, correctly feathered edge instead of a dark fringe. `tests/test_blur_fast_path.py`
  compares both paths (maximum one 8-bit step) and covers the transparent-margin case.
- **Caption sources** (`tests/test_caption_sources.py`): each Auto Captions source is now
  pinned - standalone audio, linked video+audio (clicking either half analyses the
  sound), embedded-audio video, trimmed clips and clips with a source offset. Generating
  captions never changes the video/audio link.
- **File dialog theme** (`meenifx/ui/filedialogs.py`): the scoped stylesheet is built per
  dialog rather than once at import, so it follows a live theme switch; the tinted
  navigation icons use the active theme text colour.

## 6. Known limitations

- Animation presets are a render layer: the Graph Editor shows nothing for them until
  *Bake* turns an instance into ordinary keys, and per-unit (letter/word/line) instances
  cannot be baked at all - *Split Text* is the way to get editable per-letter clips. A
  per-unit blur/tint is applied per glyph (a layer-wide Gaussian would merge the glyphs).
  *Split Text* keeps the words visible at the playhead; text keys that changed the string
  earlier in the clip are not copied onto the pieces.

- `build/`, `dist/` and `dist_new/` are **stale PyInstaller output** (built 2 November).
  Rebuild with `pyinstaller "meeniFX Studios.spec"`; they are excluded from git.
- The Graph Editor does not auto-scroll horizontally during playback; when zoomed in past
  the visible range the playhead clamps at the edge instead of dragging the view along.
- OpenCL compositing measures slower than the CPU path on this machine, so *Auto* keeps
  the CPU; hardware **decode** is unavailable in this OpenCV build (the app reports what
  the driver says). NVENC encoding is used after a verified probe encode.
- Heavy effect stacks are CPU work: three of the heaviest at 1920x1080 measure 136-354 ms
  per frame at 75 % preview quality (see `tests/effects_bench.py`), so 30 fps is not
  reached there; at 50 % they are 64-161 ms. The preview quality slider, proxy media and
  the frame cache are the practical answer. LUTs are quantised to a 7-bit lookup grid for
  speed (one gather per pixel instead of eight trilinear gathers) - visually identical to
  the exact interpolation and identical at every render scale, but a hidden 7-bit table
  cannot represent a cube's tiniest sub-step.
- A user look stores effect values only: keyframes on the saved stack become their current
  base value, because a look is a readable recipe rather than an animation.
- Auto Captions needs the optional `faster-whisper` package and a downloaded model; without
  them the dialog explains the install and only SRT import/export works. Each run is capped
  at 30 minutes of audio. Karaoke-style colour per word within one phrase is not
  implemented; per-word captions (word timing) are. ASS export writes one style block from
  the first caption rather than a full styling system.
- Silence removal works on decoded audio, so a clip whose source cannot be decoded is
  rejected with a message. This build normalizes by **peak**, not perceived loudness.
- Matching source video/audio uses the project's own metadata (same media file, speed,
  direction and sync key), not audio fingerprinting, so separately recorded files must be
  linked manually first. Ripple delete closes gaps on the tracks of the included clips and
  therefore also moves later, unrelated clips on those same tracks; other tracks are left
  alone. **Audio only** deliberately cuts just the audio and can move it out of sync with a
  linked picture; that is the explicit request, not a fallback.
- Smart Paste of a picture URL downloads only when explicitly chosen and only accepts an
  image response that decodes; other links are ignored.
- The recorder is Windows-only (WASAPI). Screen recording is written by the bundled
  ffmpeg (gdigrab) rather than by Qt, because DXGI desktop duplication - what Qt uses -
  delivers a pointer-free image and QScreenCapture has no cursor option. gdigrab draws the
  pointer, so the "Show mouse cursor" switch controls it; the Qt capture keeps running for
  the preview only. There is still no region select. NVENC is used only after a verified
  encode probe; a screen take that produces no data switches to libx264 within the first
  two seconds instead of failing. Webcam recording still uses Qt's Media Foundation path,
  so its encoder choice is reported by Qt rather than selected here.
- Projects (`*.mfxpf`) and their `Cache/` and `Recordings/` folders live outside this
  repository; nothing but source, assets and test fixtures is committed.

## 7. Checking it

```powershell
python -m pytest tests --ignore=tests/test_project_app.py --ignore=tests/test_project_large.py `
  --ignore=tests/test_project_panel.py --ignore=tests/test_start_window.py
```

Four scripts build a real window and call `os._exit`, so they are excluded from collection
and run directly (`python tests\test_project_app.py` and friends). The new effects and compositing are pinned by
`tests/test_effect_parameters.py` (every parameter at its extremes, animated, defaults),
`tests/test_effects_new.py` (determinism, seeds, temporal holds, quality scaling, LUT
files), `tests/test_compositing.py` (blend modes against known pixels, alpha, adjustment
layer isolation and range, Mix, save/load) and `tests/test_looks.py` (shipped looks,
Strength, `.mfxlook` round trips). The Graph Editor work above is pinned by
`tests/test_graph_editor.py`, `tests/test_graph_editor_perf.py` and
`tests/test_graph_editor_robustness.py` (real mouse and wheel events, live-drag and
playhead-follow regressions, and a guard against the paint crashes).

The Keyframe Editor and the compact clip badge are pinned by `tests/test_keyframe_editor.py`
(lanes and groups, add/delete, multi-select, the press-time snapshot group drag and its single
undo entry, clipboard, value editing, zoom/fit/snap and the shared selection with the Graph
Editor); `tests/test_keyframe_ui.py` and `tests/test_keyframe_workflow.py` cover the badge and
the end-to-end workflow, and `tests/test_presets.py` checks that baked Bounce, Slide, Fade,
Flip, Scale and Rotation keep every animated channel - the Bounce Y position included - and
evaluate to the same values as the procedural animation.
