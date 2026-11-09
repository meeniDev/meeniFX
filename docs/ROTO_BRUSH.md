# Roto Brush

Roto Brush cuts a subject out of a clip by painting on the picture, the way a rotoscoping
tool should: green strokes say *keep this*, Alt-red strokes say *remove this*, and the
result is a per-frame alpha matte that follows the subject through the clip. The strokes
are hints for a segmenter — they are never painted into the footage.

## The workflow

1. Select a picture clip and press the **Roto Brush** tool (or **Paint Mask** on its effect
   card). The preview switches to a dedicated rotoscoping mode.
2. Paint a green stroke inside the subject. Paint more strokes, and Alt-drag red strokes
   over anything the matte wrongly included.
3. The purple outline is the real segmented boundary, not a box around the clip. `O` hides
   it.
4. **Forward** / **Backward** / **Analyze All** carry the matte through the source frames.
   **Stop** cancels; finished frames are already on disk.
5. Scrub to a weak frame and paint correction strokes there. Corrections belong to that one
   source frame.
6. **Freeze** marks the analysed masks as final so previews stop re-checking them.
   **Unfreeze** allows edits, **Recalculate** re-runs the analysis and **Clear Cache**
   deletes the masks from disk.
7. **Done** (or `Esc`) leaves the mode and brings the normal transform tools back. The clip
   transform is never changed by rotoscoping.

While the mode is active the bounding box, resize handles, rotation knob, anchor and motion
path are hidden, and the pointer is a circular brush that matches the real painted radius.

## Preview modes and matte controls

Composite, transparent checkerboard, alpha matte, and foreground on a solid colour are all
available from the effect's **Preview** parameter. Smooth, feather, choke/expand, edge
contrast and edge colour cleanup run on the cached raw matte, so changing them re-shapes
every analysed frame without segmenting again. Search radius, colour tolerance, GrabCut
refinement and quality change the analysis itself and therefore reset the mask key.

## What the backend is, and what it is not

The shipped segmenter is the classical path in `meenifx/roto/segment.py`:

- OpenCV GrabCut seeded by the painted foreground/background strokes,
- a LAB colour-distance model sampled from the strokes,
- an edge-aware guided filter driven by the Refine Edge brush,
- frame-to-frame propagation through Farneback optical flow plus a per-frame GrabCut
  refinement in `meenifx/roto/propagate.py`.

It runs on the CPU at a bounded analysis size, needs no model download, and keeps the
Windows release a single package. It is **not** a learned video-segmentation model: there
is no object memory, so fast motion, occlusion, motion blur and crowded scenes still need
correction strokes, and hair, fur and semi-transparent edges are approximate. The panel and
this document say so instead of pretending otherwise.

### Models evaluated for the upgrade path

| Option | Licence | Runtime / size | Windows packaging |
| --- | --- | --- | --- |
| SAM 2 / SAM 2.1 | Apache-2.0 | PyTorch ≥ 2.5; 110–770 MB checkpoints | WSL strongly recommended; a bundled Python/PyTorch runtime would dwarf the editor |
| SAM 2 ONNX exports | Apache-2.0 | ONNX Runtime; same weights (hiera-tiny ~111 MB … large ~768 MB) | Portable in principle, but untested on this machine and adds a large download |

None is bundled: shipping a multi-hundred-megabyte model we cannot test here would be worse
than an honest fallback. The segmentation entry points (`segment_frame`, `propagate_seed`,
`raw_matte`) are the seams where a model backend can be added without touching the UI.

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
frames that still need analysing — it never displays stale or invented masks. Cleaning the
cache, invalidating a key or unfreezing never touches the source media.

## Keyboard

| Key | Action |
| --- | --- |
| drag | paint a green (keep) stroke |
| Alt + drag | paint a red (remove) stroke |
| `B` | Refine Edge brush |
| `G` | back to the green foreground brush |
| `[` / `]` | smaller / larger brush |
| `O` | show or hide the segmentation outline |
| `Esc` | leave Roto Brush (same as **Done**) |

## Verification

Run the focused suites with the offscreen platform:

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=".;tests" python -m pytest \
  tests/test_roto_brush.py tests/test_roto_brush_ui.py -q -p no:cacheprovider
```

The backend suite checks that strokes segment without painting or deleting pixels, that red
corrections are hints, that every matte control changes the matte, that the outline follows
the boundary, that forward and backward propagation track a moving subject, that the cache
freezes and invalidates by key, that unanalysed frames render unchanged, and that the
project keeps its Roto metadata without embedding masks. The UI suite drives real mouse
press/move/release events and checks hidden handles, continuous green and Alt-red strokes,
undo/redo, the coloured brush ring, outline updates after a correction, zoom-independent
stroke alignment and the Done control.
