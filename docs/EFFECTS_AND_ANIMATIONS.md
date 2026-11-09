# Effects and animations

meeniFX renders effects through one pipeline: the effect browser's previews, the timeline
preview, the cache and the export all run the same effect objects. That is why a 35 %
preview and the exported frame are the same picture, only at different resolutions.

The full parameter-by-parameter reference is in [FEATURES.md](FEATURES.md). This page is the
map.

## Effects browser

**Effects**, **Animations** and **Styles** live in one browser with a category filter, search
by name or everyday word, favourites, recently used, and a remembered grid or list view.
Every tile is a real render on the reference picture (or the nearest honest fallback, such
as the effect's icon for audio and time effects). Double-click applies; double-clicking an
applied effect removes it. Each apply or remove is one undo step.

## Available effect groups

- **Compositing** - blend modes on every clip, adjustment layers, and a keyframeable Mix on
  every effect card.
- **Colour** - Colour Correction with presets, LUT (`.cube`, three built-in tables), Duotone,
  Invert, Vignette, Posterize.
- **Blur and sharpen** - Gaussian, Box, Directional, Radial, Zoom and Motion blur.
- **Glow and light** - Glow, Bloom Streaks, Flash/Strobe, Exposure Flicker, Light Leak.
- **Stylize** - VHS/CRT, Film Grain, Halftone, Edge Outline, Mosaic, ASCII Art.
- **Glitch and distortion** - RGB Split, Glitch, Wave Warp, Bulge/Pinch, Twirl, Mirror and
  Kaleidoscope.
- **Warp** - Puppet Warp (a real mesh deformation), Corner Pin, Perspective, Mesh Warp and
  Skew/Shear.
- **Keying and mattes** - Chroma Key, Smart Key, Roto Brush and Simple Choker. Smart Key
  samples the shot's border; Roto Brush paints subject/background strokes and refines them
  with GrabCut; Simple Choker grows or shrinks any alpha edge.
- **Time** - Posterize Time (a **Frame Rate** in fps; 23.976, 29.97 and 59.94 are kept as
  their exact 24000/1001 and 30000/1001 rates, and a keyframed rate never samples
  backwards), Stutter and Echo/Trail.
- **Tracking** - Face, Hand and Blob trackers.
- **Audio** - Gain, fades, EQ, low/high-pass, compressor, limiter, reverb, delay and stereo
  pan, processed on the samples rather than on a decorative knob.

Every effect declares which media types it supports, and the browser, the drop target and the
engine all read that declaration. Visual effects are disabled on audio clips; an effect
loaded from an older project onto the wrong media type is switched off with an explanation
instead of crashing.

## Looks

A **Look** applies an editable group of effects with one Strength control. Ten ship with the
app (VHS Tape, CRT Monitor, Anime Glow, Cyber Glitch, Dreamy Bloom, Retro Film, Comic
Halftone, Neon Night, Faded Memory, Hard Contrast B/W). Removing a look removes exactly its
members and leaves the rest of the stack alone. Your own looks can be saved, imported and
exported as `.mfxlook`.

## Animation

- **Keyframes** exist on every numeric parameter, plus clip opacity, audio gain, text and
  shape channels. The stopwatch enables animation, the diamond keys the current value, and
  the arrows jump between keys.
- **Auto-Keyframe** (`K`) decides whether a change keys the new value at the playhead or
  shifts the whole curve without creating a key.
- The **Keyframe Editor** (`Shift+G`) is the timing and value view; the **Graph Editor**
  (`G`) shows the same channels as curves or speed graphs. Both read one animation model, so
  they can never disagree.
- **Easing** families (Sine, Quadratic, Cubic, Back, Bounce, Elastic and more) plus technical
  Hold/Bezier/Auto/Continuous/Broken handling live under a friendly menu with curve
  thumbnails.
- **Animation presets** ship as In/Out/Loop families that stack on top of hand-keyed motion:
  Fade, Slide, Pop, Zoom, Rotate, Bounce, Blur, Wipe, Wave, Flip, Shake, Float, Pulse, Jitter,
  Wiggle and Camera Shake. They can be baked to ordinary keyframes, saved as `.mfxpreset`,
  and applied per letter, word or line for text.
- **Motion paths** show position keys on the picture with draggable points and spatial Bezier
  handles.

## Experimental and limited

- Heavy stacks (VHS/CRT + grain + bloom at 1080p) do not play at 30 fps. Use the preview
  quality, proxy media and the frame cache; those paths are real, but the effects themselves
  are not GPU-accelerated.
- Puppet Warp, Mesh Warp and the trackers are CPU implementations. They render identically in
  preview and export, but their cost grows with resolution.
- Per-glyph animation is available through text presets and Split Text, not as individual
  keyframes on each glyph.
- Audio is resampled when a clip's speed changes, so speed changes pitch; there is no
  time-stretch/pitch-preserve stage.
- The effect browser's reference image and preview quality are preferences and are never
  written into a project.
