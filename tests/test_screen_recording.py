"""Screen recording: the ffmpeg command, and a real segment with the pointer drawn.

Qt's own capture path cannot see the mouse pointer, so the screen is recorded by ffmpeg
(gdigrab). These tests check the command the class builds and that a real one-second take
produces a readable file, which is what the timeline insertion depends on.
"""

import os
import time

import pytest

from meenifx.recording import screen as screen_module


@pytest.fixture(scope="module")
def recorder(qapp):
    recorder = screen_module.ScreenRecorder()
    screen = qapp.primaryScreen()
    if screen is None:
        pytest.skip("no screen to capture")
    recorder.use_screen(screen)
    yield recorder
    recorder.release()


def command_for(recorder, show_cursor=True, bitrate=0):
    recorder.show_cursor = show_cursor
    recorder.configure(bitrate)
    return recorder.command("out.mkv")


def test_command_records_the_screen_with_the_pointer(recorder):
    command = command_for(recorder)
    assert command[command.index("-f") + 1] == "gdigrab"
    assert command[command.index("-draw_mouse") + 1] == "1", "the pointer is drawn by ffmpeg"
    width, height = recorder.size
    assert f"-video_size" in command and command[command.index("-video_size") + 1] == f"{width}x{height}"
    # gdigrab needs the physical origin of the screen, and even dimensions for yuv420p.
    assert width % 2 == 0 and height % 2 == 0
    assert command.index("-offset_x") < command.index("-i")
    assert command[-1] == "out.mkv"


def test_the_pointer_can_be_switched_off(recorder):
    command = command_for(recorder, show_cursor=False)
    assert command[command.index("-draw_mouse") + 1] == "0"


def test_a_manual_bitrate_is_used(recorder):
    command = command_for(recorder, bitrate=7)
    assert command[command.index("-b:v") + 1] == str(7_000_000)


def wait_until(condition, seconds=6.0):
    end = time.monotonic() + seconds
    from PyQt6.QtWidgets import QApplication
    while time.monotonic() < end:
        QApplication.processEvents()
        if condition():
            return True
        time.sleep(0.02)
    QApplication.processEvents()
    return condition()


def test_a_short_take_is_written_and_readable(qapp, recorder, tmp_path):
    pytest.importorskip("cv2")
    import cv2

    path = str(tmp_path / "Screen take.mkv")
    stopped, failed = [], []
    recorder.stopped.connect(stopped.append)
    recorder.failed.connect(lambda kind, message: failed.append((kind, message)))
    recorder.show_cursor = True
    recorder.configure(0)
    recorder.begin(path)
    assert recorder.is_recording(), f"ffmpeg did not start: {failed}"
    assert wait_until(lambda: os.path.isfile(path) and os.path.getsize(path) > 0, 4.0), \
        "ffmpeg wrote nothing"
    time.sleep(0.4)
    recorder.end()
    assert wait_until(lambda: stopped, 6.0), "the source never reported back"
    assert stopped == ["screen"] and not failed
    assert not recorder.is_recording()

    capture = cv2.VideoCapture(path)
    try:
        assert capture.isOpened(), "the segment is not readable"
        ok, frame = capture.read()
        assert ok and frame is not None and frame.ndim == 3
    finally:
        capture.release()
