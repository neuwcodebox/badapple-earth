"""Evaluate the union at 64/128 pixels and at full canonical MS-SSIM."""

from functools import lru_cache

import numpy as np
from PIL import Image

from ...matching.photos import read_rgb_rows
from ...matching.views import rotate_image
from ..rerank import pyramid, similarity
from .features import fine


@lru_cache(maxsize=384)
def target_features(path):
    with Image.open(path) as image:
        gray = np.asarray(image.convert("L"), dtype=np.float32) / 255
    white = gray > .5
    if white.any() and not white.all():
        contrast = white.astype(np.float32) / np.sum(white) - (~white).astype(np.float32) / np.sum(~white)
    else:
        contrast = np.zeros_like(gray)
    z, g = fine(gray)
    return gray, z, g, pyramid(gray), contrast


def score_groups(groups, config):
    results = []
    for filename, entries in groups:
        rgb = read_rgb_rows(filename, [entry[0] for entry in entries])
        for position, candidate, targets in entries:
            original = Image.fromarray(np.asarray(rgb[position]))
            image = rotate_image(original, candidate["rotation_degrees"], original.size, candidate["rotation_crop"])
            gray = np.asarray(image.convert("L"), dtype=np.float32) / 255
            z, g = fine(gray)
            ssim = pyramid(gray)
            std = float(np.std(gray))
            for target in targets:
                target_gray, tz, tg, ts, contrast = target_features(target["path"])
                zncc, gc = float(np.clip(tz @ z, -1, 1)), float(np.clip(tg @ g, -1, 1))
                structure = config["structure_weights"][0]*zncc + config["structure_weights"][1]*gc
                results.append({"slot": target["slot"], "dense_row": candidate["dense_row"],
                                "zncc": zncc, "gc": gc, "structure": structure,
                                "ms_ssim": similarity(ts, ssim), "photo_std": std,
                                "contrast": float(np.sum(gray*contrast, dtype=np.float64)),
                                "full_mse": float(np.mean((target_gray-gray)**2, dtype=np.float64))})
    return results


def visible(row, config):
    return (row["contrast"] >= config["contrast_min"] and row["photo_std"] >= config["photo_std_min"]
            and row["zncc"] > 0)


def choose(rows, metric, config, fallback_position, flat=False):
    """Structure first; brightness breaks a configured near-structure band."""
    if flat:
        return fallback_position, "flat_target_mse"
    eligible = [r for r in rows if visible(r, config)]
    if not eligible:
        return fallback_position, "no_visible_positive_contrast_ms_ssim20"
    best = max(r[metric] for r in eligible)
    band = [r for r in eligible if r[metric] >= best-config["shape_window"]]
    selected = min(band, key=lambda r: (r["full_mse"], -r[metric], r["view_id"]))
    return selected["dense_row"], "visible_structure_then_brightness"


def display_five(rows, chosen_position, metadata, config):
    """Select display places only after evaluation; include the real winner."""
    chosen = metadata[chosen_position]
    display, seen = [chosen_position], {chosen["tile_id"]}
    for row in sorted((r for r in rows if visible(r, config)),
                      key=lambda r: (-r["structure"], r["full_mse"], r["view_id"])):
        tile = metadata[row["dense_row"]]["tile_id"]
        if tile not in seen:
            seen.add(tile)
            display.append(row["dense_row"])
            if len(display) == 5:
                break
    return display
