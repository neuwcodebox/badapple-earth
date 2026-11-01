"""Quarter-turn views of the same real photo, with an explicit center crop."""

import json
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
from pyproj import CRS
from rasterio.transform import Affine
from rasterio.windows import Window

from ..collection.windows import identity
from ..evidence import write_json
from ..features import extract
from ..imagery import window_location

VIEW_VERSION = "canonical-quarter-turn-center-crop-v1"
ANGLES = (0, 90, 180, 270)


@lru_cache(maxsize=128)
def geodesic(crs):
    return CRS.from_user_input(crs).get_geod()


def rotation_recipe(size, degrees):
    if degrees not in ANGLES:
        raise ValueError("Rotations must be one of 0, 90, 180, 270 degrees clockwise")
    width, height = size
    rw, rh = (height, width) if degrees in (90, 270) else (width, height)
    crop_width, crop_height = rw, rh
    if rw * height > rh * width:
        crop_width = max(1, round(rh * width / height))
    elif rw * height < rh * width:
        crop_height = max(1, round(rw * height / width))
    left, top = (rw - crop_width) // 2, (rh - crop_height) // 2
    return [left, top, left + crop_width, top + crop_height]


def rotate_image(image: Image.Image, degrees: int, size, crop=None):
    if image.size != tuple(size):
        raise ValueError("Base photo does not match the index canonical size")
    crop = rotation_recipe(size, degrees) if crop is None else crop
    if crop != rotation_recipe(size, degrees):
        raise ValueError("Rotation crop does not match the saved recipe")
    transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                 270: Image.Transpose.ROTATE_90}
    rotated = image.transpose(transpose[degrees]) if degrees else image
    return rotated.crop(tuple(crop)).resize(tuple(size), Image.Resampling.BOX)


def view_metadata(tile, scene, size, degrees):
    crop = rotation_recipe(size, degrees)
    width, height = size
    corners = [(crop[0], crop[1]), (crop[2], crop[1]), (crop[2], crop[3]), (crop[0], crop[3])]
    def inverse(x, y):
        if degrees == 90:
            return y, height - x
        if degrees == 180:
            return width - x, height - y
        if degrees == 270:
            return width - y, x
        return x, y
    points = [inverse(x, y) for x, y in corners]
    left, top, sw, sh = tile["window"]
    x0, x1 = min(p[0] for p in points), max(p[0] for p in points)
    y0, y1 = min(p[1] for p in points), max(p[1] for p in points)
    effective = Window(left + sw * x0 / width, top + sh * y0 / height,
                       sw * (x1 - x0) / width, sh * (y1 - y0) / height)
    transform = Affine(*scene["affine"])
    location = window_location(transform, scene["crs"], effective)
    polygon = location["footprint"]["coordinates"][0]
    geod = geodesic(scene["crs"])
    ground_width = abs(geod.inv(*polygon[0], *polygon[1])[2])
    ground_height = abs(geod.inv(*polygon[0], *polygon[3])[2])
    if degrees in (90, 270):
        ground_width, ground_height = ground_height, ground_width
    return {**tile, "view_id": identity([tile["tile_id"], VIEW_VERSION, degrees, crop, list(size)]),
            "rotation_degrees": degrees, "rotation_direction": "clockwise", "rotation_crop": crop,
            "view_version": VIEW_VERSION, "view_source_window": list(effective.flatten()),
            "source_footprint_json": tile["footprint_json"],
            "footprint_json": json.dumps(location["footprint"]),
            "longitude": location["longitude"], "latitude": location["latitude"],
            "ground_width_m": ground_width, "ground_height_m": ground_height,
            "base_feature_row": tile["feature_row"]}


def write_view_shard(destination, tiles, images, scene, manifest, source_shard):
    """Use already-read canonical RGB while a collection batch is in memory."""
    destination.mkdir(parents=True, exist_ok=True)
    rows, coarse, fine = [], [], []
    for tile, image in zip(tiles, images):
        for degrees in (90, 180, 270):
            row = view_metadata(tile, scene, manifest["canonical_size"], degrees)
            rotated = rotate_image(image, degrees, manifest["canonical_size"], row["rotation_crop"])
            c, f, _ = extract(rotated, manifest["config"]["aspect_ratio"])
            row["view_feature_row"] = len(rows)
            rows.append(row)
            coarse.append(c)
            fine.append(f)
    pq.write_table(pa.Table.from_pylist(rows), destination / "views.parquet", compression="zstd")
    np.save(destination / "gray_coarse.npy", np.stack(coarse), allow_pickle=False)
    np.save(destination / "gray_fine.npy", np.stack(fine), allow_pickle=False)
    write_json(destination / "complete.json", {"source_shard": source_shard, "rows": len(rows), "inline": True})


def prepare_views(index_root: Path, manifest: dict, photo_cache: Path | None = None):
    from .photos import PhotoCache
    from ..collection.storage import writer_lock
    root = index_root / "views" / VIEW_VERSION
    with writer_lock(root):
        root.mkdir(parents=True, exist_ok=True)
        path = root / "manifest.json"
        source_shards = [s["shard_id"] for s in manifest["shards"] if s["rows"]]
        view_manifest = {"view_version": VIEW_VERSION, "index_id": manifest["index_id"],
                         "source_shards": source_shards, "rotations": [90, 180, 270], "shards": []}
        if path.exists():
            view_manifest = json.loads(path.read_text(encoding="utf-8"))
            if view_manifest["index_id"] != manifest["index_id"] or view_manifest["source_shards"] != source_shards:
                raise ValueError("Rotation cache belongs to a different index snapshot")
        completed = {s["source_shard"] for s in view_manifest["shards"]}
        with PhotoCache(index_root, manifest, photo_cache) as photos:
            for number, source in enumerate(source_shards):
                if source in completed:
                    continue
                inline = index_root / "shards" / source / "views/complete.json"
                if inline.exists():
                    view_manifest["shards"].append(json.loads(inline.read_text(encoding="utf-8")))
                    write_json(path, view_manifest)
                    continue
                destination = root / source
                if not (destination / "complete.json").exists():
                    destination.mkdir(exist_ok=True)
                    tiles = pq.read_table(index_root / "shards" / source / "tiles.parquet").to_pylist()
                    rows, coarse, fine = [], [], []
                    for tile in tiles:
                        image = photos.base(tile)
                        scene = photos.scene(tile["scene_id"])
                        for degrees in (90, 180, 270):
                            row = view_metadata(tile, scene, manifest["canonical_size"], degrees)
                            rotated = rotate_image(image, degrees, manifest["canonical_size"], row["rotation_crop"])
                            c, f, _ = extract(rotated, manifest["config"]["aspect_ratio"])
                            row["view_feature_row"] = len(rows)
                            rows.append(row)
                            coarse.append(c)
                            fine.append(f)
                    pq.write_table(pa.Table.from_pylist(rows), destination / "views.parquet", compression="zstd")
                    np.save(destination / "gray_coarse.npy", np.stack(coarse), allow_pickle=False)
                    np.save(destination / "gray_fine.npy", np.stack(fine), allow_pickle=False)
                    write_json(destination / "complete.json", {"source_shard": source, "rows": len(rows)})
                descriptor = json.loads((destination / "complete.json").read_text(encoding="utf-8"))
                view_manifest["shards"].append(descriptor)
                write_json(path, view_manifest)
                if (number + 1) % 20 == 0 or number + 1 == len(source_shards):
                    print(f"Rotation features: {number + 1}/{len(source_shards)} shards", flush=True)
        view_manifest["view_count"] = sum(s["rows"] for s in view_manifest["shards"])
        prior = view_manifest.get("base_reads", {})
        view_manifest["base_reads"] = {key: prior.get(key, 0) + value for key, value in photos.counts.items()}
        write_json(path, view_manifest)
    return root, view_manifest
