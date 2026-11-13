import threading
import numpy as np
import pytest
from meenifx.processing import shaders as S


@pytest.fixture
def gpu():
    backend = S.ShaderRunner()
    try:
        try:
            backend.render("identity", np.full((4, 6, 4), 255, np.uint8))
        except S.ShaderUnavailable as exc:
            pytest.skip(str(exc))
        yield backend
    finally:
        backend.close()


def test_identity_orientation_alpha_and_resource_reuse(gpu):
    rng = np.random.default_rng(42)
    image = rng.integers(0, 256, (47, 81, 4), dtype=np.uint8)
    image[:, :, 3] = np.maximum(1, image[:, :, 3])
    first = gpu.render("identity", image)
    resources = (gpu._fbo, tuple(gpu._textures), gpu._programs["identity"])
    assert np.array_equal(image, first)
    assert np.array_equal(first, gpu.render("identity", image))
    assert resources == (gpu._fbo, tuple(gpu._textures), gpu._programs["identity"])
    gpu.render("identity", image[:15, :21])
    assert gpu._size == (21, 15)
    gpu.close()
    assert not gpu._programs and not gpu._textures and gpu._ctx is None
    gpu.close()


def test_disabled_context_and_invalid_inputs(monkeypatch):
    monkeypatch.setenv("MEENIFX_DISABLE_SHADERS", "1")
    backend = S.ShaderRunner()
    image = np.zeros((4, 5, 4), np.uint8)
    try:
        with pytest.raises(S.ShaderUnavailable, match="disabled"):
            backend.render("identity", image)
        with pytest.raises(ValueError):
            backend.render("../../external", image)
        with pytest.raises(ValueError):
            backend.render("identity", image, {"value": float("nan")})
        with pytest.raises(ValueError):
            backend.render("identity", image[:, :, :3])
        with pytest.raises(ValueError):
            backend.render("identity", image, extra=(image,) * 4)
        with pytest.raises(InterruptedError):
            backend.render("identity", image, cancelled=lambda: True)
    finally:
        backend.close()


def test_worker_context_and_compiler_error(gpu, monkeypatch):
    original = S.resource
    threads = []
    def invalid(name):
        threads.append(threading.get_ident())
        return "invalid shader" if name == "lens_warp" else original(name)
    monkeypatch.setattr(S, "resource", invalid)
    with pytest.raises(S.ShaderUnavailable):
        gpu.render("lens_warp", np.zeros((5, 7, 4), np.uint8))
    assert threads and all(t != threading.get_ident() for t in threads)
    assert "lens_warp" not in gpu._programs


def test_packaged_resource_location(tmp_path, monkeypatch):
    assets = tmp_path / "assets" / "shaders"
    assets.mkdir(parents=True)
    (assets / "identity.glsl").write_text("packaged shader", encoding="utf-8")
    monkeypatch.setattr(S.sys, "_MEIPASS", str(tmp_path), raising=False)
    assert S.resource("identity") == "packaged shader"
