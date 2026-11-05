"""LUT (.cube) colour grading with an intensity slider.

A ``.cube`` file is a 3-D lookup table: the picture's RGB is interpolated through the
cube and the result is mixed with the original by ``intensity``. Parsed tables are
cached by path and modification time, so dragging the intensity slider never re-reads
the file. With no file loaded the effect uses one of a few built-in, procedurally
generated tables, which is what makes it useful (and testable) out of the box.
"""

from __future__ import annotations

import os

import numpy as np

from ..model.values import Value
from .base import LayerEffect, ParamSpec, param_number

BUILTIN_LUTS = (("teal_orange", "Teal & Orange"), ("warm_film", "Warm Film"),
                ("black_white", "Black & White"))


def _luma(rgb):
    return np.clip(rgb @ np.array([0.0722, 0.7152, 0.2126], np.float32) / 255.0, 0.0, 1.0)


def builtin_lut(name, size=33):
    """A generated (size, size, size, 3) float32 table in 0..255, indexed [r][g][b]."""
    coords = np.linspace(0.0, 1.0, size, dtype=np.float32)
    r, g, b = np.meshgrid(coords, coords, coords, indexing="ij")
    rgb = np.stack([r, g, b], axis=-1)                      # R, G, B in 0..1
    luma = rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    if name == "black_white":
        out = np.repeat(luma[..., None], 3, axis=-1)
    elif name == "warm_film":
        lifted = 0.045 + rgb * 0.94
        out = lifted * np.array([1.10, 0.99, 0.85], np.float32) + \
            np.array([0.02, 0.01, -0.01], np.float32)
        out = luma[..., None] * 0.25 + out * 0.75
    else:   # teal & orange: cool shadows, warm highlights
        shadow = (1.0 - luma) ** 1.6
        high = luma ** 1.6
        out = rgb + shadow[..., None] * np.array([0.02, 0.075, 0.11], np.float32) \
            - high[..., None] * np.array([0.13, 0.03, -0.02], np.float32)
        out = (out - 0.5) * 1.12 + 0.5
    # Table is indexed [r][g][b] but stored BGR (the pipeline's channel order).
    out = np.clip(out, 0.0, 1.0)[..., ::-1] * 255.0
    return out.astype(np.float32)


def read_cube(path):
    """Parse a .cube file into (size, table) with the table indexed [r][g][b] and BGR values."""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.readlines()
    size = 0
    domain_min = np.array([0.0, 0.0, 0.0], np.float32)
    domain_max = np.array([1.0, 1.0, 1.0], np.float32)
    values = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith("LUT_3D_SIZE"):
            size = int(line.split()[-1])
        elif upper.startswith("LUT_1D_SIZE"):
            raise ValueError("1-D .cube LUTs are not supported; use a 3-D table")
        elif upper.startswith("DOMAIN_MIN"):
            domain_min = np.array([float(v) for v in line.split()[1:4]], np.float32)
        elif upper.startswith("DOMAIN_MAX"):
            domain_max = np.array([float(v) for v in line.split()[1:4]], np.float32)
        elif upper.startswith(("TITLE", "LUT_3D_INPUT_RANGE")):
            continue
        elif line[0].isdigit() or line[0] in "+-.":
            parts = [float(v) for v in line.split()]
            if len(parts) >= 3:
                values.append(parts[:3])
    if size < 2 or len(values) < size ** 3:
        raise ValueError(f"{os.path.basename(path)} does not contain a complete 3-D LUT")
    data = np.array(values[:size ** 3], np.float32).reshape(size, size, size, 3)
    # .cube rows run with red fastest, so the parsed array is [b][g][r].
    data = np.transpose(data, (2, 1, 0, 3))
    span = max(1e-6, float((domain_max - domain_min).max()))
    data = (data - domain_min[::-1][None, None, None, :]) / span
    if float(data.max()) > 1.5:
        data = data / 255.0
    return size, np.clip(data[..., ::-1], 0.0, 1.0).astype(np.float32) * 255.0


class LutEffect(LayerEffect):
    """Grade through a .cube table (or a built-in one) with a mixable intensity."""

    key = "lut"
    name = "LUT"
    category = "Color Correction"
    _cache = {}

    PARAMS = (
        ParamSpec("lut_file", "LUT file", "text", default="",
                  help="Path to a .cube table; empty uses the built-in preset below"),
        ParamSpec("intensity", "Intensity", "float", 0.0, 100.0, 100.0, step=1.0, unit="%"),
        ParamSpec("preset", "Built-in", "choice", choices=tuple(key for key, _l in BUILTIN_LUTS),
                  choice_labels=tuple(label for _k, label in BUILTIN_LUTS), default="teal_orange",
                  help="Used when no .cube file is loaded"),
        ParamSpec("preserve_luma", "Keep luminance", "bool", default=False, advanced=True,
                  help="Blend the graded colour back toward the original brightness"),
    )

    def __init__(self, app):
        super().__init__(app)
        self.lut_file = Value("")
        self.intensity = Value(100.0)
        self.preset = Value("teal_orange")
        self.preserve_luma = Value(False)
        self.load_error = ""

    def table(self):
        """(size, table) for the selected file or built-in preset."""
        path = str(self.lut_file.get() or "").strip()
        if path:
            try:
                stamp = os.path.getmtime(path)
                key = (path, stamp)
                cached = self._cache.get(key)
                if cached is None:
                    cached = read_cube(path)
                    if len(self._cache) > 8:
                        self._cache.clear()
                    self._cache[key] = cached
                self.load_error = ""
                return cached
            except (OSError, ValueError) as error:
                self.load_error = str(error)
        key = ("builtin", str(self.preset.get()))
        if key not in self._cache:
            self._cache[key] = (33, builtin_lut(str(self.preset.get())))
        return self._cache[key]

    # The fast path: a 7-bit per-channel index into a precomputed table. Quantising the
    # *lookup coordinate* (not the picture) costs one gather per frame instead of eight
    # trilinear gathers, and the value depends only on the pixel, so every render scale
    # produces exactly the same colour.
    INDEX_BITS = 7
    _fast_cache = {}

    @staticmethod
    def _trilinear(flat_rgb, size, table):
        """Trilinear interpolation; `flat_rgb` is (N, 3) in R, G, B order, table [r][g][b] BGR."""
        position = np.clip(flat_rgb, 0.0, 1.0) * (size - 1)
        lower = np.floor(position).astype(np.int32)
        upper = np.minimum(lower + 1, size - 1)
        frac = position - lower
        out = np.zeros((position.shape[0], 3), np.float32)
        for dr in (0, 1):
            ir = upper[:, 0] if dr else lower[:, 0]
            wr = frac[:, 0] if dr else 1.0 - frac[:, 0]
            for dg in (0, 1):
                ig = upper[:, 1] if dg else lower[:, 1]
                wg = frac[:, 1] if dg else 1.0 - frac[:, 1]
                for db in (0, 1):
                    ib = upper[:, 2] if db else lower[:, 2]
                    wb = frac[:, 2] if db else 1.0 - frac[:, 2]
                    out += table[ir, ig, ib] * (wr * wg * wb)[:, None]
        return np.clip(out, 0.0, 255.0).astype(np.uint8)

    @classmethod
    def apply_table(cls, rgb, size, table):
        """Trilinear interpolation of an (H, W, 3) uint8 BGR picture through the table."""
        flat = rgb[:, :, ::-1].reshape(-1, 3).astype(np.float32) / 255.0
        return cls._trilinear(flat, size, table).reshape(rgb.shape)

    @classmethod
    def indexed_table(cls, size, table):
        """The precomputed 2^7 cubed table used by :meth:`apply_layer`."""
        key = (id(table), size)
        cached = cls._fast_cache.get(key)
        if cached is not None:
            return cached
        levels = 1 << cls.INDEX_BITS
        ramp = np.linspace(0.0, 1.0, levels, dtype=np.float32)
        rr, gg, bb = np.meshgrid(ramp, ramp, ramp, indexing="ij")
        flat = np.stack([rr.ravel(), gg.ravel(), bb.ravel()], axis=1)
        values = cls._trilinear(flat, size, table).reshape(levels, levels, levels, 3)
        if len(cls._fast_cache) > 4:
            cls._fast_cache.clear()
        cls._fast_cache[key] = values
        return values

    @classmethod
    def lookup(cls, rgb, indexed):
        """One gather through the 7-bit table: the same colour at every render scale."""
        shift = 8 - cls.INDEX_BITS
        red = rgb[:, :, 2].astype(np.int32) >> shift
        green = rgb[:, :, 1].astype(np.int32) >> shift
        blue = rgb[:, :, 0].astype(np.int32) >> shift
        index = (red << (2 * cls.INDEX_BITS)) | (green << cls.INDEX_BITS) | blue
        flat = indexed.reshape(-1, 3)
        return flat[index.reshape(-1)].reshape(rgb.shape)

    def apply_layer(self, layer):
        frame = int(self.local_frame)
        intensity = param_number(self.intensity, frame) / 100.0
        if intensity <= 0.001:
            return layer
        size, table = self.table()
        rgb = layer[:, :, :3]
        graded = self.lookup(rgb, self.indexed_table(size, table)).astype(np.float32)
        if self.preserve_luma.get():
            grey = _luma(rgb)[:, :, None] * 255.0
            graded_luma = _luma(graded.astype(np.uint8))[:, :, None] * 255.0
            graded = graded + (grey - graded_luma)
        blended = rgb.astype(np.float32) * (1.0 - intensity) + graded * intensity
        out = np.empty_like(layer)
        out[:, :, :3] = np.clip(blended, 0.0, 255.0).astype(np.uint8)
        out[:, :, 3] = layer[:, :, 3]
        return out


LUT_EFFECTS = (LutEffect,)
