"""Frame math for the mosaic preview: tile sizing and palette rendering."""

import cv2
import numpy as np


def frame_pixel(width, height, scale):
    dim = [((width / (scale / 10)) / (scale / 2)), ((height / (scale / 10)) / (scale / 2))]
    tile_w = round(dim[0])
    tile_h = round(dim[1])
    if tile_w < 2:
        tile_w = 2
    if tile_h < 2:
        tile_h = 2
    if tile_w % 2 != 0:
        tile_w -= 1
    if tile_h % 2 != 0:
        tile_h -= 1
    return tile_w, tile_h


def build_palette(tile_images, tile_w, tile_h):
    resized = [cv2.resize(img, (tile_w, tile_h)) for img in tile_images]
    palette = [np.mean(img, axis=(0, 1)) for img in resized]
    return np.array(resized, dtype=np.uint8), np.array(palette, dtype=np.float32)


def mosaic_frame(frame, small_tiles, small_palette, tile_w, tile_h):
    frame_h, frame_w = frame.shape[:2]
    tiles_height = max(1, (frame_h + tile_h - 1) // tile_h)
    tiles_width = max(1, (frame_w + tile_w - 1) // tile_w)

    avg_grid = cv2.resize(frame, (tiles_width, tiles_height), interpolation=cv2.INTER_AREA).astype(np.float32)
    dist_sq = np.sum((avg_grid[:, :, None, :] - small_palette[None, None, :, :]) ** 2, axis=3)
    tile_idx = np.argmin(dist_sq, axis=2)
    tile_grid = small_tiles[tile_idx]
    mosaic = tile_grid.transpose(0, 2, 1, 3, 4).reshape(tiles_height * tile_h, tiles_width * tile_w, 3)
    output = mosaic[:frame_h, :frame_w]
    return output
