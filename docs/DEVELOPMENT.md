# Development

## Setup

```powershell
python -m pip install -r meenifx/requirements.txt
python .\main.py
```

Python 3.10 or newer, PyQt6, OpenCV, numpy, Pillow, imageio, and the packages listed in
`meenifx/requirements.txt`. FFmpeg comes from `imageio-ffmpeg`; Whisper transcription is
optional and needs `faster-whisper`.

Run with `--no-recent` (or set `MEENIFX_NO_RECENT=1`) when scripting or testing: the run is
kept out of the real recent list and skips the crash/unsaved-work prompts.

## Tests

```powershell
python -m pytest tests -q `
  --ignore=tests/test_project_app.py `
  --ignore=tests/test_project_large.py `
  --ignore=tests/test_project_panel.py `
  --ignore=tests/test_start_window.py
```

`test_project_app.py`, `test_project_large.py`, `test_project_panel.py` and
`test_start_window.py` are app-driving scripts that call `os._exit` when imported, so they
must be run directly (`python tests\<name>.py`) rather than collected by pytest.

The update system is covered without the network by `tests/test_updates.py` (GitHub payloads,
channel selection, rate limits, caching, checksums, cancellation, disk limits), with the UI
by `tests/test_updates_ui.py`, and with the generated packaging resources by
`tests/test_packaging.py`.

## Packaging

The version lives in exactly one place: `meenifx/version.py`. `packaging/version_info.txt`
(Windows executable metadata) and `packaging/version.iss` (Inno Setup) are generated from it.

```powershell
python packaging/build_windows.py
```

This regenerates the version resources, builds the PyInstaller bundle
(`dist/meeniFX/meeniFX.exe`), makes the portable ZIP, runs Inno Setup 6 when `ISCC.exe` is
available, and writes `dist/SHA256SUMS.txt`. Outputs:

- `dist/meeniFX-Studios-<version>-Windows-Setup.exe`
- `dist/meeniFX-Studios-<version>-Windows-Portable.zip`
- `dist/SHA256SUMS.txt`

Use `--skip-installer` for just the bundle and ZIP. The installer is per user
(`PrivilegesRequired=lowest`), installs under `%LOCALAPPDATA%\Programs\meeniFX Studios`,
creates Start Menu and optional Desktop shortcuts, and leaves projects and settings alone on
upgrade and uninstall.

## Releases

`.github/workflows/release.yml` runs on a `v*` tag (or manually). It installs the
requirements and PyInstaller, runs the test suite, builds the installer/ZIP/checksums, and
publishes them to the matching GitHub Release as a prerelease. Ordinary pushes never publish.

Release notes are user-facing files in `docs/releases/<version>.md`; they ship in the
installer and are used by the What's New dialog and `gh release create --notes-file`.

The update client only accepts assets from
`https://github.com/meeniDev/meeniFX/releases/download/...`, verifies the installer's SHA-256
against the release's `SHA256SUMS.txt`, and hands the verified file to Inno Setup only after
saving open work and confirming no recording or export is running.

## Layout

- `meenifx/model/` - timeline, clips, channels, project format.
- `meenifx/processing/` - effects, compositing, transforms.
- `meenifx/preview/` - render thread, cache, proxy media, invalidation.
- `meenifx/ui/` - main window, panels, dialogs, timeline widgets.
- `meenifx/updates/` - version-aware release selection, caching, downloads, installer rules.
- `packaging/` - PyInstaller spec, build script, Inno Setup script.
- `docs/` - user documentation and release notes.

## Known development limitations

- There is no dependency lockfile; `meenifx/requirements.txt` pins minimum versions. CI pins
  PyInstaller itself.
- There is no code signing yet, so installers are checksum-verified but not
  publisher-authenticated.
- There is no automatic rollback: an update replaces the installed version.
- Windows is the only supported platform. The Python application may launch elsewhere, but
  that is not a supported build.
