# Getting started

meeniFX Studios is a Windows video editor. This page takes you from a fresh download to
your first exported video.

## Install

The [Releases page](https://github.com/meeniDev/meeniFX/releases) is the source of truth.
If it does not list an Alpha build yet, use the portable ZIP or run from source.

### Installer (recommended)

1. Open the [Releases page](https://github.com/meeniDev/meeniFX/releases).
2. Download the newest **Alpha** release's `meeniFX-Studios-<version>-Windows-Setup.exe`.
3. Run it. It installs for the current user under
   `%LOCALAPPDATA%\Programs\meeniFX Studios`, so no administrator password is needed.
4. Launch **meeniFX Studios** from the Start Menu, or from the optional desktop shortcut.

The installed build updates itself from inside the application. When a newer release is
found, the installer downloads in the background, is checked against the release's
`SHA256SUMS.txt`, and changes the update notice to **Restart and Install**. One click saves
your work, installs the verified update and reopens the new version. Automatic downloads
are on by default and can be switched off in Preferences > Updates.

### Portable ZIP

Download `meeniFX-Studios-<version>-Windows-Portable.zip`, extract it anywhere and run
`meeniFX.exe`. Portable builds do not install anything and must be updated by replacing the
folder with a newer ZIP. Projects and preferences live outside that folder, so replacing it
does not touch your work.

### From source (development)

```powershell
python -m pip install -r meenifx/requirements.txt
python .\main.py
```

Python 3.10 or newer is required. See [DEVELOPMENT.md](DEVELOPMENT.md) for the test and
packaging workflow.

## First project

1. **New Project** on the start window. Choose a name and a location; the project becomes
   `<Location>\<Name>\<Name>.mfxpf`.
2. Drag video, images or audio into the Project panel, or use **Import Media** (`Ctrl+I`).
3. Drag media onto the timeline. A video that has sound arrives as a linked video + audio
   pair.
4. Move the playhead and press `Space` to play. Snapping, trimming and fades are explained
   in [FEATURES.md](FEATURES.md).
5. **Export** (`Ctrl+R`) writes the whole project, or the loop region, to MP4, AVI, GIF or
   an image sequence.

Your project is a single `.mfxpf` file with a `.bak` beside it. Media is referenced, never
embedded, so keep the original files where they are (or use Relink if they move).

## Updates and the Alpha channel

Updates are checked automatically in the background, at most once every few hours. The
default channel is **Alpha**, which includes prereleases. Installed builds also download a
found update automatically, then wait for **Restart and Install**. Preferences > Updates
lets you turn automatic checks or automatic downloads off, change the channel, clear
skipped versions, or open the Updates page.

- **Restart and Install** saves your work, closes the editor and runs the verified
  installer. The new version opens when the install finishes.
- **Remind Me Later** hides the notice for 24 hours.
- **Skip This Version** hides and does not download that exact version; a newer version
  will still be offered.
- A manual **Check for Updates** always overrides both.
- What's New appears once after an update and stays available under **Help > What's New**.

## Where things live

- Projects: wherever you create them.
- Preferences, recent projects, layouts and update state: the application INI in your user
  configuration folder.
- Preview cache and recordings: inside the project folder, in `Cache\` and `Recordings\`.
- Speech models: `%USERPROFILE%\.meenifx\models`.

Uninstalling removes the application; it does not delete your projects or settings.
