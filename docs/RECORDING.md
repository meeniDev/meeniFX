# Recording

**Record** in the toolbar (or **File > Record...**) opens a compact panel with four source
cards, each with its own switch:

- **Webcam** - camera list, resolution and frame rate limited to what the camera offers,
  live preview.
- **Screen** - the whole screen or a window, live preview, optional mouse cursor.
- **Microphone** - device list, live level meter and gain.
- **System audio** - the output device's sound, with its own meter and gain.

Any combination works. Devices and settings are remembered, and a saved device that has
disappeared falls back to the default.

## How it records

- Audio is captured through WASAPI directly, with no extra dependency: microphones from a
  capture endpoint and system sound from the render endpoint in **loopback** mode. The
  device's shared-mode format is converted to the chosen rate (48 kHz by default) and
  written as 16-bit WAV.
- Each source has its own gain, and the meter shows the post-gain signal of the stream that
  is actually written, so what you see is what you hear in the file.
- Long recordings stay in sync: the WAV length follows the device frame count, and the
  resampler trims its ratio slowly against the wall clock (bounded to 0.3 %). If a device
  is lost, that source closes cleanly and the rest of the take continues.
- The screen is recorded by the bundled ffmpeg (`gdigrab`), not by Qt, because Qt's desktop
  capture cannot include the mouse cursor.

## After a take

- Each source is saved as its own file in the project's `Recordings` folder
  (`Webcam <date>.mkv`, `Screen <date>.mkv`, `Voice <date>.wav`,
  `System sound <date>.wav`).
- When a picture and sound were recorded together, the sound is mixed into the video
  (`Screen <date> (with sound).mkv`, AAC 192 kbps) and inserted as a linked video + audio
  pair. The WAVs stay on disk as uncompressed masters.
- Clips land on separate tracks at the playhead where recording started, as one undo step.

## Controls

- **Record** starts an optional 3-2-1 countdown, then the main window minimises and a small
  always-on-top bar shows elapsed time, pause, stop and the microphone level. The bar is
  excluded from screen capture. It is **off by default**; enable it under Preferences >
  Recording.
- Global hotkeys, active only while recording: `Ctrl+Shift+R` stops, `Ctrl+Shift+P`
  pauses/resumes. Both can be changed in the recorder's gear menu.
- The gear also holds the recording folder, video bitrate, audio sample rate, mono/stereo
  and the "put the sound inside the recorded video" switch.
- **Voice-over** records only the microphone while the timeline plays from the playhead.
- Pause is implemented as segments: after a pause each source gets a `part 2` file, placed at
  the right time so sources stay in sync.

## Limits

- Recording is Windows-only, like the rest of the application.
- Screen recording needs a working graphics driver and the bundled ffmpeg.
- A lost device ends that source; it is not reattached automatically mid-take.
- There is no automatic audio/video drift repair after the file is written; the capture path
  keeps them in sync while recording.
