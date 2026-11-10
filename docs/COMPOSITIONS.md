# Compositions and transparency

A pre-composition is a reusable timeline asset. It renders at its own size and is then
fitted into the parent clip, so it can contain video, images, text, shapes, effects and
nested compositions.

## Background setting

**Composition Settings** has a **Transparent background** checkbox.

- **On** (the default for a new pre-composition): the composition keeps its alpha. A Smart
  Key or Roto Brush cutout, a shape, text or a transparent PNG can therefore show the
  parent timeline through the empty areas of the nested clip.
- **Off**: the composition is rendered over its background colour and the nested clip is
  opaque, which is the historical behavior.

Projects saved before this setting existed have no stored value and load as **Off**, so
their appearance does not change. The setting is per composition and is saved with the
project; changing it invalidates the cached parent frames through the composition digest.

## Alpha pipeline

Preview, export and nested rendering use the same compositor. For a transparent
composition the canvas is float BGRA (straight colour plus coverage), layers are
composited source-over, and the result is converted to 8-bit BGRA only at the composition
boundary. Shapes and text already render into transparent full-frame layers. Blur,
transform and adjustment layers preserve the fourth channel; an adjustment layer evaluates
the coverage below it instead of making the whole frame opaque.

The Preview panel's checkerboard is a display aid only. It is not a composition
background and it never reaches export. A root timeline still renders over the project
background; transparency matters when a composition is nested over another clip.

Export formats that do not carry alpha (MP4, AVI, GIF) flatten the final composite over the
project background, as before. Rendering a composition as part of a parent timeline uses
the parent's visible layers, not the editor preview's checkerboard.

## Cache behavior

The composition digest includes the transparent flag, background, size, frame rate and
contents. Toggling **Transparent background** therefore cannot reuse an opaque cached
frame. **Rebuild Composition Cache** remains available after changing colors, effects or
nested contents.
