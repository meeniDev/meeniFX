import copy
import numpy as np
import pytest
from conftest import build_engine, synthetic_frame
from meenifx.effects.shader_effects import LensWarpEffect
from meenifx.effects.page_curl import PageCurlEffect
from meenifx.processing import shaders as S


def picture(width=160, height=100, alpha=True):
    y, x = np.indices((height, width))
    checker = ((x // 9 + y // 9) % 2 * 180 + 40).astype(np.uint8)
    layer = np.dstack((checker, np.roll(checker, 4, axis=1), 255-checker,
                       np.full_like(checker, 255)))
    if alpha:
        layer[:, :width//4, 3] = 0
        layer[height//3:height//2, :, 3] = 100
    return layer


@pytest.fixture
def backend():
    runner = S.ShaderRunner()
    try:
        runner.render("identity", picture(4, 3))
    except S.ShaderUnavailable as exc:
        runner.close()
        pytest.skip(str(exc))
    yield runner
    runner.close()


@pytest.mark.parametrize("settings", [
    {}, {"amount": -100, "edge": "transparent"}, {"amount": -100, "edge": "mirror", "radius": 100},
    {"center_x": 0, "center_y": 100, "aspect": .3},
    {"mode": "pinch", "chromatic": 15, "edge": "clamp"},
    {"interpolation": "nearest", "amount": 31, "chromatic": 6}])
def test_lens_gpu_matches_cpu(backend, settings):
    effect = LensWarpEffect(None)
    effect.load_params(settings)
    layer = picture()
    values, extra = effect.uniforms(effect.values(), layer)
    cpu = S.finish(effect.cpu(S.prepare(layer), values))
    gpu = backend.render(effect.key, layer, values)
    diff = np.abs(cpu.astype(float) - gpu.astype(float))
    # Float arithmetic at isolated interpolation boundaries can differ by one code value.
    assert diff.mean() < .05 and np.quantile(diff, .999) <= 2
    assert not np.array_equal(gpu, layer)


def test_lens_neutral_invalid_values_and_repeatable_seeking():
    effect = LensWarpEffect(None)
    source = picture()
    effect.amount.set(0)
    assert np.array_equal(effect.apply_layer(source), source)
    effect.amount.set(float("nan"))
    effect.aspect.set(0)
    assert effect.apply_layer(source).shape == source.shape
    effect.load_params({"amount": 80, "aspect": 1})
    result = effect.apply_layer(source)
    effect.local_frame = 900
    assert np.array_equal(result, effect.apply_layer(source))


def test_lens_registry_keyframes_mix_and_parameter_defaults():
    from meenifx.model.keyframes import AnimatedValue
    engine, clip = build_engine()
    assert engine.runtime_effects["lens_warp"].name == "Lens Warp"
    runtime = engine.runtime_effects["lens_warp"]
    assert all(runtime.param_vars()[spec.name].get() == spec.default for spec in runtime.PARAMS)
    amount = AnimatedValue()
    amount.set_keyframe(0, 0)
    amount.set_keyframe(20, 90)
    clip.effects = [{"effect_id": "lens_warp", "params": {"amount": amount, "backend": "cpu"}}]
    first = engine.render_frame(0)[1]
    mid = engine.render_frame(10)[1]
    last = engine.render_frame(20)[1]
    assert not np.array_equal(first, mid) and not np.array_equal(mid, last)
    clip.effects[0]["params"]["mix"] = 0
    assert np.array_equal(first, engine.render_frame(20)[1])


@pytest.mark.parametrize("settings", [
    {}, {"progress": .1, "direction": "top"}, {"progress": .7, "direction": "left", "angle": 30},
    {"radius": 45, "position_x": 20, "position_y": 70, "depth": 95},
    {"antialias": "draft", "back_texture": "source", "thickness": 4},
    {"shadow": 90, "highlight": 100, "softness": .1}])
def test_page_gpu_matches_cpu(backend, settings):
    effect = PageCurlEffect(None)
    effect.load_params(settings)
    source = picture(173, 91)
    uniforms, extra = effect.uniforms(effect.values(), source)
    cpu = S.finish(effect.cpu(S.prepare(source), uniforms))
    gpu = backend.render(effect.key, source, uniforms)
    diff = np.abs(cpu.astype(float) - gpu.astype(float))
    assert diff.mean() < .1 and np.quantile(diff, .999) <= 3
    assert not np.array_equal(gpu, source)


def test_page_endpoints_curvature_backside_and_texture(backend, tmp_path):
    import cv2
    effect = PageCurlEffect(None)
    source = picture()
    effect.progress.set(0)
    assert np.array_equal(effect.apply_layer(source), source)
    effect.progress.set(1)
    assert not effect.apply_layer(source).any()
    effect.progress.set(.4)
    first = effect.apply_layer(source)
    # Curved shading has many values and changes source coordinates; it is not a wipe.
    assert np.unique(first[:, :, :3].reshape(-1, 3), axis=0).shape[0] > 150
    effect.back_texture.set("source")
    assert not np.array_equal(first, effect.apply_layer(source))
    image = tmp_path / "back.png"
    cv2.imwrite(str(image), np.full_like(source, (20, 70, 240, 255)))
    effect.back_texture.set("image")
    effect.back_path.set(str(image))
    uniforms, extra = effect.uniforms(effect.values(), source)
    cpu = effect.apply_layer(source)
    gpu = backend.render(effect.key, source, uniforms, extra=extra)
    assert np.abs(cpu.astype(float) - gpu.astype(float)).mean() < .1
    effect.back_path.set(str(tmp_path / "missing.png"))
    with pytest.raises(ValueError, match="backside image"):
        effect.apply_layer(source)


@pytest.mark.parametrize("effect_type", [LensWarpEffect, PageCurlEffect])
def test_transparent_input_has_no_invented_opaque_pixels(effect_type):
    source = np.full((40, 80, 4), 255, np.uint8)
    source[:, :, 3] = 0
    assert not effect_type(None).apply_layer(source).any()


def test_lens_filtering_does_not_create_black_alpha_halos():
    source = np.full((60, 100, 4), 255, np.uint8)
    source[:, :35] = 0
    effect = LensWarpEffect(None)
    effect.radius.set(80)
    out = effect.apply_layer(source)
    visible = out[:, :, 3] > 2
    assert visible.any() and np.all(out[:, :, :3][visible] == 255)


def test_gpu_failure_has_matching_fallback_or_explicit_error(monkeypatch):
    class Unsupported:
        def render(self, *args, **kwargs):
            raise S.ShaderUnavailable("unsupported test driver")
    monkeypatch.setattr(S, "runner", lambda: Unsupported())
    effect = LensWarpEffect(None)
    effect.backend.set("gpu")
    with pytest.raises(S.ShaderUnavailable, match="unsupported"):
        effect.apply_layer(picture())
    effect.backend.set("auto")
    # Exercise auto fallback on a render worker, where GPU would be attempted.
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as pool:
        fallback = pool.submit(effect.apply_layer, picture()).result()
    assert "fallback" in effect.status.lower()
    effect.backend.set("cpu")
    assert np.array_equal(fallback, effect.apply_layer(picture()))


@pytest.mark.parametrize("key", ["lens_warp", "page_curl"])
@pytest.mark.parametrize("fps", [23.976, 60.])
def test_preview_scaling_and_absolute_frame_time(key, fps, monkeypatch):
    import cv2
    engine, clip = build_engine(frame=synthetic_frame(320, 180), fps=fps)
    clip.effects = [{"effect_id": key, "params": {"backend": "cpu"}}]
    full = engine.render_frame(7, 1)[1]
    preview = engine.render_frame(7, .5)[1]
    expected = cv2.resize(full, (160, 90), interpolation=cv2.INTER_AREA)
    assert np.abs(preview.astype(float) - expected.astype(float)).mean() < 8
    seen = []
    class Capture:
        def render(self, name, image, uniforms, extra=()):
            seen.append(uniforms)
            return image.copy()
    monkeypatch.setattr(S, "runner", lambda: Capture())
    effect = engine.runtime_effects[key]
    effect.backend.set("gpu")
    effect.local_frame = 47
    effect.apply_layer(np.dstack((synthetic_frame(), np.full((180, 320), 255, np.uint8))))
    assert seen[0]["iFrame"] == 47 and seen[0]["iTime"] == pytest.approx(47/fps)
    seen.clear()
    engine.apply_effects(synthetic_frame(), [{"effect_id": key, "params": {"backend": "gpu"}}],
                         local_frame=17)
    assert seen[0]["iFrame"] == 17 and seen[0]["iTime"] == pytest.approx(17/fps)
