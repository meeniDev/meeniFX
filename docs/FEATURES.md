# Feature reference

This is the detailed description of what meeniFX Studios implements, preserved from
the original README. Start with [GETTING_STARTED.md](GETTING_STARTED.md) if you are new,
or [SHORTCUTS.md](SHORTCUTS.md), [RECORDING.md](RECORDING.md) and
[EFFECTS_AND_ANIMATIONS.md](EFFECTS_AND_ANIMATIONS.md) for the focused guides.

## Current features

- PyQt6 main window with movable, floatable, tabbable Project, Preview, Effects, Properties, Timeline, Keyframe Editor and Graph Editor docks; layout persists through QSettings (layout v9; the old Controls panel is gone and Effects/Properties are now two docks). View toggles visibility and Reset Layout restores the default docking arrangement. Preview quality, zoom and guides live in the one row under the preview, the master volume/meter sits beside it, Undo/Redo are in the top bar, and the playback FPS cap is in Preferences.
- Video/GIF/image import into video tracks and audio clips in audio tracks. Drop supported media on the Project panel, Preview, or the Timeline. A video that carries sound arrives as a **linked video + audio pair** (see *Video with sound* below).
- Multi-track timeline with frame-based positioning, thumbnails, playhead, a fixed ruler/loop strip that clips cleanly against the scrolling tracks, a translucent selection band, Vegas-style snapping feedback, split/razor, trim, move, Alt-drag time stretch, loop region, freeze frames, overlap modes, clipboard operations, and undo/redo.
- Snapping resolves to one deterministic target: the closest clip edge, playhead, marker, loop edge or frame 0 within an 8 px radius (12 px once engaged, so it never flickers), with clip edges winning a tie, then the playhead, markers, the loop and finally frame 0. The radius is in screen pixels at every zoom, Shift places the clip exactly where the pointer is, and linked clips move as a pair on their own tracks. The feedback names the target: the resolved edge of the target clip lights up white with a short gradient fading inward (only that edge of only that clip, never a full border or a full-height band), a thin guide marks the exact frame the clips landed on, the dragged clip shows where its edge sits, and a small caption reads e.g. `Screen 1 · end`. Several aligned boundaries all light up and the caption says `3 clip edges aligned`. The playhead snaps the same way, including to the edge of the clip it lands on.
- **Pre-compositions**: select one or more clips (video, images, text, shapes and their linked audio) and choose *Composition > Precompose Selected Clips* (`Ctrl+Alt+C`) or the timeline context menu. The clips **move** into a reusable composition with their relative timing, track order, effects, keyframes, source offsets, transforms, text/shape data and linked audio, and one composition clip replaces them in place; the whole operation is a single undo step. Compositions are project assets with their own icon, folders, search, rename, drag-to-timeline, **Duplicate Composition** and **Delete Composition** (the warning counts the nested clips that will go with it). Double-click a composition clip to open it: a breadcrumb bar above the panels always shows the main timeline and the path to the composition being edited, with **Back to Parent**, **Composition Settings** and **Open Composition**. Nested compositions render in full (video, images, text, shapes, effects, keyframes, alpha and multiple tracks) with cycle prevention, and their audio is routed through the child mix at the right time, once, honouring both levels' volume and fades. New compositions default to a **Transparent background**, so a cutout inside a pre-composition composites over tracks below the nested clip on the parent timeline; Composition Settings can turn it off for the legacy opaque background, and older projects stay opaque. **Render Composition** / **Rebuild Composition Cache** cache the current timeline, the bar reports the cached-frame count, each editing context gets its own disk cache folder, and the cache digest follows edits inside a composition so the parent preview is never stale; export renders from the model and never reuses preview frames.
- Named, coloured markers pinned to the ruler: click to select, drag to move (they snap to clip edges, the playhead, loop edges and other markers), Shift-click or Shift-drag a rectangle to select several, double-click or F2 to rename, right-click for colour/delete/go-to, and a non-modal Markers list under View. Markers and the loop region are saved with the project.
- Per-clip fades, Vegas style: a grip on the top-left and top-right corner of every video, image and audio clip (shown on hover or selection). Drag a grip sideways to set the fade length in frames with snapping, up/down (or Alt+drag) to bend the curve, right-click for Linear/Fast/Slow/Smooth/Sharp presets, double-click to clear it. Video fades blend the layers underneath (black on the bottom track), audio fades ramp the waveform, live playback, scrub bursts and the export through the same curve. Split keeps the fade-in on the left piece and the fade-out on the right, trimming clamps both, copy/paste/duplicate carry them, Shift sets the linked video/audio pair together.
- No status bar and no instruction text in the panels: state lives on the buttons, one-line hints live in tooltips, an empty timeline shows a faint "Drop files here", and the few messages that matter (failed import, finished export, missing audio device) appear as a small fading toast at the bottom of the window.
- Warm light and dark themes (View → Appearance: Light, Dark or Match system; remembered between launches) that cover tooltips, menus, context menus, combo drop-downs, message boxes and the non-native file dialogs. See *Design system* below.
- Preview fit and percentage zoom, cursor-centered wheel zoom, pan, guides, and resize handling. The preview keeps two sizes: the logical project frame (which drives fit, zoom, pan and every crop decision) and the render size (project size x quality, drawn into the logical rectangle). Pixel-based effect parameters (mosaic tiles, ASCII cell size, tracker line thickness, minimum face size, blob areas, blur radius) are project pixels scaled by the quality factor, so lowering the quality only softens the image - it never changes the framing, and a 35% preview is the same picture as the export.
- Mosaic, ASCII Art, Face Tracker, Hand Tracker and Blob Tracker plus the new Glitch, Stylize, Light, Time and LUT packs and one-click **Looks** (see *Compositing, effects and looks*); per-clip **blend modes**, **adjustment layers** and a **Mix** slider on every effect card. Every effect declares the media types it can process (`MEDIA_TYPES` on the effect class), and the browser, the timeline drop target and the engine all read that one declaration: on an audio clip the visual effects are listed greyed as *"(video only)"* and cannot be added, a drag or double-click is refused with a status message, and an effect loaded from an older project onto the wrong media type is switched off (kept, with an explanation in its card) instead of crashing or rendering. Each effect publishes a parameter table (range, unit, default, pixel-based or not) that the properties panel and the parameter audit test both build from, so a control can never write a value the effect does not read.
- **Simple Choker** (Matte / Keying) moves a matte's edge: positive Choke shrinks the visible alpha, negative grows it, with edge softness, an edge-quality choice, optional colour repair and a Mix slider. It works on shapes, text, transparent PNGs and any alpha matte, and says so instead of doing nothing on fully opaque footage. Choke and Edge softness are ordinary keyframe channels.
- **Smart Key** automatically detects the main subject and removes the background with the optional U^2-Net model, with no green screen, colour picker or brush stroke. Remove Background analyses the clip, Subject Auto/Choose handles multiple subjects, Quality, Edge Smoothness, Feather, Expand/Shrink and Composite/Isolated/Matte previews tune the result, and Reanalyze refreshes the cache. Chroma Key remains the separate screen-colour tool. **Roto Brush** is the manual alternative for a subject on a normal background, rebuilt as a real rotoscoping workflow: activating it puts the preview in a dedicated mode (no transform box or handles, circular brush cursor), green drags mark the subject, Alt-drags mark what to remove, a Refine Edge brush (`B`) helps difficult boundaries, and the purple outline follows the segmented boundary. A base frame is segmented from those hints; Forward, Backward and Analyze All carry the matte through the clip's **source** frames with optical flow plus per-frame refinement, with progress and Stop, and corrections can be painted on any frame. Preview modes (Original, Outline, Isolated Subject, Alpha Matte, Transparency Checkerboard, Composite) and matte controls (smooth, feather, expand/shrink, edge refinement, cleanup) all change the result. Freeze keeps the analysed masks on disk so previews stop re-segmenting, Unfreeze/Recalculate/Clear Cache manage them, and nothing is written into the source media. Masks are stored beside the preview cache and keyed by the strokes, so a changed stroke never reuses stale pixels; the project keeps only the base frame, strokes, settings, propagation range and cache metadata. The preferred segmentation backend is the optional U^2-Net ONNX model, downloaded from the Roto toolbar with progress, cancellation and SHA-256 verification. Basic GrabCut remains available without a download and is labelled as such. SAM 2 was evaluated but not bundled because it requires PyTorch and a large checkpoint. See `docs/ROTO_BRUSH.md`.
- Keyframes on every numeric effect parameter: the stopwatch turns animation on and is accent-coloured while active, the value follows the playhead, the diamond is filled exactly on a keyframe and hollow between, and the previous/next buttons jump between them (the cluster only appears for animated parameters). The timeline stays clip-focused: a clip with animation shows one compact `◆ 12 keys` badge, and clicking it opens the dedicated **Keyframe Editor** instead of packing property rows inside the clip. See *Keyframe workflow* below for timing/value editing, the envelopes, clip opacity and the text animation presets.
- One numeric widget everywhere (`ScrubField`: fixed label column, a compact value with its unit, optional stopwatch and keyframe cluster): Transform, text, audio gain, every effect parameter including Mix, clip speed, fades, the Animation card, the export dialog, playback FPS and track height. The value is the handle: drag it sideways to scrub (right = up), Shift for a tenth of the speed, Ctrl for ten times, Alt to land between the field's steps; click once (or double-click, or just start typing) to type an exact number, Enter applies, Esc puts the previous value back; the arrow keys step, Page Up/Down jump ten steps, and the arrows that appear beside the value on hover step it too; double-clicking the label resets the default. A whole drag is one undo step and a typed edit is one step. Units (`px`, `%`, `°`, `dB`, `f`, `s`, `fps`) are drawn dimmed beside the number and accepted when typed, with both `,` and `.` as the decimal separator.
- **Vector shape clips**: Rectangle, Rounded Rectangle, Ellipse, Polygon, Star and Line, drawn
  with the shape tool (pick one in the toolbar dropdown, then drag in the preview) or added from
  *Add > Shape*. Shift draws a circle/square, Alt draws from the centre, Esc cancels, and the new
  clip is selected with its transform handles ready. A shape is a real clip on a video track - the
  same transform, handles, keyframes, effects, fades, blend modes, split, copy/paste and export as
  a picture - rendered from its parameters at every resolution (never a bitmap upscale), with
  fill/stroke colour and opacity, stroke width and centre/inside/outside alignment, corner radius,
  sides, star points and inner radius, line caps and arrowheads. Geometry and colours are
  keyframeable (`shape.*` channels), and the inspector groups them in a Shape card that only
  appears while a shape is selected. See *Shape generator* below.
- Whole-project or loop-region video/GIF/image export (the chosen range is written from frame 0) and Save Frame; frame processing and export run off the UI thread with progress and cancellation.

## Design system

- `meenifx/tokens.py` holds every colour (light and dark palettes), spacing, radius, type size and control height; `meenifx/stylesheet.py` generates the application stylesheet from them and `meenifx/theme.py` applies it (`configure_theme`, `resolve_theme`).
- Shared widgets live in `meenifx/ui/components.py` (buttons, icon buttons, search field, dropdown, section header, collapsible group, status dot, empty state) and `meenifx/ui/cards.py` (media and folder cards). Icons come from one vector set in `meenifx/icons.py` and repaint in the current theme's colours.
- Widgets that paint their own colours register with `meenifx/ui/themed.py` (`bind_theme`, `on_theme_change`) so a theme switch restyles them without restarting. The preview canvas stays neutral in both themes.
- Effects with no meaningful still preview (audio and time effects) show a theme-coloured icon on their card instead of text.
- `python tests/ui_prototype.py` renders the window in both themes at 1000x600 and 1420x880 into `docs/design/`. Text contrast is checked by `tests/test_ui_tokens.py`.

## Effects browser, previews and styles

- One browser, three tabs. **Effects** lists every effect with a category filter, search by
  name or everyday word ("pixel" finds Mosaic), favourites, recently used, and a grid or
  list view that is remembered between sessions. **Animations** lists the animation
  families described above, and **Styles** holds the Looks, shipped and your own.
- Every tile is a **real preview rendered by the same pipeline the export uses**: a private
  effect instance runs on the reference picture through the same blend and compositor as a
  clip, at the card's device pixel ratio and the chosen preview quality. Effects that have
  no meaningful still fall back in that order: a real render, then the bundled
  `figure_cafe.png` reference picture with the effect's own icon on top (Face and Hand
  Tracker - a still life has no face or hand, so no detection is drawn and the tile never
  pretends one was found), then the effect's name. Audio and time effects use the name.
  Rendering runs on background workers
  behind a shared cache keyed by effect, parameters, picture and quality, so scrolling
  never blocks the UI; only the cards in view are rendered, and the cache is dropped when
  the preview settings change.
- The reference picture is `figure_cafe.png`, read once from beside `main.py`. Preferences >
  Appearance > **Effect Previews** switches it to your own image (Choose Image… / Reset to
  Default) and sets the **Background** (checkerboard, dark, light) and the preview
  **Quality** (low, medium, high); the browser's `…` menu opens the same page. The setting
  is global, is never written into a project, and falls back to `figure_cafe.png` when the
  custom image cannot be read. Transparency is kept, the aspect ratio is letterboxed rather
  than stretched, and the picture is scaled with smooth filtering.
- **Grouped property cards**: a tracker's settings fold into **Detection**, **Visualization**,
  **Labels**, **Connectors** and **Face Effects**. Detection and Visualization are open,
  the rest are one click away, and the open/closed state survives a rebuild. The sections
  come from the effect's own parameter table (`ParamSpec.group`), so a card only shows a
  heading it actually implements - no Sprite Attachment placeholder, and an effect with
  no groups (Mosaic, Glow, audio) still renders flat. Technical values stay in the card's
  *Advanced* fold, which now sits at the end of the card below the groups.
- **Double-click applies**; double-clicking a card whose effect is already on the selected
  clip removes it again. Applied effects carry a check mark, and when a clip holds several
  instances of one effect the browser asks which one to remove. Every apply and remove is a
  single undo step. Effects that cannot run on the selected media type are disabled and say
  why.
- A **Style** applies as one group: Properties shows a single **Style card** with its member
  effects collapsed inside it (each still expandable, disableable and reorderable within
  the group, in the order the style defines), plus the style's Strength. Removing the style
  removes exactly its members and leaves every other effect on the clip untouched;
  disabling the group disables the whole look. Older Looks in an existing `.mfxpf` file open
  as the same groups, and their ids keep working.

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
  picture to move (Shift constrains to one axis), a handle to resize, the knob to rotate
  (Shift snaps to 15 degrees), the crosshair to move the anchor without moving the
  picture. A corner drag is a free transform: width and height change independently and
  the opposite corner stays exactly where it is, even on a rotated, already-stretched or
  re-anchored clip. Hold Ctrl while resizing to keep the picture's proportions and grow
  around the centre, Shift to resize from the centre with free axes, Ctrl+Shift for both.
  Side handles change one axis only. The scale box never
  flips or collapses: it stops at a three-pixel minimum, and "Lock Scale X/Y" in the
  inspector stays the numeric-scrub lock. Arrow keys nudge by one project pixel (Shift:
  ten). Each drag is exactly one undo step.
- Snapping guides light up at the frame edges and centre, and the overlay is editing-only:
  it is painted on top of the preview widget and can never reach the exported frames.
  Coordinates are always converted through the project frame, so a 4K project previewed at
  25 % quality still shows handles computed in 4K space.
- **Motion paths** (toolbar eye icon, Transform menu) show every position keyframe on the
  picture. Points can be dragged, each point has spatial Bezier in/out handles, and the
  current position is drawn on the path.
- The **Keyframe Editor** dock (`Shift+G`, tabbed next to the timeline) is the timing and
  value view: one readable row per animated transform, text, audio or effect property,
  grouped and collapsible. Add/delete, click/Ctrl-click multi-select, live drag, group
  move, copy/paste, duplicate, typed values, previous/next navigation, wheel zoom,
  middle-drag pan, Fit, frame snapping, playhead scrubbing and selection shared with the
  Graph Editor are all here. A drag rebuilds from the press-time snapshot, so it moves the
  existing keys, never leaves a trail of duplicates, and commits one undo step.
- The **Graph Editor** dock (toolbar button or `G`, tabbed next to the Keyframe Editor)
  plots the selected clip's channels as curves or as speed graphs, preserving the animation
  when switching. Its **Keyframe Editor | Graph Editor** buttons switch to the timing view
  without changing the data. Curves are sampled from the animation itself every 0.5 px (up
  to 2400 points) and drawn through the real easing and Bezier handles, so what you shape is
  what renders.
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
  selected keys in the Keyframe Editor and inside the Graph Editor; custom presets can be
  saved, renamed and
  deleted from Preferences and are shared between projects.
- Easing is stored per keyframe (`interpolation` plus normalised Bezier handles), so the
  preview, the Graph Editor and the exporter read the same numbers.

## Keyframe workflow (Keyframe Editor and Graph Editor)

- **One animation model**: the stopwatch in the Properties panel, the Keyframe Editor and
  the Graph Editor all read and write the same channels (`meenifx/model/channels.py`).
  There is no second keyframe system and no bake step, so the views can never disagree.
- **Compact timeline**: a clip with keys shows one badge with its key count and a diamond;
  clicking it selects the clip and raises the Keyframe Editor. The main timeline keeps its
  trimming, moving, splitting, snapping and linked-clip behaviour, and never grows rows of
  tiny diamonds. Clips without animation show no badge.
- **Keyframe Editor**: one lane per animated property, grouped under Transform,
  Compositing, effect names, Text and Audio. Groups collapse; clicking a key selects it,
  Ctrl/Shift-click builds a multi-selection, and dragging one selected key moves the whole
  selection together in time. `Esc` cancels; one drag is one undo step. `+ Key` adds on the
  active property at the playhead, the toolbar value field edits the selected keys (or
  keys the active property when nothing is selected), and Delete, Copy, Paste, Duplicate,
  previous/next, Fit, wheel zoom, middle-drag pan, frame snapping and the playhead are all
  available without leaving the panel. The dock is closable and comes back from View >
  Keyframe Editor (`Shift+G`).
- **Key context menu** in the Keyframe Editor: Delete, Copy, Paste, Duplicate, Fit
  Selection, Add Keyframe Here and *Open in Graph Editor*. Easing and Bezier handles live in
  the Graph Editor, where they apply to the same shared selection.
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
  the linked video clip. **Audio Gain** (scrubbable value and reset) in the audio
  clip's Inspector edits the same channel, so the Inspector and the envelope always agree.
- **Anchor presets**: a 3x3 grid plus *Center Anchor Point* in the Transform card. Choosing
  a preset keeps the picture where it is on screen (the position is compensated) unless
  *Keep picture in place* is switched off; the anchor is draggable in the preview,
  keyframeable and undoable like any other channel.
- **Animation families**: the browser's Animations tab folds 81 shipped In/Out/Loop presets
  into 22 families with a Mode and Direction selector, search, favourites and grid/list
  views; instances stack on top of the clip's own transform and keyframes, animate text per
  letter/word/line, show as bars on the timeline, bake to keyframes, and can be saved,
  imported and exported as `.mfxpreset`. See *Animation presets* below.
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
- **Format 5/6**: `.mfxpf` v5 adds the per-clip `gain` column and the text animation block;
  v6 adds the per-clip preset instances. Version 1-5 projects migrate on load with no
  change in appearance or sound. See *Animation presets* for the instance model.
- Limits: keyframe selection and dragging live in the Keyframe Editor (Ctrl/Shift-click
  builds the selection, one drag moves all of it), so Ctrl+drag on the timeline always
  extends the clip selection and never picks up keys. The envelope line is grabbed within 3 px
  (a wider band would steal the press that drags the clip, because a 0 dB gain line runs
  through the middle of an audio clip), and near the left/right edge the fade grips win the
  hit test - the exported channel and the Inspector row are always available. Gain
  automation is sampled with the audio block (~15 times a second), so a gain ramp shorter
  than that is piecewise linear rather than sample-exact, and gain is per clip (a linked
  pair is adjusted one clip at a time).

## Animation presets (In / Out / Loop, on any clip)

- The **Animations** tab ships the same **81 presets** folded into **22 families** (Fade,
  Slide, Pop, Zoom, Rotate, Bounce, Blur, Wipe, Wave, Flip, Shake, Float, Pulse, Jitter,
  Wiggle, Camera Shake, ...): one card per behaviour instead of separate In/Out/Loop
  entries. A **Mode** selector (In / Out / Loop / Both) and a **Direction** selector sit
  above the grid; categories All / Motion / Fade / Scale / Rotation / Text / Distortion /
  Other, search by name or keyword, favourites and recently used, and a grid or list view.
  Double-click a card, press Apply or drag it onto a clip to apply the chosen variant.
- A preset is an **instance on the clip**, not a replacement for its animation. Each has a
  category (In, Out, Loop), a window, and the knobs Duration, Intensity, Easing, Direction,
  Stagger, Unit and Seed. A clip can carry several (In + Loop + Out) and they compose as a
  layer **on top of** the clip's own transform and keyframes: position, rotation, blur, hue
  and light add, scale and opacity multiply. So a hand-animated move keeps playing while a
  shake rides on it, and nothing you keyed is overwritten. In is anchored to the clip start
  and Out to the clip end. A **Loop** repeats its cycle across the whole clip: **Cycle** is
  the length of one pass and keeps repeating when the clip is trimmed or extended, **Span**
  limits how much of the clip it covers, **Cycles** stops it after N passes (0 repeats to
  the end), the **Loop** menu chooses **Repeat** or **Ping-Pong** (there and back), **Gap**
  holds the first pose between passes and **Phase** shifts the pattern in frames. Splitting
  a clip cuts the span at the split and carries the cycle phase onto the right half, so the
  two pieces look exactly like the original.
- Everything is deterministic: noise-based loops (shake, wiggle, jitter, flicker) are
  functions of the Seed, and the instances are part of the render-cache fingerprint, so the
  preview, the cache, baked keys and the export produce identical frames.
- **Text per unit**: the Unit dropdown (Letter, Word, Line, Whole) makes a text preset
  animate each unit separately with a staggered delay, in a direction (left to right, right
  to left, from centre, edges in, random by seed); Direction/Stagger shape the timing, and
  each unit gets its own offset, scale, rotation, opacity, blur and colour shift around its
  own centre. At rest - before, between and after its window - the text draws through the
  static, kerning-exact path, so the layout never shifts by a pixel. Glyph bitmaps are
  rendered once and cached, and a title's pixels only cover a band of the frame, so a
  60-character title with per-letter motion costs about as much as the video under it:
  measured at 1920x1080, 48 ms a frame at Full quality, 31 ms at 75 % and 15 ms at 50 %
  (median of 5 renders) - 32 fps and better at the two preview scales, while the same
  title drawn statically with its default shadow took 67 / 40 / 18 ms before the text
  buffer was trimmed to its content.
- The **Animation card** in Properties lists the instances on the selected clip with their
  **Mode** and **Direction** menus and their knobs (the shared scrubbable values),
  **Bake** and remove. Changing Mode or Direction edits that same instance - it never
  stacks a second animation; a Loop also exposes Cycle, Span, Cycles, Loop mode, Gap and
  Phase. Baking samples the instance into
  ordinary keyframes on the existing channels in one undo step, so the Graph Editor can then
  edit every value; per-unit instances are not bakeable there (their motion lives in the
  text layout) - use Split text instead.
- **Split text**: right-click a text clip > *Split Text* > *Into Letters / Into Words / Into
  Lines* turns it into one text clip per unit, each at exactly the position it had, keeping
  the picture's transform, effects, fades, timing and presets (a per-unit instance becomes a
  whole-clip instance on every piece). One undo step.
- **On the timeline** each instance is a thin coloured bar on its clip: green at the start
  for In, orange at the end for Out, blue across the span for Loop, with a faint tick where
  every cycle starts. Drag the inner edge to change the duration (In/Out) or the span
  (Loop), snapping to the playhead, markers, clip edges and other keys - Shift switches
  snapping off; right-click a bar to bake or remove it. A whole drag is one undo step.
- **Library and sharing**: shipped presets are plain JSON files in `presets/`; a user
  folder (the platform config directory, `%APPDATA%\meeniFX\presets` on Windows) is merged
  on top, so your own presets survive upgrades and override shipped ones with the same id.
  *Save as preset* stores the selected clip's current animation, and Import/Export write
  `.mfxpreset` files - versioned JSON (`format: "mfxpreset"`, `version: 1`) that can be
  shared; a file from a newer version is refused rather than half-read.
- **Format 6**: `.mfxpf` v6 adds the per-clip `presets` column; v1-5 projects open with no
  presets and no change in appearance.
- Limits: presets are a layer, so until you bake, the Graph Editor shows nothing for them;
  per-unit blur and tint are per glyph (a layer-wide Gaussian would smear the letters
  together); and *Split text* keeps the words visible at the playhead - text keys that
  changed the string earlier in the clip are not carried onto the pieces.

## Compositing, effects and looks

- **Blend modes per clip**: Normal, Add, Screen, Multiply, Overlay, Soft Light, Hard Light,
  Difference, Darken, Lighten, Color Dodge and Color Burn in the clip card. One
  implementation (`processing/blend.py`) is used by the preview, the export and the
  thumbnails, and the blended colour is mixed by the layer's own alpha, so a rotated clip,
  text or a PNG character blends by its real coverage instead of its bounding box.
- **Adjustment layers**: *Add > Adjustment Layer* creates a clip with no picture on a video
  track. Its effects run over everything already composited below it, and only while the
  clip plays; it trims, fades, keyframes its opacity and takes a blend mode like any clip.
- **Mix on every effect card**: 0-100 %, animatable like any parameter, blending the
  untouched layer with the effect's result (colour and alpha). At 0 % the effect is skipped
  entirely; Bake and Reset leave it at 100 %.
- **New effects**, grouped by category with two or three main values and the rest under
  *Advanced*; pixel parameters scale with the preview quality and every random pattern is
  seeded from the seed plus the frame, so a preview is the exported picture:
  - *Glitch & Distortion*: RGB Split (linear/radial with edge falloff), Glitch (slices,
    blocks, channel tears, per-frame seed), Wave Warp, Bulge / Pinch, Twirl, Mirror /
    Kaleidoscope.
  - *Stylize*: VHS / CRT (scanlines, tape noise, chroma bleed, grain, curvature, wobble,
    vignette), Film Grain, Halftone, Posterize, Edge Outline (cartoon), Duotone, Invert,
    Vignette.
  - *Blur & Sharpen*: the existing Gaussian, Box, Directional, Radial, Zoom and Motion
    blurs cover the blur pack; their radii are project pixels at every quality.
  - *Light*: Flash / Strobe with a flash, fade or pulse envelope, Exposure Flicker,
    procedural Light Leak, Bloom Streaks.
  - *Time*: Posterize Time (hold each picture at a chosen **Frame Rate** in fps, 12 by default - a sampling rate, not playback speed; the field keeps three decimals so 23.976, 29.97 and 59.94 stay exact as 24000/1001 and 30000/1001, and a keyframed rate is sampled on deterministic boundaries), Stutter (hold, then jump) and
    Echo / Trail (blend the clip's own earlier frames). The frame timing is decided once in
    `EditorEngine.held_frame`, so picture, effects and transform freeze together.
  - *Color*: LUT with a `.cube` loader, an intensity value and three built-in tables; the
    lookup runs through a cached 7-bit table, one gather per pixel, and the colour depends
    only on the pixel, so every render scale matches exactly.
- **Looks**: a *Looks* category with ten shipped bundles (VHS Tape, CRT Monitor, Anime
  Glow, Cyber Glitch, Dreamy Bloom, Retro Film, Comic Halftone, Neon Night, Faded Memory,
  Hard Contrast B/W). Applying one inserts a normal editable effect stack plus a single
  *Look* card whose Strength scales the whole stack. There is a *...* menu beside the
  effect search to save the current stack as a user look, import one, or export a
  `.mfxlook` file - versioned JSON (`format: "mfxlook"`, `version: 1`) in `looks/`, with a
  per-user folder merged on top; a file from a newer version is refused rather than
  half-read.
- **Format 7**: `.mfxpf` v7 adds the per-clip `blend` and `adjust` columns; v1-6 projects
  migrate on load, blending normally and carrying no adjustment layer.
- **Performance** (`tests/effects_bench.py`, 1920x1080, median of five renders):

  | stack | 100% | 75% | 50% |
  |---|---|---|---|
  | composite only | 22 ms | 16 ms | 7 ms |
  | VHS/CRT + Film Grain + Bloom Streaks | 583 ms | 354 ms | 161 ms |
  | Glitch + RGB Split + Light Leak | 242 ms | 136 ms | 64 ms |
  | Halftone + Edge Outline + Glow | 556 ms | 304 ms | 130 ms |
  | LUT + Duotone + Vignette | 232 ms | 133 ms | 58 ms |

  Three heavy effects stacked are *not* a 30 fps CPU preview at 1080p; the preview quality
  slider (75 % by default, 50 % for heavy grades), proxy media and the RAM/disk frame cache
  are what keep playback usable. The LUT path was made 5.6x faster, bloom 2x, light leaks
  2.4x and glow 1.3x by computing smooth fields on a bounded grid and using a lookup table
  instead of eight per-pixel gathers.

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

## Shape generator

- The toolbar's shape dropdown (next to Text) picks one of six shapes: Rectangle, Rounded
  Rectangle, Ellipse, Polygon, Star, Line. The button re-arms the last choice; *Add > Shape* has
  the same six commands and drops a default-sized shape in the middle of the frame.
- Drag in the preview to draw it: the outline follows the pointer live, Shift constrains to a
  circle/square, Alt draws from the centre, Esc cancels without creating anything. On release the
  clip is created at the playhead (on the topmost clear video lane, or a new one) with a default
  five-second duration and is selected with its transform handles, exactly like a picture.
- It is a clip like any other: move, trim, split, duplicate, copy/paste, reorder, blend modes,
  effects, fades, keyframes and undo/redo all work, it saves in `.mfxpf`, and it uses the same free
  transform as images and text (no modifier = non-uniform corner resize with the opposite corner
  pinned, Ctrl = proportional around the centre).
- Rendering is native vector work, not a bitmap: the geometry, colours and stroke are drawn by
  OpenCV at the render resolution of the moment, at 2x supersampling with antialiased edges and a
  transparent background. A 35 % preview and a 100 % export are the same picture, at any project
  size. Only a tile around the shape is drawn, it is cached per parameter set, and the inspector's
  opacity/colour/stroke values are ordinary `shape.*` channels, so a small shape stays cheap.
- The Shape card in Properties holds the type, fill colour and opacity, stroke on/off, colour,
  width, opacity and alignment, width, height, corner radius (rounded rectangle), sides (polygon),
  points and inner radius (star), caps and arrowheads (line), plus position, rotation and the
  clip's own opacity from the shared transform. Colours are keyed like text words (held, not
  interpolated), so fill and stroke colour can change over time without a numeric fudge.

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
- **Blur cost**: Gaussian and Box Blur paint a fully opaque layer - the ordinary full-frame clip - straight onto the 8-bit picture instead of round-tripping it through a float32 premultiplied buffer. On this machine that took 1080p Gaussian Blur (radius 8) from **134 ms to 27 ms**, Box Blur (radius 6) from **110 ms to 27 ms**, and 720p from 75 ms to 12 ms. A layer that really has transparency (a letterboxed picture, a PNG character, text) keeps the premultiplied path, so its edges still blur softly instead of picking up a dark fringe; the two paths agree within one 8-bit step and `tests/test_blur_fast_path.py` pins that.
- **Preferences > Playback & Performance**: GPU Auto/On/Off, preview quality, RAM and disk cache limits, cache location, background rendering, automatic proxies, proxy width, hardware decoding/encoding, the live backend line and a *Clear preview cache now* button. All values persist in the app INI (`playback/...`), and never inside a project file.
- **Performance overlay**: real playback FPS, target FPS, dropped frames (clock ticks that produced no new picture), last/average render time, cache frames and hit rate, preview resolution and the active backend. Nothing is estimated or hardcoded.
- **Robustness**: a crash guard replaces `sys.excepthook` at startup so an exception inside a Qt slot is logged to the app-config `meenifx-errors.log` instead of aborting the application, and the render thread is stopped deterministically on exit.
- **Still expensive**: Glow is the one everyday effect the shortcut does not help - it is full-frame float maths by design (198 ms/frame at 1080p Full, 80 ms at 720p), and Mosaic is 40 ms at 1080p. Auto quality is what keeps playback responsive there; those kernels have not been reduced, so lowering the preview quality is the honest fix for a glow-heavy timeline.
- **Limits**: OpenCL compositing is slower than the CPU path on typical hardware, so GPU acceleration of frame operations is *available but not selected* by Auto; there is no CUDA build of OpenCV in the wheel and no hardware decode in this OpenCV build. The separate Phase 1 shader backend accelerates Page Curl and Lens Warp, rather than replacing the OpenCV frame-operation backend. Prerendering is single-threaded by design (it must never compete with playback). Proxies are generated one file per source and are not reused across projects.
- **Measured** with `python tests/preview_bench.py` on this machine (RTX 3070, 16 threads, 1080p synthetic source, 30 fps project): 1080p no effects renders in 23 ms/frame at Full and 7.6 ms at 1/2; the old discard rule showed **1 distinct frame in 2 s while the playhead advanced 59 frames**, the new pipeline shows **30.8 fps with 0 dropped frames**; three stacked 1080p layers at 1/2 play at 13.2 fps uncached (33 dropped) and 30.6 fps from the cache; puppet-warp and animated-transform timelines play at 30.3 fps cached; a 4K/three-effect timeline runs 26.7 fps uncached and 27.5 fps cached; forced OpenCL takes 15.4 ms/frame versus 9.3 ms on the CPU; NVENC encodes 200 1080p frames in 1.27 s versus 1.36 s for the OpenCV writer (and 80 4K frames in 1.59 s versus 1.22 s - the pipe overhead dominates short exports, which is why the writer is chosen per machine and reported).

## Projects

- meeniFX starts on a **start window** (recent projects as cards, search, sort, New / Open, drag a `.mfxpf` file onto it). "Show on startup" is also under File > Preferences. The old demo is only under Help > Open sample project.
- The recent list cleans up after itself: the **Clean up** button next to the search box removes every entry whose file is gone (one click, a "Removed 12" toast, list only) or every project that lives in the system temp folder, where tests and automation leave throwaway copies. The temp one asks first and can also move each project's own folder to the Recycle Bin - only a folder that holds nothing but that project; a folder with anything else in it is left alone. Temp cards carry a small **TEMP** badge, missing ones stay dimmed, and both menu items are greyed out (and count what they would remove) when there is nothing to clean. File > Preferences has "Remove missing projects from the list automatically at startup" (on by default).
- `--no-recent` (or `MEENIFX_NO_RECENT=1` in the environment) keeps a whole run out of the recent list: nothing is registered and the unsaved-work/crash prompt is skipped. Scripts, tests and coding agents that build projects must use it - see Tests.
- A project is one **`.mfxpf`** file (meeniFX Project File): a ZIP container with versioned entries for settings, media references, tracks/clips (columnar, so thousands of clips stay small and fast), effects, keyframes, markers, UI/layout state and thumbnails. Metadata is JSON; the thumbnail JPEGs are stored without recompression. A format version (currently 5: v2 added the linked-stream columns, v5 the clip gain column and the text animation block), a validation pass and a migration hook (`meenifx/project/container.py`) are built in. Original media is never embedded; paths are stored relative to the project with an absolute fallback.
- New Project creates `<Location>/<Name>/<Name>.mfxpf` plus `Recordings/` and `Cache/`. File > Project Settings reopens the same fields; a frame-rate change converts all clip times (one undo step), a resolution change letterboxes clips to fit.
- Saving is atomic (temp file, verified, then renamed) and keeps one `.bak`. Autosave every 3 minutes goes to `<project>.mfxpf.autosave` and is offered for recovery after a crash.
- The **Project** dock (on the left) lists the project's media in grid or list view with background thumbnails (cached in `Cache/thumbs`), search, bins, Import, drag to the timeline/preview, double-click to insert at the playhead, Rename, Relink, Remove. Missing media shows a warning icon in the timeline and the panel instead of failing.
- A `.mfxpf` path passed on the command line is opened on startup, so double-clicking a project works once the association exists. `packaging\associate_mfxpf.ps1` registers the extension (per user, no administrator rights) for the packaged `dist\meeniFX\meeniFX.exe`; `-Remove` undoes it.
- Tests: `python tests\test_project_format.py`, `test_project_storage.py`, `test_project_app.py`, `test_start_window.py`, `test_project_panel.py`, `test_project_large.py`, plus `tests\test_start_window_cleanup.py` (missing/temp pruning, the Recycle Bin rules and the no-recent switch).

## Phase 1 shader effects (development)

- **Lens Warp** (`lens_warp`): a localized elliptical bulge/pinch with center/radius
  preview handles, signed distortion, aspect, transparent/clamp/mirror boundaries,
  chromatic aberration and smooth/nearest sampling.
- **Page Curl** (`page_curl`): a cylindrical sheet turn with distinct front/back shading,
  position, angle, direction, radius, thickness, shadow, highlight, foreshortening and
  four-sample edge antialiasing. The backside can use color, the page, an image or a
  timeline clip. Progress 0 preserves the source; 1 reveals the layer underneath.
- Both use the existing Mix, masks, adjustment layers, compositions, editable keyframes,
  Graph Editor, copy/duplicate and project serialization. Original names and stable IDs
  are used throughout. Existing Lens Distortion and Kaleidoscope keep their own IDs.
- The Properties preset menu offers Corner Lift, Broad Turn, Top Fold, Soft Glass,
  Wide View and Prism Lens. They set real editable parameters through the existing
  effect-preset system. Existing Looks can include either effect as a normal stack entry.
- Automatic selects a matching CPU implementation when GPU shaders are unavailable.
  Forced GPU reports an error. The Properties status identifies the rendering path.
  See [the architecture and verification report](SHADER_PHASE1.md) for limits and results.

### Phase 2 shader development

Heat Haze, Ripple, Pixel Stretch and Light Rays use matching CPU/GPU algorithms,
editable presets and keyframeable parameters. Kaleidoscope offers an optional
alpha-correct GPU renderer while retaining its original renderer for existing projects.
See [verification and limitations](SHADER_PHASE2.md).
