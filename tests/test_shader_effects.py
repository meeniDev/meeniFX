import copy
import numpy as np
import pytest
from conftest import build_engine, synthetic_frame
from meenifx.effects.shader_effects import LensWarpEffect
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
