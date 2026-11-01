"""Versioned PHOG and relative-luma layout descriptors."""

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, sobel
from scipy.stats import rankdata

VERSION = "shape-phog-rank-v2.1"
SIGMAS = (1.5, 3.0)
LEVELS = ((1, .10), (2, .20), (4, .30), (8, .40))


def resize_float(gray, size):
    return np.asarray(Image.fromarray(np.asarray(gray, np.float32)).resize(size, Image.Resampling.BOX)).copy()


def structures(gray, width=128):
    height = round(width * gray.shape[0] / gray.shape[1])
    small = resize_float(np.asarray(gray, np.float32) / 255, (width, height))
    return [gaussian_filter(small, sigma=s * width / 128, mode="reflect", output=np.float32) for s in SIGMAS]


def gradient(image):
    return (sobel(image, axis=1, mode="reflect") / 8,
            sobel(image, axis=0, mode="reflect") / 8)


def descriptors(gray):
    scales = structures(gray)
    parts = []
    for image in scales:
        gx, gy = gradient(image)
        magnitude = np.hypot(gx, gy).ravel()
        angle = np.mod(np.arctan2(gy, gx), np.pi).ravel() * (8 / np.pi)
        lower = np.floor(angle).astype(np.int32)
        fraction = angle - lower
        yy, xx = np.indices(image.shape)
        for cells, weight in LEVELS:
            cell = ((yy * cells // image.shape[0]) * cells + xx * cells // image.shape[1]).ravel()
            first = cell * 8 + lower % 8
            second = cell * 8 + (lower + 1) % 8
            hist = (np.bincount(first, magnitude * (1 - fraction), minlength=cells*cells*8)
                    + np.bincount(second, magnitude * fraction, minlength=cells*cells*8)).astype(np.float32)
            total = hist.sum()
            if total > 0:
                hist /= total
            parts.append(np.sqrt(hist) * np.float32(np.sqrt(weight * .5)))
    phog = np.concatenate(parts)
    norm = np.linalg.norm(phog)
    if norm > 0:
        phog /= norm
    grid = resize_float(scales[0], (32, round(32 * gray.shape[0] / gray.shape[1]))).ravel()
    rank = ((rankdata(grid, method="average") - (len(grid) + 1) / 2) / len(grid)).astype(np.float32)
    return phog.astype(np.float32), rank, {"flat_structure": bool(norm == 0),
                                          "structure_energy": float(np.mean(np.hypot(*gradient(scales[0])))),
                                          "luma_mean": float(np.mean(gray) / 255),
                                          "luma_variance": float(np.var(np.asarray(gray, np.float32) / 255))}
