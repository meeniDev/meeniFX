"""Standalone freeze probe for GLSL resources, ModernGL and native context dependencies."""
import json
from pathlib import Path
import sys
import numpy as np

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "meenifx" / "processing"))
import shaders as S


def main():
    runner = S.ShaderRunner()
    image = np.full((48, 80, 4), 255, np.uint8)
    image[:24, :, :3] = (40, 100, 220)
    image[:, 20:40, :3] = (220, 180, 80)
    try:
        assert np.array_equal(image, runner.render("identity", image))
        lens = runner.render("lens_warp", image, dict(center=(40., 24.), radius=22.,
            aspect=1., amount=.6, chromatic=.02, edge=0, nearest=False))
        page = runner.render("page_curl", image, dict(normal=(1., 0.), center=(40.,24.),
            axis=35., radius=12., thickness=.5, front=1., back=.8, shadow=.4,
            softness=3., highlight=.3, depth=.3, backColor=(.8,.8,.7), backMode=0,
            antialias=True, complete=False))
        assert lens.shape == image.shape and page.shape == image.shape
        assert not np.array_equal(page, image)
        cases = {
            "heat_haze": dict(amount=10., wavelength=30., detail=.6, heightBias=1., phase=.7, seedPhase=.2, direction=(1.,0.), edge=1),
            "ripple": dict(center=(40.,24.), radius=40., amount=10., wavelength=20., falloff=1., phase=.7, edge=1),
            "pixel_stretch": dict(direction=(.707,.707), position=0., compression=.1, edge=1),
            "light_rays": dict(center=(40.,24.), strength=1.5, reach=.7, threshold=.3, falloff=3., samples=24, rayColor=(1.,1.,1.), additive=False),
            "kaleidoscope": dict(center=(40.,24.), zoom=1., rotation=.2, segments=6, mirrored=True),
        }
        for key, uniforms in cases.items():
            rendered = runner.render(key, image, uniforms)
            assert rendered.shape == image.shape and not np.array_equal(rendered, image), key
        print(json.dumps({"frozen": bool(getattr(sys, "frozen", False)),
                          "gpu": runner.info, "identity": "exact", "effects": ["lens_warp", "page_curl", *cases]}))
    finally:
        runner.close()


if __name__ == "__main__":
    main()
