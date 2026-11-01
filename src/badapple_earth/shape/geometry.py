"""Filled-region, signed-distance and exact joint directional edge costs."""

import numpy as np
from scipy.ndimage import distance_transform_edt, gaussian_filter, maximum_filter, minimum_filter
from scipy.spatial import cKDTree

from .features import descriptors, gradient, resize_float, structures


def boundary(mask):
    """Both in-frame sides of four-neighbour interfaces; no outside padding."""
    edge = np.zeros(mask.shape, bool)
    change = mask[1:] != mask[:-1]
    edge[1:] |= change; edge[:-1] |= change
    change = mask[:, 1:] != mask[:, :-1]
    edge[:, 1:] |= change; edge[:, :-1] |= change
    return edge


def edges(mask):
    points = np.argwhere(boundary(mask)).astype(np.float64)
    gx, gy = gradient(gaussian_filter(mask.astype(np.float32), .75, mode="reflect"))
    theta = np.mod(np.arctan2(gy, gx), np.pi)[tuple(points.astype(int).T)] if len(points) else np.empty(0)
    return {"points": points, "theta": theta, "sorted_theta": np.sort(theta),
            "tree": cKDTree(points) if len(points) else None}


def angular_nearest(theta, sorted_other):
    positions = np.searchsorted(sorted_other, theta)
    left = np.abs(theta - sorted_other[(positions - 1) % len(sorted_other)])
    right = np.abs(theta - sorted_other[positions % len(sorted_other)])
    return np.minimum(np.minimum(left, np.pi - left), np.minimum(right, np.pi - right)) / (np.pi / 2)


def chamfer(first, second, radius):
    """Exact capped-space + unsigned-angle joint minimum, in both directions.

    Outside radius all spatial costs equal .8, so only the globally closest
    angle matters. Enumerating pairs inside radius and this angular baseline
    gives the exact joint minimum without allocating an all-pairs matrix.
    """
    if not len(first["points"]) or not len(second["points"]):
        return float(bool(len(first["points"]) or len(second["points"])))
    forward = .8 + .2 * angular_nearest(first["theta"], second["sorted_theta"])
    backward = .8 + .2 * angular_nearest(second["theta"], first["sorted_theta"])
    nearby = first["tree"].sparse_distance_matrix(second["tree"], radius, output_type="coo_matrix")
    angle = np.abs(first["theta"][nearby.row] - second["theta"][nearby.col])
    angle = np.minimum(angle, np.pi - angle) / (np.pi / 2)
    cost = .8 * np.minimum(nearby.data.astype(np.float32) / radius, 1) + .2 * angle
    np.minimum.at(forward, nearby.row, cost)
    np.minimum.at(backward, nearby.col, cost)
    return float(.5 * (np.mean(forward) + np.mean(backward)))


def sdf(mask):
    if not np.any(mask):
        return np.full(mask.shape, -1, np.float32)
    if np.all(mask):
        return np.ones(mask.shape, np.float32)
    radius = .05 * min(mask.shape)
    phi = (distance_transform_edt(mask) - distance_transform_edt(~mask)).astype(np.float32)
    return np.clip(phi, -radius, radius) / radius


def region_distance(first, second):
    intersection = int(np.count_nonzero(first & second))
    union = int(np.count_nonzero(first)) + int(np.count_nonzero(second)) - intersection
    white_intersection, white_union = first.size - union, first.size - intersection
    black = intersection / union if union else 1.
    white = white_intersection / white_union if white_union else 1.
    return 1 - .5 * (black + white)


def target(gray):
    original = np.asarray(gray, np.float32) / 255
    low, high = np.quantile(original, [.01, .99])
    fallback = high - low <= 1 / 255
    if fallback:
        low, high = float(original.min()), float(original.max())
    normalized = np.clip((original - low) / (high - low), 0, 1) if high > low else np.zeros_like(original)
    normalized = normalized.astype(np.float32)
    dark = normalized <= .5
    fraction = float(dark.mean())
    near_uniform = bool(original.max() - original.min() <= 1 / 255 or min(fraction, 1-fraction) < .001)
    band = maximum_filter(dark, size=5, mode="reflect") != minimum_filter(dark, size=5, mode="reflect")
    gray_fraction = float(np.mean((normalized > .1) & (normalized < .9) & ~band))
    levels = [.25, .5, .75] if gray_fraction > .005 else [.5]
    full_masks = [normalized <= level for level in levels]
    full_masks = [mask for mask in full_masks if 0 < mask.mean() < 1]
    result = {"gray": gray, "original": original, "normalized": normalized, "levels": levels,
              "near_uniform": near_uniform, "dark_fraction": fraction, "luma_range": float(np.ptp(original)),
              "normalization": "minmax" if fallback else "p1-p99", "gray_fraction": gray_fraction,
              "full_masks": full_masks, "geometry": {}}
    phog, _, _ = descriptors((normalized * 255).astype(np.float32))
    result["phog"] = phog
    small = structures(normalized * 255)[0]
    grid = resize_float(small, (32, round(32 * gray.shape[0] / gray.shape[1])))
    queries, query_levels, used = [], [], set()
    for level in levels:
        mask = grid <= level
        if 0 < mask.sum() < mask.size and mask.tobytes() not in used:
            queries.append(-mask.astype(np.float32).ravel())
            query_levels.append(level)
            used.add(mask.tobytes())
    result["rank_queries"] = queries
    result["rank_query_levels"] = query_levels
    for width in [128, gray.shape[1]]:
        scales = []
        for structure in structures(normalized * 255, width):
            masks, used = [], set()
            for level in levels:
                mask = structure <= level
                if mask.tobytes() in used:
                    continue
                used.add(mask.tobytes())
                # If smoothing erases a thin form, keep the unsmoothed mask.
                if not 0 < mask.mean() < 1 and not near_uniform:
                    mask = resize_float(normalized, (structure.shape[1], structure.shape[0])) <= level
                masks.append({"mask": mask, "area": float(mask.mean()), "phi": sdf(mask),
                              "edges": edges(mask) if width == gray.shape[1] else None, "level": level})
            scales.append(masks)
        result["geometry"][width] = scales
    return result


def thresholds(image, area):
    delta = min(.05, area / 2, (1 - area) / 2)
    quantiles = np.clip(area + np.arange(-2, 3) * delta, 1 / image.size, 1 - 1 / image.size)
    return np.unique(np.quantile(image, quantiles, method="linear"))


def ordered_choice(options, key):
    """Minimum total level score subject to nondecreasing luma thresholds."""
    paths = [(row[key], [row]) for row in options[0]]
    for current in options[1:]:
        next_paths = []
        for row in current:
            eligible = [p for p in paths if p[1][-1]["threshold"] <= row["threshold"]]
            if eligible:
                best = min(eligible, key=lambda p: (p[0], [v["threshold"] for v in p[1]]))
                next_paths.append((best[0] + row[key], best[1] + [row]))
        paths = next_paths
    if not paths:
        raise ValueError("No ordered threshold assignment")
    return min(paths, key=lambda p: (p[0], [v["threshold"] for v in p[1]]))[1]


def score(gray, prepared, fine=False):
    width = gray.shape[1] if fine else 128
    scale_results = []
    for image, masks in zip(structures(gray, width), prepared["geometry"][width]):
        options = []
        for expected in masks:
            choices = []
            for threshold in thresholds(image, expected["area"]):
                mask = image <= threshold
                d_region = region_distance(expected["mask"], mask)
                d_sdf = float(np.mean(np.abs(expected["phi"] - sdf(mask))) / 2)
                d_chamfer = chamfer(expected["edges"], edges(mask), .05 * min(mask.shape)) if fine else 0.
                row = {"threshold": float(threshold), "target_level": expected["level"],
                       "D_sdf": d_sdf, "D_region": d_region, "D_chamfer": d_chamfer,
                       "D_coarse": (.40*d_sdf + .25*d_region) / .65,
                       "D_shape": .40*d_sdf + .35*d_chamfer + .25*d_region}
                choices.append(row)
            options.append(choices)
        chosen = ordered_choice(options, "D_shape" if fine else "D_coarse")
        scale_results.append({"levels": chosen, **{key: float(np.mean([row[key] for row in chosen]))
                                                   for key in ["D_sdf", "D_region", "D_chamfer", "D_coarse", "D_shape"]}})
    return {**{key: float(np.mean([s[key] for s in scale_results]))
               for key in ["D_sdf", "D_region", "D_chamfer", "D_coarse", "D_shape"]},
            "scale_scores": scale_results,
            "chosen_thresholds": [[r["threshold"] for r in s["levels"]] for s in scale_results]}


def tone(gray, prepared):
    luma = np.asarray(gray, np.float32) / 255
    deltas, errors = [], []
    for mask in prepared["full_masks"]:
        black, white = float(luma[mask].mean()), float(luma[~mask].mean())
        deltas.append(white - black)
        errors.append(.5 * (abs(black - float(prepared["original"][mask].mean()))
                            + abs(white - float(prepared["original"][~mask].mean()))))
    return {"contrast_delta": min(deltas) if deltas else 0., "contrast_deltas": deltas,
            "D_tone": float(np.mean(errors)) if errors else abs(float(luma.mean()) - float(prepared["original"].mean()))}


def select(rows, tolerance=.01, min_contrast=.03):
    visible = [row for row in rows if row["contrast_delta"] >= min_contrast]
    if not visible:
        chosen = min(rows, key=lambda r: (r["D_shape"], r["view_id"]))
        return {**chosen, "low_visibility": True, "eligible_count": 0}
    best = min(row["D_shape"] for row in visible)
    eligible = [row for row in visible if row["D_shape"] <= best + tolerance]
    chosen = min(eligible, key=lambda r: (r["D_tone"], r["D_shape"], r["view_id"]))
    return {**chosen, "low_visibility": False, "eligible_count": len(eligible), "best_visible_D_shape": best}
