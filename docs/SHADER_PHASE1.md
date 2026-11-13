# Phase 1 shader architecture

## Existing pipeline audit

Baseline: e94997b, preserving the stabilization work. Registry: `EditorEngine.EFFECT_TYPES`.
Effects publish `ParamSpec` controls backed by `Value`; the engine evaluates stored
`AnimatedValue` parameters at clip-local frame time before `apply_effects_layer`.
`render_frame` is shared by preview and all exports. Frames are uint8 BGR or straight
BGRA in the existing display-referred sRGB working space. Mix uses the existing alpha-aware
layer mixer. Masks, adjustment layers and compositions surround this same effect path.
Looks use stable `effect_id` entries; keyframe channels address effect instances/parameters,
not translated display names. Existing JSON Looks can contain a one-effect editable stack.
The PyInstaller spec bundles assets, Looks and presets explicitly.

Existing Lens Distortion and Bulge/Pinch remain separate working effects. Kaleidoscope,
Chromatic Aberration and the glow/color/generator families also already exist. No shipped
effect or preset uses a branded prefix. Keep every existing internal ID unchanged.

## Design

A single dedicated worker owns a lazy ModernGL standalone OpenGL 3.3 context (WGL on
Windows), cached programs/VAOs, reusable float textures and one framebuffer size. Context
creation, compilation, draw, readback and destruction all occur on that worker. The render
worker waits on a bounded submission; the GUI thread does not create or compile GL objects.
Only shipped, allowlisted GLSL resources are executable. There is no downloaded shader API.

Inputs are decoded from sRGB to linear light and premultiplied before filtering. Results
are unpremultiplied, encoded back to sRGB and returned as straight BGRA. Texture row zero
and shader coordinates correspond to the top row of the input array, despite OpenGL's
framebuffer origin. `iResolution` is pixels; `iFrame` is clip-local integer frame;
`iTime` is that frame divided by project FPS, in seconds. Additional textures use iChannel1+.
Scalar/vector uniforms include finite float, int/bool/enum, vec2/vec3/vec4 and sampler inputs.

Page Curl and Lens Warp have original GLSL and matching NumPy CPU algorithms. Automatic
mode reports a CPU fallback when the GPU is unavailable; forced GPU mode reports an error.
The current CPU compositor requires upload/readback at each shader boundary; no claim of
an entirely GPU-resident chain is made. Existing frame cache/latest-wins preview queue and
export cancellation remain authoritative; cancellation is checked at submission boundaries.

Resource limits: bounded frame size/texture count/program count/estimated texture memory.
Trusted shaders have no unbounded loops. A hung driver cannot be safely preempted inside
OpenGL: process isolation would be required for arbitrary untrusted shaders, which are out
of scope. Context timeouts report failure without executing a different visual effect.

## Naming and compatibility

Canonical names/IDs: Page Curl (`page_curl`) and Lens Warp (`lens_warp`). Future names:
Kaleidoscope (`kaleidoscope`, already present), Light Rays (`light_rays`), Particle Studio
(`particle_studio`), Pixel Shatter (`pixel_shatter`). Names are not serialized identities.
No speculative migration renames existing IDs. Add explicit aliases at loading boundaries
only when an actually shipped legacy ID is established, preserving effect/parameter data.
Do not copy branded preset names, artwork, icons or shader implementations.

## References and scope

ModernGL and glcontext supply the GL bindings/context creation (MIT; their distributions
retain license metadata). Consulted the official ModernGL context/lifecycle/API documents:
https://moderngl.readthedocs.io/en/latest/topics/context.html
https://moderngl.readthedocs.io/en/latest/reference/context.html
No shader code is copied from external projects. Shadertoy-style uniform names are a
convention only. External effects are functional references, not branding or assets.

Future OpenFX hosting needs a separate native ABI bridge, plugin discovery, parameter and
image-buffer adapters, thread/lifecycle rules and process isolation. It is not a Python
shader loader and is not implemented here. Coldtype, particles and codec datamoshing are
future work; none is a dependency of Phase 1.
