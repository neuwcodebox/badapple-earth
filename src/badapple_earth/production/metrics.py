"""CPU comparison metrics on the same canonical luminance and fixed alignment."""

from functools import lru_cache

import numpy as np
from PIL import Image

from . import fsim
from .rerank import pyramid, similarity as ms_ssim

VERSION = "top20-fsim-reference-multimse64-32-16-zncc-v1"
METHODS = ("mse", "ms-ssim", "fsim", "multi-mse", "zncc")
LABELS = {"mse": "MSE", "ms-ssim": "MS-SSIM", "fsim": "FSIM",
          "multi-mse": "Multi-MSE", "zncc": "ZNCC"}


def features(gray):
    image = Image.fromarray(np.asarray(gray, dtype=np.float32), mode="F")
    scales = [np.asarray(image.resize((width, max(1, round(width*image.height/image.width))),
                                      Image.Resampling.BOX)) for width in (64, 32, 16)]
    centered = np.asarray(gray, dtype=np.float64) - np.mean(gray, dtype=np.float64)
    return {"gray": gray, "scales": scales, "centered": centered,
            "norm": float(np.linalg.norm(centered.ravel())), "ssim": pyramid(gray),
            "fsim": fsim.features(gray)}


@lru_cache(maxsize=128)
def image_features(filename):
    with Image.open(filename) as image:
        return features(np.asarray(image.convert("L"), dtype=np.float32) / 255)


def scores(left, right, retrieval_mse):
    full_mse = float(np.mean((left["gray"] - right["gray"])**2, dtype=np.float64))
    multi = float(np.mean([np.mean((x-y)**2, dtype=np.float64)
                          for x, y in zip(left["scales"], right["scales"])]))
    denominator = left["norm"] * right["norm"]
    # Undefined correlations cannot win over defined ones. If the target is
    # constant every candidate ties at -1 and the original MSE order wins.
    correlation = float(np.clip(np.sum(left["centered"] * right["centered"])
                                / denominator, -1, 1)) if denominator > 1e-10 else -1.0
    return {"mse": retrieval_mse, "ms-ssim": ms_ssim(left["ssim"], right["ssim"]),
            "fsim": fsim.similarity(left["fsim"], right["fsim"], full_mse),
            "multi-mse": multi, "zncc": correlation}


def winner(method, values, candidates):
    sign = 1 if method in {"mse", "multi-mse"} else -1
    return min(range(len(values)), key=lambda i: (sign*values[i], candidates[i]["rank"],
                                                candidates[i]["view_id"]))
