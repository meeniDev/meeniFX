"""The built-in effects: mosaic, ASCII art and the color trackers.

Pixel-based parameters (cell sizes, line thickness, minimum face size, blob areas,
blur radius) are expressed in logical-resolution pixels. When the preview renders at
a lower quality the engine sets `param_scale`, and every such parameter is multiplied
by it, so a 35% preview is the same picture as the export, only softer.
"""

import logging
import os
from dataclasses import replace

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..config import ASSETS_DIR
from ..constants import DEFAULT_UNICODE_CHARSET
from ..model.values import Value
from ..processing.mosaic import frame_pixel, mosaic_frame
from . import tracking_regions as REGIONS
from .base import EffectBase, ParamSpec, TrackerColorMixin
from .tracking_viz import Detection

def _tracker_vis_params(label, contour=False):
    """Shared tracker controls with a useful default label for new effects.

    Existing projects carry their own label_text value, so an empty saved label stays
    empty (and now draws nothing) instead of being replaced with a tracker name.
    """
    result = []
    for spec in TrackerColorMixin.VIS_PARAMS:
        if spec.name == "label_text":
            spec = replace(spec, default=label)
        elif spec.name == "show_contour" and contour:
            spec = replace(spec, default=True)
        result.append(spec)
    return tuple(result)


FACE_VIS_PARAMS = _tracker_vis_params("Face")
HAND_VIS_PARAMS = _tracker_vis_params("Hand", contour=True)
BLOB_VIS_PARAMS = _tracker_vis_params("Blob")

FACE_CASCADE_FILE = "haarcascade_frontalface_default.xml"
FACE_MODEL_FILE = "face_detection_yunet_2023mar.onnx"

# MediaPipe's ``HandLandmarker.__del__`` calls close() again and can block inside its
# dispatcher if a wrapper is collected during a later allocation.  Closed wrappers are
# retained here (their native handle is already freed) so that destructor never runs.
_RETIRED_HAND_BACKENDS = []


def face_cascade_paths():
    """Cascade locations: the copy bundled with the app first, then OpenCV's data dir."""
    paths = [ASSETS_DIR / "haarcascades" / FACE_CASCADE_FILE]
    data_dir = getattr(cv2, "data", None)
    if data_dir is not None:
        paths.append(__import__("pathlib").Path(data_dir.haarcascades) / FACE_CASCADE_FILE)
    return [str(path) for path in paths]


def face_model_path():
    """OpenCV's YuNet detector: the backend that still exists in OpenCV 5."""
    return ASSETS_DIR / "models" / FACE_MODEL_FILE


def _create_yunet(width, height, score):
    """Create YuNet without leaking OpenCV 5's benign target warning into the console.

    OpenCV 5's new DNN graph reports ``setPreferableTarget`` as unsupported while the
    YuNet wrapper constructs the network.  Detection still runs on the CPU; the warning
    is noise for users, so silence only this construction call and restore the previous
    OpenCV log level immediately afterwards.
    """
    logging = getattr(getattr(cv2, "utils", None), "logging", None)
    previous = None
    if logging is not None:
        try:
            previous = logging.getLogLevel()
            logging.setLogLevel(logging.LOG_LEVEL_ERROR)
        except (AttributeError, RuntimeError):
            previous = None
    try:
        return cv2.FaceDetectorYN.create(str(face_model_path()), "",
                                         (width, height), score, 0.3, 50)
    finally:
        if previous is not None:
            try:
                logging.setLogLevel(previous)
            except (AttributeError, RuntimeError):
                pass


class MosaicEffect(EffectBase):
    key = "mosaic"
    name = "Mosaic"

    PARAMS = (
        ParamSpec("scale", "Scale", "int", 20, 90, 40, step=1,
                  help="Tile density: 20 is a coarse mosaic, 90 is a fine one"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.scale = Value(40)

    def tile_size(self, logical_w, logical_h):
        """Tile size in project pixels, scaled to the current render scale.

        Tiles are defined on the logical frame so a 35% preview keeps the same tile
        count and aspect as the export, only with smaller tiles.
        """
        tile_w, tile_h = frame_pixel(max(1, int(logical_w)), max(1, int(logical_h)),
                                     max(20, int(self.scale.get())))
        return (max(2, int(round(tile_w * self.param_scale))),
                max(2, int(round(tile_h * self.param_scale))))

    def apply(self, frame):
        logical_w, logical_h = self.app.project_size() if hasattr(self.app, "project_size") else (0, 0)
        if not logical_w or not logical_h:
            logical_w, logical_h = frame.shape[1], frame.shape[0]
        tile_w, tile_h = self.tile_size(logical_w, logical_h)
        small_tiles, small_palette = self.app.prepare_mosaic_resources(tile_w, tile_h)
        return mosaic_frame(frame, small_tiles, small_palette, tile_w, tile_h)


class AsciiEffect(EffectBase):
    key = "ascii"
    name = "ASCII Art"

    PARAMS = (
        ParamSpec("charset", "Characters", "text", default="",
                  help="Characters from dark to light; empty uses the built-in set"),
        ParamSpec("cell_size", "Cell size", "int", 4, 40, 10, unit=" px", pixels=True,
                  help="Size of one character cell in project pixels"),
        ParamSpec("color_mode", "Source color", "bool", default=True,
                  help="Colour the glyphs from the frame instead of using grey levels"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.charset = Value("")
        self.cell_size = Value(10)
        self.color_mode = Value(True)
        self._font_cache = {}
        self._font_path = self._resolve_font_path()

    def _resolve_font_path(self):
        candidates = [
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msyh.ttc"),
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "meiryo.ttc"),
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "msgothic.ttc"),
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "simhei.ttf"),
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialuni.ttf"),
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    def _get_font(self, cell_size):
        font_size = max(6, int(round(cell_size * 0.95)))
        cache_key = (self._font_path, font_size)
        cached = self._font_cache.get(cache_key)
        if cached is not None:
            return cached

        if self._font_path:
            try:
                font = ImageFont.truetype(self._font_path, font_size)
                self._font_cache[cache_key] = font
                return font
            except Exception:
                pass

        font = ImageFont.load_default()
        self._font_cache[cache_key] = font
        return font

    def on_registered(self):
        self._font_cache.clear()

    def apply(self, frame):
        chars = self.charset.get()
        if not chars:
            chars = DEFAULT_UNICODE_CHARSET

        cell = max(3, self.px(self.cell_size.get()))
        h, w = frame.shape[:2]
        cols = max(1, w // cell)
        rows = max(1, h // cell)

        small = cv2.resize(frame, (cols, rows), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        canvas = np.zeros_like(frame)
        canvas_rgb = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        image = Image.fromarray(canvas_rgb)
        draw = ImageDraw.Draw(image)
        font = self._get_font(cell)

        for y in range(rows):
            for x in range(cols):
                value = int(gray[y, x])
                idx = int((value / 255.0) * (len(chars) - 1))
                ch = chars[idx]
                if self.color_mode.get():
                    b, g, r = [int(v) for v in small[y, x]]
                    color = (b, g, r)
                else:
                    color = (value, value, value)
                draw.text((x * cell, y * cell), ch, font=font, fill=(color[2], color[1], color[0]))

        return cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)


class FaceTrackerEffect(TrackerColorMixin, EffectBase):
    category = "Tracking"
    key = "face_tracker"
    name = "Face Tracker"
    TRACKING_LABEL = "Face"

    PARAMS = (
        ParamSpec("confidence", "Confidence", "int", 1, 99, 50, unit="%",
                  help="How sure the detector must be before a face is accepted",
                  group="Detection"),
        ParamSpec("min_size", "Smallest face", "int", 12, 240, 30, unit=" px", pixels=True,
                  help="Smallest face to accept, in project pixels", group="Detection"),
        ParamSpec("thickness", "Line thickness", "int", 1, 8, 2, unit=" px", pixels=True,
                  group="Visualization"),
        ParamSpec("blur_faces", "Blur faces", "bool", default=False,
                  help="Blur the detected face region before drawing its overlay",
                  group="Face Effects"),
        ParamSpec("blur_amount", "Blur strength", "int", 5, 100, 35, unit=" %",
                  help="Blur radius as a percentage of the smaller face edge",
                  group="Face Effects"),
        ParamSpec("blur_feather", "Blur feather", "int", 0, 80, 10, unit=" px", pixels=True,
                  help="Soften the blur edge so it does not leave a hard rectangle",
                  group="Face Effects"),
        ParamSpec("blur_mode", "Blur style", "choice",
                  choices=("gaussian", "box", "pixelate", "mosaic"),
                  choice_labels=("Gaussian blur", "Box blur", "Pixelate", "Mosaic"),
                  default="gaussian", help="How the detected face is obscured",
                  group="Face Effects"),
        ParamSpec("face_smoothing", "Track smoothing", "int", 0, 95, 30, unit=" %",
                  help="Average face boxes over time to reduce jitter",
                  group="Face Effects"),
        ParamSpec("custom_effects", "Custom effect stack", "text", default="",
                  help="Extra effects inside each face, for example blur:60, pixelate:40",
                  group="Face Effects"),
        ParamSpec("sprite_path", "Sprite image", "text", default="",
                  help="PNG or other image attached to each face (alpha is preserved)",
                  group="Face Effects"),
        ParamSpec("sprite_scale", "Sprite size", "int", 5, 300, 70, unit=" %",
                  group="Face Effects"),
        ParamSpec("sprite_offset_x", "Sprite X", "int", -600, 600, 0, unit=" px",
                  pixels=True, group="Face Effects"),
        ParamSpec("sprite_offset_y", "Sprite Y", "int", -600, 600, 0, unit=" px",
                  pixels=True, group="Face Effects"),
        ParamSpec("sprite_rotation", "Sprite rotation", "int", -180, 180, 0, unit="\u00b0",
                  group="Face Effects"),
        ParamSpec("sprite_opacity", "Sprite opacity", "int", 0, 100, 100, unit=" %",
                  group="Face Effects"),
    ) + TrackerColorMixin.COLOR_PARAMS + FACE_VIS_PARAMS

    def __init__(self, app):
        super().__init__(app)
        self.confidence = Value(50)
        self.min_size = Value(30)
        self.thickness = Value(2)
        self.blur_faces = Value(False)
        self.blur_amount = Value(35)
        self.blur_feather = Value(10)
        self.blur_mode = Value("gaussian")
        self.face_smoothing = Value(30)
        self.custom_effects = Value("")
        self.sprite_path = Value("")
        self.sprite_scale = Value(70)
        self.sprite_offset_x = Value(0)
        self.sprite_offset_y = Value(0)
        self.sprite_rotation = Value(0)
        self.sprite_opacity = Value(100)
        self._face_smooth = {}
        self._sprite_cache = {}
        self._init_color_controls()
        self._backend = "none"
        self._cascade = None
        self._detector = None
        self._detector_key = None
        self.cascade_error = ""

    def on_registered(self):
        """Pick a detector that this OpenCV build actually has.

        OpenCV 5 removed `CascadeClassifier` and the Haar XML files from the wheel,
        so YuNet (DNN) is the primary backend and the cascade remains the fallback
        for OpenCV 4 installs.
        """
        model = face_model_path()
        if hasattr(cv2, "FaceDetectorYN") and model.exists():
            self._backend = "yunet"
            self.cascade_error = ""
            return
        if hasattr(cv2, "CascadeClassifier"):
            for path in face_cascade_paths():
                if not os.path.exists(path):
                    continue
                cascade = cv2.CascadeClassifier(path)
                if not cascade.empty():
                    self._cascade = cascade
                    self._backend = "haar"
                    self.cascade_error = ""
                    return
        self._backend = "none"
        self.cascade_error = ("No face detection backend: restore assets/models "
                              "(YuNet) or use an OpenCV build with Haar cascades")

    def available(self):
        return self._backend != "none"

    def detect_faces(self, frame):
        """[(x, y, w, h)] of detected faces in the frame's own pixels.

        Detection may run on a smaller working image according to the quality preset;
        boxes are always scaled back before they leave this method.
        """
        if self._backend == "none":
            self.on_registered()
        work, factor = self._detection_scale(frame, 480, 720)
        if self._backend == "yunet":
            height, width = work.shape[:2]
            key = (width, height, int(self.confidence.get()))
            if self._detector is None or key != self._detector_key:
                score = max(0.05, min(0.99, int(self.confidence.get()) / 100.0))
                self._detector = _create_yunet(width, height, score)
                self._detector_key = key
            _ok, faces = self._detector.detect(work)
            result = []
            if faces is not None:
                for face in faces:
                    x, y, w, h = [int(round(float(value) / factor)) for value in face[:4]]
                    result.append((x, y, w, h))
            return result
        if self._backend == "haar":
            gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
            min_size = max(8, int(round(self.px(self.min_size.get()) * factor)))
            # The old min-neighbour knob is now a confidence percentage.
            neighbors = max(1, int(round(1 + int(self.confidence.get()) / 100.0 * 11)))
            boxes = self._cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=neighbors,
                                                   minSize=(min_size, min_size))
            return [tuple(int(round(v / factor)) for v in box) for box in boxes]
        return []

    def _smooth_faces(self, faces):
        """Temporal box smoothing; 0 passes detections straight through."""
        amount = max(0, min(95, int(self.face_smoothing.get()))) / 100.0
        if amount <= 0.0:
            self._face_smooth.clear()
            return [tuple(int(round(float(value))) for value in box) for box in faces]
        smoothed = []
        for index, box in enumerate(faces):
            previous = self._face_smooth.get(index)
            value = [float(component) for component in box]
            if previous is not None and len(previous) == 4:
                value = [previous[axis] * amount + value[axis] * (1.0 - amount)
                         for axis in range(4)]
            self._face_smooth[index] = value
            smoothed.append(tuple(int(round(component)) for component in value))
        for key in [key for key in self._face_smooth if key >= len(smoothed)]:
            del self._face_smooth[key]
        return smoothed

    def _blur_face(self, output, box):
        x, y, w, h = [int(value) for value in box]
        x, y = max(0, x), max(0, y)
        w, h = min(output.shape[1] - x, max(1, w)), min(output.shape[0] - y, max(1, h))
        if w <= 0 or h <= 0:
            return
        roi = output[y:y + h, x:x + w]
        if not roi.size:
            return
        strength = max(5, min(100, int(self.blur_amount.get()))) / 100.0
        mode = str(self.blur_mode.get() or "gaussian").lower()
        k = max(3, int(round(min(w, h) * strength)) | 1)
        if mode == "box":
            blurred = cv2.blur(roi, (k, k))
        elif mode in ("pixelate", "mosaic"):
            divisor = 7.0 if mode == "pixelate" else 4.0
            block = max(2, int(round(min(w, h) * strength / divisor)))
            small = cv2.resize(roi, (max(1, w // block), max(1, h // block)),
                               interpolation=cv2.INTER_AREA)
            blurred = cv2.resize(small, (w, h), interpolation=cv2.INTER_NEAREST)
            if mode == "mosaic":
                blurred = (blurred // 32 * 32).astype(np.uint8)
        else:
            blurred = cv2.GaussianBlur(roi, (k, k), 0)
        feather = max(0, self.px(self.blur_feather.get()))
        if feather <= 1:
            output[y:y + h, x:x + w] = blurred
            return
        mask = np.zeros((h, w), np.uint8)
        cv2.ellipse(mask, (w // 2, h // 2), (max(1, w // 2 - 1), max(1, h // 2 - 1)),
                    0, 0, 360, 255, -1)
        mask = cv2.GaussianBlur(mask, (0, 0), max(0.5, feather / 2.0))
        alpha = mask.astype(np.float32)[:, :, None] / 255.0
        output[y:y + h, x:x + w] = np.clip(blurred * alpha + roi * (1.0 - alpha), 0, 255).astype(np.uint8)

    def _sprite(self):
        """The attached image, cached by path and modification time."""
        path = str(self.sprite_path.get() or "").strip()
        if not path or not os.path.isfile(path):
            return None
        try:
            stamp = os.path.getmtime(path)
        except OSError:
            return None
        cached = self._sprite_cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        image = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if image is None:
            return None
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGRA)
        elif image.shape[2] == 3:
            image = np.dstack([image, np.full(image.shape[:2], 255, np.uint8)])
        self._sprite_cache[path] = (stamp, image)
        return image

    def _draw_sprite(self, output, box):
        sprite = self._sprite()
        if sprite is None:
            return
        scale = max(0.05, float(self.sprite_scale.get()) / 100.0)
        face_w = max(1, int(round(box[2])))
        width = max(1, int(round(face_w * scale)))
        height = max(1, int(round(sprite.shape[0] * width / max(1, sprite.shape[1]))))
        image = cv2.resize(sprite, (width, height), interpolation=cv2.INTER_AREA)
        angle = float(self.sprite_rotation.get())
        if abs(angle) > 0.1:
            matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
            cos, sin = abs(matrix[0, 0]), abs(matrix[0, 1])
            new_w = int(round(height * sin + width * cos))
            new_h = int(round(height * cos + width * sin))
            matrix[0, 2] += new_w / 2.0 - width / 2.0
            matrix[1, 2] += new_h / 2.0 - height / 2.0
            image = cv2.warpAffine(image, matrix, (new_w, new_h), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))
            width, height = new_w, new_h
        centre_x = box[0] + box[2] / 2.0 + int(self.px(self.sprite_offset_x.get()))
        centre_y = box[1] + box[3] / 2.0 + int(self.px(self.sprite_offset_y.get()))
        left = int(round(centre_x - width / 2.0))
        top = int(round(centre_y - height / 2.0))
        x0, y0 = max(0, left), max(0, top)
        x1 = min(output.shape[1], left + width)
        y1 = min(output.shape[0], top + height)
        if x1 <= x0 or y1 <= y0:
            return
        patch = image[y0 - top:y1 - top, x0 - left:x1 - left]
        if patch.size == 0:
            return
        alpha = patch[:, :, 3].astype(np.float32)[:, :, None] / 255.0
        alpha *= max(0.0, min(1.0, float(self.sprite_opacity.get()) / 100.0))
        region = output[y0:y1, x0:x1]
        output[y0:y1, x0:x1] = (patch[:, :, :3].astype(np.float32) * alpha
                                + region.astype(np.float32) * (1.0 - alpha)).astype(np.uint8)

    def apply(self, frame):
        if self._backend == "none":
            if not self.available():
                return frame

        output = frame.copy()
        min_size = max(4, self.px(self.min_size.get()))
        signature = (self._backend, int(self.confidence.get()), min_size,
                     str(self.tracking_quality.get()))
        faces = self._cached_detections(
            output, signature, lambda: self.detect_faces(output))
        faces = [box for box in faces if box[2] >= min_size and box[3] >= min_size]
        if not faces:
            return output
        faces = self._smooth_faces(faces)

        if self.blur_faces.get():
            for box in faces:
                self._blur_face(output, box)
        detections = [Detection(box=box, confidence=1.0, track_id=index + 1)
                      for index, box in enumerate(faces)]
        output = self.apply_tracked_regions(output, detections)
        stack = REGIONS.parse_stack(self.custom_effects.get())
        if stack:
            for detection in detections:
                expand = self.px(self.region_expand.get())
                if self.region_expand.get() < 0:
                    expand = -self.px(-self.region_expand.get())
                mask = REGIONS.region_mask(output.shape, detection.box, detection.contour,
                                           expand=expand,
                                           feather=self.px(self.blur_feather.get()))
                output = REGIONS.apply_stack(output, mask, stack,
                                             scale=max(0.2, self.param_scale))
        for detection in detections:
            self._draw_sprite(output, detection.box)
        return self.draw_tracking(output, detections, label="Face")


class HandTrackerEffect(TrackerColorMixin, EffectBase):
    category = "Tracking"
    key = "hand_tracker"
    name = "Hand Tracker"
    TRACKING_LABEL = "Hand"

    PARAMS = (
        ParamSpec("max_hands", "Max hands", "int", 1, 6, 2, group="Detection"),
        ParamSpec("min_area", "Smallest hand", "int", 200, 40000, 2500, unit=" px²", area=True, advanced=True,
                  help="Ignore skin-coloured regions smaller than this (area in project pixels)",
                  group="Detection"),
        ParamSpec("max_area", "Largest hand", "int", 5000, 400000, 180000, unit=" px²", area=True,
                  advanced=True,
                  help="Ignore skin-coloured regions larger than this (area in project pixels)",
                  group="Detection"),
        ParamSpec("thickness", "Line thickness", "int", 1, 12, 2, unit=" px", pixels=True,
                  group="Visualization"),
        ParamSpec("hand_confidence", "Hand confidence", "int", 1, 99, 50, unit=" %",
                  help="Minimum landmark-model confidence for a hand", group="Detection"),
        ParamSpec("show_skeleton", "Skeleton", "bool", default=True,
                  help="Draw the 21-landmark skeletal connections", group="Visualization"),
        ParamSpec("point_size", "Joint size", "int", 1, 20, 4, unit=" px", pixels=True,
                  group="Visualization"),
        ParamSpec("line_width", "Skeleton width", "int", 1, 12, 3, unit=" px", pixels=True,
                  group="Visualization"),
        ParamSpec("skeleton_color", "Skeleton colour", "text", default="#00FFFF",
                  help="Skeletal connections; empty uses each hand's colour",
                  group="Visualization"),
    ) + TrackerColorMixin.COLOR_PARAMS + HAND_VIS_PARAMS

    # MediaPipe's 21-landmark hand topology (palm, thumb, index, middle, ring, pinky).
    SKELETON_CONNECTIONS = ((0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7),
                            (7, 8), (5, 9), (9, 10), (10, 11), (11, 12), (9, 13),
                            (13, 14), (14, 15), (15, 16), (13, 17), (17, 18), (18, 19),
                            (19, 20), (0, 17))

    def __init__(self, app):
        super().__init__(app)
        self.max_hands = Value(2)
        self.min_area = Value(2500)
        self.max_area = Value(180000)
        self.thickness = Value(2)
        self.hand_confidence = Value(50)
        self.show_skeleton = Value(True)
        self.point_size = Value(4)
        self.line_width = Value(3)
        self.skeleton_color = Value("#00FFFF")
        self._init_color_controls()
        self.show_contour = Value(True)
        self._hands = None
        self._hands_key = None
        self._landmark_error = ""
        self._reported_error = ""

    @staticmethod
    def model_path():
        """Path to the official hand-landmarker ``.task`` model, or an empty string."""
        override = os.environ.get("MEENIFX_HAND_MODEL", "").strip()
        if override:
            return override if os.path.isfile(override) else ""
        candidate = os.path.join(ASSETS_DIR, "models", "hand_landmarker.task")
        return candidate if os.path.isfile(candidate) else ""

    def close(self):
        """Release MediaPipe's native resources before Qt/pytest teardown.

        ``HandLandmarker.__del__`` can block while pytest is collecting unraisable
        exceptions, so callers close it explicitly instead of leaving it to GC.
        """
        hands, self._hands = self._hands, None
        self._hands_key = None
        if hands is not None and hasattr(hands, "close"):
            try:
                hands.close()
            except Exception as exc:
                logging.getLogger(__name__).warning("Hand Tracker close failed: %s", exc)
            _RETIRED_HAND_BACKENDS.append(hands)

    def landmark_status(self):
        """Human-readable backend state; errors are never hidden behind a fallback."""
        if self.landmark_backend() is not None:
            return "MediaPipe hand landmarks ready"
        return self._landmark_error or "MediaPipe hand landmarks unavailable"

    def landmark_backend(self):
        """The MediaPipe Tasks hand landmarker, or ``None`` with a recorded reason.

        MediaPipe 1.x exposes the Tasks API (``HandLandmarker``) rather than the
        legacy ``mp.solutions.hands`` API.  It also ships no model, so a missing
        model is an explicit unavailable state: this effect never substitutes
        skin-colour regions for landmark detections.
        """
        model = self.model_path()
        key = (model, max(1, int(self.max_hands.get())),
               max(0.05, min(0.99, int(self.hand_confidence.get()) / 100.0)))
        if self._hands is not None and (self._hands_key == key or self._hands_key is None):
            return self._hands
        if self._hands is not None and hasattr(self._hands, "close"):
            try:
                self._hands.close()
            except Exception:
                pass
            self._hands = None
        if not model:
            self._landmark_error = (
                "MediaPipe hand model not found. Run 'python tools/fetch_hand_model.py' "
                "or set MEENIFX_HAND_MODEL to hand_landmarker.task.")
            return None
        try:
            import mediapipe as mp
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except Exception as exc:  # ImportError on runtimes without the backend
            self._landmark_error = f"MediaPipe is not installed: {exc}"
            return None
        try:
            options = vision.HandLandmarkerOptions(
                base_options=mp_python.BaseOptions(model_asset_path=model),
                running_mode=vision.RunningMode.IMAGE,
                num_hands=key[1],
                min_hand_detection_confidence=key[2],
                min_hand_presence_confidence=key[2],
                min_tracking_confidence=key[2])
            self._hands = vision.HandLandmarker.create_from_options(options)
            self._hands_key = key
            self._landmark_error = ""
        except Exception as exc:
            self._landmark_error = f"MediaPipe hand model failed to load: {exc}"
            return None
        return self._hands

    def _detect_landmarks(self, output):
        backend = self.landmark_backend()
        if backend is None:
            return None
        work, factor = self._detection_scale(output, 480, 720)
        rgb = cv2.cvtColor(work, cv2.COLOR_BGR2RGB)
        if hasattr(backend, "detect"):
            import mediapipe as mp
            results = backend.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
            hands = getattr(results, "hand_landmarks", None) or []
            handedness = getattr(results, "handedness", None) or []
        else:  # test doubles and older callers
            results = backend.process(rgb)
            hands = getattr(results, "multi_hand_landmarks", None) or []
            handedness = getattr(results, "multi_handedness", None) or []
        detections = []
        for index, landmarks in enumerate(hands[:max(1, int(self.max_hands.get()))]):
            points = [(float(point.x) * work.shape[1] / factor,
                       float(point.y) * work.shape[0] / factor)
                      for point in getattr(landmarks, "landmark", landmarks)]
            if len(points) < 21:
                continue
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            side, score = "", 1.0
            if index < len(handedness):
                classes = getattr(handedness[index], "classification", None) or handedness[index]
                if classes:
                    category = classes[0]
                    side = str(getattr(category, "label", "")
                               or getattr(category, "category_name", "") or "")
                    score = float(getattr(category, "score", 1.0) or 1.0)
            contour = cv2.convexHull(np.round(np.asarray(points, np.float32)).astype(np.int32))
            detections.append(Detection(
                box=(min(xs), min(ys), max(2.0, max(xs) - min(xs)),
                     max(2.0, max(ys) - min(ys))),
                contour=contour, points=points, confidence=score,
                extra={"side": side or ("Left" if index == 0 else "Right")}))
        return detections

    def apply(self, frame):
        output = frame.copy()
        signature = (str(self.tracking_quality.get()), int(self.min_area.get()),
                     int(self.max_area.get()), int(self.max_hands.get()))
        if self.landmark_backend() is None:
            if self._landmark_error != self._reported_error:
                self._reported_error = self._landmark_error
                logging.getLogger(__name__).warning("Hand Tracker: %s", self._landmark_error)
            return output
        detections = self._cached_detections(
            output, signature + ("landmarks", int(self.hand_confidence.get())),
            lambda: self._detect_landmarks(output))
        if not detections:
            return output
        output = self.apply_tracked_regions(output, detections)
        return self.draw_tracking(output, detections, label="Hand")


class BlobTrackerEffect(TrackerColorMixin, EffectBase):
    category = "Tracking"
    key = "blob_tracker"
    name = "Blob Tracker"
    TRACKING_LABEL = "Blob"

    PARAMS = (
        ParamSpec("max_blobs", "Max blobs", "int", 1, 80, 12, group="Detection"),
        ParamSpec("min_area", "Smallest blob", "int", 1, 20000, 80, unit=" px²", area=True, advanced=True,
                  help="Ignore blobs with a smaller area than this (project pixels)",
                  group="Detection"),
        ParamSpec("max_area", "Largest blob", "int", 200, 80000, 18000, unit=" px²", area=True, advanced=True,
                  help="Ignore blobs with a larger area than this (project pixels)",
                  group="Detection"),
        ParamSpec("threshold", "Brightness cutoff", "int", 0, 255, 165, advanced=True,
                  help="Brightness cut-off for a pixel to belong to a blob", group="Detection"),
        ParamSpec("invert", "Invert mask", "bool", default=False, group="Detection"),
        ParamSpec("thickness", "Line thickness", "int", 1, 12, 2, unit=" px", pixels=True,
                  group="Visualization"),
    ) + TrackerColorMixin.COLOR_PARAMS + BLOB_VIS_PARAMS

    def __init__(self, app):
        super().__init__(app)
        self.max_blobs = Value(12)
        self.min_area = Value(80)
        self.max_area = Value(18000)
        self.threshold = Value(165)
        self.invert = Value(False)
        self.thickness = Value(2)
        self._init_color_controls()
        # Old projects that stored draw_mode still open as circles; new projects start
        # with the shared box, which is the least surprising default for a tracker.
        self.show_circle = Value(False)

    def _detect(self, output):
        work, factor = self._detection_scale(output, 480, 720)
        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        thresh_mode = cv2.THRESH_BINARY_INV if self.invert.get() else cv2.THRESH_BINARY
        _, mask = cv2.threshold(gray, int(self.threshold.get()), 255, thresh_mode)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        max_blobs = max(1, int(self.max_blobs.get()))
        min_area = max(1, self.area(self.min_area.get())) * factor * factor
        max_area = max(min_area, self.area(self.max_area.get()) * factor * factor)
        detections = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area or area > max_area:
                continue
            scaled = (contour.astype(np.float32) / factor).round().astype(np.int32)
            detections.append(Detection(box=cv2.boundingRect(scaled), contour=scaled,
                                        track_id=len(detections) + 1))
            if len(detections) >= max_blobs:
                break
        return detections

    def apply(self, frame):
        output = frame.copy()
        signature = (str(self.tracking_quality.get()), int(self.threshold.get()),
                     bool(self.invert.get()), int(self.min_area.get()),
                     int(self.max_area.get()), int(self.max_blobs.get()))
        detections = self._cached_detections(output, signature, lambda: self._detect(output))
        if not detections:
            return output
        output = self.apply_tracked_regions(output, detections)
        return self.draw_tracking(output, detections, label="Blob")
