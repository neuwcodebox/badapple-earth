"""Canonical source photos shared by rotation indexing and final materialization."""

from contextlib import ExitStack
import json
from pathlib import Path

import numpy as np
from PIL import Image
from rasterio.windows import Window

from ..collection.windows import identity, slices
from ..features import canonical_image
from ..imagery import SceneReader, check_quality
from ..sources.sentinel import safe_asset


class PhotoCache:
    def __init__(self, root: Path, manifest: dict, extra: Path | None = None):
        self.root, self.manifest, self.extra = root, manifest, extra
        self.stack = ExitStack()
        self.readers, self.scenes, self.plans = {}, {}, {}
        self.cached_job, self.cached_rgb = None, None
        self.counts = {"png": 0, "block": 0, "remote": 0}
        self.local = root / "cache/base-photos"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def scene(self, scene_id):
        if scene_id not in self.scenes:
            self.scenes[scene_id] = json.loads((self.root / "catalog" / (scene_id + ".json")).read_text(encoding="utf-8"))
        return self.scenes[scene_id]

    def base(self, tile):
        paths = [self.local / (tile["tile_id"] + ".png")]
        if tile["sample_image"]:
            paths.append(self.root / tile["sample_image"])
        if self.extra:
            paths.append(self.extra / (tile["tile_id"] + ".png"))
        for path in paths:
            if path.exists():
                with Image.open(path) as source:
                    image = source.convert("RGB")
                if image.size != tuple(self.manifest["canonical_size"]):
                    raise ValueError("Cached source image has an incompatible size")
                if self.extra and path == self.extra / (tile["tile_id"] + ".png"):
                    # Keep a shared base copy for materialization after the view build.
                    self.local.mkdir(exist_ok=True)
                    destination = self.local / (tile["tile_id"] + ".png")
                    temporary = destination.with_suffix(".tmp")
                    image.save(temporary, format="PNG")
                    temporary.replace(destination)
                self.counts["png"] += 1
                return image
        left, top, _, _ = tile["window"]
        size = self.manifest["config"]["block_size"]
        job_id = identity([tile["aoi_id"], tile["scene_id"], left // size, top // size])
        cache = self.root / "cache/blocks" / (job_id + ".npz")
        if cache.exists():
            if self.cached_job != job_id:
                with np.load(cache, allow_pickle=False) as data:
                    self.cached_rgb = data["rgb"]
                self.cached_job = job_id
            key = (tile["aoi_id"], tile["scene_id"])
            if key not in self.plans:
                path = self.root / "catalog" / f"{key[0]}-{key[1]}.json"
                self.plans[key] = {p["job_id"]: p for p in json.loads(path.read_text(encoding="utf-8"))}
            sy, sx = slices(tile["window"], self.plans[key][job_id]["read_window"])
            self.counts["block"] += 1
            return canonical_image(self.cached_rgb[:, sy, sx], tuple(self.manifest["canonical_size"]))
        if tile["scene_id"] not in self.readers:
            scene = self.scene(tile["scene_id"])
            self.readers[tile["scene_id"]] = self.stack.enter_context(
                SceneReader(safe_asset(scene["visual"]), safe_asset(scene["scl"])))
        rgb, valid, scl = self.readers[tile["scene_id"]].read_block(Window(*tile["window"]))
        quality = check_quality(valid, scl, self.manifest["config"]["max_cloud_ratio"])
        if quality["reasons"]:
            raise ValueError("Source window is no longer valid")
        image = canonical_image(rgb, tuple(self.manifest["canonical_size"]))
        self.local.mkdir(exist_ok=True)
        destination = self.local / (tile["tile_id"] + ".png")
        temporary = destination.with_suffix(".tmp")
        image.save(temporary, format="PNG")
        temporary.replace(destination)
        self.counts["remote"] += 1
        return image
