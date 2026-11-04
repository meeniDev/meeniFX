"""Waveform peak computation and a disk/memory cache. Pure numpy, no Qt.

Peaks are stored as (min, max) pairs per bucket of mono samples. Several levels are
precomputed (each 4x coarser than the last) so the timeline can pick a level where one
bucket is roughly one pixel, whatever the zoom.
"""

import hashlib
import os
import threading

import numpy as np

BASE_BLOCK = 256
LEVEL_STEP = 4
LEVEL_COUNT = 5


def _mono(samples):
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data


def compute_peaks(samples, block=BASE_BLOCK, levels=LEVEL_COUNT):
    """Return [level0..levelN] arrays of shape (buckets, 2) holding (min, max)."""
    mono = _mono(samples)
    if mono.size == 0:
        return []
    padded = mono
    remainder = padded.size % block
    if remainder:
        padded = np.concatenate([padded, np.zeros(block - remainder, dtype=np.float32)])
    frames = padded.reshape(-1, block)
    result = [np.stack([frames.min(axis=1), frames.max(axis=1)], axis=1).astype(np.float32)]
    for _ in range(max(0, levels - 1)):
        previous = result[-1]
        if previous.shape[0] <= 1:
            break
        count = previous.shape[0] // LEVEL_STEP
        if count < 1:
            break
        trimmed = previous[:count * LEVEL_STEP]
        chunks = trimmed.reshape(count, LEVEL_STEP, 2)
        result.append(np.stack([chunks[:, :, 0].min(axis=1), chunks[:, :, 1].max(axis=1)], axis=1)
                      .astype(np.float32))
    return result


def level_block(level):
    """Samples covered by one bucket at a given peak level."""
    return BASE_BLOCK * (LEVEL_STEP ** max(0, int(level)))


def envelope(levels, first_sample, samples_per_column, columns):
    """Min/max per drawn column for a run of samples, at the right level of detail.

    ``first_sample`` and ``samples_per_column`` are in source samples, so the caller
    maps timeline pixels through the clip's own timeline->source mapping. Returns two
    float32 arrays of length ``columns`` (zeros where the run falls outside the file).
    """
    columns = int(columns)
    if not levels or columns <= 0:
        return np.zeros(max(0, columns), np.float32), np.zeros(max(0, columns), np.float32)
    samples_per_column = max(1.0, float(samples_per_column))
    level_index = choose_level(levels, samples_per_column)
    blocks = np.asarray(levels[level_index], dtype=np.float32)
    if blocks.ndim != 2 or blocks.shape[0] == 0:
        return np.zeros(columns, np.float32), np.zeros(columns, np.float32)
    block = float(level_block(level_index))
    count = blocks.shape[0]
    positions = first_sample + np.arange(columns, dtype=np.float64) * samples_per_column
    starts = np.floor(positions / block).astype(np.int64)
    last = np.floor((positions + samples_per_column - 1e-6) / block).astype(np.int64)
    last = np.maximum(last, starts)
    span = int(min(16, max(1, int(np.max(last - starts)) + 1)))
    indices = starts[:, None] + np.arange(span)[None, :]
    valid = (indices <= last[:, None]) & (indices >= 0) & (indices < count)
    clipped = np.clip(indices, 0, count - 1)
    mins = np.where(valid, blocks[clipped, 0], np.inf).min(axis=1)
    maxs = np.where(valid, blocks[clipped, 1], -np.inf).max(axis=1)
    mins = np.where(np.isfinite(mins), mins, 0.0).astype(np.float32)
    maxs = np.where(np.isfinite(maxs), maxs, 0.0).astype(np.float32)
    return mins, maxs


def choose_level(levels, samples_per_pixel):
    """Coarsest level whose bucket still fills at least one pixel."""
    chosen = 0
    for index in range(len(levels)):
        if level_block(index) <= max(1.0, samples_per_pixel):
            chosen = index
        else:
            break
    return chosen


class WaveformCache:
    """Peaks keyed by path + file stamp, cached in memory and next to the project."""

    def __init__(self, cache_dir):
        self.cache_dir = str(cache_dir)
        self._memory = {}
        self._lock = threading.Lock()

    @staticmethod
    def _stamp(path):
        try:
            stat = os.stat(path)
            return f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            return ""

    def _file(self, path, stamp):
        digest = hashlib.sha1(f"{os.path.abspath(path)}|{stamp}".encode("utf-8")).hexdigest()[:20]
        return os.path.join(self.cache_dir, f"peaks-{digest}.npz")

    def get(self, path):
        key = (path, self._stamp(path))
        with self._lock:
            cached = self._memory.get(key)
        if cached is not None:
            return cached
        target = self._file(path, key[1])
        if not os.path.exists(target):
            return None
        try:
            with np.load(target) as data:
                levels = [data[f"level{index}"] for index in range(len(data.files))]
        except Exception:
            return None
        levels = [level for level in levels if level.size]
        if levels:
            with self._lock:
                self._memory[key] = levels
            return levels
        return None

    def put(self, path, levels):
        if not levels:
            return
        key = (path, self._stamp(path))
        with self._lock:
            self._memory[key] = levels
            while len(self._memory) > 64:
                self._memory.pop(next(iter(self._memory)))
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            np.savez_compressed(self._file(path, key[1]),
                                **{f"level{index}": level for index, level in enumerate(levels)})
        except Exception:
            pass

    def has(self, path):
        return self.get(path) is not None

    def forget(self, path):
        """Drop the cached levels for a source's current file stamp."""
        key = (path, self._stamp(path))
        with self._lock:
            self._memory.pop(key, None)
        try:
            os.remove(self._file(path, key[1]))
        except OSError:
            pass
