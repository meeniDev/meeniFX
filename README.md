# meeniFX Studios

Video editor with a dockable, resizable multi-track timeline.

## Requirements

- Python 3.10+
- PyQt6
- opencv-python
- numpy
- Pillow
- imageio

## Install and run

```powershell
python -m pip install -r meenifx/requirements.txt
python .\main.py
```

The application resolves bundled tile images, the demo clip, and default output paths relative to the project/application location rather than the current working directory.

## Current features

- PyQt6 main window with movable, floatable, tabbable Project, Preview, Effects, Properties, Timeline and Graph Editor docks; layout persists through QSettings (layout v8; the old Controls panel is gone and Effects/Properties are now two docks). View toggles visibility and Reset Layout restores the default docking arrangement. Preview quality, zoom and guides live in the one row under the preview, the master volume/meter sits beside it, Undo/Redo are in the top bar, and the playback FPS cap is in Preferences.
- Video/GIF/image import into video tracks and audio clips in audio tracks. Drop supported media on the Project panel, Preview, or the Timeline. A video that carries sound arrives as a **linked video + audio pair** (see *Video with sound* below).
- Multi-track timeline with frame-based positioning, thumbnails, playhead, a fixed ruler/loop strip that clips cleanly against the scrolling tracks, a translucent selection band, Vegas-style snapping feedback, split/razor, trim, move, Alt-drag time stretch, loop region, freeze frames, overlap modes, clipboard operations, and undo/redo.
- Snapping resolves to one deterministic target: the closest clip edge, playhead, marker, loop edge or frame 0 within an 8 px radius (12 px once engaged, so it never flickers), with clip edges winning a tie, then the playhead, markers, the loop and finally frame 0. The radius is in screen pixels at every zoom, Shift places the clip exactly where the pointer is, and linked clips move as a pair on their own tracks. The feedback names the target: the resolved edge of the target clip lights up white with a short gradient fading inward (only that edge of only that clip, never a full border or a full-height band), a thin guide marks the exact frame the clips landed on, the dragged clip shows where its edge sits, and a small caption reads e.g. `Screen 1 · end`. Several aligned boundaries all light up and the caption says `3 clip edges aligned`. The playhead snaps the same way, including to the edge of the clip it lands on.
- Named, coloured markers pinned to the ruler: click to select, drag to move (they snap to clip edges, the playhead, loop edges and other markers), Shift-click or Shift-drag a rectangle to select several, double-click or F2 to rename, right-click for colour/delete/go-to, and a non-modal Markers list under View. Markers and the loop region are saved with the project.
- Per-clip fades, Vegas style: a grip on the top-left and top-right corner of every video, image and audio clip (shown on hover or selection). Drag a grip sideways to set the fade length in frames with snapping, up/down (or Alt+drag) to bend the curve, right-click for Linear/Fast/Slow/Smooth/Sharp presets, double-click to clear it. Video fades blend the layers underneath (black on the bottom track), audio fades ramp the waveform, live playback, scrub bursts and the export through the same curve. Split keeps the fade-in on the left piece and the fade-out on the right, trimming clamps both, copy/paste/duplicate carry them, Shift sets the linked video/audio pair together.
- No status bar and no instruction text in the panels: state lives on the buttons, one-line hints live in tooltips, an empty timeline shows a faint "Drop files here", and the few messages that matter (failed import, finished export, missing audio device) appear as a small fading toast at the bottom of the window.
- One global dark theme for the whole application, including tooltips (dark #1b2230 tooltip, ~450 ms delay, shortcuts in the text), menus, context menus, combo drop-downs, message boxes and the non-native file dialogs.
- Preview fit and percentage zoom, cursor-centered wheel zoom, pan, guides, and resize handling. The preview keeps two sizes: the logical project frame (which drives fit, zoom, pan and every crop decision) and the render size (project size x quality, drawn into the logical rectangle). Pixel-based effect parameters (mosaic tiles, ASCII cell size, tracker line thickness, minimum face size, blob areas, blur radius) are project pixels scaled by the quality factor, so lowering the quality only softens the image - it never changes the framing, and a 35% preview is the same picture as the export.
- Per-clip Mosaic, ASCII Art, Face Tracker, Hand Tracker, and Blob Tracker effects; editable clip speed/reverse/loop. Every effect declares the media types it can process (`MEDIA_TYPES` on the effect class), and the browser, the timeline drop target and the engine all read that one declaration: on an audio clip the visual effects are listed greyed as *"(video only)"* and cannot be added, a drag or double-click is refused with a status message, and an effect loaded from an older project onto the wrong media type is switched off (kept, with an explanation in its card) instead of crashing or rendering. Each effect publishes a parameter table (range, unit, default, pixel-based or not) that the properties panel and the parameter audit test both build from, so a control can never write a value the effect does not read.
- Keyframes on every numeric effect parameter: the stopwatch turns animation on and is accent-coloured while active, the slider and number follow the playhead, the diamond is filled exactly on a keyframe and hollow between, and the previous/next buttons jump between them (the cluster only appears for animated parameters). On the timeline a clip with keyframes shows a small diamond and an expand arrow that opens one animation row per parameter; its diamonds can be selected, dragged in time (snapping to the playhead) and deleted, all as single undo steps. See *Keyframe workflow* below for the full timeline keyframe editing, the envelopes, clip opacity and the text animation presets.
- One numeric widget everywhere (`SliderField`: fixed label column, slider, typed number box, optional stopwatch): effect parameters, preview quality, playback FPS, speed, master volume, clip speed, fades, timeline zoom, track height and the export dialog. The number box selects all on focus, accepts both `,` and `.` and values like `50%`, applies on Enter, cancels on Esc, steps on Up/Down (Shift x10, Ctrl x0.1), and double-clicking the label resets the default. A drag is one undo step, a typed edit is one step.
- Whole-project or loop-region video/GIF/image export (the chosen range is written from frame 0) and Save Frame; frame processing and export run off the UI thread with progress and cancellation.

## Transform, motion paths and the Graph Editor

- Every video/text clip has a full transform: position X/Y, independent scale X/Y (or
  uniform), rotation, opacity, anchor point and horizontal/vertical flips. Values are
  precise numbers in the Properties panel, in project pixels, degrees and percent, and
  every one of them has a stopwatch, keyframe diamond and previous/next key buttons.
- **Auto-Keyframe** (toolbar button or `K`) decides what a change does to an animated
  property: on, it keys the new value at the playhead; off, it shifts the whole curve and
  never creates a keyframe silently. Pressing the diamond keys a static property too.
- The preview is an editor as well: a bounding box with eight scale handles, a rotation
  knob, the visible anchor crosshair, the motion path, warp pins and snap guides. Drag the
  picture to move (Shift constrains to one axis), the handles to scale (Shift forces a
  free ratio, the "Uniform scale" switch is the default), the knob to rotate (Shift snaps
  to 15 degrees), the crosshair to move the anchor without moving the picture. Arrow keys
  nudge by one project pixel (Shift: ten). Each drag is exactly one undo step.
- Snapping guides light up at the frame edges and centre, and the overlay is editing-only:
  it is painted on top of the preview widget and can never reach the exported frames.
  Coordinates are always converted through the project frame, so a 4K project previewed at
  25 % quality still shows handles computed in 4K space.
- **Motion paths** (toolbar eye icon, Transform menu) show every position keyframe on the
  picture. Points can be dragged, each point has spatial Bezier in/out handles, and the
  current position is drawn on the path.
- The **Graph Editor** dock (toolbar button or `G`, tabbed next to the timeline) plots
  the selected clip's channels as curves or as speed graphs, preserving the animation when
  switching. Curves are sampled from the animation itself every 0.5 px (up to 2400 points)
  and drawn through the real easing and Bezier handles, so what you shape is what renders.
  A plain click picks one key, Ctrl/Shift toggles keys in and out, clicking empty space
  clears the selection and moves the playhead there, a plain drag in empty space draws a
  box and a plain drag on a key moves the whole selection in time and value together.
  Double-clicking a curve adds a key whose value comes from the evaluated curve, so
  nothing jumps. Handles are visible and draggable for every selected key on any visible
  channel, Delete removes the selection in one undo step, and Ctrl+A/C/V/D/Z, Fit All, Fit
  Selected, Reset View, wheel zoom (anchored at the pointer) and middle-drag pan are all
  there. Right-click a key or a curve for the easing presets and Add/Delete Key.
- Easing is offered in friendly families first - Linear, Ease In/Out/In Out, Sine,
  Quadratic, Cubic, Quartic, Quintic, Exponential, Circular, Back, Bounce, Elastic (each
  In/Out/In Out), Smooth and Custom - with curve thumbnails, and the technical forms
  (Hold, Bezier, Auto Bezier, Continuous Bezier, Broken Bezier Handles) live under an
  *Advanced Interpolation* submenu. Bounce and Elastic are real mathematical functions, not
  approximations. Auto and Continuous handles are computed from the neighbouring keys, so
  moving a key reshapes the curve the way it does in After Effects. The same menu is on the
  timeline keys and inside the Graph Editor; custom presets can be saved, renamed and
  deleted from Preferences and are shared between projects.
- Easing is stored per keyframe (`interpolation` plus normalised Bezier handles), so the
  preview, the Graph Editor and the exporter read the same numbers.

## Keyframe workflow (timeline and Graph Editor)

- **One animation model**: the stopwatch in the Properties panel, the diamonds on the
  timeline and the curves in the Graph Editor all read and write the same channels
  (`meenifx/model/channels.py`). There is no second keyframe system and no bake step, so
  the three views can never disagree.
- **Expandable clips**: a clip with keys shows a small diamond badge and an expand arrow;
  expanding it adds one lane per animated channel (transform, effects, text, plus the
  opacity and gain envelopes). Clips without animation show neither.
- **Keyframe lane**: diamonds are click-selected, Ctrl-click adds to the selection,
  Shift-click selects a range, and Ctrl+drag in empty lane space (or in an expanded row
  band) draws a marquee. Selected keys drag together in time and snap to the playhead,
  markers, the loop edges and other keys; `Esc` cancels a drag and every drag is one undo
  step. Delete removes the selection, `Ctrl+Shift+C` / `Ctrl+Shift+V` copy and paste at the
  playhead (relative timing kept), `Ctrl+Shift+D` duplicates in place, and `Ctrl+K` (or the
  toolbar diamond) keys every animated channel of the selected clips at the playhead. The
  selection is shared with the Graph Editor both ways, and double-clicking a key opens it
  there (*Keyframes > Double-click Keyframe Opens Graph Editor*; switched off, the same
  double-click deletes the key, the classic behaviour).
- **Key context menu** on one key or a multi-selection: Linear, Hold, Ease In, Ease Out,
  Ease In Out, Smooth, Auto Bézier, Continuous Bézier, Custom Bézier (opens the handles in
  the Graph Editor), Bounce, Elastic, Overshoot, Copy, Paste, Delete and *Open in Graph
  Editor*. Easing applies to the whole selection at once, and entries that make no sense
  for the selection are hidden rather than disabled.
- **Universal Clip Opacity** (0-100 %, default 100) in the Transform card: a real,
  keyframeable channel that multiplies the whole layer (picture and text, before the
  effects' own opacities), separate from the Text card's fill/shadow/background opacities
  and from any effect opacity. Preview and export read the same value.
- **Opacity envelope** on every visual clip: a faint line across the clip body that can be
  dragged (with a live `%` readout), keyed by double-click, and handled from its
  right-click menu (add/remove key, Easing, *Open in Graph Editor*, show/hide, reset). Each
  drag is one undo step; hiding the envelope never deletes the animation.
- **Audio gain envelope** on audio clips: the same line, in dB (0 dB by default, -60 dB is
  silence, the ceiling comes from Preferences > Playback & Performance > *Gain headroom*,
  0-24 dB). It is keyframeable, drives the real mixer and the export, and is independent of
  the linked video clip. **Audio Gain** (numeric box, slider and reset) in the audio
  clip's Inspector edits the same channel, so slider and envelope always agree.
- **Anchor presets**: a 3x3 grid plus *Center Anchor Point* in the Transform card. Choosing
  a preset keeps the picture where it is on screen (the position is compensated) unless
  *Keep picture in place* is switched off; the anchor is draggable in the preview,
  keyframeable and undoable like any other channel.
- **Text animation presets** in the Text card: Fade In/Out, Slide In/Out Left/Right/Up/Down,
  Scale In/Out, Pop In, Typewriter, Blur In/Out, Rotation In/Out and Bounce In, applied to
  an **In** or **Out** slot with a duration, easing and delay. *Preview* sets the loop range
  to the covered frames and plays it. A preset writes ordinary keyframes on the existing
  channels (transform opacity/position/scale/rotation plus `text.blur` and `text.reveal`),
  so the result can be edited key by key; the text formatting is untouched, Typewriter
  reveals the string character by character in the renderer, Blur is a Gaussian blur on
  the text layer, and both export like any other channel. Applying a preset replaces only
  the keys the previous one wrote, *Clear* restores the resting values, and the preset,
  slot and generated key times are stored in the project file.
- **Format 5**: `.mfxpf` v5 adds the per-clip `gain` column and the text animation block;
  version 1-4 projects migrate on load with no change in appearance or sound.
- Limits: the marquee needs empty lane space or an expanded row band, so a Ctrl+drag over
  clip bodies still extends the clip selection. The envelope line is grabbed within 3 px
  (a wider band would steal the press that drags the clip, because a 0 dB gain line runs
  through the middle of an audio clip), and near the left/right edge the fade grips win the
  hit test - the exported channel and the Inspector row are always available. Gain
  automation is sampled with the audio block (~15 times a second), so a gain ramp shorter
  than that is piecewise linear rather than sample-exact, and gain is per clip (a linked
  pair is adjusted one clip at a time).

## Warp effects

- **Puppet Warp** is a real mesh deformation: pins move the picture through a
  moving-least-squares affine warp (with fixed frame-border anchors so the layer never
  tears), not a translation or scale. Add pins on the picture with the Puppet tool (P) and
  drag them; Ctrl/Shift-click adds or removes a pin from the selection, and dragging one
  selected pin moves them all by the same offset so a group keeps its shape. Pins are
  keyframable and share the same easing, Graph Editor curves, timeline rows and undo steps
  as every other channel; with auto-keyframe on, the status line confirms the keys it wrote.
  Ctrl+C/Ctrl+V copy and paste pin keyframes (pasted keys start at the playhead), Ctrl+R
  sends the selected pins back to their rest pose and clears their keys, `L` locks a pin
  (it becomes an anchor), Shift+E disables one (it leaves the deformation entirely, which is
  not the same as locking), Delete removes the whole selection in one undo step. All of it
  is also on the preview's right-click menu. Mesh density, stiffness, influence and mesh
  visibility live in the effect's Properties card; the deformation maps are cached and the
  preview runs at preview quality while the export always renders at full quality.
- Also on the layer pipeline: **Corner Pin** (four draggable corners in the preview),
  **Perspective** (real keystone foreshortening, not a shear), **Mesh Warp** (2x2 to 5x5
  grid with draggable nodes), **Skew/Shear**, **Distortion**, plus the Flip commands in the
  Transform menu. Numeric warp parameters are keyframable like any other effect
  parameter, and all of them render identically in the preview and in the export.

## Colour, blur and glow

- **Colour Correction** (Color Correction category): brightness, contrast, saturation,
  exposure (in stops), highlights, shadows, temperature, tint, gamma, hue rotation, red,
  green and blue gains plus a measured colour balance, all keyframable and all applied in
  one documented order (exposure, white balance, RGB gains, brightness, contrast, shadows
  and highlights, gamma, saturation, hue, preset grade) in floating point on premultiplied
  BGRA, so the alpha channel survives and nothing clips. Presets: Black & White, Warm, Cool,
  High Contrast, Vintage, Desaturated, Cinematic. Stack several instances and they chain;
  Reset returns the identity.
- **Blur & Sharpen**: Gaussian, Box, Directional (any angle), Radial, Zoom and Motion blur
  (with a shutter-angle parameter). **Glow & Light**: glow with intensity, radius,
  threshold, colour, opacity, blend mode (screen/add/lighten/overlay) and quality, with
  bloom compensation so a wide radius still reads as light instead of a faint smudge.
- The preview and the export run the very same effect objects through the same pipeline;
  there is no second renderer for either.

## Audio effects

- Ten real signal processors in the **Audio Effects** category, applied to the samples, not
  to a knob that does nothing: Gain, Fade In, Fade Out, EQ (three bands), Low-Pass,
  High-Pass, Compressor (threshold/ratio/attack/release/makeup), Limiter, Reverb, Delay/Echo
  and Stereo Pan. They are ordinary effect entries (`MEDIA_TYPES = ("audio",)`), so they
  live in the same stack as the picture effects, are undoable, saved in `.mfxpf`, and show
  up only on audio clips.
- Gain, fades and pan are automated per sample, so their keyframes are sample-accurate;
  compressor and limiter work in 64-sample blocks with carried state; the filters are FIR
  kernels and reverb/delay are impulse responses (FFT overlap-add). The chain output passes
  through a soft clipper when its peak exceeds 0 dBFS, so a stack cannot produce a clipped
  file. Decoded audio is cached per clip and chain signature, which is what makes scrubbing
  with an effect chain usable.
- Filters and reverb/delay are built from the clip's first frame's parameters (they are
  time-invariant by nature); gain, fades and pan follow their curves over the whole clip.

## Text generator

- **Add Text Clip** (`Ctrl+T`, the `T`-shaped toolbar button, or Text menu) creates a
  generated text clip at the playhead on a video track. It is an ordinary clip: it has the
  same transform (move/scale/rotate/anchor/opacity), fades, track placement, split, trim,
  copy/paste, undo and export path as a video, and every effect works on it (mosaic,
  ASCII, blurs, trackers, warps...).
- The Properties panel gets a **Text** card with the words box, font family, bold /
  italic / underline, alignment, fill / stroke / shadow / background colours, size,
  stroke width, tracking, line spacing, block offset, shadow offset/blur/opacity,
  background padding/opacity and wrap width. Size, stroke, tracking, spacing, offsets,
  blur/opacity, padding and wrap are animatable channels with the usual stopwatches,
  diamonds and Graph Editor curves.
- **Keyed text**: the words are a held channel. Press the diamond in the Text card (or
  `Ctrl+Shift+T`) to key the current text, or simply type with Auto-Keyframe on - the new
  words are keyed at the playhead, so one clip can change what it says over time without
  being cut. Text keys show in the timeline's animation row and can be dragged or deleted
  there; splitting a text clip keeps each side's words and keys.
- Text is drawn with Pillow at the render resolution (so it is sharp in the export and
  stays correctly framed at any preview quality), supports multi-line text, and the
  generated layer keeps alpha, so it composites over video like any other clip.
- Limits: Typewriter is the per-character animation (there are no per-glyph keyframes), no text-on-a-path and no automatic wrapping inside
  a shape - `Wrap` wraps at a pixel width, and the block is centred in the frame before the
  transform is applied.

## Video with sound

- Importing a file that has both streams (drag from the Project panel, Preview or Explorer, *Insert at playhead*, or double-click) creates **two clips**: the picture on the video track, and a **linked audio clip on the lane directly below** with the same source file, start, in/out and speed. The video clip is display only, the audio clip is sound only, so nothing is ever decoded twice. Files with no audio stream get a single video clip, audio files get a single audio clip, and a missing lane is created on demand.
- Importing into the Project panel only adds media to the bin - it never touches the timeline.
- A pair shares a persistent `link_id`, so it survives save/load, undo/redo, copy/paste, splitting (both halves stay linked) and tracks being reordered. Selecting, moving in time, trimming, splitting, deleting and ripple-deleting act on both clips. They stay two clips; there is no hidden merge.
- Dragging is per axis: a link moves **horizontally** as one (same frames, same length, same speed), but a vertical drag re-lanes only the clip you grabbed, because track assignment belongs to each clip. A linked video therefore lands on another video lane while its audio stays on its own audio lane - and the other way round. The dragged clip still lands only on lanes of its own type, and a locked lane still refuses the move.
- **Linked Selection** in the toolbar (default on) decides whether a click on one half also selects the other. Turning it off never breaks a link - it only stops the selection from following it.
- Right-click a clip: *Unlink Video and Audio* separates a pair; *Link Video and Audio* joins a compatible unlinked video+audio selection. Linking never moves a clip, so a pair with mismatched positions shows the **amber chain badge with its offset** (e.g. `+12f`) until the clips line up again; a synced pair shows a small grey chain.
- **Alt-drag either end** of a clip to stretch and change its speed; on a linked pair the partner stretches with it (same start frame, length and speed) in one undo step, so picture and sound never drift apart. A partner on a locked track is left untouched. Speeds are resampled, so a stretched linked audio clip changes pitch with its speed - the status bar says so instead of silently pretending otherwise.
- Timeline drag-and-drop carries media **ids** on an internal MIME type, works in grid and list view from the thumbnail or the file name, shows a semi-transparent snapped preview of both clips before the drop, drops at the cursor (not the playhead), never overwrites, and inserts nothing at all if the whole insertion cannot be placed (locked lane, no room). A completed drop is one undo step; redo restores the pair and its link.
- Audio playback comes from the audio clip: it stays in sync through seeks, scrubs and trims (source timestamps follow the in-point), honours clip volume, the clip's gain (dB) and keyframed gain envelope, fades, effects, speed and reverse, and the mixer never schedules the same file twice. Muting or hiding a video track does not silence the linked audio; muting the audio track silences only the sound.
- `.mfxpf` files store the two clips separately with their own ids, `link` id, `stream` role, position, in/out, properties and track. Format version 2 adds the `stream`/`link` clip columns; version 1 projects migrate with every clip marked `both`, so their existing audio keeps playing and no partner clip is invented.
- Limits: audio is resampled to follow the clip's speed, so speed changes pitch too (there is no time-stretch/pitch-preserve stage); there is no automatic re-sync command - drag either half to line it up and the badge clears.

## Playback, preview cache and performance

- **Why playback used to freeze**: the old preview path asked a thread pool for a frame and then *threw the result away unless the frame number still equalled the playhead when it arrived*. During playback the playhead advances every tick, so almost every finished render was discarded and the picture stayed on one frame while the audio ran on. The same single-flight flag also blocked the next request, so a slow effect could stall the preview for seconds.
- **What replaced it** (`meenifx/preview/`): one dedicated render thread with two queues - an **interactive** slot that keeps only the newest request (a slow effect can never build a backlog) and a **background** queue for prerendering, which is paused and throttled while playback runs. Results go into a two-tier cache and the newest *finished* frame is always displayed, so the picture follows the timeline and late frames are dropped instead of blocking. Stop/Pause stay instant because the GUI thread never renders; audio remains the playback clock.
- **Cache indicator** above the ruler (5 px band, colours read straight from the cache): **green** = frame present at the current quality, **yellow** = queued/rendering right now, **red** = must be rendered. Changing the preview quality switches to that quality's entries, so the bar never claims a frame that is only cached at another size.
- **Cache** (`meenifx/preview/cache.py`): RAM LRU bounded by a byte budget over a disk tier of `.npy` frames. Disk frames live in `<project>/Cache/preview`, are written only by background rendering, and are disposable. A `manifest.json` stores a timeline fingerprint: reopening the same project reuses them, any other fingerprint wipes the folder (RAM too) instead of showing stale pictures. Nothing is ever written into `.mfxpf`.
- **Invalidation** (`meenifx/preview/invalidation.py`): every clip has a digest of exactly the properties that reach the renderer (source, timing, speed, fades, transform values including keyframes, effect parameters, text). Only the frame ranges of clips whose digest changed are dropped (`±1` frame for fades and transforms), so editing one clip keeps the rest of the timeline green.
- **Preview Cache menu**: *Render Preview* (`Ctrl+Shift+R`), *Render Selected Region* (`Ctrl+Shift+E` - the loop region, else the selected clips), *Cancel Preview Rendering*, *Clear Preview Cache*, *Preview Cache Settings...* and the *Performance Overlay* toggle (`Ctrl+Shift+F`). Rendering shows a progress dialog with a working Cancel; the editor stays usable meanwhile because all of it happens on the render thread, and the frames finished before a cancel stay cached.
- **Idle prefetch**: after a pause or an edit, the frames just after the playhead are quietly rendered (window length in Preferences) unless playback is running.
- **Quality**: Full / 1/2 / 1/4 / 1/8 / Auto. Auto starts at the panel fit and adapts to measured render time (drops to half/quarter/eighth when a frame is slower than 2x the frame interval, climbs back when it is faster than half). Project resolution and export quality are never affected.
- **Proxy media** (`meenifx/media/proxy.py`): with *Automatic Proxy Generation* on, heavy sources get an ffmpeg-generated MP4 in `<project>/Cache/proxy` and the preview decodes that instead; `engine.export` suspends the proxy switch so exports always use the originals. Measured decode cost on a noise-heavy 1080p clip: 8.5 ms/frame from the source, 1.3 ms/frame from a 960 px proxy.
- **GPU**: the app detects OpenCL, then **measures** `resize`, `warpAffine`, `warpPerspective`, `remap` and `cvtColor` on a 1080p frame at startup (in a background thread) and routes each call to whichever side is faster (`meenifx/preview/backend.py`). On this test machine (RTX 3070) OpenCL measured **3-4x slower** than CPU for those operations - a full BGRA frame costs more to copy than the kernel costs to run - so Auto correctly keeps the CPU path and Preferences says so. *On* forces the OpenCL path (useful for experiments, visually identical within sub-pixel rounding), *Off* hides it. Mask/composite blending stays in vectorised numpy. Hardware **encoding** is real: MP4 export uses NVENC when it verifies a probe encode and falls back to OpenCV's writer otherwise (`meenifx/media/hardware.py`), and Preferences reports which one ran. Hardware **decoding** is requested via `CAP_PROP_HW_ACCELERATION`, but this OpenCV build reports `VIDEO_ACCELERATION_NONE`, so frames are decoded in software here; the value the driver reports is what the Preferences dialog shows.
- **Preferences > Playback & Performance**: GPU Auto/On/Off, preview quality, RAM and disk cache limits, cache location, background rendering, automatic proxies, proxy width, hardware decoding/encoding, the live backend line and a *Clear preview cache now* button. All values persist in the app INI (`playback/...`), and never inside a project file.
- **Performance overlay**: real playback FPS, target FPS, dropped frames (clock ticks that produced no new picture), last/average render time, cache frames and hit rate, preview resolution and the active backend. Nothing is estimated or hardcoded.
- **Robustness**: a crash guard replaces `sys.excepthook` at startup so an exception inside a Qt slot is logged to the app-config `meenifx-errors.log` instead of aborting the application, and the render thread is stopped deterministically on exit.
- **Limits**: OpenCL compositing is slower than the CPU path on typical hardware, so GPU acceleration of frame operations is *available but not selected* by Auto; there is no CUDA build of OpenCV in the wheel, no shader pipeline, and no hardware decode in this OpenCV build. Prerendering is single-threaded by design (it must never compete with playback). Proxies are generated one file per source and are not reused across projects.
- **Measured** with `python tests/preview_bench.py` on this machine (RTX 3070, 16 threads, 1080p synthetic source, 30 fps project): 1080p no effects renders in 23 ms/frame at Full and 7.6 ms at 1/2; the old discard rule showed **1 distinct frame in 2 s while the playhead advanced 59 frames**, the new pipeline shows **30.8 fps with 0 dropped frames**; three stacked 1080p layers at 1/2 play at 13.2 fps uncached (33 dropped) and 30.6 fps from the cache; puppet-warp and animated-transform timelines play at 30.3 fps cached; a 4K/three-effect timeline runs 26.7 fps uncached and 27.5 fps cached; forced OpenCL takes 15.4 ms/frame versus 9.3 ms on the CPU; NVENC encodes 200 1080p frames in 1.27 s versus 1.36 s for the OpenCV writer (and 80 4K frames in 1.59 s versus 1.22 s - the pipe overhead dominates short exports, which is why the writer is chosen per machine and reported).

## Projects

- meeniFX starts on a **start window** (recent projects as cards, search, sort, New / Open, drag a `.mfxpf` file onto it). "Show on startup" is also under File > Preferences. The old demo is only under Help > Open sample project.
- A project is one **`.mfxpf`** file (meeniFX Project File): a ZIP container with versioned entries for settings, media references, tracks/clips (columnar, so thousands of clips stay small and fast), effects, keyframes, markers, UI/layout state and thumbnails. Metadata is JSON; the thumbnail JPEGs are stored without recompression. A format version (currently 5: v2 added the linked-stream columns, v5 the clip gain column and the text animation block), a validation pass and a migration hook (`meenifx/project/container.py`) are built in. Original media is never embedded; paths are stored relative to the project with an absolute fallback.
- New Project creates `<Location>/<Name>/<Name>.mfxpf` plus `Recordings/` and `Cache/`. File > Project Settings reopens the same fields; a frame-rate change converts all clip times (one undo step), a resolution change letterboxes clips to fit.
- Saving is atomic (temp file, verified, then renamed) and keeps one `.bak`. Autosave every 3 minutes goes to `<project>.mfxpf.autosave` and is offered for recovery after a crash.
- The **Project** dock (on the left) lists the project's media in grid or list view with background thumbnails (cached in `Cache/thumbs`), search, bins, Import, drag to the timeline/preview, double-click to insert at the playhead, Rename, Relink, Remove. Missing media shows a warning icon in the timeline and the panel instead of failing.
- A `.mfxpf` path passed on the command line is opened on startup, so double-clicking a project works once the association exists. `packaging\associate_mfxpf.ps1` registers the extension (per user, no administrator rights) for the packaged `dist\meeniFX\meeniFX.exe`; `-Remove` undoes it.
- Tests: `python tests\test_project_format.py`, `test_project_storage.py`, `test_project_app.py`, `test_start_window.py`, `test_project_panel.py`, `test_project_large.py`.

## Recorder

- **Record** in the top toolbar (or File > Record...) opens a compact panel with four source cards, each with a switch: **Webcam** (camera list with refresh, resolution/fps limited to what the camera offers, live preview), **Screen** (entire screen or a window, live preview, "Show mouse cursor"), **Microphone** (device list, live level meter, gain) and **System audio** (the output device whose sound is captured, its own meter and gain). Any combination works. Devices and settings are remembered; a missing saved device falls back to the default.
- Audio is captured through WASAPI directly (`meenifx/recording/wasapi.py`, ctypes, no extra dependency): microphones from a capture endpoint, system sound from the render endpoint opened in **loopback** mode, which is the only way Windows lets an application hear what the speakers play. The endpoint's shared-mode mix format is captured, converted to the requested rate (48 kHz by default, 44.1/32/22.05 kHz selectable, stereo or mono) and written as 16-bit WAV. Each source has its own gain (the sliders), and the meters read the post-gain signal of that same stream, so what you see is what is recorded.
- Long recordings stay in sync: the WAV length follows the device frame count, and the resampler slowly trims its ratio against the wall clock (bounded to 0.3 %, inaudible), which is the clock the video timestamps use. A lost device ends that source cleanly: the file is closed and playable, the rest of the recording continues, and the toast says what happened.
- On stop, each source is saved as its own file (`Webcam <date>.mkv`, `Screen <date>.mkv`, `Voice <date>.wav`, `System sound <date>.wav`). When a picture and sound were recorded together, the sound is mixed and **muxed into the video** (`Screen <date> (with sound).mkv`, AAC 192 kbps, original video stream copied) and that file is inserted as a **linked video + audio pair**, exactly like an imported clip with sound. The WAVs stay on disk as the uncompressed masters.
- Press Record: optional 3-2-1 countdown, then the main window minimizes and a small always-on-top bar (time, pause, stop, mic level) appears. The bar is excluded from screen capture. Global hotkeys, active only during a countdown/recording: **Ctrl+Shift+R** stops, **Ctrl+Shift+P** pauses/resumes (changeable under the gear). The gear also holds the recording folder, video bitrate, audio sample rate, mono/stereo and the "put the sound inside the recorded video" switch.
- **Voice-over** records only the microphone while the timeline plays from the playhead.
- Files go to the project's `Recordings` folder (change it under the gear) and the clips land on separate tracks at the playhead position where Record was pressed, as one undo step, followed by a short toast.
- Pause is implemented as segments: after a pause each source has a `part 2` file/clip, placed at the right time so sources stay in sync.
- The screen is recorded by the ffmpeg binary the app already ships (gdigrab), not by Qt: Qt grabs the desktop through DXGI duplication, which hands over a pointer-free image and has no cursor option, so a Qt screen recording could never show the mouse. gdigrab draws the pointer itself, which is what the "Show mouse cursor" switch turns on and off. Packets are flushed and the encoder runs without lookahead, so an interrupted recording still leaves a playable Matroska segment. The Qt capture stays on for the card's live preview, so the preview is still the real screen.
- Limits: the webcam is still recorded by Qt (MPEG-4 in Matroska, encoder chosen by Qt). Screen recording is H.264 (libx264, veryfast) because it needs to draw the pointer; its bitrate follows the same area-based estimate, or the value under the gear. No region capture. Closing a webcam can block the UI for ~2.5 s inside Qt, so it is deferred until after the clips are inserted. Recording the same microphone in two applications at once is up to Windows: a shared-mode stream is grabbed by the first one that opens it.
- Tests: `tests\test_recording.py`, `tests\test_recording_audio.py` (WAV writing, resampling, drift correction, muxing, linked insertion, and a real capture from this machine's own endpoints), `tests\test_screen_recording.py` (the gdigrab command, the cursor switch, and a real one-second take that must be readable), `tests\test_stream_frames.py`; real-device scripts `tests\rec_hw_check.py` and `tests\rec_app_check.py`.

## Shortcuts

Ctrl+N new project; Ctrl+O open; Ctrl+S save; Ctrl+Shift+S save as; Ctrl+I import media; Ctrl+R export; Ctrl+Alt+S save frame; Ctrl+T add text clip; Ctrl+Shift+T text keyframe; Space playback; C/S split; Ctrl+Z/Y undo/redo; Ctrl+C/X/V/D/A clipboard and select all; Home/End timeline start/end; I/O set the loop start/end at the playhead; Q toggle loop playback; F8 snap; V select tool; Shift+F add a freeze frame; M add a marker at the playhead; [ and ] jump to the previous/next marker; F2 rename the selected marker; Delete removes selected markers (or clips); Shift+Delete ripple delete.

Transform tools: V selection, T transform, P puppet warp, K auto-keyframe, G graph editor, Ctrl+Shift+P show motion paths, arrows nudge, Shift+F7/F8/F9 fit/fill/centre, Shift+F10 reset the transform, Esc cancels a drag and drops the overlay selection. With pins selected in the preview: Ctrl+C/Ctrl+V copy and paste their keyframes, Ctrl+R resets them to rest, Ctrl+A selects them all, L locks, Shift+E enables/disables, Delete removes them - all of it also on the preview's right-click menu. Fade grips: drag sideways for length, up/down (or Alt+drag) to bend, Shift to apply to the linked clip, right-click for presets, double-click to clear. Text-entry widgets keep their normal Qt shortcuts.

Keyframes: Ctrl+K add a key at the playhead, Ctrl+Shift+C/V/D copy, paste and duplicate the selected keys, Delete removes them, Ctrl+drag draws the keyframe marquee, double-click a key opens it in the Graph Editor, Esc cancels a keyframe or envelope drag. Envelopes: drag the line to change the value, double-click to key it, right-click for the menu (add/remove key, easing, show/hide, reset).

Preview cache: Ctrl+Shift+R render preview, Ctrl+Shift+E render the selected region, Ctrl+Shift+F performance overlay.

Mosaic tile assets (`black.png`, `white.png`, `whiteandblack.png`) are stored in `assets/mosaic/`. The default sample media remains in the project root. Face Tracker uses OpenCV's YuNet model in `assets/models/` (OpenCV 5 no longer ships Haar cascades); `assets/haarcascades/` keeps the classic cascade for OpenCV 4 installs.

## Tests

```powershell
python -m pytest tests --ignore=tests/test_project_app.py --ignore=tests/test_project_large.py --ignore=tests/test_project_panel.py --ignore=tests/test_start_window.py
```

`test_project_app.py`, `test_project_large.py`, `test_project_panel.py` and `test_start_window.py` are
app-driving scripts that call `os._exit` when imported (they build a real window and check it by
hand), so they have to be ignored when pytest collects the whole folder - otherwise collection
ends the process. Run them directly with `python tests\<name>.py`.

`tests/test_effects_extended.py` renders synthetic frames through the colour, blur and glow effects (per-parameter change detection, preset identities, alpha preservation and preview/export equality), `tests/test_audio_effects.py` checks the DSP against the signal itself (filter response, compressor gain reduction, limiter ceiling, reverb tail, delay timing, pan law, chain caching), `tests/test_easing_presets.py` verifies every easing family, the auto/continuous handles and the preset menus, `tests/test_graph_editor.py` drives the Graph Editor with real mouse and wheel events (sampling, painting, selection, box select, key add, delete/undo, presets, handle drags, zoom and pan), and `tests/test_timeline_snapping.py` covers snapping resolution, determinism, the pixel threshold at several zooms, linked pairs, the playhead and the published glow/caption feedback. `tests/test_puppet_pins.py` drives pin multi-selection, group drags, auto-keyframe feedback, disabling, reset and the pin-key clipboard. `tests/test_av_links.py` covers the link model (planning, trimming, splitting, undo, save/load, migration and the mixer's one-segment-per-file rule), `tests/test_av_import_ui.py` drives the real window with real drag events from the Project panel to the canvas, and `tests/test_media_probe.py` checks header-only audio detection against hand-built MP4/Matroska files plus the A/V fixture from `tests/av_fixture.py`. `tests/test_transform_animation.py` covers the transform/keyframe/easing model, motion-path handles and the project format; `tests/test_transform_ui.py` and `tests/test_warp_ui.py` drive the preview override, the inspector and the Graph Editor with real mouse events (handles, snapping, puppet pins, corner pin, mesh nodes, motion paths, keyframe clipboard, easing presets, undo); `tests/test_text_generator.py` and `tests/test_text_ui.py` cover text rendering, text keyframes, effects on text, splitting and save/load. `tests/test_effect_parameters.py` renders a synthetic frame with every effect parameter at its minimum and maximum (and every toggle/choice alternative), checks that animated parameters change over time and evaluate at clip-local frames, and that the export path matches the preview. `tests/test_preview_cache.py` covers the cache (LRU, disk tier, manifest validation, pending/invalidation), the render thread (latest-wins, prerender, playback priority), the digest-based invalidation, the GPU dispatcher and the metrics. `tests/preview_live_check.py` and `tests/preview_ui_check.py` drive the real window (playback advancing, cache bar colours, prerender/cancel, invalidation, disk cache, Preferences) and `tests/preview_bench.py` produces the measured numbers above. `tests/test_preview_quality.py` checks that framing is identical at 10/35/50/75/100% and at Fit/100%/200% zoom with panning. `tests/test_keyframe_workflow.py` drives the whole AE-style workflow (timeline lanes, marquee, multi-move with snapping, clipboard, easing menu, shared selection with the Graph Editor, clip opacity, both envelopes, audio gain, anchor presets, text animation presets, rendering and the format-5 round trip), while `tests/test_keyframe_ui.py` and `tests/test_numeric_fields.py` cover the properties/timeline keyframe controls and the shared numeric widget. They run headless (`QT_QPA_PLATFORM=offscreen` is set by `tests/conftest.py`).
