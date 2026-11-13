# Alpha 10 development verification - 13 November 2026

## Result

Final per-module results after fixes and focused reruns:

| Check | Result |
| --- | --- |
| Pytest modules | 104 completed successfully |
| Automated tests | 2,032 passed, 92 skipped, 0 failures/errors |
| Standalone project-format and storage assertion scripts | Both passed |
| Real MediaPipe photograph integration | 4 passed (included above) |
| New typography/direction regressions | 13 passed (included above) |
| Published Alpha 9 Setup URL | HTTP 200 |

The 92 skips comprise 90 documented effect-audit cases whose prerequisites or
specialized coverage live elsewhere, and 2 hardware/audio integration conditions.
A skipped test is not a verified feature.

The complete initial per-module run found failures; each affected module was rerun
after correction. The counts above use the final successful result for every module,
without double-counting reruns. Local XML/log/JSON evidence is stored in the testing
workspace, outside tracked source. Reproduce with `python tools/run_tests.py`.

## Environment and scope

Windows x64, Python 3.14.7, PyQt6 6.12.0, pytest 9.2.0, pytest-timeout 2.4.0,
MediaPipe 1.2.2 and OpenCV contrib 5.0.0.93. GUI tests use Qt's offscreen platform;
the three new README captures use the native Windows Qt platform.

Coverage includes tracking overlays and real hand photographs, masks/pen gestures,
timeline snapping/editing, effects and parameter defaults, text formatting and all
eight slide directions, keyframes, project persistence, updates, packaging metadata,
and the existing broader editor suites. Photos do not establish tracking accuracy
on arbitrary video, skin tones, occlusion, or fast movement.

The long single-process suite accumulated roughly 6 GB of GUI fixture state and
was interrupted. It is not reported as a green run. The supported runner now uses
fresh processes per module, per-test timeouts and a 600-second module limit; crashes,
timeouts and assertion failures remain failures. Release CI uses the same runner.

## Fixes established by this pass

- Font list initialization, Favorites toggling, readable family names and samples,
  registered font-file discovery, and removal of unrenderable picker choices.
- Separate entry/exit direction controls and diagonal editable keyframes, plus a shared
  resting pose that prevents In/Out slides drifting when reapplied.
- Independent skeleton/point visibility, consistent Soft Bloom/Light Wrap defaults,
  and a shape-inspector structural signature that keeps value edits and scroll stable.
- A themed local-file fallback for YouTube import and accurate media-library wording.
- Audit fixtures that provide two simulated hands for connector checks and avoid
  native inference lifetime issues; real inference remains a separate integration check.
- Updated stale font-control/card assertions, hardware-aware microphone assertions,
  suitable stroke thickness for an erosion-survival check, and default-session-marker
  coverage that respects the explicit temporary-file override contract.

## Not verified or released

No new frozen executable or installer was built, installed, upgraded or uninstalled.
No release tag was published. Manual interactive end-to-end behavior and long-duration
tracking responsiveness/cancellation remain unverified. The planned Feedback Center
is absent. Source identifies as 0.1.0-alpha.10 in development; the README intentionally
links to the existing published Alpha 9 installer and labels that distinction.
