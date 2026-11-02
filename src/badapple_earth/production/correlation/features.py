"""Signed, spatially aligned correlation features; display pixels stay untouched."""

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

VERSION = "multiscale-zncc-gc-axis-centered-gauss1-r3-v1"


def unit(array):
    flat = np.asarray(array, dtype=np.float32).reshape(len(array), -1)
    flat = flat - flat.mean(axis=1, keepdims=True)
    norm = np.sqrt(np.sum(flat*flat, axis=1, keepdims=True))
    return np.divide(flat, norm, out=np.zeros_like(flat), where=norm > 1e-6)


def representation(images):
    """Return ZNCC and average X/Y derivative correlations at one resolution.

    Each derivative axis is centered and normalized separately, as in GC.
    An absent axis contributes zero; its direction is never made unsigned.
    """
    raw = np.asarray(images)
    values = raw.astype(np.float32)
    if raw.dtype == np.uint8:
        values /= 255
    dx = gaussian_filter(values, sigma=(0, 1, 1), order=(0, 0, 1), radius=(0, 3, 3), mode="reflect")
    dy = gaussian_filter(values, sigma=(0, 1, 1), order=(0, 1, 0), radius=(0, 3, 3), mode="reflect")
    return unit(values), np.concatenate((unit(dx), unit(dy)), axis=1) / np.sqrt(np.float32(2))


def multiscale(images):
    parts = [representation(scale) for scale in images]
    weight = np.sqrt(np.float32(len(parts)))
    return tuple(np.ascontiguousarray(np.concatenate([p[i] for p in parts], axis=1) / weight)
                 for i in (0, 1))


def fine(gray):
    image = Image.fromarray(np.asarray(gray, dtype=np.float32))
    scales = [np.asarray(image.resize((w, round(w*image.height/image.width)), Image.Resampling.BOX))[None]
              for w in (64, 128)]
    return tuple(part[0] for part in multiscale(scales))
