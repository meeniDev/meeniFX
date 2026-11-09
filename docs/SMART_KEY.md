# Smart Key

Smart Key removes the background from an ordinary picture or video by finding the main
subject automatically. It does not sample a screen colour and it does not need brush
strokes. Chroma Key remains the separate tool for a known green, blue or custom screen.

## The workflow

1. Select a picture or video clip and apply **Smart Key**.
2. Click **Remove Background** in the effect card.
3. The optional U^2-Net model analyses the clip in the background. The progress bar shows
   the frame count and **Stop** cancels; masks already finished stay cached.
4. Scrub the timeline. The subject stays and the removed background is genuinely
   transparent, so lower tracks show through.
5. Use **Subject: Choose** when several subjects are present, then click the one to keep.
6. Use **Reanalyze** after changing Quality or the chosen subject.

Applying the effect already renders the current frame automatically. Remove Background is
what analyses and caches the rest of the clip.

## Controls

| Control | What it does |
| --- | --- |
| Remove Background | Analyses every source frame and caches the masks |
| Subject: Auto / Choose | Auto keeps the detected main subject; Choose then click one subject in the preview |
| Quality: Fast / Balanced / High | Model resolution and refinement work |
| Edge Smoothness | Smooths the matte before its edge is shaped |
| Feather | Softens the finished matte edge |
| Expand / Shrink | Grows or shrinks the matte in pixels |
| Preview: Composite / Isolated / Matte | Composite is the real transparent result; the other two are diagnostic views |
| Reanalyze | Clears this clip's Smart Key masks and runs the clip again |

Edge Refinement is under the folded Advanced section. Key Colour and Colour Tolerance are
not part of Smart Key; they exist only in Chroma Key.

## Model and hardware

The backend is **U^2-Net**, a salient-object segmentation network, through ONNX Runtime:

- Apache-2.0 weights, downloaded on demand, about 168 MB;
- SHA-256 verified before use;
- CPU inference at 320 x 320, no GPU, PyTorch or CUDA requirement;
- the model is shared with Roto Brush but the two effects keep separate caches and UIs.

U^2-Net is trained for salient subjects. It works well on people, cats, dogs and prominent
objects. It is not a promptable model for arbitrary named objects and it is not a learned
video-memory tracker.

## Video behaviour

The seed frame is segmented by the model. Every following source frame is re-segmented by
the model using the optical-flow-warped previous matte as a temporal prior, so the matte
follows a moving subject instead of copying the first frame. Masks are cached per source
frame and the cache key includes the effect, model version, subject point, quality and
analysis settings. Changing a shaping control such as Feather does not throw the masks
away; changing Quality or the subject point does.

## Measured performance

Measured on this Windows development machine: six logical Intel cores, ONNX Runtime CPU,
a 480 x 423 source image. Per-frame model inference through the effect:

| Quality | Mean per frame |
| --- | --- |
| Fast | 0.99 s |
| Balanced | 1.55 s |
| High | 2.12 s |

A six-frame 480 x 240 moving-cat clip scanned with Fast took 6.6 s, about 0.91 fps,
including model refinement on every frame and cache writes. A normal 30 fps clip is
thousands of frames, so Remove Background is a background batch operation; the preview
renders the current frame on demand while it runs. Loading the model is a one-off process
cost on the first analysis.

## What remains approximate

Fast motion, occlusion, motion blur, crowded scenes and subjects that leave and re-enter
the frame can still lose the matte and need a Choose Subject or Reanalyze pass. Hair, fur,
thin straps and semi-transparent edges are approximate. Pre-compositions render their own
background, so a cutout inside a pre-composition reveals clips inside that composition, not
tracks below the nested clip on the parent timeline.
