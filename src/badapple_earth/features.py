"""Shared target/candidate baseline on displayed RGB, without contrast changes."""

import numpy as np
from PIL import Image

FEATURE_VERSION = "rgb-pillow-l601-box-v1"
EXTRACTION_VERSION = "native-rgb-fixed-box-v2"


def image_size(width: int, aspect: list[int]) -> tuple[int, int]:
    return width, max(1, round(width * aspect[1] / aspect[0]))


def canonical_image(rgb: np.ndarray, size: tuple[int, int]) -> Image.Image:
    if rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[0] != 3:
        raise ValueError("Expected native uint8 RGB with shape (3, height, width)")
    return Image.fromarray(np.moveaxis(rgb, 0, 2)).resize(size, Image.Resampling.BOX)


def extract(image: Image.Image, aspect: list[int]) -> tuple[np.ndarray, np.ndarray, str]:
    gray = image.convert("L")  # Pillow's displayed-RGB Rec.601 coefficients, no linearization.
    coarse = np.asarray(gray.resize(image_size(16, aspect), Image.Resampling.BOX)).copy()
    fine = np.asarray(gray.resize(image_size(32, aspect), Image.Resampling.BOX)).copy()
    small = np.asarray(gray.resize((9, 8), Image.Resampling.BILINEAR))
    bits = np.packbits((small[:, 1:] > small[:, :-1]).reshape(-1))
    return coarse, fine, bits.tobytes().hex()
