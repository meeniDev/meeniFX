"""Real effect thumbnails for the browser, rendered by the app's own pipeline.

The reference picture is ``figure_cafe.png`` beside ``main.py`` (or the image chosen in
Preferences > Appearance > Effect Previews). A thumbnail is the reference with the effect
actually applied - the same ``apply_layer``/``apply`` calls the preview and the export use,
fed the effect's own defaults (plus a small preview-only configuration for effects that are
invisible at their defaults, see :data:`PREVIEW_ENTRIES`). Nothing here fakes a look.

Rendering happens on a small thread pool: each job builds its *own* effect instance, so a
thumbnail can never change the parameters of the effect the timeline is rendering. Results
are cached in memory and as PNGs under the user config folder, keyed by the effect, its
parameters, the reference image's path and timestamp, the requested size and the app
version - so editing the effect or replacing the image invalidates the cache by itself.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
from dataclasses import dataclass

import cv2
import numpy as np
from PyQt6.QtCore import QObject, QRunnable, QSettings, QThreadPool, pyqtSignal
from PyQt6.QtGui import QImage

from .config import APP_VERSION, ASSETS_ROOT, user_config_dir

DEFAULT_IMAGE_NAME = "figure_cafe.png"
GROUP = "preview"
# Thumbnail quality: the rendered pixels per logical pixel of the card.
QUALITY_SCALE = {"low": 1, "medium": 2, "high": 3}
BACKGROUNDS = ("checker", "dark", "light")
MAX_DISK_FILES = 400

# Effects whose result cannot be seen on a still frame: audio changes the sound, and the
# time effects change *which* source frame is shown (the engine decides that, not the
# effect). Their cards use an icon and a one-line description instead of a misleading
# picture - see the browser's card builder.
# Trackers whose subject is missing from the reference picture get the reference image
# with their own icon on top: an honest fallback that never draws a fake detection.
TRACKERS = {"face_tracker", "hand_tracker"}

ICON_ONLY = {
    "audio_gain", "audio_fade", "audio_eq", "audio_lowpass", "audio_highpass",
    "audio_compressor", "audio_limiter", "audio_reverb", "audio_delay", "audio_pan",
    "posterize_time", "stutter", "echo",
    # Trackers only draw what they detect, and a still life has no face or hand to find;
    # a card that looked like the plain reference would claim the effect does nothing.
    "face_tracker", "hand_tracker",
}

# Preview-only configurations. They exist so a card shows what the effect does; the
# effect's real defaults are never touched (the inspector still starts from them).
PREVIEW_ENTRIES = {
    "color_correct": {"params": {"preset": "cinematic"}},
    "corner_pin": {"params": {"corner_tl_x": 0.12, "corner_tl_y": 0.08,
                              "corner_tr_y": 0.16, "corner_bl_x": 0.06}},
    "mesh_warp": {"params": {"node_2_2_x": 0.12, "node_2_2_y": -0.10}},
    "skew": {"params": {"skew_x": 18.0}},
    "motion_blur": {"params": {"length": 60.0, "follow_motion": False}},
    "perspective": {"params": {"tilt_x": 22.0, "tilt_y": -12.0, "zoom": 118.0}},
    "light_leak": {"params": {"amount": 100.0, "spread": 100.0, "hue": 40.0}},
    # A puppet needs pins before it does anything; two pulled pins show the deformation.
    "puppet_warp": {"pins": [
        {"id": 1, "src": [0.42, 0.40], "x": 0.36, "y": 0.34, "locked": False},
        {"id": 2, "src": [0.60, 0.55], "x": 0.68, "y": 0.60, "locked": False},
    ]},
}

# Frames at which a time-driven effect is rendered when frame 0 is the neutral state.
PREVIEW_FRAME = {"flash": 3, "exposure_flicker": 6, "light_leak": 60}


# --------------------------------------------------------------------------- settings
def _store():
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "meeniFX", "Studios")


@dataclass
class PreviewSettings:
    """Where effect previews come from and how they are drawn (global, not per project)."""

    image_mode: str = "default"      # default | custom
    custom_path: str = ""
    background: str = "checker"      # checker | dark | light
    quality: str = "medium"          # low | medium | high

    @classmethod
    def load(cls, store=None):
        store = store or _store()
        out = cls()
        store.beginGroup(GROUP)
        try:
            out.image_mode = str(store.value("image_mode", out.image_mode) or "default")
            out.custom_path = str(store.value("custom_path", "") or "")
            out.background = str(store.value("background", out.background) or "checker")
            out.quality = str(store.value("quality", out.quality) or "medium")
        finally:
            store.endGroup()
        if out.image_mode not in ("default", "custom"):
            out.image_mode = "default"
        if out.background not in BACKGROUNDS:
            out.background = "checker"
        if out.quality not in QUALITY_SCALE:
            out.quality = "medium"
        return out

    def save(self, store=None):
        store = store or _store()
        store.beginGroup(GROUP)
        for name in ("image_mode", "custom_path", "background", "quality"):
            store.setValue(name, getattr(self, name))
        store.endGroup()
        store.sync()

    def scale(self):
        return QUALITY_SCALE.get(self.quality, 2)


def default_reference():
    """The shipped reference picture beside ``main.py``, or None when it is missing."""
    path = os.path.join(str(ASSETS_ROOT), DEFAULT_IMAGE_NAME)
    return path if os.path.isfile(path) else None


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _readable(path):
    """A custom image is only used when the app can actually decode it."""
    return bool(path) and os.path.isfile(path) and _load_image(path, _mtime(path), 1) is not None


def reference_path(settings=None):
    """(path, is_custom) for the image thumbnails are rendered from."""
    settings = settings or PreviewSettings.load()
    if settings.image_mode == "custom" and _readable(settings.custom_path):
        return settings.custom_path, True
    return default_reference(), False


# ----------------------------------------------------------------------- image layers
@functools.lru_cache(maxsize=6)
def _load_image(path, stamp, max_edge=1):
    """BGRA image at a bounded size; cached by path and modification time."""
    try:
        data = np.fromfile(path, dtype=np.uint8)
        image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    except (OSError, ValueError, cv2.error):
        return None
    if image is None:
        return None
    if image.ndim == 2:
        image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
    elif image.shape[2] == 3:
        image = cv2.cvtColor(image, cv2.COLOR_BGR2BGRA)
    elif image.shape[2] == 4:
        image = np.ascontiguousarray(image[:, :, :4])
    else:
        return None
    height, width = image.shape[:2]
    longest = max(height, width)
    if longest > max_edge > 0:
        factor = max_edge / float(longest)
        image = cv2.resize(image, (max(1, int(round(width * factor))),
                                   max(1, int(round(height * factor)))),
                           interpolation=cv2.INTER_AREA)
    return np.ascontiguousarray(image)


def reference_layer(width, height, settings=None):
    """The reference image fitted and centred on a transparent layer, aspect preserved."""
    path, _custom = reference_path(settings)
    if not path:
        return None
    max_edge = max(int(width), int(height)) * 2      # keep source detail for high quality
    image = _load_image(path, _mtime(path), max_edge)
    if image is None:
        return None
    src_h, src_w = image.shape[:2]
    factor = min(width / float(src_w), height / float(src_h))
    new_w, new_h = max(1, int(round(src_w * factor))), max(1, int(round(src_h * factor)))
    interpolation = cv2.INTER_AREA if factor < 1 else cv2.INTER_LANCZOS4
    scaled = cv2.resize(image, (new_w, new_h), interpolation=interpolation)
    layer = np.zeros((height, width, 4), dtype=np.uint8)
    x0, y0 = (width - new_w) // 2, (height - new_h) // 2
    layer[y0:y0 + new_h, x0:x0 + new_w] = scaled
    return layer


def background_layer(width, height, mode="checker", cell=8):
    """The surface behind a preview: a subtle checkerboard, or a flat dark/light fill."""
    if mode == "light":
        return np.full((height, width, 3), 216, dtype=np.uint8)
    if mode == "dark":
        return np.full((height, width, 3), 11, dtype=np.uint8)
    dark, light = np.array((27, 23, 20), dtype=np.uint8), np.array((33, 29, 23), dtype=np.uint8)
    board = np.zeros((height, width, 3), dtype=np.uint8)
    for y in range(0, height, cell):
        for x in range(0, width, cell):
            board[y:y + cell, x:x + cell] = light if ((x // cell) + (y // cell)) % 2 else dark
    return board


def _alpha_over(background, layer):
    """Composite a BGRA layer over a BGR background (what the preview does)."""
    alpha = (layer[:, :, 3:4].astype(np.float32) / 255.0)
    colour = layer[:, :, :3].astype(np.float32)
    return np.clip(colour * alpha + background.astype(np.float32) * (1.0 - alpha),
                   0, 255).astype(np.uint8)


# ----------------------------------------------------------------------- the rendering
def effect_instances(engine):
    """{key: class} for the engine's registered effects (classes, not shared objects)."""
    return {type(effect).key: type(effect) for effect in getattr(engine, "effects", ())}


def preview_entry(effect):
    """The entry a thumbnail renders: the effect's defaults plus a preview-only nudge."""
    entry = {"effect_id": effect.key, "enabled": True,
             "params": {name: value for name, value in effect.capture_params().items()}}
    extra = PREVIEW_ENTRIES.get(effect.key)
    if extra:
        entry["params"].update(extra.get("params") or {})
        if extra.get("pins"):
            entry["pins"] = [dict(pin) for pin in extra["pins"]]
    return entry


def style_entries(look, strength=100.0):
    """A Style's stack plus its group card, so the preview includes the group's strength."""
    from .model import looks as LOOKS
    stack = LOOKS.stack_for(look)
    for entry in stack:
        entry["look_id"] = look["id"]
    stack.insert(0, {"effect_id": "look", "enabled": True, "look_id": look["id"],
                     "params": {"look": look["id"], "strength": strength}})
    return stack


class _PreviewClip:
    """The smallest clip the preset model needs: a duration, an instance and its text."""

    def __init__(self, media, preset, width, height, duration=180):
        from .model import preset_library as LIB
        from .model import presets as PM

        self.duration = int(duration)
        self.is_text = media == "text"
        self.effects = []
        self.text = {}
        category, program, args, knobs = LIB.preset_spec(preset or {}, None)
        knobs = dict(knobs)
        if not self.is_text:
            knobs["unit"] = "whole"
        self.presets = [PM.make(preset.get("id") or "preview", category, program, args,
                                knobs, duration=30, name=preset.get("name") or "", clip=self)]
        if self.is_text:
            self.text = {"text": "Move", "bold": True, "shadow_opacity": 0.35,
                         "shadow_blur": 2.0, "size": max(10.0, min(width, height) * 0.36)}


def _motion_score(effect):
    """How much a preset pose moves or changes the picture, per unit included."""
    def score(pose):
        return (abs(pose.dx) + abs(pose.dy) + abs(1.0 - pose.scale_x) * 100.0
                + abs(1.0 - pose.scale_y) * 100.0 + abs(pose.rotation) / 2.0
                + abs(pose.blur) * 4.0 + abs(pose.hue) / 10.0 + abs(pose.light) * 40.0)

    best = score(effect.pose)
    for poses in effect.unit_poses.values():
        for pose in poses:
            best = max(best, score(pose))
    return best + (1.0 if not effect.is_identity() else 0.0)


def animation_entry(family, media="clip"):
    """The entry an animation card renders: the family's default variant."""
    from .model import animation_families as FAMILIES

    mode = FAMILIES.default_mode(family)
    preset = FAMILIES.variant(family, media, mode, "")
    if preset is None:
        return None
    return {"animation": {"preset": preset, "media": "text" if media == "text" else "clip"}}


def animation_layer(engine, animation, width, height, settings=None, fraction=None):
    """The reference picture (or a text sample) at an animation frame.

    With no `fraction` the liveliest representative frame is chosen for a static card.
    Hover previews pass 0..1 to sample the same animation at a precise moment.
    """
    from .model import presets as PM
    from .model import text as TXT
    from .model import transform as transforms
    from .processing import layers as layer_ops
    from .processing import text as text_render
    from .processing import text_units as unit_ops

    media = "text" if animation.get("media") == "text" else "clip"
    clip = _PreviewClip(media, animation.get("preset") or {}, width, height)
    units = None
    if clip.is_text:
        units = unit_ops.layout_units(TXT.resolve(clip, 0), width, height, 1.0)
    instance = PM.instances(clip)[0]
    start, length = PM.window(instance, clip.duration)
    samples = []
    # A per-letter animation is only legible once the later letters have arrived, so text
    # samples the second half of its window; pictures look best closer to the start.
    fractions = ((0.45, 0.6, 0.75, 0.9) if clip.is_text
                 else (0.2, 0.32, 0.45, 0.58, 0.72))
    for sample_fraction in fractions:
        frame = start + int(round((length - 1) * sample_fraction))
        effect = PM.evaluate(clip, frame, units=units, project_size=(width, height))
        samples.append((frame, effect))

    def draw(frame, effect):
        if clip.is_text:
            return text_render.render(TXT.resolve(clip, frame), width, height, 1.0,
                                      poses=effect.unit_poses)
        return reference_layer(width, height, settings)

    def finish(layer, effect, opacity):
        pose = effect.pose
        if not pose.is_identity():
            state = transforms.TransformState(position_x=pose.dx, position_y=pose.dy,
                                              scale_x=pose.scale_x * 100.0,
                                              scale_y=pose.scale_y * 100.0,
                                              rotation=pose.rotation)
            layer = layer_ops.apply_transform(layer, (0.0, 0.0, float(width), float(height)),
                                              state, (width, height), 1.0)
            layer = layer_ops.preset_look(layer, pose.blur, pose.hue, pose.light)
        if opacity < 0.999:
            layer = layer.copy()
            layer[:, :, 3] = np.clip(layer[:, :, 3].astype(np.float32)
                                     * max(0.0, opacity), 0, 255).astype(np.uint8)
        return layer

    if fraction is not None:
        fraction = max(0.0, min(1.0, float(fraction)))
        frame = start + int(round((length - 1) * fraction))
        effect = PM.evaluate(clip, frame, units=units, project_size=(width, height))
        layer = draw(frame, effect)
        return None if layer is None else finish(layer, effect, effect.pose.opacity)

    # The liveliest frame wins, but a frame whose letters (or picture) have already left
    # the card is worse than a calmer one: walk the samples from most to least moved and
    # take the first that still shows something. A nearly invisible fade frame is skipped
    # first, so the card shows a half-faded picture instead of the untouched reference.
    samples.sort(key=lambda pair: _motion_score(pair[1]), reverse=True)
    fallback = None
    chosen = None
    for frame, effect in samples:
        if effect.pose.opacity < 0.35:
            continue
        layer = draw(frame, effect)
        if layer is None:
            continue
        coverage = float(layer[:, :, 3].mean())
        if fallback is None or coverage > fallback[0]:
            fallback = (coverage, layer, frame, effect)
        if coverage >= 1.5:
            chosen = (layer, frame, effect)
            break
    if chosen is None and fallback is not None:
        _coverage, chosen_layer, frame, effect = fallback
        chosen = (chosen_layer, frame, effect)
    if chosen is None:
        return None
    layer, frame, effect = chosen
    pose = effect.pose
    preview_opacity = pose.opacity
    if _motion_score(effect) <= 1.0:
        dimmest = min(samples, key=lambda pair: pair[1].pose.opacity)[1].pose.opacity
        if dimmest < 0.6:
            # A strobe or flicker changes only the opacity, so every visible frame looks
            # like the bare reference. A half-faded card says what it does - a preview
            # illustration only, the instance's own values are never touched.
            preview_opacity = 0.45
    return finish(layer, effect, preview_opacity)


def apply_entries(engine, entries, layer, frame=0):
    """Run entries over a BGRA layer exactly like the engine, on private effect objects."""
    from .model.keyframes import parameter_value_at
    from .processing import blend as blend_ops

    entries = list(entries or ())
    looks = engine.look_strengths(entries, frame)
    classes = effect_instances(engine)
    output = layer
    for entry in entries:
        effect_id = str(entry.get("effect_id") or "")
        if not entry.get("enabled", True) or effect_id == "look":
            continue
        cls = classes.get(effect_id)
        if cls is None or not cls.supports_track_type("video"):
            continue
        mix = engine.entry_mix(entry, frame, looks)
        if mix <= 0.001:
            continue
        runtime = cls(engine)          # a private instance: never the one the timeline uses
        params = {name: parameter_value_at(value, frame)
                  for name, value in (entry.get("params") or {}).items()}
        runtime.param_scale = 1.0
        runtime.clip_entry = entry
        runtime.layer_rect = None
        runtime.clip = None
        runtime.local_frame = int(frame)
        try:
            runtime.load_params(params)
            if hasattr(runtime, "apply_layer"):
                result = runtime.apply_layer(output)
            else:
                colour = runtime.apply(np.ascontiguousarray(output[:, :, :3]))
                if colour.shape[:2] != output.shape[:2]:
                    colour = cv2.resize(colour, (output.shape[1], output.shape[0]),
                                        interpolation=cv2.INTER_AREA)
                result = np.dstack([colour, output[:, :, 3]])
        except Exception:          # a broken effect must not empty the browser
            continue
        output = blend_ops.mix_layers(output, result, mix)
    return output


def render_animation_frame(engine, animation, width, height, settings=None, fraction=0.5):
    """One hover-preview frame of an animation, composited over its configured background."""
    settings = settings or PreviewSettings.load()
    layer = animation_layer(engine, animation, width, height, settings, fraction=fraction)
    if layer is None:
        return None
    return _alpha_over(background_layer(width, height, settings.background), layer)


def render_entries(engine, entries, width, height, settings=None, frame=None):
    """BGR thumbnail for an entry stack; None when the reference image is unusable."""
    settings = settings or PreviewSettings.load()
    entries = list(entries or ())
    if len(entries) == 1 and isinstance(entries[0].get("animation"), dict):
        layer = animation_layer(engine, entries[0]["animation"], width, height, settings)
    else:
        layer = reference_layer(width, height, settings)
        if layer is not None:
            layer = apply_entries(engine, entries, layer, 0 if frame is None else int(frame))
    if layer is None:
        return None
    return _alpha_over(background_layer(width, height, settings.background), layer)


def render_effect(engine, effect_key, width, height, settings=None):
    effect = next((item for item in engine.effects if type(item).key == effect_key), None)
    if effect is None:
        return None
    frame = PREVIEW_FRAME.get(effect_key, 0)
    return render_entries(engine, [preview_entry(effect)], width, height, settings, frame)


def reference_preview(width, height, settings=None):
    """The reference image over its background, no effects: the preferences thumbnail."""
    settings = settings or PreviewSettings.load()
    layer = reference_layer(width, height, settings)
    if layer is None:
        return None
    return to_qimage(_alpha_over(background_layer(width, height, settings.background), layer))


def to_qimage(bgr):
    """QImage for a BGR array (copied, so the numpy buffer can go away)."""
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    height, width = rgb.shape[:2]
    return QImage(rgb.data, width, height, rgb.strides[0],
                  QImage.Format.Format_RGB888).copy()


# ------------------------------------------------------------------------- the cache
def cache_folder():
    folder = os.path.join(user_config_dir(), "thumbnails")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        return None
    return folder


def _disk_path(key):
    folder = cache_folder()
    return os.path.join(folder, f"{key}.png") if folder else None


def _prune():
    """Keep the cache bounded: the browser only ever needs the recent thumbnails."""
    folder = cache_folder()
    if not folder:
        return
    try:
        files = [(os.path.getmtime(os.path.join(folder, name)), name)
                 for name in os.listdir(folder) if name.endswith(".png")]
    except OSError:
        return
    if len(files) <= MAX_DISK_FILES:
        return
    for _stamp, name in sorted(files)[: len(files) - MAX_DISK_FILES]:
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


class _Job(QRunnable):
    """One thumbnail: memory hit, then disk, then a real render."""

    def __init__(self, cache, key, entries, width, height, frame):
        super().__init__()
        self._cache = cache
        self._key = key
        self._entries = entries
        self._width = width
        self._height = height
        self._frame = frame
        self.setAutoDelete(True)

    def run(self):
        if getattr(self._cache, "_stopped", False):
            return
        path = _disk_path(self._key)
        if path and os.path.isfile(path):
            try:
                data = np.fromfile(path, dtype=np.uint8)
                image = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
            except (OSError, ValueError, cv2.error):
                image = None
            if image is not None:
                self._cache.delivered(self._key, to_qimage(image[:, :, :3]))
                return
        try:
            bgr = render_entries(self._cache.engine, self._entries, self._width,
                                 self._height, self._cache.settings, self._frame)
        except Exception:
            bgr = None
        if bgr is None:
            return
        if path:
            try:
                ok, data = cv2.imencode(".png", bgr)
                if ok:
                    data.tofile(path)
                    _prune()
            except (OSError, cv2.error):
                pass
        self._cache.delivered(self._key, to_qimage(bgr))


class ThumbnailCache(QObject):
    """Hands out thumbnails by key, rendering the missing ones off the GUI thread.

    ``image(...)`` returns a QImage on a hit and schedules the work on a miss; the
    ``ready`` signal fires with (key, image) when a newly rendered thumbnail arrives.
    """

    ready = pyqtSignal(str, QImage)

    def __init__(self, engine, parent=None):
        super().__init__(parent)
        self.engine = engine
        self.settings = PreviewSettings.load()
        self._images = {}
        self._pending = set()
        self._stopped = False
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(max(1, min(2, (os.cpu_count() or 2))))
        self.ready.connect(self._store)

    # ------------------------------------------------------------------ plumbing
    def reload_settings(self):
        """Pick up a changed reference image / background / quality."""
        previous = (self.settings.image_mode, self.settings.custom_path,
                    self.settings.background, self.settings.quality)
        self.settings = PreviewSettings.load()
        current = (self.settings.image_mode, self.settings.custom_path,
                   self.settings.background, self.settings.quality)
        if current != previous:
            self.clear()

    def clear(self):
        self._images.clear()
        self._pending.clear()

    def shutdown(self):
        """Stop rendering: pending jobs are dropped and running ones finish before the
        browser goes away, so a worker can never emit into a deleted widget."""
        self._stopped = True
        self._images.clear()
        self._pending.clear()
        self._pool.clear()
        self._pool.waitForDone(5000)

    def _stamp(self):
        path, custom = reference_path(self.settings)
        return [path or "", _mtime(path) if path else 0.0, bool(custom)]

    def key(self, kind, item_id, payload, width, height, frame):
        raw = json.dumps([APP_VERSION, kind, item_id, payload, width, height, int(frame),
                          self.settings.background, self._stamp()],
                         sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]

    def _store(self, key, image):
        self._images[key] = image
        self._pending.discard(key)

    def delivered(self, key, image):
        """Called from a worker thread: hop back to the GUI thread to publish."""
        if self._stopped:
            return
        self.ready.emit(key, image)

    def image(self, kind, item_id, payload, entries, width, height, frame=0):
        """A thumbnail for (kind, item_id, payload) at this size, scheduling a miss."""
        width = max(16, int(width))
        height = max(16, int(height))
        key = self.key(kind, item_id, payload, width, height, frame)
        cached = self._images.get(key)
        if cached is not None:
            return cached
        if key not in self._pending:
            self._pending.add(key)
            self._pool.start(_Job(self, key, entries, width, height, frame))
        return None

    def cached(self, kind, item_id, payload, width, height, frame=0):
        return self._images.get(self.key(kind, item_id, payload, width, height, frame))

    def wait_until_idle(self, timeout_ms=20000):
        """For tests and the contact-sheet check: block until current jobs are done."""
        self._pool.waitForDone(timeout_ms)
