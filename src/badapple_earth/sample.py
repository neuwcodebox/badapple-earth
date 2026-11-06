"""Bounded P1 connection validation. No claims of a production collector."""

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import time

import numpy as np
from PIL import Image
from rasterio.windows import Window

from .evidence import checksum, contact_sheet, write_json
from .imagery import EXTRACTION_VERSION, SceneReader, candidate_windows, stable_id, window_location
from .sources.sentinel import discover, safe_asset


def validate_config(config: dict, limit: int, *, max_window_size=2048):
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("P1 sample limit must be between 1 and 500")
    if config.get("requester_pays") is not False:
        raise ValueError("P1 requires requester_pays=false")
    widths, aspect = config["widths"], config["aspect_ratio"]
    if not widths or any(type(w) is not int or not 16 <= w <= max_window_size for w in widths):
        raise ValueError(f"Source widths must be integers between 16 and {max_window_size}")
    if len(aspect) != 2 or any(type(v) is not int or v <= 0 for v in aspect):
        raise ValueError("aspect_ratio must contain two positive integers")
    if any(not 1 <= round(w * aspect[1] / aspect[0]) <= max_window_size for w in widths):
        raise ValueError(f"Source window height must be between 1 and {max_window_size}")
    if not 16 <= config["preview_width"] <= 1024 or not 0 <= config["max_cloud_ratio"] <= 1:
        raise ValueError("Invalid preview width or cloud threshold")
    if not 1 <= round(config["preview_width"] * aspect[1] / aspect[0]) <= 1024:
        raise ValueError("P1 preview height must be between 1 and 1024")
    if not 1 <= config["scene_attempts_per_aoi"] <= 3:
        raise ValueError("P1 permits 1 to 3 scene attempts per AOI")
    areas = config["areas"]
    if not areas or len({a["id"] for a in areas}) != len(areas):
        raise ValueError("Areas must have unique IDs")
    for area in areas:
        west, south, east, north = area["bbox"]
        if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
            raise ValueError("Invalid AOI bbox")


def sample(config: dict, output: Path, limit=120) -> dict:
    validate_config(config, limit)
    # P1 stores all previews. Reserve more than their uncompressed RGB size.
    preview_height = round(config["preview_width"] * config["aspect_ratio"][1] / config["aspect_ratio"][0])
    reserve = max(512 * 1024**2, limit * config["preview_width"] * preview_height * 4)
    existing_parent = output.parent.resolve()
    while not existing_parent.exists():
        existing_parent = existing_parent.parent
    if shutil.disk_usage(existing_parent).free < reserve:
        raise ValueError("Insufficient disk space for the bounded P1 sample")
    output.mkdir(parents=True, exist_ok=False)
    (output / "images").mkdir()
    started = time.monotonic()
    manifest = {
        "schema": "p1-sample-v1", "extraction_version": EXTRACTION_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "technical_validation_only", "status": "running",
        "config": config, "requested": limit, "catalogs": [], "scenes": {}, "tiles": [],
        "areas": [], "network_requests": "unmeasured", "network_bytes": "unmeasured",
        "visual_review": "not_performed", "quality_conclusion": "insufficient_scale",
        "license_refs": ["https://registry.opendata.aws/sentinel-2-l2a-cogs/"],
    }
    seen = set()
    write_json(output / "manifest.json", manifest)
    for index, area in enumerate(config["areas"]):
        quota = limit // len(config["areas"]) + (index < limit % len(config["areas"]))
        report = {"aoi_id": area["id"], "quota": quota, "accepted": 0,
                  "attempted_windows": 0, "rejected": {}, "failures": []}
        manifest["areas"].append(report)
        if not quota:
            continue
        print(f"Discovering {area['id']} (quota {quota})", flush=True)
        try:
            catalog = discover(config, area)
        except Exception as error:
            report["failures"].append({"stage": "catalog", "error_type": type(error).__name__})
            write_json(output / "manifest.json", manifest)
            continue
        manifest["catalogs"].append(catalog)
        write_json(output / "manifest.json", manifest)
        rejected = Counter()
        for scene in catalog["candidates"][:config["scene_attempts_per_aoi"]]:
            if report["accepted"] >= quota or report["attempted_windows"] >= max(60, quota * 4):
                break
            print(f"Reading {area['id']}: {scene['item_id']}", flush=True)
            scene_attempted = 0
            scene_budget = max(20, quota * 4 // config["scene_attempts_per_aoi"])
            try:
                with SceneReader(scene["visual"], scene["scl"]) as reader:
                    manifest["scenes"][scene["item_id"]] = {**scene, **reader.metadata}
                    for window in candidate_windows(reader.src, area, config["widths"], config["aspect_ratio"], scene["geometry"]):
                        if (report["accepted"] >= quota or scene_attempted >= scene_budget
                                or report["attempted_windows"] >= max(60, quota * 4)):
                            break
                        tile_id = stable_id(scene, window)
                        if tile_id in seen:
                            rejected["exact_duplicate"] += 1
                            continue
                        report["attempted_windows"] += 1
                        scene_attempted += 1
                        image, quality, raw_checksum = reader.read(window, config["preview_width"], config["max_cloud_ratio"])
                        if quality["reasons"]:
                            rejected.update(quality["reasons"])
                            continue
                        path = Path("images") / f"{tile_id}.png"
                        image.save(output / path)
                        location = window_location(reader.src.transform, reader.src.crs, window)
                        tile = {
                            "tile_id": tile_id, "scene_id": scene["item_id"], "aoi_id": area["id"],
                            "terrain_group": area["terrain_group"], "window": list(window.flatten()),
                            **location, **quality, "raw_rgb_sha256": raw_checksum,
                            "preview_sha256": checksum(output / path), "image": path.as_posix(),
                            "output_shape": [image.height, image.width, 3],
                            "rgb_variance": float(np.asarray(image).astype(np.float32).var()),
                            "rotation_degrees": 0,
                        }
                        manifest["tiles"].append(tile)
                        seen.add(tile_id)
                        report["accepted"] += 1
                        report["rejected"] = dict(rejected)
                        write_json(output / "manifest.json", manifest)
                        if report["accepted"] % 10 == 0:
                            print(f"  {report['accepted']}/{quota} accepted", flush=True)
            except Exception as error:
                report["failures"].append({"stage": "raster", "item_id": scene["item_id"],
                                           "error_type": type(error).__name__})
            report["rejected"] = dict(rejected)
            write_json(output / "manifest.json", manifest)
    count = len(manifest["tiles"])
    manifest["accepted"] = count
    manifest["status"] = "complete" if count == limit else "partial"
    manifest["elapsed_seconds"] = round(time.monotonic() - started, 3)
    contact_sheet([(output / t["image"], f"{t['aoi_id']} {int(t['window'][2])}px\n"
                    f"{t['latitude']:.4f}, {t['longitude']:.4f}") for t in manifest["tiles"]],
                  output / "contact-sheet.jpg")
    write_json(output / "manifest.json", manifest)
    return manifest


def verify_sample(output: Path) -> dict:
    """Reread one tile per scene/scale and compare source pixels and preview."""
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    if manifest["extraction_version"] != EXTRACTION_VERSION:
        raise ValueError("Extraction version mismatch")
    config = manifest["config"]
    selected = {}
    for tile in manifest["tiles"]:
        selected.setdefault((tile["scene_id"], tile["window"][2]), tile)
    checks = []
    for scene_id in sorted({key[0] for key in selected}):
        scene = manifest["scenes"][scene_id]
        with SceneReader(safe_asset(scene["visual"]), safe_asset(scene["scl"])) as reader:
            if reader.metadata["affine"] != scene["affine"] or reader.metadata["crs"] != scene["crs"]:
                raise ValueError("Source georeferencing changed")
            for (sid, _), tile in selected.items():
                if sid != scene_id:
                    continue
                image, quality, raw_checksum = reader.read(Window(*tile["window"]), config["preview_width"], config["max_cloud_ratio"])
                with Image.open(output / tile["image"]) as cached:
                    same_preview = np.array_equal(np.asarray(image), np.asarray(cached))
                checks.append({
                    "tile_id": tile["tile_id"], "raw_rgb_equal": raw_checksum == tile["raw_rgb_sha256"],
                    "preview_pixels_equal": same_preview, "quality_valid": not quality["reasons"],
                    "cache_checksum_equal": checksum(output / tile["image"]) == tile["preview_sha256"],
                    "stable_id_equal": stable_id(scene, Window(*tile["window"])) == tile["tile_id"],
                })
    result = {"checks": checks, "passed": bool(checks) and all(
        all(value for key, value in row.items() if key != "tile_id") for row in checks)}
    write_json(output / "verification.json", result)
    return result
