# Roto Brush

Roto Brush cuts a subject out of a clip by painting on the picture, the way a rotoscoping
tool should: green strokes say *keep this*, Alt-red strokes say *remove this*, and the
result is a per-frame alpha matte that follows the subject through the clip. The strokes
are hints for a segmenter, they are never painted into the footage.

## The workflow

1. Select a picture clip and press the **Roto Brush** tool (or **Edit Mask** on its effect
   card). A contextual toolbar appears directly above the Preview. The preview is the
   dedicated rotoscoping workspace; a separate window is never opened.
2. The first visit offers the optional AI model download. The toolbar shows its status.
   If it is not installed, the clearly labelled Basic GrabCut mode is used.
3. Paint a green foreground stroke inside the subject. Paint more corrections, and Alt-drag
   red background strokes over anything the matte wrongly included.
4. The purple outline is the real segmented boundary, not a box around the clip. Use the
   preview popup to switch between Original, Outline, Isolated Subject, Alpha Matte,
   Transparency Checkerboard and Composite.
5. **Propagate Backward**, **Propagate Forward** or **Analyze Clip** carry the matte through
   the source frames. The stop button cancels; finished frames are already on disk.
6. Scrub to a weak frame and paint correction strokes there. Corrections belong to that one
   source frame. Undo and Redo are on the toolbar and on Ctrl+Z / Ctrl+Shift+Z.
7. **Freeze** marks the analysed masks as final so playback does not re-run segmentation.
   Unfreeze allows edits; changing a stroke or an analysis setting invalidates stale masks.
8. **Done**, `Esc` or `V` leaves the mode and brings the normal Selection tool and transform
   handles back. The clip transform is never changed by rotoscoping.

While the mode is active the bounding box, resize handles, rotation knob, anchor and motion
path are hidden, and the pointer is a circular brush that matches the real painted radius.

## Preview modes and matte controls

The toolbar preview popup offers Original, Outline, Isolated Subject, Alpha Matte,
Transparency Checkerboard and Composite. Isolated Subject shows the cutout immediately;
Composite leaves the removed area transparent so a lower clip shows through. Diagnostic
views are preview-only: export is always rendered as Composite.

The Properties panel contains only effect values. Smooth, Feather, Expand/Shrink, Edge
Refinement and Edge Colour Cleanup run on the cached raw matte, so changing them re-shapes
every analysed frame without segmenting again. Subject choice, Quality, Search Radius,
Colour Tolerance, correction fitting and the segmenter choice live under **Details**;
they change the analysis and therefore reset the mask key. AI model status and download
are in the toolbar, not buried in Properties.

## Segmentation backends

The preferred backend is **U^2-Net**, an Apache-2.0 salient-subject segmentation model.
The ONNX weights are optional and downloaded from the AI button on the Roto Brush toolbar:

- size: 175,997,641 bytes (about 168 MB),
- SHA-256 is verified before the file is installed,
- inference runs on the CPU through ONNX Runtime (no GPU or PyTorch required),
- the download has progress, cancellation and clear error reporting,
- the model is not bundled in the installer and is not committed to the repository.

For each analysed frame the model predicts the subject from the picture itself. Green and
red strokes are applied as corrections, the flow-warped previous matte is used as a
temporal prior, and the result is refined along the picture edge. The model is run again on
every propagated frame instead of copying the base mask.

**Basic** mode remains available without a download. It is the classical path in
`meenifx/roto/segment.py`:

- OpenCV GrabCut seeded by the painted foreground/background strokes,
- a LAB colour-distance model sampled from the strokes,
- an edge-aware guided filter driven by the Refine Edge brush,
- frame-to-frame propagation through Farneback optical flow plus a per-frame GrabCut
  refinement in `meenifx/roto/propagate.py`.

Basic is **not** AI-assisted rotoscoping, and the UI labels it accordingly. It has no
learned object prior and no object memory.

### What remains approximate

Neither backend is a learned video-object tracker with memory. Fast motion, occlusion,
motion blur, crowded scenes and subjects that leave and re-enter the frame still need
correction strokes. Hair, fur, thin straps and semi-transparent edges remain approximate,
and the Refine Edge brush is a guided filter rather than a dedicated alpha-matting network.

Pre-compositions render their own background. A cutout inside a pre-composition reveals
clips on lower tracks inside that composition, but the nested clip itself still paints its
background, so the transparency does not pass through to tracks below it on the parent
timeline.

SAM 2 and SAM 2.1 were evaluated. They are Apache-2.0, but require PyTorch (roughly 2.5 or
newer) and a 110 to 770 MB checkpoint; bundling that runtime would dwarf the editor and is
difficult to package reliably on Windows. U^2-Net keeps the release portable while giving
a real learned subject prior on CPU. The segmentation entry points (`segment_subject`,
`propagate_seed`, `raw_matte`) remain the seams for a future matting backend.

## Coordinates, trims and transforms

Strokes are normalised inside the picture rectangle the engine hands the effect, and are
stored per **source** frame. `clip.source_frame_at()` already applies trims, source offsets,
speed and reverse playback, so moving the clip on the timeline, trimming it, changing its
speed or transforming it cannot shift a stroke or a mask. The overlay converts pointer
positions back through the same position/scale/rotation matrix the renderer uses, which is
what keeps the brush aligned at every zoom and pan.

## Persistence and cache

The `.mfxpf` project stores only small data on the effect entry:

```json
{
  "effect_id": "roto_brush",
  "base_frame": 12,
  "strokes": [{"frame": 12, "mode": "foreground", "radius": 0.05, "points": [[0.42, 0.31]]}],
  "params": {"quality": "balanced", "search_radius": 24.0},
  "propagation": {"base_frame": 12, "analysed": [13, 14], "range": [0, 89]},
  "cache": {"key": "…", "frames": 41, "frozen": true}
}
```

Frame-by-frame masks are 8-bit PNGs under `%APPDATA%/meeniFX/meenifx-roto/<clip>/` (or the
directory in `MEENIFX_ROTO_CACHE`). The cache key is a digest of the base frame, every
stroke and the analysis parameters. A reopened project without its cache simply shows the
frames that still need analysing, it never displays stale or invented masks. Cleaning the
cache, invalidating a key or unfreezing never touches the source media.

## Keyboard

| Key | Action |
| --- | --- |
| drag | paint a green (keep) stroke |
| Alt + drag | paint a red (remove) stroke |
| `B` | Roto Brush (green foreground brush) |
| `V` | leave Roto Brush and restore Selection |
| `Esc` | leave Roto Brush (same as **Done**) |
| `Ctrl+Z` | undo the last stroke |
| `Ctrl+Shift+Z` | redo the undone stroke |
| `[` / `]` | smaller / larger brush |
| `O` | show or hide the segmentation outline |

Shortcuts are ignored while a text field or a dialog has focus. In particular, pressing
`V` in a text input never leaves the workspace.

## Verification

Run the focused suites with the offscreen platform:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=".;tests" python -m pytest \
  tests/test_roto_brush.py tests/test_roto_brush_ui.py tests/test_roto_toolbar.py \
  tests/test_roto_brush_window.py -q -p no:cacheprovider
```

The real-model checks in `tests/test_roto_model.py` run when
`artifacts/roto-model-test/u2net.onnx` is present. They verify the downloaded weights, the
alpha cutout and model propagation on a moving subject; otherwise they skip.

The backend suite checks that strokes segment without painting or deleting pixels, that red
corrections are hints, that every matte control changes the matte, that the outline follows
the boundary, that forward and backward propagation track a moving subject, that the cache
freezes and invalidates by key, that unanalysed frames render unchanged, and that the
project keeps its Roto metadata without embedding masks. The UI suite drives real mouse
press/move/release events and checks hidden handles, continuous green and Alt-red strokes,
undo/redo, the coloured brush ring, outline updates after a correction, zoom-independent
stroke alignment and the Done control.
