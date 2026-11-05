# Changelog

## Unreleased (2026-11-05)

### Reliability and performance

- **Auto Captions on linked clips:** linked-selection expansion no longer loses the clip
  that was clicked, and the caption range resolves a silent linked video to its audio (or
  uses an embedded audio stream). A longer picture no longer widens the analysed range into
  silence.
- **Bounce baking:** fixed `position_at()` dropping an animated Y when X was static. Baking
  Bounce now writes visible vertical motion as well as opacity, with a regression test
  comparing every sampled frame.
- **Auto preview quality:** fixed the climb-back branch, which compared the current scale
  against 1.0 and could never recover, so a slow frame permanently lowered preview quality.
- **Tracker cards:** icon-only effects (Face Tracker, Hand Tracker and audio/time effects)
  now show a readable label instead of an empty tile; no fake detection is drawn.
- **Timeline toolbar:** removed the redundant Linked Selection chain button. The same
  selection behaviour remains in Edit as a checkable command with Ctrl+Shift+K; explicit
  Link/Unlink commands and shortcuts are unchanged.

### Recording

- Added Performance / Balanced / High Quality recorder presets. They change bitrate
  headroom and encoder effort only; capture timing, timestamps and audio/video sync are
  unchanged.
- Screen recording uses a verified NVENC encoder when one works for gdigrab, reports the
  chosen encoder in Recorder settings, and falls back to software libx264 when hardware
  produces no data instead of leaving an empty take.

### Project organization

- Renamed the Project panel's bin terminology to **New Folder**, with nested virtual
  folders, breadcrumbs, drag and drop, multi-item moves and search across all folders.
  Folder organization is saved in `.mfxpf`; old flat bin lists still load.
- Folder deletion offers **Move Contents to Parent**, **Remove Folder and Media from
  Project**, or Cancel. It never deletes a source file, and timeline clips are removed
  only after an explicit warning.
- Split **Remove from Project** from **Delete File from Computer...**. The destructive
  action lists exact paths, warns about timeline use, delegates only to the operating
  system's Recycle Bin and refuses to proceed when no safe trash mechanism exists.

### Linking

- Added explicit **Link Selected Clips** (Ctrl+L, Edit menu and clip right-click menu) and
  **Unlink Selected Clips** (Ctrl+Shift+L). Linking no longer requires both clips to come
  from the same media file, so separately recorded video and audio can be paired.
- Link commands are disabled when they cannot apply, and right-clicking a clip that is part
  of a multi-selection now keeps that selection, so the pair can be linked from either clip.
- The **Linked Selection** toolbar toggle is documented and tooltipped as selection
  behaviour only; it no longer looks like the way to create a link.
- Unlinking one member of a larger group leaves the remaining members linked; the operation
  changes no timing, source range, effect or keyframe and is undoable.
- Splitting a linked pair continues to give every resulting pair its own group id; the
  regression test now asserts the independent ids, contiguous source offsets, undo and
  independent movement.

### Remove Dead Silence

- Reworked the dialog around three scopes: **Audio and linked video**, **Matching source
  video and audio** (same file, speed, direction and sync key, even when unlinked) and
  **Audio only**. The included clips are listed before Apply.
- Silence detection gains a **minimum retained segment** control; threshold is labelled
  dBFS.
- One authoritative frame plan now drives every included clip. Cuts are clamped to the span
  all members cover, so a cut can never pull a group out of sync; survivors are re-linked per
  retained segment, including pairs that intentionally start at different positions.
- Locked tracks block the pass with an explanation instead of silently cutting only the
  unlocked partners. Ripple closes gaps on the included tracks only.
- The command is available from the clip right-click menu and Tools → **Remove Dead
  Silence...**.

### Tests

- Added model tests for cross-file linking, group unlinking, audio-only and matching scopes,
  locked tracks, offset pairs, short retained fragments and the corrected split-group
  contract.
- Added UI tests for the context-menu commands, Ctrl+L / Ctrl+Shift+L, selection
  preservation, the toolbar tooltip and the dialog's scope defaults.
