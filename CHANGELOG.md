# Changelog

## Unreleased (2026-11-05)

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
