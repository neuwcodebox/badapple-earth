"""Reuse canonical RGB for candidates; fetch native detail only for selected photos."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
import rasterio
from rasterio.windows import Window

from ..collection.bundle import members, read_manifest
from ..evidence import write_json
from ..imagery import GDAL_OPTIONS
from ..matching.photos import PhotoCache
from ..matching.views import rotate_image
from ..sources.sentinel import safe_asset
from .layout import location_caption


def save_image(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    image.save(temporary, format="PNG")
    temporary.replace(path)


def native_view(image, row, canonical_size):
    degrees = row["rotation_degrees"]
    transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    if degrees:
        image = image.transpose(transpose[degrees])
    cw, ch = canonical_size[::-1] if degrees in (90, 270) else canonical_size
    crop = row["rotation_crop"]
    box = (crop[0] / cw * image.width, crop[1] / ch * image.height,
           crop[2] / cw * image.width, crop[3] / ch * image.height)
    width = min(1444, max(1, round(box[2] - box[0])))
    height = max(1, round(width * canonical_size[1] / canonical_size[0]))
    return image.resize((width, height), Image.Resampling.BOX, box=box)


def native_scene(source, rows, destination, canonical_size):
    missing = [row for row in rows if not (destination / f"{row['view_id']}.png").exists()]
    if not missing:
        return []
    scene = json.loads((source / "catalog" / f"{missing[0]['scene_id']}.json").read_text(encoding="utf-8"))
    results = []
    try:
        with rasterio.Env(**GDAL_OPTIONS), rasterio.open(safe_asset(scene["visual"])) as dataset:
            # A source tile can win in several directions. Read its RGB once.
            by_tile = defaultdict(list)
            for row in missing:
                by_tile[row["tile_id"]].append(row)
            for tile, views in by_tile.items():
                try:
                    data = dataset.read((1, 2, 3), window=Window(*views[0]["window"]))
                    image = Image.fromarray(np.moveaxis(data, 0, 2))
                    for row in views:
                        result = native_view(image, row, canonical_size)
                        save_image(result, destination / f"{row['view_id']}.png")
                        results.append({"view_id": row["view_id"], "status": "native", "size": list(result.size)})
                except (OSError, ValueError) as error:
                    results.extend({"view_id": row["view_id"], "status": "canonical_fallback", "error_type": type(error).__name__}
                                   for row in views)
    except (OSError, ValueError) as error:
        results.extend({"view_id": row["view_id"], "status": "canonical_fallback", "error_type": type(error).__name__}
                       for row in missing if not (destination / f"{row['view_id']}.png").exists())
    return results


def old_rgb_refs(extra, wanted):
    result = {}
    if not extra or not (extra / "manifest.json").exists() or not wanted:
        return result
    manifest = read_manifest(extra)
    for shard in manifest["shards"]:
        folder = extra / "shards" / shard["shard_id"]
        for position, row in enumerate(pq.read_table(folder / "tiles.parquet", columns=["tile_id"]).to_pylist()):
            if row["tile_id"] in wanted:
                result[row["tile_id"]] = (folder / "rgb.npy", position)
    return result


def prepare(root: Path, config):
    if (root / "frame-plan.parquet").exists():
        return json.loads((root / "assets/manifest.json").read_text(encoding="utf-8"))
    index_root = Path(config["index"])
    index = read_manifest(index_root)
    target = read_manifest(root / "targets")
    search = read_manifest(root / "search")
    matches = pq.read_table(root / "search/matches.parquet").to_pylist()
    inverse = np.load(root / "search/query_inverse.npy", allow_pickle=False)
    by_query = defaultdict(list)
    for row in matches:
        by_query[row["query"]].append(row)
    top, thumbnails, selected = {}, {}, {}
    for query, rows in by_query.items():
        chosen, seen = [], set()
        for row in sorted(rows, key=lambda r: r["rank"]):
            if row["tile_id"] in seen:
                continue
            chosen.append(row)
            seen.add(row["tile_id"])
            if len(chosen) == config["display_top_k"]:
                break
        if len(chosen) != config["display_top_k"]:
            raise ValueError("Top-K contains too few distinct places for the presentation")
        top[query] = chosen
        for row in chosen:
            thumbnails[row["view_id"]] = row
        selected[chosen[0]["view_id"]] = chosen[0]
    source_paths = {manifest["index_id"]: directory for directory, manifest, _ in members(index_root, index)}
    folder = root / "assets"
    thumbs, hero = folder / "thumbs", folder / "selected"
    thumbs.mkdir(parents=True, exist_ok=True)
    hero.mkdir(exist_ok=True)
    extra = Path(config["extra_rgb_cache"]) if config.get("extra_rgb_cache") else None
    needed = {row["tile_id"] for row in thumbnails.values() if not row.get("rgb_cache_ref")
              and not (thumbs / f"{row['view_id']}.png").exists()}
    refs = old_rgb_refs(extra, needed)
    by_tile = defaultdict(list)
    for row in thumbnails.values():
        by_tile[(row["index_member"], row["tile_id"])].append(row)
    print(f"Assets: {len(thumbnails):,} candidate views, {len(selected):,} selected views", flush=True)
    cached_path, cached_array = None, None
    with PhotoCache(index_root, index) as photos:
        for number, (_, rows) in enumerate(sorted(by_tile.items(), key=lambda item: (item[0][0], item[1][0].get("rgb_cache_ref") or "", item[0][1]))):
            missing = [row for row in rows if not (thumbs / f"{row['view_id']}.png").exists()]
            if not missing:
                continue
            row = missing[0]
            if row["tile_id"] in refs:
                path, position = refs[row["tile_id"]]
                if cached_path != path:
                    cached_array = np.load(path, mmap_mode="r", allow_pickle=False)
                    cached_path = path
                image = Image.fromarray(np.asarray(cached_array[position]))
            else:
                image = photos.base(row)
            for view in missing:
                transformed = rotate_image(image, view["rotation_degrees"], target["canonical_size"], view["rotation_crop"])
                save_image(transformed, thumbs / f"{view['view_id']}.png")
            if (number + 1) % 500 == 0:
                print(f"Candidate RGB: {number+1:,}/{len(by_tile):,} places", flush=True)
    groups = defaultdict(list)
    for row in selected.values():
        if config.get("high_resolution_selected", True) and row["window"][2] > target["canonical_size"][0]:
            groups[(row["index_member"], row["scene_id"])].append(row)
    audit_path = folder / "native-state.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else []
    # No SCL re-download or downloaded-image verification. Only display detail
    # not present in the canonical cache is requested from the known asset.
    with ThreadPoolExecutor(max_workers=config["asset_workers"]) as pool:
        jobs = {pool.submit(native_scene, source_paths[member], rows, hero, target["canonical_size"]): key
                for key, rows in groups.items() for member, _ in [key]}
        for number, future in enumerate(as_completed(jobs), 1):
            audit.extend(future.result())
            if number % 25 == 0 or number == len(jobs):
                print(f"Selected native detail: {number}/{len(jobs)} scene groups", flush=True)
                write_json(audit_path, audit)
    frames, selections = [], []
    years = sorted({r["acquired_at"][:4] for r in thumbnails.values()})
    credit = "Contains modified Copernicus Sentinel data " + (years[0] if len(years) == 1 else f"{years[0]}–{years[-1]}")
    targets = pq.read_table(root / "targets/targets.parquet").to_pylist()
    for row, query in zip(targets, inverse):
        candidates = top[int(query)]
        choice = candidates[0]
        image = hero / f"{choice['view_id']}.png"
        if not image.exists():
            image = thumbs / image.name
        frames.append({"slot": row["slot"], "source_pts": row["source_pts"], "source_time_base": row["source_time_base"],
                       "time": row["target_time"], "duration": target["duration_seconds"], "total": target["count"],
                       "places": search["candidate_count"], "source_image": f"targets/{row['frame']}",
                       "credit": credit,
                       "selected_image": image.relative_to(root).as_posix(), "caption": location_caption(choice),
                       "candidates": [{"path": (thumbs / f"{c['view_id']}.png").relative_to(root).as_posix(),
                                       "rank": c["rank"], "raw_mse": c["raw_mse"], "view_id": c["view_id"]} for c in candidates]})
        selections.append({**choice, "slot": row["slot"], "source_pts": row["source_pts"],
                           "source_time_base": row["source_time_base"], "image": image.relative_to(root).as_posix()})
    selection_path = root / "selected.tmp.parquet"
    pq.write_table(pa.Table.from_pylist(selections), selection_path, compression="zstd")
    selection_path.replace(root / "selected.parquet")
    result = {"candidate_views": len(thumbnails), "selected_views": len(selected),
              "selected_places": len({r["tile_id"] for r in selected.values()}), "native_results": audit,
              "native_detail_available": sum((hero / f"{key}.png").exists() for key in selected),
              "acquisition_years": years,
              "metadata": "actual coordinates, acquisition date, source and ground dimensions; no inferred place names"}
    write_json(folder / "manifest.json", result)
    # Publication of the frame plan is the completion marker for this stage.
    plan_path = root / "frame-plan.tmp.parquet"
    pq.write_table(pa.Table.from_pylist(frames), plan_path, compression="zstd")
    plan_path.replace(root / "frame-plan.parquet")
    return result
