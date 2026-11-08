# Changelog

All notable changes to meeniFX Studios. The current public build is an **Alpha**: it is
usable and tested, but unfinished, and some features are still experimental.

## 0.1.0-alpha.1 - 8 November 2026

First public Alpha, and the first build with Windows packaging and in-app updates.
Windows only for now.

### Added

- Windows installer (per user, no administrator prompt) and a portable ZIP, with SHA-256
  checksums published beside every release.
- Automatic update checks against GitHub Releases through the Alpha channel, with a
  non-modal notice offering **View Update**, **Remind Me Later** and **Skip This Version**.
- A dedicated **Updates** page with the installed version, channel, last check, release
  date, size and notes, plus a verified background download and installer handoff.
- **What's New** on the first launch after an update, reopenable from Help.
- A multi-track timeline with snapping, markers, fades, linked video/audio, and undo/redo.
- Compositing with blend modes and adjustment layers, the Stylize, Warp, Light, Time, Glow
  and keying effects, and one-click Looks.
- Keyframes on every numeric parameter, with the Keyframe Editor and Graph Editor.
- Vector shapes, styled text clips, motion paths and animation presets.
- Recording from a webcam, screen, microphone and system audio, including voice-over.
- Auto Captions with a local speech model, a transcript editor and SRT import/export.
- Smart Paste for clipboard pictures, files, direct media links and YouTube videos.

### Improved

- Preview playback keeps the newest finished frame and shows a cache indicator above the
  ruler, so the picture follows the playhead instead of freezing on one frame.
- Posterize Time now takes a frame rate in fps instead of a count of frames per hold.
- The README is a short project homepage; detailed references moved to `docs/`.

### Fixed

- Smart Paste no longer loses a download job while its worker thread is still running.
- Posterize Time no longer samples backwards when its rate is keyframed.
- Discord GIF downloads no longer crash the application.

### Known Issues

- Windows only. The editor is not supported on Linux, and Linux support is planned but
  not implemented.
- Installers are checksum-verified but not publisher-signed. Windows SmartScreen may warn
  about the first downloads.
- Updates replace the installed version in place; there is no rollback yet.
- Portable builds must be updated manually with the new ZIP.
- Auto Captions needs `faster-whisper` and a downloaded model before the first run.
- Screen recording and hardware codecs depend on the machine's drivers.
- Heavy effect stacks are not 30 fps at 1080p; preview quality, proxies and the cache are the
  supported ways to keep playback responsive.

## Earlier development (2026-11-05)

### Posterize Time: a Frame Rate control

- **Frame Rate (fps), 12 fps by default.** The primary control is now a frame rate in fps
  instead of a count of frames per hold, with its own ` fps` unit and the same compact
  numeric field as every other property. 6, 8, 12, 15, 24 and 30 fps work directly, and a
  fractional rate (for example 7.5) is accepted. Offset stays as the secondary control and
  shifts where the holds begin. This is a sampling rate, not playback speed.
- **Drift-free holds.** Each project frame is quantized by the index of its window
  (`floor((frame - offset) * fps / project fps)`) and samples
  `round(index * project fps / fps)`, so 60 fps / 12 fps holds five frames, 30 / 10 and
  24 / 8 hold three, and 30 / 7 alternates four and five without accumulating error. A
  rate at or above the project's own rate adds no hold, and the timing of the clip, its
  duration and its audio are never touched.
- **Keyframed, and the whole state is held.** Frame Rate keyframes like every parameter;
  the rate curve is evaluated at the clip's local frame, and the sampled moment is used
  for the source picture, transforms, effect parameters, text animation and presets in
  both preview and export.
- **Old projects keep their picture.** A saved `step` (frames per hold) is converted on
  load using the project's frame rate (`fps = project fps / step`), with keyframed steps
  turned into keyframed rates and every keyframe time preserved, so a static old project
  holds exactly the same frames as before. Frames are cached by the existing source seek
  cache, so a held picture is not decoded again for each held project frame.
- `tests/test_posterize_time.py` covers the 60/12, 30/10 and 24/8 matrix, fractional
  drift, animated transform and effect state, keyframed rates, trimmed and split clips,
  preview/export parity, unchanged duration and audio, and legacy `step` migration; the
  existing temporal tests keep pinning the legacy alias.

### YouTube links in Smart Paste

- **Import YouTube Video.** A pasted `youtube.com/watch`, `youtu.be`, `/shorts/`, `/live/` or
  `/embed/` link opens its own window, and a `t=`/`#t=` timestamp becomes the start time.
  Lookalike domains (`notyoutube.com`, `youtube.example.com`), playlist/channel links and
  invalid IDs are explained instead of guessed at. Thumbnail, title, channel and duration are
  read off the GUI thread while the window is already open.
- **Only what the video offers.** The quality list comes from the formats yt-dlp reported -
  Best available, then the heights that really exist - and Audio Only lists the real bitrate
  streams. Exact format ids stay collapsed under *Advanced format settings*. The three modes
  are Video + Audio, Video Only and Audio Only (`.m4a`, an ordinary timeline, waveform and
  Auto Captions asset), and nothing is upscaled to fill a missing resolution.
- **A real range.** Start and end are exact `HH:MM:SS.mmm` fields with a draggable two-handle
  bar and a *Full Video* reset; negative, inverted, too-short and past-the-end ranges disable
  Import and say why. A ranged download is requested when the source supports it, and the
  bundled ffmpeg trims and muxes either way; a whole file that already fits is copied instead
  of re-encoded.
- **Isolated and safe.** `meenifx/media/youtube.py` is the only code that touches yt-dlp.
  Streams download into a temporary folder inside `Media`, free space is checked first, the
  result is validated and moved into place atomically, and Cancel (or a failure) removes the
  folder and leaves no asset. DRM, sign-in, paywall and region workarounds are deliberately
  absent: the permission box must be ticked, and *Use a Local File...* is always offered.
- **Ordinary project media.** The import lands in the Project panel with its thumbnail or
  waveform, inserts at the playhead when asked, is one undo step, and survives `.mfxpf`
  save/reopen through a relative `Media/...` path.
- **Fixed while testing it.** The first version of the import job emitted its result without
  the URL the slot expected; PyQt turns that argument mismatch into a process abort with no
  traceback. The signal now carries the URL, and finished runnables are held briefly after
  reporting so none is collected under its own thread.
- `tests/test_youtube_media.py` covers link forms, impostor hosts, metadata refusals,
  real-quality planning, the disk check and a real local download/trim/mute/audio-only pass;
  `tests/test_smart_paste_youtube.py` covers the window, permission gate, modes, range
  validation, cancel, the local fallback, undo and save/reopen.

### Smart Paste media links (and the Discord GIF crash)

- **Fixed the crash.** The download worker was a local variable, and a `QRunnable` with
  `autoDelete(False)` is owned by Python, so the job was collected while its thread was still
  running: the app died on the first frame of a Discord GIF download with no traceback. Jobs
  now live in `SmartPaste` until they report back, which `tests/test_smart_paste_links.py`
  asserts directly.
- **Import Media from Link.** A pasted direct link is probed (headers only) and then shows its
  host, media type, file name, size and, for signed links, expiry, with Add to Project Media,
  Insert at Playhead and Preserve original file; nothing is fetched before Import. From the
  Media panel the default is import-only, from the timeline it inserts at the playhead.
- **Real media, checked.** Images, animated GIFs, MP4/WebM/MOV, WAV/MP3/FLAC/OGG and Discord
  attachments are recognised by a parsed URL and, after the transfer, by the bytes themselves:
  the suffix on disk comes from a sniff plus a decode, not from the URL. Signed `ex`/`is`/`hm`
  queries are sent untouched and kept out of the file name and the log; loopback and private
  addresses are refused before a socket opens and again on every redirect; size, time and
  total-length limits apply, the stream is written to `.part` and renamed only when valid, and
  cancel or a broken transfer leaves nothing behind. Expired signed links say so.
- **Animated GIFs stay animated.** The original `.gif` is preserved byte for byte, and
  `read_gif_frames` now resamples each frame's own delay onto a steady rate (repeating long
  frames) and keeps transparency as alpha instead of flattening it, so GIF clips keep their
  duration, timing and alpha compositing.

### Simple Choker (Matte / Keying)

- **New alpha-edge effect.** `Simple Choker` moves a layer's matte: positive Choke pulls the
  edge inward so the visible alpha shrinks, negative pushes it outward, in project pixels so a
  50 % preview and the export agree. The edge is rebuilt from a signed distance field around the
  matte's 50 % contour, so fractional amounts land where they say and the result keeps an
  antialiased ramp; Edge softness turns that ramp into a smooth band, Edge quality trades
  distance accuracy for speed (Fast solves at half resolution), Preserve original RGB keeps
  partial-alpha colour, and Mix blends the result with the source.
- **No dark halos, no wasted frames.** Colour is never multiplied by alpha, and a grown edge
  takes the nearest opaque colour for its new ring instead of RGB 0. Only the matte's bounding
  box plus the growth margin is processed, the crop is padded so outward growth is not clipped
  by the region of interest, and results are cached per matte pixels and parameters.
- **Real layers, real keyframes.** Shapes, text, transparent PNGs and any alpha matte work;
  fully opaque footage is left byte-for-byte untouched and the card says it needs an alpha
  edge. Choke and Edge softness use the normal keyframe channels, and the browser card renders
  the effect on a soft-matted copy of the reference picture (the shipped café illustration is
  opaque), without touching the reference asset.
- `tests/test_simple_choker.py` covers every shape kind, text, a transparent PNG, partial
  alpha, positive/negative/zero choke, softness, quality, cache reuse, keyframes, the
  thumbnail, the opaque-footage note, preview/export parity and save/reopen.

### Free transform: Ctrl is a proportional centre resize

- **Ctrl now grows a corner drag around the centre.** Holding Ctrl keeps the aspect ratio and
  fixes the picture's middle instead of anchoring the opposite corner; with no modifier the
  corner drag stays free on each axis with the opposite corner pinned, as in Photoshop and
  Photopea. Shift and Alt still resize from the centre with free axes, Ctrl+Shift is the
  proportional centre resize with either modifier, and switching modifiers mid-drag rebases on
  the current picture, so the size and position never jump.

### Vector shape clips

- **Native shape layers.** Six shapes - Rectangle, Rounded Rectangle, Ellipse, Polygon, Star and
  Line - are drawn in the preview (Shift = circle/square, Alt = from the centre, Esc = cancel) or
  added centred from *Add > Shape*. A shape is an ordinary clip on a video track: same transform
  and free-transform corner behaviour, handles, keyframes, effects, blend modes, fades, split,
  copy/paste, undo and `.mfxpf` persistence; splitting, duplicating and pasting keep the settings.
- **Drawn, never upscaled.** `processing/shapes.py` renders the geometry from parameters at the
  render resolution, 2x supersampled and area-downsampled, in a bounded tile around the shape and
  cached per parameter set, so a 35 % preview and the 100 % export are the same picture and a small
  shape does not pay for the whole frame. Fill and stroke have their own colour and opacity, stroke
  width supports centre/inside/outside alignment (real distance rings, not a clipped half stroke),
  and each kind exposes its own geometry: corner radius, sides, star points and inner radius, line
  caps and arrowheads.
- **Animated like anything else.** Geometry numbers are `shape.*` channels with the usual
  stopwatch, diamonds, timeline rows and Graph Editor curves; fill and stroke colour are held keys
  (like text words) so they can be keyed without pretending to be a number.
- `tests/test_shapes.py` and `tests/test_shape_ui.py` cover every kind, transparency, antialiasing,
  stroke alignment, preview/export parity, keyframes, colour keys, split, paste, effects, undo,
  save/reopen, the toolbar, the live drag, the modifiers and an inspector corner resize.

### Preview free transform

- **Corner handles now behave like a free transform.** A corner drag scaled about the
  anchor, and the clip's default "Lock Scale X/Y" switch forced both axes on at once, so
  the opposite corner drifted and a picture could not be stretched. Each move is now
  computed from the transform captured at mouse-down: the dragged corner follows the
  pointer on both local axes while the opposite corner is translated back to exactly
  where it was - on rotated, non-uniformly scaled, re-anchored, non-square and text
  clips, at 50/100/200 % preview zoom. Ctrl locks the aspect ratio the picture had when
  the drag (or the current modifier state) began, Shift resizes from the centre,
  Ctrl+Shift does both, and a modifier change mid-drag rebases instead of jumping. Side
  handles stay single-axis, a resize clamps at a three-pixel minimum instead of flipping,
  and one drag is still one undo step. `tests/test_transform_corners.py` pins all of it.

### Design system and themes

- **New warm light and dark themes** driven by central tokens (`tokens.py`), a generated
  stylesheet (`stylesheet.py`) and shared components (`ui/components.py`, `ui/cards.py`).
  Switch via View → Appearance (Light / Dark / Match system); the choice is remembered.
- Redesigned Project panel (folder/media cards, empty state), Properties empty state,
  theme-aware timeline, track headers, toasts and dialogs, and theme-following icons.
- New meeniFX brand logos (light and dark variants). Preview canvas stays neutral.
- Audio and time effect cards show a theme-coloured icon instead of words.
- Screenshots: `docs/design/`, regenerated by `tests/ui_prototype.py`.

### Render performance

- **Opaque blurs no longer take the float path.** Gaussian and Box Blur premultiplied
  every layer into a float32 buffer and unpremultiplied the result, which on a layer with
  no transparency at all - the ordinary full-frame clip - cost about 90 % of the frame
  time. A fully opaque layer is now blurred directly in 8 bits: 1080p Gaussian Blur
  (radius 8) went from **134 ms to 27 ms per frame**, Box Blur (radius 6) from 110 ms to
  27 ms, and 720p from 75 ms to 12 ms. Layers that really carry alpha keep the
  premultiplied path, so letterboxed pictures, PNG characters and text still get soft,
  correctly feathered edges. The two paths agree within one 8-bit step and
  `tests/test_blur_fast_path.py` pins that, including the transparent-margin case.
- Remaining measured hotspots are unchanged and documented: Glow is 198 ms/frame at
  1080p Full (5 fps ceiling) and 80 ms at 720p; Mosaic is 40 ms at 1080p. Auto preview
  quality is what keeps those timelines responsive.

### Auto Captions

- Every source choice is now covered by a regression test: standalone audio, a linked
  video+audio pair (clicking either member analyses the sound), a video with embedded
  audio, a trimmed clip and a clip with a source offset. The reported "range has no audio"
  failure came from reading the clicked clip instead of its link group; the range now
  follows the audible members, so nothing has to be unlinked to generate captions.

### File dialogs

- The file dialog's stylesheet is built when the dialog opens instead of once at import,
  so it follows a theme switch instead of freezing the palette the app started with. The
  tinted navigation icons therefore always use the current theme's text colour, which is
  what `tests/test_filedialogs.py` now asserts (in either theme).

### Properties panel and effect browser

- **Grouped property cards:** effect parameters can declare a group (`ParamSpec.group`),
  and the inspector renders those as collapsible **Detection**, **Visualization**,
  **Labels**, **Connectors** and **Face Effects** sections. Detection and Visualization
  start open, the rest stay folded, and the state is remembered per effect and group
  across rebuilds. A group is only created when the effect declares members, so no card
  grows an empty heading (there is no Sprite Attachment setting in this build) and
  effects without groups render exactly as before. The *Advanced* fold now always sits at
  the end of the card, after the groups.
- **Tracker previews:** Face and Hand Tracker cards used to show only a word placeholder.
  They now fall back to the bundled `figure_cafe.png` reference picture with the tracker's
  own reticle icon on top - never a fake detection, since the still life has no face or
  hand. Audio and time effects keep the readable name fallback.

### Keyframe editing

- **Clean timeline, dedicated editor:** clips with animation now show one compact key-count
  badge instead of dense property rows and tiny diamonds. Clicking the badge raises the new
  dockable **Keyframe Editor**; trimming, moving, splitting, snapping and linked clips are
  unchanged.
- **Two named views, one model:** the Keyframe Editor and Graph Editor switch through
  explicit mode buttons and share channels and selection. Add/delete, multi-select, group
  drag, copy/paste, duplicate, typed values, previous/next, zoom, pan, fit, snapping,
  playhead scrubbing and collapsible groups live in the Keyframe Editor; easing, speed
  graphs and Bezier handles stay in the Graph Editor.
- **Drag duplication fixed:** the Keyframe Editor now rebuilds every mouse move from the
  press-time snapshot. Earlier moves can no longer leave extra keys behind, dragging starts
  without a jump, and one drag is one undo step. Regression tests cover multi-row group
  moves and repeated moves during a single drag.
- **Docking and layout:** the Keyframe Editor is a closable, resizable, floatable dock next
  to the Timeline, reopenable from View (`Shift+G`). Layout v9 adds it to saved workspaces;
  older layouts fall back to the default.
- **Animation baking:** Bounce still bakes visible Y motion plus opacity, and the baked Y
  curve is now asserted in both editors as well as at the model and export level.

### Reliability and performance

- **Auto Captions on linked clips:** linked-selection expansion no longer loses the clip
  that was clicked, and the caption range resolves a silent linked video to its audio (or
  uses an embedded audio stream). A longer picture no longer widens the analysed range into
  silence.
- **Bounce baking:** fixed `position_at()` dropping an animated Y when X was static. Baking
  Bounce now writes visible vertical motion as well as opacity, with a regression test
  comparing every sampled frame.
- **Auto preview quality:** fixed the climb-back branch, which compared the current scale
  against 1.0 and could never recover, so a slow frame permanently lowered preview quality.
- **Tracker cards:** icon-only effects (Face Tracker, Hand Tracker and audio/time effects)
  now show a readable label instead of an empty tile; no fake detection is drawn.
- **Timeline toolbar:** removed the redundant Linked Selection chain button. The same
  selection behaviour remains in Edit as a checkable command with Ctrl+Shift+K; explicit
  Link/Unlink commands and shortcuts are unchanged.
- **Effects browser:** animation cards now play a real six-frame hover preview generated by
  the preset evaluator, after a short delay, and restore the static tile when the pointer
  leaves or after two short loops. Previews stop during playback or when the panel hides.
  Browser toggles share one 16 px icon grid and 28 px button size, fixing the mismatched
  Favorites/Recently Used icons at scaled display sizes.

### Tracking

- Face, Hand and Blob Tracker now share one visualization system: box/corner/ellipse
  shapes, contours, sampled points, circles, labels, separate tracking numbers, opacity,
  glow, and capped connectors (nearest, all within range, or track order). Named looks
  (Clean, Minimal, Numbered, Neon, HUD, Motion points) are one choice; switching back to
  Custom keeps the values the look used. Old `draw_box`, `draw_hull`, `draw_mode` and
  `label_faces` values still load.
- Face Tracker faces can be blurred with a feathered ellipse edge (`Blur faces`,
  `Blur strength`, `Blur feather`) instead of a hard rectangle, and all three trackers
  expose Fast/Balanced/Precise detection quality. Detection runs on a smaller working
  image at the faster settings and boxes/contours are scaled back.
- Detector results are cached for eight frame/parameter combinations, so replaying the
  same preview frame or exporting after previewing it does not repeat face detection.
  Random overlay colours are now stable per object instead of changing every frame.
- Hand Tracker still uses the existing skin-colour/morphology detector. It is **not** a
  hand-pose model and can still pick up a face or forearm; the new max-area filter, shared
  overlay and cache reduce false boxes but do not turn it into validated hand tracking.
- **Face Tracker visualization controls are independent:** named looks now seed the
  ordinary parameters instead of overriding them at render time, so Numbers, Labels,
  Points, Outline, Circle and Box all work without choosing a look. A manual change after
  a look returns the Look dropdown to Custom, and the change survives preview refreshes.
  Labels are off by default and no longer append a track number; the separate Numbers
  control owns IDs, so the old `Face 1` text is gone unless explicitly enabled.
  Emptying the label text now removes the label instead of falling back to `Face`, and
  the field has a clear button; legacy projects with no saved label stay clean.
- Silenced OpenCV's benign `setPreferableTarget` warning during YuNet construction while
  preserving the detector's CPU execution and restoring OpenCV's log level immediately.

### Recording

- Added Performance / Balanced / High Quality recorder presets. They change bitrate
  headroom and encoder effort only; capture timing, timestamps and audio/video sync are
  unchanged.
- Screen recording uses a verified NVENC encoder when one works for gdigrab, reports the
  chosen encoder in Recorder settings, and falls back to software libx264 when hardware
  produces no data instead of leaving an empty take.

### Project organization

- **Project folders now accept real drag and drop:** folder rows highlight as drop
targets, one or many selected assets can be moved into a folder, between folders, or back
to the project root through the root row/panel background, and the toolbar has a Back
button. Folder rows use a recognizable folder icon, item counts, hover/selection styling
and tooltips. Long media names elide cleanly with the full name in the tooltip, and
moving an asset does not clear its cached thumbnail.
- Renamed the Project panel's bin terminology to **New Folder**, with nested virtual
  folders, breadcrumbs, drag and drop, multi-item moves and search across all folders.
  Folder organization is saved in `.mfxpf`; old flat bin lists still load.
- Folder deletion offers **Move Contents to Parent**, **Remove Folder and Media from
  Project**, or Cancel. It never deletes a source file, and timeline clips are removed
  only after an explicit warning.
- Split **Remove from Project** from **Delete File from Computer...**. The destructive
  action lists exact paths, warns about timeline use, delegates only to the operating
  system's Recycle Bin and refuses to proceed when no safe trash mechanism exists.

### Linking

- Added explicit **Link Selected Clips** (Ctrl+L, Edit menu and clip right-click menu) and
  **Unlink Selected Clips** (Ctrl+Shift+L). Linking no longer requires both clips to come
  from the same media file, so separately recorded video and audio can be paired.
- Link commands are disabled when they cannot apply, and right-clicking a clip that is part
  of a multi-selection now keeps that selection, so the pair can be linked from either clip.
- The **Linked Selection** toolbar toggle is documented and tooltipped as selection
  behaviour only; it no longer looks like the way to create a link.
- Unlinking one member of a larger group leaves the remaining members linked; the operation
  changes no timing, source range, effect or keyframe and is undoable.
- Splitting a linked pair continues to give every resulting pair its own group id; the
  regression test now asserts the independent ids, contiguous source offsets, undo and
  independent movement.

### Remove Dead Silence

- Reworked the dialog around three scopes: **Audio and linked video**, **Matching source
  video and audio** (same file, speed, direction and sync key, even when unlinked) and
  **Audio only**. The included clips are listed before Apply.
- Silence detection gains a **minimum retained segment** control; threshold is labelled
  dBFS.
- One authoritative frame plan now drives every included clip. Cuts are clamped to the span
  all members cover, so a cut can never pull a group out of sync; survivors are re-linked per
  retained segment, including pairs that intentionally start at different positions.
- Locked tracks block the pass with an explanation instead of silently cutting only the
  unlocked partners. Ripple closes gaps on the included tracks only.
- The command is available from the clip right-click menu and Tools → **Remove Dead
  Silence...**.

### Tests

- New `tests/test_blur_fast_path.py` (opaque shortcut against the float reference, alpha
  preservation, transparent margins, scale consistency) and `tests/test_caption_sources.py`
  (nine caption-source scenarios). `tests/test_filedialogs.py` gained a theme-switch case
  and a theme-independent icon-tint assertion; `tests/test_ui_refactor.py`'s empty-state
  check no longer asserts on an impossible count.

- Added model tests for cross-file linking, group unlinking, audio-only and matching scopes,
  locked tracks, offset pairs, short retained fragments and the corrected split-group
  contract.
- Added UI tests for the context-menu commands, Ctrl+L / Ctrl+Shift+L, selection
  preservation, the toolbar tooltip and the dialog's scope defaults.
