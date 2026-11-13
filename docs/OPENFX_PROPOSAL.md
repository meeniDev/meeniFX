# Future OpenFX hosting proposal (not implemented)

## Boundary

Keep OpenFX separate from the shipped GLSL backend. OpenFX is a native plugin-host
interface, not a shader file format. Begin with a Windows x64 native host process and a
small versioned IPC protocol. Do not load arbitrary third-party DLLs into the Qt editor.
Process separation alone is not a security sandbox; permission limits and a reviewed
plugin allowlist are separate requirements.

## Data and parameters

Use shared-memory image buffers with explicit dimensions, stride, channel order,
bit depth, color encoding and alpha association. Adapt the current BGRA/sRGB layer API
at this boundary and measure conversion overhead. Render requests specify clip-local
frame, exact FPS, region of interest and cancellation token. Declare supported formats
and reject unsupported combinations rather than guessing alpha/color conventions.

Map plugin parameters to existing channels and the Graph Editor using stable plugin and
parameter identifiers. Preserve unrecognized metadata in project files. A missing plugin
must produce a clear unavailable state without deleting the saved effect or its keys.
Version plugin identifiers and migration rules independently of translated display names.

## Lifecycle and scope

Probe discovery candidates in a disposable process with time/resource limits. Crash,
restart and cancellation behavior need tests before exposing plugins to projects.
Start with ordinary frame-based CPU rendering and existing Mix/masks/adjustments.
GPU interop, custom plugin interfaces, temporal fetching, multi-input recursion and
concurrent render scheduling require explicit capability negotiation and a native
thread/lifecycle design. Review SDK and per-plugin licenses before borrowing or bundling.

No SDK dependency, plugin discovery, native bridge or OpenFX renderer was added in
Phase 1. Coldtype is likewise a possible future typography integration, not a mandatory
shader dependency. Particles, datamoshing and later procedural/color phases are separate
future work and must not be described as implemented.
