"""Bounded, resumable real imagery collection for reusable CPU search."""

from datetime import datetime, timedelta, timezone
import heapq
import json
import math
from pathlib import Path
import re
import shutil
import time

import numpy as np
import psutil
from pyproj import CRS
import pyarrow as pa
import pyarrow.parquet as pq
from rasterio.errors import RasterioIOError
from rasterio.enums import Resampling
from rasterio.windows import Window

from ..evidence import write_json
from ..features import EXTRACTION_VERSION, canonical_image, extract
from ..imagery import QUALITY_POLICY, SceneReader, quality_tables, window_quality, window_location
from ..sample import validate_config as validate_sample_config
from ..sources.sentinel import discover, safe_asset
from ..sources import aerial
from .storage import Index, writer_lock
from .windows import allocate, area_quotas, blocks, identity, refill_limits, slices


class ResourceLimit(ValueError):
    pass


def validate_config(config: dict):
    # Reuse the geometry/pixel budget checks already used for small source reads.
    if config.get("source") in aerial.SOURCES:
        if type(config["scenes_per_aoi"]) is not int or not 1 <= config["scenes_per_aoi"] <= 32:
            raise ValueError("Aerial scenes_per_aoi must be between 1 and 32")
        if type(config["catalog_limit_per_aoi"]) is not int or not config["scenes_per_aoi"] <= config["catalog_limit_per_aoi"] <= 100:
            raise ValueError("Aerial catalogue limit must be between scenes_per_aoi and 100")
        if not config.get("cache_rgb"):
            raise ValueError("Aerial collection requires reusable RGB caching")
        validate_sample_config({**config, "scene_attempts_per_aoi": min(3, config["scenes_per_aoi"])}, 1)
    else:
        validate_sample_config({**config, "scene_attempts_per_aoi": config["scenes_per_aoi"]}, 1)
    for name, low, high in [("block_size", 256, 4096), ("report_samples", 1, 200),
                            ("max_memory_mb", 256, 65536), ("max_disk_mb", 64, 1048576),
                            ("min_free_disk_mb", 64, 1048576), ("max_block_attempts", 1, 5),
                            ("max_blocks_per_scene", 1, 1000)]:
        if type(config[name]) is not int or not low <= config[name] <= high:
            raise ValueError(f"Invalid {name}")
    if not 0.25 <= config["stride_fraction"] <= 1:
        raise ValueError("stride_fraction must be between 0.25 and 1")
    weights = config.get("scale_weights")
    if weights is not None and (len(weights) != len(config["widths"]) or any(weight <= 0 for weight in weights)):
        raise ValueError("scale_weights must have one positive weight for each width")


def catalog(root: Path, config: dict):
    path = root / "catalog/catalog.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    catalogs, selected = [], {}
    for area in config["areas"]:
        print(f"Discovering {area['id']}", flush=True)
        area_path = root / "catalog" / (area["id"] + "-discovery.json")
        if area_path.exists():
            result = json.loads(area_path.read_text(encoding="utf-8"))
        else:
            result = (aerial.discover(config, area, root / "catalog/source")
                      if config.get("source") in aerial.SOURCES else discover(config, area))
            write_json(area_path, result)
        catalogs.append(result)
        chosen, spatial = [], set()
        for scene in result["candidates"]:
            # Sentinel IDs contain the stable MGRS tile after the first underscore.
            tile = scene["spatial_id"] if "spatial_id" in scene else scene["item_id"].split("_")[1]
            if tile in spatial:
                continue
            spatial.add(tile)
            chosen.append(scene)
            if len(chosen) == config["scenes_per_aoi"]:
                break
        selected[area["id"]] = chosen
    result = {"catalogs": catalogs, "selected": selected,
              "selection": ("source-specific bounded discovery; one acquisition per spatial ID"
                            if config.get("source") in aerial.SOURCES
                            else "AOI coverage, cloud cover, one acquisition per MGRS tile")}
    write_json(path, result)
    scenes = {scene["item_id"]: scene for items in selected.values() for scene in items}
    pq.write_table(pa.Table.from_pylist([{"scene_id": key, "metadata_json": json.dumps(value)}
                                       for key, value in scenes.items()]), root / "catalog/scenes.parquet")
    return result


def resources(index: Index) -> int:
    config = index.config
    rss = psutil.Process().memory_info().rss
    previous = index.manifest.get("peak_observed_rss_bytes", 0)
    index.manifest["peak_observed_rss_bytes"] = max(previous, rss)
    if rss > config["max_memory_mb"] * 1024**2:
        raise ResourceLimit("Memory budget reached; committed work is preserved")
    free = shutil.disk_usage(index.root).free
    if free < config["min_free_disk_mb"] * 1024**2:
        raise ResourceLimit("Free disk floor reached; committed work is preserved")
    now = time.monotonic()
    if now - getattr(index, "last_disk_scan", -float("inf")) >= 15:
        index.manifest["observed_disk_bytes"] = sum(path.stat().st_size for path in index.root.rglob("*") if path.is_file())
        index.last_disk_scan = now
    used = index.manifest["observed_disk_bytes"]
    if used > config["max_disk_mb"] * 1024**2:
        raise ResourceLimit("Index disk budget reached; committed work is preserved")
    index.manifest["observed_disk_bytes"] = used
    return rss


def cached_block(index: Index, reader: SceneReader, plan: dict):
    path = index.root / "cache/blocks" / (plan["job_id"] + ".npz")
    if path.exists():
        with np.load(path, allow_pickle=False) as data:
            rgb, valid, scl = data["rgb"], data["valid"], data["scl"]
            if not scl.size:
                scl = None
            if "mask_policy" not in data and getattr(reader, "zero_nodata_masks", False):
                valid = np.any(rgb != 0, axis=0)
            return rgb, valid, scl
    rgb, valid, scl = reader.read_block(Window(*plan["read_window"]))
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as stream:
        np.savez_compressed(stream, rgb=rgb, valid=valid,
                            scl=scl if scl is not None else np.empty(0, np.uint8),
                            mask_policy=np.array(scene_policy(reader)))
    temporary.replace(path)
    return rgb, valid, scl


def scene_policy(reader):
    return reader.metadata.get("quality_policy", QUALITY_POLICY)


def open_scene(scene):
    if scene["source_id"] in aerial.SOURCES:
        return aerial.AerialReader(scene)
    return SceneReader(safe_asset(scene["visual"]), safe_asset(scene["scl"]))


def process_block(index, reader, scene, area, plan, scale_limits, scene_limit):
    config = index.config
    job = index.db.execute("SELECT * FROM jobs WHERE job_id=?", (plan["job_id"],)).fetchone()
    if job["state"] == "DONE" or job["attempts"] >= config["max_block_attempts"]:
        return
    counts = {width: index.count(area=area["id"], width=width) for width in config["widths"]}
    scene_count = index.count(area=area["id"], scene=scene["item_id"])
    if scene_count >= scene_limit or all(counts[width] >= scale_limits[width] for width in config["widths"]):
        return
    # Quota passes may revisit a block whose eligible scales are already exhausted.
    # Decide this from the ledger before decompressing any pixels.
    eligible = False
    for window in plan["windows"][job["cursor"]:]:
        if counts[window[2]] >= scale_limits[window[2]]:
            continue
        tile_id = identity([scene["source_id"], scene["collection_id"], scene["item_id"], "visual", window, EXTRACTION_VERSION])
        if not index.known(tile_id):
            eligible = True
            break
    if not eligible:
        return
    with index.db:
        index.db.execute("UPDATE jobs SET state='RUNNING',attempts=attempts+1 WHERE job_id=?", (plan["job_id"],))
    rss = resources(index)
    read_pixels = plan["read_window"][2] * plan["read_window"][3]
    if rss + read_pixels * 28 > config["max_memory_mb"] * 1024**2:
        raise ResourceLimit("Read block would exceed the memory budget; committed work is preserved")
    rgb, valid, scl = cached_block(index, reader, plan)
    tables = quality_tables(valid, scl)
    resources(index)
    records, coarse_values, fine_values, rejected, images = [], [], [], [], {}
    photo_images = [] if config.get("collect_rotations", False) or config.get("cache_rgb", False) else None
    crs = CRS.from_user_input(reader.src.crs)
    row_map = {}
    heap = [-int(row[0], 16) for row in index.db.execute(
        "SELECT sample_key FROM tiles ORDER BY sample_key LIMIT ?", (config["report_samples"],))]
    heapq.heapify(heap)
    deferred = None
    cursor = len(plan["windows"])
    for position in range(job["cursor"], len(plan["windows"])):
        window = plan["windows"][position]
        width = window[2]
        tile_id = identity([scene["source_id"], scene["collection_id"], scene["item_id"], "visual", window, EXTRACTION_VERSION])
        if index.known(tile_id):
            continue
        if counts[width] >= scale_limits[width] or scene_count >= scene_limit:
            deferred = position if deferred is None else min(deferred, position)
            continue
        sy, sx = slices(window, plan["read_window"])
        quality = window_quality(tables, sy, sx, config["max_cloud_ratio"])
        if quality["reasons"]:
            rejected.append({"tile_id": tile_id, "scene_id": scene["item_id"], "aoi_id": area["id"],
                             "window": window, "reason": "+".join(quality["reasons"]), **quality})
            continue
        image = canonical_image(rgb[:, sy, sx], tuple(index.manifest["canonical_size"]))
        coarse, fine, dhash = extract(image, config["aspect_ratio"])
        # Keep valid flat terrain; near duplicates are linked, not silently erased.
        near = index.near_duplicate(scene["item_id"], window, dhash, coarse)
        key = (width, dhash)
        for previous, previous_coarse in row_map.get(key, []):
            if (abs(previous["window"][0] - window[0]) <= width // 2
                    and abs(previous["window"][1] - window[1]) <= window[3] // 2
                    and np.mean((coarse.astype(np.float32) - previous_coarse.astype(np.float32)) ** 2) <= 4):
                near = previous["tile_id"]
                break
        location = window_location(reader.src.transform, reader.src.crs, Window(*window))
        unit = crs.axis_info[0].unit_conversion_factor if crs.is_projected else None
        if unit is not None:
            ground_width = math.hypot(reader.src.transform.a, reader.src.transform.d) * width * unit
            ground_height = math.hypot(reader.src.transform.b, reader.src.transform.e) * window[3] * unit
        else:
            corners = location["footprint"]["coordinates"][0]
            geod = crs.get_geod()
            ground_width = abs(geod.inv(*corners[0], *corners[1])[2])
            ground_height = abs(geod.inv(*corners[0], *corners[3])[2])
        sample_key = identity([config["seed"], tile_id])
        row = {
            "tile_id": tile_id, "view_id": tile_id, "scene_id": scene["item_id"],
            "source_id": scene["source_id"], "collection_id": scene["collection_id"],
            "asset_key": scene.get("asset_key", "visual"), "acquired_at": scene["acquired_at"],
            "acquired_start": scene.get("acquired_start"), "acquired_end": scene.get("acquired_end"),
            "source_item_id": scene.get("source_item_id", scene["item_id"]),
            "source_asset_href": scene["visual"],
            "source_license": scene.get("license"),
            "source_licensors": [p["name"] for p in scene.get("providers", []) if "licensor" in p.get("roles", [])],
            "aoi_id": area["id"], "terrain_group": area["terrain_group"], "window": window,
            "longitude": location["longitude"], "latitude": location["latitude"],
            "footprint_json": json.dumps(location["footprint"]),
            "ground_width_m": ground_width, "ground_height_m": ground_height,
            "source_gsd": list(reader.src.res), "valid_ratio": quality["valid_ratio"],
            "local_cloud_ratio": quality["local_cloud_ratio"],
            "cloud_quality": "unassessed" if scl is None else "scl",
            "quality_policy": scene_policy(reader),
            "gray_mean": float(coarse.mean()), "gray_std": float(coarse.std()),
            "uniform": float(coarse.std()) < 2, "dhash": dhash, "near_duplicate_of": near,
            "extraction_version": EXTRACTION_VERSION, "rotation_degrees": 0,
            "sample_key": sample_key, "sample_image": None,
        }
        if len(heap) < config["report_samples"] or int(sample_key, 16) < -heap[0]:
            images[tile_id] = image
            heapq.heappush(heap, -int(sample_key, 16))
            if len(heap) > config["report_samples"]:
                heapq.heappop(heap)
        records.append(row)
        if photo_images is not None:
            photo_images.append(image)
        row_map.setdefault(key, []).append((row, coarse))
        coarse_values.append(coarse)
        fine_values.append(fine)
        counts[width] += 1
        scene_count += 1
    if deferred is not None:
        cursor = deferred
    if records or rejected or cursor > job["cursor"]:
        rotations = {"images": photo_images, "scene": reader.metadata} if config.get("collect_rotations", False) else None
        index.commit(plan, cursor, deferred is None, records, coarse_values, fine_values, rejected, images,
                     rotations, photos=photo_images if config.get("cache_rgb", False) else None)
        with index.db:
            index.db.execute("UPDATE jobs SET attempts=0 WHERE job_id=?", (plan["job_id"],))
    else:
        with index.db:
            index.db.execute("UPDATE jobs SET state='PENDING',attempts=0 WHERE job_id=?", (plan["job_id"],))
    if deferred is None:
        (index.root / "cache/blocks" / (plan["job_id"] + ".npz")).unlink(missing_ok=True)
    resources(index)
    if records:
        print(f"  {area['id']}: {sum(counts.values())} candidates, total {index.count()}", flush=True)


def collect(config: dict, root: Path, limit: int = 10000, stop_after_blocks: int | None = None, redistribute=False):
    validate_config(config)
    if type(limit) is not int or limit < 1:
        raise ValueError("Candidate limit must be a positive integer")
    started = time.monotonic()
    with writer_lock(root):
        index = Index(root, config)
        outcome = "partial"
        block_count = 0
        try:
            limits = area_quotas(limit, config["areas"])
            for area_id, quota in limits.items():
                if index.count(area=area_id) >= quota:
                    index.release_blocks(area_id)
            resources(index)
            # A completed invocation exits before any catalogue/raster requests.
            if all(index.count(area=area) >= quota for area, quota in limits.items()):
                outcome = "complete"
                return index.report()
            sources = catalog(root, config)
            for quota_round in range(5 if redistribute else 1):
                before_round = index.count()
                for area in config["areas"]:
                    quota = limits[area["id"]]
                    if index.count(area=area["id"]) >= quota:
                        continue
                    weights = config.get("scale_weights")
                    if weights:
                        scaled = [quota * weight / sum(weights) for weight in weights]
                        parts = [int(value) for value in scaled]
                        for i in sorted(range(len(parts)), key=lambda i: (scaled[i] - parts[i], -i), reverse=True)[:quota-sum(parts)]:
                            parts[i] += 1
                        scale_limits = dict(zip(config["widths"], parts))
                    else:
                        scale_limits = allocate(quota, config["widths"])
                    scenes = sources["selected"][area["id"]]
                    # First balance scene/scale coverage; then spend cached capacity
                    # before requesting additional pixels to meet the AOI total.
                    for pass_number in range(4):
                        relaxed = pass_number > 0
                        active_scale_limits = scale_limits if pass_number < 2 else {width: quota for width in config["widths"]}
                        for scene_number, scene in enumerate(scenes):
                            if index.count(area=area["id"]) >= quota:
                                break
                            # Fixed caps keep pause/resume from progressively favoring scene 1.
                            if not relaxed and scene_number + 1 < len(scenes):
                                scene_limit = allocate(quota, [item["item_id"] for item in scenes])[scene["item_id"]]
                            else:
                                scene_limit = quota - index.count(area=area["id"]) + index.count(area=area["id"], scene=scene["item_id"])
                            if index.count(area=area["id"], scene=scene["item_id"]) >= scene_limit:
                                continue
                            plan_path = root / "catalog" / f"{area['id']}-{scene['item_id']}.json"
                            print(f"Collecting {area['id']}: {scene['item_id']}", flush=True)
                            try:
                                with open_scene(scene) as reader:
                                    metadata_path = root / "catalog" / (scene["item_id"] + ".json")
                                    if not metadata_path.exists():
                                        write_json(metadata_path, {**scene, **reader.metadata})
                                    if plan_path.exists():
                                        plans = json.loads(plan_path.read_text(encoding="utf-8"))
                                    else:
                                        # Rank useful terrain before requesting large RGB blocks.
                                        # This coarse SCL view is a planning hint, not the final quality test.
                                        if reader.scl is not None:
                                            scl_hint = reader.scl.read(1, out_shape=(256, 256), resampling=Resampling.nearest)
                                            potential = ~np.isin(scl_hint, [0, 1, 3, 8, 9, 10])
                                        else:
                                            potential = reader.src.dataset_mask(out_shape=(256, 256)) > 0
                                        rgb_hint = reader.src.read_masks((1, 2, 3), out_shape=(3, 256, 256))
                                        potential &= np.any(rgb_hint > 0, axis=0)
                                        plans = [p for p in blocks(reader.src, scene, area, config, potential)
                                                 if p["potential_score"] > 0][:config["max_blocks_per_scene"]]
                                        write_json(plan_path, plans)
                                    index.register(plans)
                                    for plan in plans:
                                        if index.count(area=area["id"]) >= quota or index.count(area=area["id"], scene=scene["item_id"]) >= scene_limit:
                                            break
                                        if pass_number in (1, 2) and not (root / "cache/blocks" / (plan["job_id"] + ".npz")).exists():
                                            continue
                                        while True:
                                            try:
                                                process_block(index, reader, scene, area, plan, active_scale_limits, scene_limit)
                                                break
                                            except RasterioIOError as error:
                                                status = re.search(r"\b(401|403|404)\b", str(error))
                                                attempts = index.db.execute("SELECT attempts FROM jobs WHERE job_id=?", (plan["job_id"],)).fetchone()[0]
                                                next_retry = (datetime.now(timezone.utc) + timedelta(seconds=2**attempts)).isoformat()
                                                with index.db:
                                                    index.db.execute("UPDATE jobs SET state='FAILED',error_type=?,next_retry=? WHERE job_id=?",
                                                        (("http_" + status[0]) if status else type(error).__name__, next_retry, plan["job_id"]))
                                                if status or attempts >= config["max_block_attempts"]:
                                                    break
                                                time.sleep(min(2**attempts, 30))
                                        block_count += 1
                                        if stop_after_blocks is not None and block_count >= stop_after_blocks:
                                            outcome = "paused"
                                            return index.report()
                            except RasterioIOError as error:
                                index.manifest.setdefault("scene_failures", []).append({"item_id": scene["item_id"], "error_type": type(error).__name__})
                                print(f"  Scene unavailable: {type(error).__name__}", flush=True)
                    index.release_blocks(area["id"])
                if index.count() >= limit or not redistribute:
                    break
                if quota_round and index.count() == before_round:
                    break
                counts = {area["id"]: index.count(area=area["id"]) for area in config["areas"]}
                next_limits = refill_limits(limit, config["areas"], counts, limits)
                if sum(next_limits.values()) <= index.count():
                    break
                limits = next_limits
                index.manifest["quota_redistribution"] = True
                print(f"Redistributing quota: {index.count()}/{limit} candidates", flush=True)
            outcome = "complete" if index.count() >= limit else "partial"
            return index.report()
        except KeyboardInterrupt:
            outcome = "interrupted"
            return index.report()
        except ResourceLimit as error:
            outcome = "resource_limit"
            index.manifest["stop_reason"] = str(error)
            return index.report()
        finally:
            index.manifest["status"] = outcome
            index.manifest["requested"] = limit
            index.manifest["elapsed_seconds_last_run"] = round(time.monotonic() - started, 3)
            index.manifest["network_requests"] = "unmeasured"
            index.manifest["network_bytes"] = "unmeasured"
            index.manifest["quality_policy"] = aerial.QUALITY_POLICY if config.get("source") in aerial.SOURCES else QUALITY_POLICY
            index.manifest["scale_quota_policy"] = "equal shares first; remaining AOI quota can use other valid scales"
            write_json(root / "manifest.json", index.manifest)
            index.close()
