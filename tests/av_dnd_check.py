"""Real mouse drag from the Project panel onto the timeline, in the real windowed app.

Run it directly (not under pytest, so the native platform plugin and a real desktop exist):

    python tests/av_dnd_check.py

It opens meeniFX, imports a small A/V file, then presses the left mouse button on the
media thumbnail and moves the *hardware cursor* to the timeline before releasing, using
Win32 mouse input. Qt therefore runs its own native drag loop - the same code path a
person triggers - so a broken drag source, MIME payload, drop target or coordinate
conversion cannot hide behind synthetic events.

The cursor is moved by the script for about a second; the window closes by itself.
"""

import ctypes
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

import av_fixture  # noqa: E402
from meenifx.project.settings import ProjectSettings  # noqa: E402
from meenifx.ui.main_window import MainWindow  # noqa: E402

USER32 = ctypes.windll.user32
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
SHOT = os.path.join(tempfile.gettempdir(), "meenifx_diag", "real-dnd.png")


def pump(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def cursor_to(x, y):
    USER32.SetCursorPos(int(x), int(y))


def button(down):
    USER32.mouse_event(MOUSEEVENTF_LEFTDOWN if down else MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)


def global_point(widget, point):
    return widget.mapToGlobal(QPoint(int(point.x()), int(point.y())))


def main():
    os.makedirs(os.path.dirname(SHOT), exist_ok=True)
    app = QApplication(sys.argv)
    MainWindow._load_demo_project = lambda self: None
    window = MainWindow(app)
    window.resize(1500, 900)
    window.show()
    window.raise_()
    window.activateWindow()
    pump(0.6)

    workdir = tempfile.mkdtemp(prefix="meenifx-dnd-")
    settings = ProjectSettings(name="DnD check", width=640, height=360, fps=30.0)
    assert window.projects.create_project(settings, workdir), "project creation failed"
    pump(0.3)

    media_path = av_fixture.write_av_avi(os.path.join(workdir, "spoken.avi"), seconds=3.0)
    panel = window.project_panel
    assert panel.add_files([media_path]) == 1, "the A/V file was not added to the bin"
    pump(0.8)                                    # let the thumbnail land

    media = panel.project.library.by_path(media_path)
    item = None
    for index in range(panel.list.count()):
        entry = panel.list.item(index)
        kind, key, _path = entry.data(Qt.ItemDataRole.UserRole)
        if kind == "item" and key == media.id:
            item = entry
            break
    assert item is not None, "media is missing from the panel"
    panel.list.scrollToItem(item)
    pump(0.2)

    canvas = window.timeline_panel.canvas
    lane = canvas.timeline.tracks_of_type("video")[0]
    start = global_point(panel.list.viewport(), panel.list.visualItemRect(item).center())
    target_frame = 90
    target = global_point(canvas, QPoint(int(canvas.x_at(target_frame)),
                                         int(canvas.header_y(lane) + canvas.track_height(lane) / 2)))
    print(f"press at {start.x()},{start.y()}  ->  release at {target.x()},{target.y()} (frame {target_frame})")

    cursor_to(start.x(), start.y())
    pump(0.3)
    button(True)

    def travel():
        steps = 40
        for step in range(1, steps + 1):
            x = start.x() + (target.x() - start.x()) * step / steps
            y = start.y() + (target.y() - start.y()) * step / steps
            cursor_to(x, y)
            time.sleep(0.03)
        time.sleep(0.35)
        button(False)

    worker = threading.Thread(target=travel, daemon=True)
    worker.start()
    pump(1.4)
    worker.join(timeout=3.0)
    pump(0.9)

    clips = canvas.timeline.all_clips()
    print(f"clips after the drop: {len(clips)}")
    for clip in clips:
        print(f"  {clip.stream:5s} link={clip.link_id:<3d} start={clip.timeline_start:<4d} "
              f"len={clip.duration:<4d} lane={clip.track.name} file={os.path.basename(clip.source_path)}")
    linked = [c for c in clips if c.link_id]
    groups = {c.link_id for c in linked}
    print(f"linked groups: {len(groups)}")
    for group_id in groups:
        members = canvas.timeline.link_group(group_id)
        print(f"  link {group_id}: {[(c.stream, c.id) for c in members]} "
              f"synced={canvas.timeline.link_is_synced(members[0])}")
    print(f"preview cleared after drop: {canvas._media_drop is None}")
    waited = 0.0
    while waited < 8.0 and not getattr(window.audio, "_segments", ()):
        pump(0.1)
        waited += 0.1
    segments = getattr(window.audio, "_segments", ())
    print(f"mixer segments for the pair: {len(segments)} after {waited:.1f}s "
          f"({[(round(s.start_seconds, 3), round(s.base_seconds, 3)) for s in segments]})")
    print(f"audio device available: {window.audio.available}")
    if segments:
        expected = canvas.timeline.all_clips()[0].timeline_start / canvas.timeline.fps
        print(f"first segment starts at {segments[0].start_seconds:.3f}s, clip at {expected:.3f}s")

    window.grab().save(SHOT)
    print(f"screenshot: {SHOT}")

    window.projects._set_clean()
    window.close()
    pump(0.3)
    ok = len(clips) == 2 and len(groups) == 1 and len(segments) == 1 and abs(segments[0].start_seconds - canvas.timeline.all_clips()[0].timeline_start / canvas.timeline.fps) < 1e-6
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
