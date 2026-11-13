<p align="center">
  <img src="assets/logo_horizontal.png" alt="meeniFX Studios" width="430">
</p>

<p align="center">
  <strong>A friendly, creative video editor for Windows.</strong>
</p>

![meeniFX Studios editing the sample project](docs/design/window-light-1420x880.png)

## Download

**meeniFX Studios is in Alpha.** It is usable and tested, but unfinished, and some features
are still experimental.

**Latest published build: 0.1.0-alpha.9.**

[**Download the Windows installer (.exe)**](https://github.com/meeniDev/meeniFX/releases/download/v0.1.0-alpha.9/meeniFX-Studios-0.1.0-alpha.9-Windows-Setup.exe)
 · [Portable ZIP](https://github.com/meeniDev/meeniFX/releases/download/v0.1.0-alpha.9/meeniFX-Studios-0.1.0-alpha.9-Windows-Portable.zip)
 · [Checksums](https://github.com/meeniDev/meeniFX/releases/download/v0.1.0-alpha.9/SHA256SUMS.txt)
 · [Release notes](https://github.com/meeniDev/meeniFX/releases/tag/v0.1.0-alpha.9)

**Source development version: 0.1.0-alpha.10.** The typography and animation improvements
shown below are in development; the Alpha 9 downloads above do not include them.

Each published release contains:

- `meeniFX-Studios-<version>-Windows-Setup.exe` - the installer, recommended.
- `meeniFX-Studios-<version>-Windows-Portable.zip` - no installation required.
- `SHA256SUMS.txt` - checksums for both downloads.

Installed builds update themselves from inside the app: a detected release downloads in
the background, is checksum-verified, and waits for **Restart and Install**. Portable builds
are updated by replacing the extracted folder with a newer ZIP; your projects and
preferences live outside it.

**Windows only for now. Linux support is planned but not officially supported yet.**
Windows 10 or 11, 64-bit. The installed application does not require Python.

## What it does

- **A real timeline** - multiple video and audio tracks, snapping, markers, fades, linked
  video and audio, trimming, splitting and full undo/redo.
- **Effects that look the same in the export** - colour correction, blurs, glow, stylize,
  glitch and warp effects, keying, LUTs and one-click Looks.
- **Automatic background removal** - Smart Key isolates the main subject with the optional
  U^2-Net model and makes the background genuinely transparent, with no green screen,
  colour picker or brush strokes. Chroma Key remains the separate screen-colour tool.
- **Rotoscoping** - a Roto Brush workspace with its own toolbar: green keep and Alt-red
  remove strokes, a Refine Edge tool, a purple outline that follows the subject, a base
  frame, forward and backward propagation, correction strokes, six matte previews and a
  freezeable mask cache. An optional U^2-Net subject model can be downloaded from the
  toolbar, with SHA-256 verification and CPU inference; Basic GrabCut remains available.
- **Animation** - keyframes on every numeric parameter, a Keyframe Editor, a Graph Editor,
  easing families, motion paths and reusable animation presets.
- **Text and shapes** - styled, keyframeable text clips and vector shapes that stay sharp at
  any resolution.
- **Recording and captions** - webcam, screen, microphone and system audio, plus local Auto
  Captions with a transcript editor and SRT import/export.
- **Smart Paste** - paste a picture, a file, a direct media link or a YouTube link and keep
  working.
- **Export** - MP4, AVI, GIF and image sequences, whole project or loop region, with hardware
  encoding when it is genuinely faster.

The detailed reference lives in [docs/FEATURES.md](docs/FEATURES.md).

Development work also includes [Page Curl, Lens Warp and a reusable shader backend](docs/SHADER_PHASE1.md),
with matching CPU fallbacks. The [next shader batch](docs/SHADER_PHASE2.md) adds Heat Haze,
Ripple, Pixel Stretch and Light Rays, plus optional GPU Kaleidoscope rendering.
These development branches do not publish a new installer.

## A look inside

### Text and editable motion

![Alpha 10 development workspace with a text layer and an editable diagonal motion path](docs/design/alpha10-text-workspace.png)

Text animation uses ordinary keyframes. Choose Slide In or Slide Out, then an edge or
corner; duration, delay and easing stay editable alongside the animation.

| Installed-font picker | Text animation controls |
| --- | --- |
| ![Font picker populated immediately, with readable font names and live samples](docs/design/alpha10-font-picker.png) | ![Separate entry and exit presets with diagonal direction controls](docs/design/alpha10-text-animation.png) |

These are actual Qt captures from the Alpha 10 development source, not mockups.

### Dark workspace

![meeniFX dark workspace with preview, effects and timeline](docs/design/window-dark-1420x880.png)

### Shader development preview

![Heat Haze rendered from a checker test plate](docs/design/heat_haze-phase2.png)

This is an actual rendered test frame from the development shader branch. See the
[Phase 2 report](docs/SHADER_PHASE2.md) for measurements and remaining verification.

## Install

1. Open the [Releases page](https://github.com/meeniDev/meeniFX/releases).
2. Download the newest Alpha's Windows Setup executable.
3. Run it and launch **meeniFX Studios** from the Start Menu.

The installer is per user, so it does not ask for an administrator password, and it creates
an uninstaller. Upgrading keeps your projects, preferences, layouts and presets.

Full instructions, including the portable build and running from source, are in
[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md).

## Documentation

- [Getting started](docs/GETTING_STARTED.md) - install, first project, export, updates.
- [Feature reference](docs/FEATURES.md) - the detailed description of what is implemented.
- [Keyboard shortcuts](docs/SHORTCUTS.md)
- [Recording](docs/RECORDING.md)
- [Effects and animations](docs/EFFECTS_AND_ANIMATIONS.md)
- [Development and packaging](docs/DEVELOPMENT.md)
- [Changelog](CHANGELOG.md)

## Project status

meeniFX Studios is under active development. Alpha builds are labeled as such in the About
box, the installer and every release, and the app checks GitHub Releases for newer Alpha
builds in the background. Updates are downloaded to a temporary folder, verified against the
release's SHA-256 checksum, and installed only after you confirm; there is no automatic
rollback yet, and installers are not publisher-signed.

Bug reports and feature requests are welcome in
[Issues](https://github.com/meeniDev/meeniFX/issues). Please include the version from
**Help > About** and, for crashes, the log path shown in the crash dialog.

## Development

```powershell
python -m pip install -r meenifx/requirements.txt
python .\main.py
```

Python 3.10+ is required to run from source. Build and release instructions, the test
commands and the repository layout are in [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md).

The application version is defined once, in `meenifx/version.py`, and the Windows executable
metadata and installer version are generated from it.
