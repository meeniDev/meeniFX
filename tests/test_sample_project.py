"""The bundled sample must be a real project, not a screenshot or a placeholder."""

import os

from meenifx.project import container
from meenifx.project.settings import ProjectSettings
from meenifx.sample_project import bundled_path


def test_the_sample_project_is_bundled_and_self_contained():
    path = bundled_path()
    assert os.path.isfile(path), "examples/Getting Started/Getting Started.mfxpf must ship"

    data = container.read_container(path)
    assert ProjectSettings.from_dict(data["settings"]).name == "Getting Started"

    clips = data["clips"]
    names = list(clips["name"])
    assert "Getting Started" in names
    assert "Made with meeniFX Studios" in names
    assert any(name in ("Opening shape", "Closing shape") for name in names)
    assert all(not source for source in clips["media"]), "the sample must not point at media files"

    # Every clip has a picture source of its own: generated text or a vector shape.
    assert all(text is not None or shape is not None
               for text, shape in zip(clips["text"], clips["shape"]))

    effects = data["effects"]
    assert "glow" in list(effects["effect_id"])
    assert data["transitions"]["transitions"], "the sample must demonstrate a transition"
    assert data["keyframes"]["time"], "the sample must contain keyframe animation"
    assert len(data["tracks"]) >= 3
