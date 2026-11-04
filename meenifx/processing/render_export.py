"""Toolkit-independent export helper facade; UI progress and errors belong to the caller."""

import os

import cv2
import numpy as np


def normalize_even_size(width, height):
    width, height = max(2, int(width)), max(2, int(height))
    return width + width % 2, height + height % 2


def force_extension(path, extension):
    return os.path.splitext(path)[0] + "." + extension.lower().lstrip(".")


def save_frame(frame, path):
    if frame is None:
        raise RuntimeError("No source frame available to save.")
    if not cv2.imwrite(path, frame):
        raise OSError(f"Could not save frame to {path}")
    return path


def black_frame(width, height):
    return np.zeros((max(1, int(height)), max(1, int(width)), 3), dtype=np.uint8)
