"""Phase 2 GPU/CPU parity and deterministic time, including legacy Kaleidoscope."""
import numpy as np
import pytest
from conftest import build_engine
from test_shader_effects import picture, backend
from meenifx.effects.shader_expansion import SHADER_EXPANSION_EFFECTS
from meenifx.effects.glitch import KaleidoscopeEffect
from meenifx.processing import shaders as S

TYPES = (*SHADER_EXPANSION_EFFECTS, KaleidoscopeEffect)


@pytest.mark.parametrize("effect_type", TYPES)
@pytest.mark.parametrize("frame", [0, 47, 10987])
def test_phase2_gpu_matches_cpu(backend, effect_type, frame):
    effect = effect_type(None)
    effect.local_frame = frame
    if effect.key == "kaleidoscope":
        effect.backend.set("cpu")
    source = picture(171, 103)
    u, extra = effect.uniforms(effect.values(), source)
    cpu = S.finish(effect.cpu(S.prepare(source), u))
    gpu = backend.render(effect.key, source, u)
    diff = np.abs(cpu.astype(float)-gpu.astype(float))
    assert diff.mean() < .15 and np.quantile(diff,.999) <= 3, (effect.key,diff.mean(),diff.max())
    assert not np.array_equal(gpu, source)


@pytest.mark.parametrize("effect_type", TYPES)
def test_phase2_defaults_alpha_invalid_and_seeking(effect_type):
    effect = effect_type(None)
    effect.backend.set("cpu")
    source = picture()
    effect.local_frame = 77
    first = effect.apply_layer(source)
    for frame in (0, 40, 2000):
        effect.local_frame = frame
        effect.apply_layer(source)
    effect.local_frame = 77
    assert np.array_equal(first,effect.apply_layer(source))
    source[...,3] = 0
    assert not effect.apply_layer(source).any()
    for spec in effect.PARAMS:
        assert spec.name in effect.param_vars()
        if spec.kind in ("float","int"):
            getattr(effect,spec.name).set(float("nan"))
    assert effect.apply_layer(picture()).dtype == np.uint8


def test_kaleidoscope_preserves_default_legacy_path(monkeypatch):
    import meenifx.effects.glitch as G
    effect = KaleidoscopeEffect(None)
    assert effect.backend.get() == "legacy"
    calls = []
    original = G._remap
    def remap(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(G,"_remap",remap)
    legacy = effect.apply_layer(picture())
    assert calls
    effect.load_params({"segments": 6,"mode":"mirror","rotation":0})
    assert np.array_equal(legacy,effect.apply_layer(picture()))
    effect.backend.set("cpu")
    calls.clear()
    effect.apply_layer(picture())
    assert not calls


@pytest.mark.parametrize("effect_type",SHADER_EXPANSION_EFFECTS)
def test_phase2_registry_and_neutral_input(effect_type):
    engine,clip = build_engine()
    assert engine.runtime_effects[effect_type.key].name == effect_type.name
    effect = engine.runtime_effects[effect_type.key]
    for spec in effect.PARAMS:
        assert effect.param_vars()[spec.name].get() == spec.default
    source = picture()
    getattr(effect,"strength" if effect.key == "light_rays" else "amount").set(0)
    assert np.array_equal(source,effect.apply_layer(source))


@pytest.mark.parametrize("effect_type", SHADER_EXPANSION_EFFECTS[:2])
def test_wave_cycles_are_continuous_and_fps_based(effect_type):
    from types import SimpleNamespace
    effect = effect_type(SimpleNamespace(project_fps=60.))
    effect.speed.set(1.)
    source = picture()
    effect.local_frame = 0
    start, _ = effect.uniforms(effect.values(), source)
    effect.local_frame = 60
    loop, _ = effect.uniforms(effect.values(), source)
    assert start == loop
    effect.local_frame = 59.999
    before, _ = effect.uniforms(effect.values(), source)
    a = effect.cpu(S.prepare(source), start)
    b = effect.cpu(S.prepare(source), before)
    assert np.abs(a-b).mean() < .001


@pytest.mark.parametrize("effect_type,params", [
    (SHADER_EXPANSION_EFFECTS[0], dict(amount=100, wavelength=8, detail=100, height_bias=0, angle=137, edge="mirror", seed=65535)),
    (SHADER_EXPANSION_EFFECTS[1], dict(amount=-50, wavelength=5, radius=150, center_x=0, center_y=100, edge="transparent", falloff=0)),
    (SHADER_EXPANSION_EFFECTS[2], dict(position=0, width=.1, amount=100, angle=45, edge="mirror")),
    (SHADER_EXPANSION_EFFECTS[3], dict(strength=400, reach=150, threshold=0, samples=64, ray_r=35, ray_g=180, blend="add")),
    (KaleidoscopeEffect, dict(segments=7, rotation=30, center_x=0, center_y=1, zoom=50, mode="kaleidoscope")),
])
def test_phase2_gpu_parameter_boundaries(backend, effect_type, params):
    effect = effect_type(None)
    effect.load_params(params)
    effect.local_frame = 119
    source = picture(162,98)
    u, extra = effect.uniforms(effect.values(),source)
    cpu = S.finish(effect.cpu(S.prepare(source),u))
    gpu = backend.render(effect.key,source,u)
    assert np.abs(cpu.astype(float)-gpu.astype(float)).mean() < .2


def test_legacy_kaleidoscope_project_roundtrip(tmp_path):
    from test_keyframe_workflow import _roundtrip
    engine, clip = build_engine()
    params = {"segments": 8, "rotation": 12, "mode": "mirror", "zoom": 135}
    clip.effects = [{"effect_id": "kaleidoscope", "params": params}]
    restored, _ = _roundtrip(engine.timeline, tmp_path / "legacy-kaleidoscope.mfxpf")
    saved = restored.all_clips()[0].effects[0]
    assert saved["effect_id"] == "kaleidoscope"
    assert "backend" not in saved["params"]
    effect = KaleidoscopeEffect(None)
    effect.load_params(params)
    expected = effect.apply_layer(picture())
    effect.load_params(saved["params"])
    assert effect.backend.get() == "legacy"
    assert np.array_equal(expected, effect.apply_layer(picture()))
