"""Resumable analysis shards, sharing lossless RGB across all four views."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack
from functools import lru_cache
import hashlib
import json
import os
from multiprocessing import get_context
from pathlib import Path
import shutil
import sqlite3
import time

import numpy as np
from PIL import Image
import psutil
import pyarrow as pa
import pyarrow.parquet as pq
from rasterio.errors import RasterioIOError
from rasterio.windows import Window

from ..collection.bundle import members, read_manifest
from ..collection.storage import writer_lock
from ..collection.windows import identity, slices
from ..evidence import checksum, write_json
from ..features import canonical_image
from ..imagery import SceneReader, check_quality
from ..matching.views import ANGLES, VIEW_VERSION, rotate_image, rotation_recipe
from ..sources.sentinel import safe_asset
from .features import VERSION, descriptors


def analyze(item):
    tile, rgb = item
    image = Image.fromarray(rgb)
    rows, phogs, ranks = [], [], []
    for angle in ANGLES:
        crop = rotation_recipe(image.size, angle)
        gray = np.asarray(rotate_image(image, angle, image.size, crop).convert("L"))
        phog, rank, quality = descriptors(gray)
        view_id = tile.get("view_id", tile["tile_id"]) if angle == 0 else identity(
            [tile["tile_id"], VIEW_VERSION, angle, crop, list(image.size)])
        rows.append({"view_id": view_id, "tile_id": tile["tile_id"], "rotation_degrees": angle,
                     "rotation_crop": crop, "gray_checksum": hashlib.sha256(gray.tobytes()).hexdigest(),
                     "gray_cache_kind": "shared-lossless-rgb-recipe", "full_valid": True, **quality})
        phogs.append(phog)
        ranks.append(rank)
    return rows, phogs, ranks


class Pixels:
    def __init__(self):
        self.stack = ExitStack()
        self.key = None
        self.counts = {"local_png": 0, "local_block": 0, "source_tiles": 0, "source_blocks": 0}

    def close(self):
        self.stack.close()

    def load(self, source, manifest, tiles, job):
        size = tuple(manifest["canonical_size"])
        result, missing = [None] * len(tiles), []
        for i, tile in enumerate(tiles):
            paths = [source / "cache/base-photos" / (tile["tile_id"] + ".png")]
            if tile.get("sample_image"):
                paths.append(source / tile["sample_image"])
            path = next((p for p in paths if p.exists()), None)
            if path:
                with Image.open(path) as image:
                    if image.size != size:
                        raise ValueError("Canonical source cache size differs")
                    result[i] = np.asarray(image.convert("RGB")).copy()
                self.counts["local_png"] += 1
            else:
                missing.append(i)
        if not missing:
            return result
        plan_path = source / "catalog" / f"{tiles[0]['aoi_id']}-{tiles[0]['scene_id']}.json"
        plans = json.loads(plan_path.read_text(encoding="utf-8"))
        plan = next(p for p in plans if p["job_id"] == job)
        cache = source / "cache/blocks" / (job + ".npz")
        valid = scl = None
        if cache.exists():
            with np.load(cache, allow_pickle=False) as data:
                rgb = data["rgb"]
            self.counts["local_block"] += len(missing)
        else:
            key = (str(source), tiles[0]["scene_id"])
            if key != self.key:
                self.stack.close()
                self.stack = ExitStack()
                scene = json.loads((source / "catalog" / (key[1] + ".json")).read_text(encoding="utf-8"))
                self.reader = self.stack.enter_context(SceneReader(safe_asset(scene["visual"]), safe_asset(scene["scl"])))
                self.key = key
            for attempt in range(3):
                try:
                    rgb, valid, scl = self.reader.read_block(Window(*plan["read_window"]))
                    break
                except RasterioIOError:
                    if attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            self.counts["source_tiles"] += len(missing)
            self.counts["source_blocks"] += 1
        for i in missing:
            sy, sx = slices(tiles[i]["window"], plan["read_window"])
            if valid is not None and check_quality(valid[sy, sx], scl[sy, sx], manifest["config"]["max_cloud_ratio"])["reasons"]:
                raise ValueError("Previously accepted source window is no longer fully valid")
            result[i] = np.asarray(canonical_image(rgb[:, sy, sx], size))
        return result


def build(index_root: Path, root: Path, workers=8):
    source_manifest = read_manifest(index_root)
    snapshot = [{"index_id": m["index_id"], "shards": [s["shard_id"] for s in m["shards"]],
                 "excluded": sorted(excluded)} for _, m, excluded in members(index_root, source_manifest)]
    cache_id = identity([VERSION, VIEW_VERSION, snapshot, "shared-lossless-rgb-v1"])
    with writer_lock(root):
        for name in ["shards", "staging"]:
            (root / name).mkdir(exist_ok=True)
        path = root / "manifest.json"
        manifest = {"schema": "shape-index-v2", "index_id": cache_id, "feature_version": VERSION,
                    "view_version": VIEW_VERSION, "source_index_id": source_manifest["index_id"],
                    "source_snapshot": snapshot, "canonical_size": source_manifest["canonical_size"],
                    "rotations": list(ANGLES), "shards": [], "status": "building", "backend": "numpy-scipy-cpu-float32",
                    "numpy_version": np.__version__, "cache_kind": "shared-lossless-rgb-recipe"}
        if path.exists():
            manifest = read_manifest(root)
            if manifest["index_id"] != cache_id:
                raise ValueError("Analysis cache belongs to another snapshot or feature version")
        db = sqlite3.connect(root / "progress.sqlite")
        db.execute("CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, state TEXT, error_type TEXT)")
        seen = {s["shard_id"] for s in manifest["shards"]}
        db.execute("UPDATE jobs SET state='PENDING' WHERE state='RUNNING'")
        db.executemany("INSERT OR REPLACE INTO jobs VALUES (?, 'DONE', NULL)", [(job,) for job in seen])
        db.commit()
        pixels = Pixels()
        started = time.monotonic()
        try:
            with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as executor:
                for source, original, excluded in members(index_root, source_manifest):
                    groups = defaultdict(list)
                    block_size = original["config"]["block_size"]
                    for shard in original["shards"]:
                        if not shard["rows"]:
                            continue
                        for tile in pq.read_table(source / "shards" / shard["shard_id"] / "tiles.parquet").to_pylist():
                            if tile["tile_id"] in excluded:
                                continue
                            tile["index_member"] = original["index_id"]
                            left, top = tile["window"][:2]
                            job = identity([tile["aoi_id"], tile["scene_id"], left // block_size, top // block_size])
                            groups[(tile["scene_id"], job)].append(tile)
                    for (_, job), tiles in sorted(groups.items()):
                        tiles.sort(key=lambda t: t["tile_id"])
                        shard_id = identity([original["index_id"], job, [t["tile_id"] for t in tiles]])
                        destination = root / "shards" / shard_id
                        if shard_id in seen:
                            continue
                        db.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?)", (shard_id, "RUNNING", None)); db.commit()
                        if (destination / "complete.json").exists():
                            descriptor = json.loads((destination / "complete.json").read_text())
                        else:
                            if shutil.disk_usage(root).free < 12 * 1024**3:
                                raise ValueError("Analysis cache reached the 12GiB free disk floor")
                            rgb = pixels.load(source, original, tiles, job)
                            stage = root / "staging" / shard_id
                            stage.mkdir(exist_ok=True)
                            np.save(stage / "rgb.npy", np.stack(rgb), allow_pickle=False)
                            views, phog, rank = [], [], []
                            for base_row, (rows, p, r) in enumerate(executor.map(analyze, zip(tiles, rgb), chunksize=8)):
                                for row in rows:
                                    row.update({"base_row": base_row, "feature_row": len(views), "feature_version": VERSION,
                                                "gray_cache_ref": f"shards/{shard_id}/rgb.npy#{base_row}:{row['rotation_degrees']}"})
                                    views.append(row)
                                phog.extend(p); rank.extend(r)
                            pq.write_table(pa.Table.from_pylist(tiles), stage / "tiles.parquet", compression="zstd")
                            pq.write_table(pa.Table.from_pylist(views), stage / "views.parquet", compression="zstd")
                            np.save(stage / "ids.npy", np.asarray([r["view_id"] for r in views], dtype="S64"), allow_pickle=False)
                            np.save(stage / "phog.npy", np.stack(phog), allow_pickle=False)
                            np.save(stage / "rank.npy", np.stack(rank), allow_pickle=False)
                            descriptor = {"shard_id": shard_id, "places": len(tiles), "rows": len(views),
                                          "source_root": os.path.relpath(source.resolve(), root.resolve()),
                                          "sha256": {p.name: checksum(p) for p in stage.iterdir() if p.is_file() and p.name != "complete.json"}}
                            write_json(stage / "complete.json", descriptor)
                            stage.replace(destination)
                        manifest["shards"].append(descriptor)
                        seen.add(shard_id)
                        manifest["places"] = sum(s["places"] for s in manifest["shards"])
                        manifest["view_count"] = sum(s["rows"] for s in manifest["shards"])
                        manifest["peak_observed_rss_bytes"] = max(manifest.get("peak_observed_rss_bytes", 0), psutil.Process().memory_info().rss)
                        manifest["base_reads_last_run"] = pixels.counts
                        write_json(path, manifest)
                        db.execute("UPDATE jobs SET state='DONE',error_type=NULL WHERE job_id=?", (shard_id,)); db.commit()
                        print(f"Shape cache: {manifest['places']}/{source_manifest['rows']} places, {manifest['view_count']} views", flush=True)
            manifest["status"] = "complete"
            return manifest
        except BaseException as error:
            db.execute("UPDATE jobs SET state='FAILED',error_type=? WHERE state='RUNNING'", (type(error).__name__,)); db.commit()
            manifest["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            raise
        finally:
            manifest["elapsed_seconds_last_run"] = time.monotonic() - started
            manifest["base_reads_last_run"] = pixels.counts
            write_json(path, manifest)
            pixels.close(); db.close()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.manifest = read_manifest(self.root)
        self.shards = {s["shard_id"]: s for s in self.manifest["shards"]}

    @lru_cache(maxsize=32)
    def array(self, shard, name):
        return np.load(self.root / "shards" / shard / name, mmap_mode="r", allow_pickle=False)

    @lru_cache(maxsize=32)
    def rows(self, shard, name="views.parquet"):
        return pq.read_table(self.root / "shards" / shard / name).to_pylist()

    def row(self, ref):
        shard, position = ref
        view = self.rows(shard)[position]
        if view["feature_row"] != position:
            raise ValueError("Shape feature-row mapping differs")
        return {**self.rows(shard, "tiles.parquet")[view["base_row"]], **view, "shape_shard": shard}

    def rgb(self, ref):
        row = self.rows(ref[0])[ref[1]]
        image = Image.fromarray(np.asarray(self.array(ref[0], "rgb.npy")[row["base_row"]]))
        return rotate_image(image, row["rotation_degrees"], image.size, row["rotation_crop"])

    def gray(self, ref):
        return np.asarray(self.rgb(ref).convert("L")).copy()
