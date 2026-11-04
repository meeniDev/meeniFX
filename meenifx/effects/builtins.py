"""The built-in effects: mosaic, ASCII art and the color trackers.

Pixel-based parameters (cell sizes, line thickness, minimum face size, blob areas,
blur radius) are expressed in logical-resolution pixels. When the preview renders at
a lower quality the engine sets `param_scale`, and every such parameter is multiplied
by it, so a 35% preview is the same picture as the export, only softer.
"""

import os

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from ..config import ASSETS_DIR
from ..constants import DEFAULT_UNICODE_CHARSET
from ..model.values import Value
from ..processing.mosaic import frame_pixel, mosaic_frame
from .base import EffectBase, ParamSpec, TrackerColorMixin

FACE_CASCADE_FILE = "haarcascade_frontalface_default.xml"
FACE_MODEL_FILE = "face_detection_yunet_2023mar.onnx"


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

    PARAMS = (
        ParamSpec("confidence", "Confidence", "int", 1, 99, 50, unit="%",
                  help="How sure the detector must be before a face is accepted"),
        ParamSpec("min_size", "Smallest face", "int", 12, 240, 30, unit=" px", pixels=True,
                  help="Smallest face to accept, in project pixels"),
        ParamSpec("thickness", "Line thickness", "int", 1, 8, 2, unit=" px", pixels=True),
        ParamSpec("blur_faces", "Blur faces", "bool", default=False),
        ParamSpec("label_faces", "Labels", "bool", default=True),
    ) + TrackerColorMixin.COLOR_PARAMS

    def __init__(self, app):
        super().__init__(app)
        self.confidence = Value(50)
        self.min_size = Value(30)
        self.thickness = Value(2)
        self.blur_faces = Value(False)
        self.label_faces = Value(True)
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
        """[(x, y, w, h)] of detected faces in the frame's own pixels."""
        if self._backend == "none":
            self.on_registered()
        if self._backend == "yunet":
            height, width = frame.shape[:2]
            key = (width, height, int(self.confidence.get()))
            if self._detector is None or key != self._detector_key:
                score = max(0.05, min(0.99, int(self.confidence.get()) / 100.0))
                self._detector = cv2.FaceDetectorYN.create(str(face_model_path()), "",
                                                           (width, height), score, 0.3, 50)
                self._detector_key = key
            _ok, faces = self._detector.detect(frame)
            result = []
            if faces is not None:
                for face in faces:
                    x, y, w, h = [int(round(float(value))) for value in face[:4]]
                    result.append((x, y, w, h))
            return result
        if self._backend == "haar":
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            min_size = max(8, self.px(self.min_size.get()))
            # The old min-neighbour knob is now a confidence percentage.
            neighbors = max(1, int(round(1 + int(self.confidence.get()) / 100.0 * 11)))
            boxes = self._cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=neighbors,
                                                   minSize=(min_size, min_size))
            return [tuple(int(v) for v in box) for box in boxes]
        return []

    def apply(self, frame):
        if self._backend == "none":
            if not self.available():
                return frame

        output = frame.copy()
        min_size = max(4, self.px(self.min_size.get()))
        thickness = max(1, self.px(self.thickness.get()))
        faces = [box for box in self.detect_faces(output)
                 if box[2] >= min_size and box[3] >= min_size]

        for idx, (x, y, w, h) in enumerate(faces):
            color = self._resolve_draw_color(idx)
            if self.blur_faces.get():
                roi = output[y: y + h, x: x + w]
                if roi.size:
                    k = max(3, (min(w, h) // 6) | 1)
                    output[y: y + h, x: x + w] = cv2.GaussianBlur(roi, (k, k), 0)

            cv2.rectangle(output, (x, y), (x + w, y + h), color, thickness)
            if self.label_faces.get():
                cv2.putText(output, f"Face {idx + 1}", (x, max(14, y - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.55 * max(0.4, self.param_scale), color,
                            max(1, thickness // 2), cv2.LINE_AA)

        return output


class HandTrackerEffect(TrackerColorMixin, EffectBase):
    category = "Tracking"
    key = "hand_tracker"
    name = "Hand Tracker"

    PARAMS = (
        ParamSpec("max_hands", "Max hands", "int", 1, 6, 2),
        ParamSpec("min_area", "Smallest hand", "int", 200, 40000, 2500, unit=" px²", area=True, advanced=True,
                  help="Ignore skin-coloured regions smaller than this (area in project pixels)"),
        ParamSpec("thickness", "Line thickness", "int", 1, 12, 2, unit=" px", pixels=True),
        ParamSpec("draw_hull", "Draw hull", "bool", default=True),
        ParamSpec("draw_box", "Draw box", "bool", default=True),
    ) + TrackerColorMixin.COLOR_PARAMS

    def __init__(self, app):
        super().__init__(app)
        self.max_hands = Value(2)
        self.min_area = Value(2500)
        self.thickness = Value(2)
        self.draw_hull = Value(True)
        self.draw_box = Value(True)
        self._init_color_controls()

    def apply(self, frame):
        output = frame.copy()
        ycrcb = cv2.cvtColor(output, cv2.COLOR_BGR2YCrCb)
        mask = cv2.inRange(ycrcb, (0, 133, 77), (255, 173, 127))
        kernel = np.ones((5, 5), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        kept = 0
        min_area = max(1, self.area(self.min_area.get()))
        thickness = max(1, self.px(self.thickness.get()))
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area:
                continue
            color = self._resolve_draw_color(kept)

            if self.draw_hull.get():
                hull = cv2.convexHull(contour)
                cv2.drawContours(output, [hull], -1, color, thickness)
            if self.draw_box.get():
                x, y, w, h = cv2.boundingRect(contour)
                cv2.rectangle(output, (x, y), (x + w, y + h), color, thickness)
                cv2.putText(output, f"Hand {kept + 1}", (x, max(14, y - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                            0.55 * max(0.4, self.param_scale), color, max(1, thickness // 2), cv2.LINE_AA)

            kept += 1
            if kept >= max(1, int(self.max_hands.get())):
                break

        return output


class BlobTrackerEffect(TrackerColorMixin, EffectBase):
    category = "Tracking"
    key = "blob_tracker"
    name = "Blob Tracker"

    PARAMS = (
        ParamSpec("max_blobs", "Max blobs", "int", 1, 80, 12),
        ParamSpec("min_area", "Smallest blob", "int", 1, 20000, 80, unit=" px²", area=True, advanced=True,
                  help="Ignore blobs with a smaller area than this (project pixels)"),
        ParamSpec("max_area", "Largest blob", "int", 200, 80000, 18000, unit=" px²", area=True, advanced=True,
                  help="Ignore blobs with a larger area than this (project pixels)"),
        ParamSpec("threshold", "Brightness cutoff", "int", 0, 255, 165, advanced=True,
                  help="Brightness cut-off for a pixel to belong to a blob"),
        ParamSpec("invert", "Invert mask", "bool", default=False),
        ParamSpec("draw_mode", "Shape", "choice", choices=("circle", "box", "contour"),
                  default="circle"),
        ParamSpec("thickness", "Line thickness", "int", 1, 12, 2, unit=" px", pixels=True),
    ) + TrackerColorMixin.COLOR_PARAMS

    def __init__(self, app):
        super().__init__(app)
        self.max_blobs = Value(12)
        self.min_area = Value(80)
        self.max_area = Value(18000)
        self.threshold = Value(165)
        self.invert = Value(False)
        self.draw_mode = Value("circle")
        self.thickness = Value(2)
        self._init_color_controls()

    def apply(self, frame):
        output = frame.copy()
        gray = cv2.cvtColor(output, cv2.COLOR_BGR2GRAY)
        thresh_mode = cv2.THRESH_BINARY_INV if self.invert.get() else cv2.THRESH_BINARY
        _, mask = cv2.threshold(gray, int(self.threshold.get()), 255, thresh_mode)

        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contours = sorted(contours, key=cv2.contourArea, reverse=True)

        max_blobs = max(1, int(self.max_blobs.get()))
        min_area = max(1, self.area(self.min_area.get()))
        max_area = max(min_area, self.area(self.max_area.get()))
        thickness = max(1, self.px(self.thickness.get()))
        kept = 0

        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area or area > max_area:
                continue

            color = self._resolve_draw_color(kept)
            draw_mode = str(self.draw_mode.get()).strip().lower()

            if draw_mode == "box":
                x, y, w, h = cv2.boundingRect(contour)
                cv2.rectangle(output, (x, y), (x + w, y + h), color, thickness)
            elif draw_mode == "contour":
                cv2.drawContours(output, [contour], -1, color, thickness)
            else:
                (cx, cy), radius = cv2.minEnclosingCircle(contour)
                cv2.circle(output, (int(cx), int(cy)), max(1, int(radius)), color, thickness)

            kept += 1
            if kept >= max_blobs:
                break

        return output
