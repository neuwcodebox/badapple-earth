"""Globally anchored windows; each belongs to exactly one read block."""

from collections import defaultdict
import hashlib

from pyproj import Transformer
from shapely.geometry import Point, box, shape


def identity(value) -> str:
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def allocate(total: int, keys: list) -> dict:
    return {key: total // len(keys) + (index < total % len(keys)) for index, key in enumerate(keys)}


def area_quotas(total: int, areas: list[dict]) -> dict[str, int]:
    groups = sorted({area["terrain_group"] for area in areas})
    group_limits = allocate(total, groups)
    result = {}
    for group in groups:
        result.update(allocate(group_limits[group], [a["id"] for a in areas if a["terrain_group"] == group]))
    return result


def blocks(src, scene: dict, area: dict, config: dict, potential=None) -> list[dict]:
    grouped = defaultdict(list)
    to_geo = Transformer.from_crs(src.crs, "EPSG:4326", always_xy=True)
    coverage = shape(scene["geometry"]).intersection(box(*area["bbox"]))
    west, south, east, north = coverage.bounds if not coverage.is_empty else (0, 0, 0, 0)
    block_size = config["block_size"]
    for width in config["widths"]:
        height = round(width * config["aspect_ratio"][1] / config["aspect_ratio"][0])
        sx, sy = max(1, round(width * config["stride_fraction"])), max(1, round(height * config["stride_fraction"]))
        for top in range(0, src.height - height + 1, sy):
            for left in range(0, src.width - width + 1, sx):
                center = src.transform @ (left + width / 2, top + height / 2)
                lon, lat = to_geo.transform(*center)
                if not (west <= lon <= east and south <= lat <= north) or not coverage.covers(Point(lon, lat)):
                    continue
                grouped[(left // block_size, top // block_size)].append([left, top, width, height])
    result = []
    for (bx, by), windows in grouped.items():
        # Interleave scales so quotas do not consume a block's small windows first.
        windows.sort(key=lambda w: (w[1] / w[3], w[0] / w[2], w[2]))
        left, top = bx * block_size, by * block_size
        right = max(w[0] + w[2] for w in windows)
        bottom = max(w[1] + w[3] for w in windows)
        job_id = identity([area["id"], scene["item_id"], bx, by])
        score = 1.0
        if potential is not None:
            good = 0
            for x, y, w, h in windows:
                col = min(potential.shape[1] - 1, int((x + w / 2) * potential.shape[1] / src.width))
                row = min(potential.shape[0] - 1, int((y + h / 2) * potential.shape[0] / src.height))
                good += bool(potential[row, col])
            score = good / len(windows)
        result.append({"job_id": job_id, "scene_id": scene["item_id"], "aoi_id": area["id"],
                       "read_window": [left, top, right - left, bottom - top], "windows": windows,
                       "potential_score": score})
    # Spread early quotas across the scene instead of always taking the northwest.
    return sorted(result, key=lambda job: (-job["potential_score"], identity([config["seed"], job["job_id"]])))


def slices(window: list[int], read_window: list[int]):
    left, top, width, height = window
    x, y = left - read_window[0], top - read_window[1]
    if x < 0 or y < 0 or x + width > read_window[2] or y + height > read_window[3]:
        raise ValueError("Candidate window is not contained in its read block")
    return slice(y, y + height), slice(x, x + width)
