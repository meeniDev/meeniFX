"""One editor window: a second launch focuses the first instead of opening another."""

import time
from uuid import uuid4

from meenifx.single_instance import SingleInstance, server_name


def _pump(app, ready, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if ready():
            return True
        time.sleep(0.02)
    return ready()


def test_server_name_is_stable_and_per_user():
    assert server_name() == server_name()
    assert server_name().startswith("meeniFX-Studios-")


def test_a_second_launch_wakes_the_first_with_its_project(qapp, tmp_path):
    name = f"meeniFX-test-{uuid4().hex}"
    seen = []
    first = SingleInstance(name)
    assert first.acquire()
    first.activation_requested.connect(seen.append)
    second = SingleInstance(name)
    project = tmp_path / "Second Launch.mfxpf"
    project.write_bytes(b"not opened by this unit test")
    assert second.acquire(str(project)) is False
    assert _pump(qapp, lambda: bool(seen))
    assert seen == [str(project)]
    assert first._server.isListening()
    second.close()
    assert first._server.isListening()
    first.close()


def test_the_guard_is_released_when_the_first_instance_closes(qapp):
    name = f"meeniFX-test-{uuid4().hex}"
    first = SingleInstance(name)
    assert first.acquire()
    first.close()
    second = SingleInstance(name)
    assert second.acquire()
    second.close()
