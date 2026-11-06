"""Reuse canonical RGB for candidates; fetch native detail only for selected photos."""

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import shutil
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
from ..matching.photos import PhotoCache, read_rgb_rows
from ..matching.views import rotate_image, render_view, rotation_recipe
from ..sources.sentinel import safe_asset
from .layout import location_caption
from .rerank import VERSION as RERANK_VERSION, rerank


def save_image(image, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    image.save(temporary, format="PNG")
    temporary.replace(path)


def native_view(image, row, canonical_size):
    if row.get("transform_version"):
        from ..matching.affine import native_view as affine_native_view
        return affine_native_view(image, row)
    degrees = row["rotation_degrees"]
    transpose = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
    if degrees:
        image = image.transpose(transpose[degrees])
    cw, ch = canonical_size[::-1] if degrees in (90, 270) else canonical_size
    crop = row.get("rotation_crop") or rotation_recipe(canonical_size,degrees)
    box = (crop[0] / cw * image.width, crop[1] / ch * image.height,
           crop[2] / cw * image.width, crop[3] / ch * image.height)
    width = min(1444, max(1, round(box[2] - box[0])))
    height = max(1, round(width * canonical_size[1] / canonical_size[0]))
    return image.resize((width, height), Image.Resampling.BOX, box=box)


def native_scene(source, rows, destination, canonical_size, previous=None):
    if previous:
        for row in rows:
            saved = previous / "assets/selected" / f"{row['view_id']}.png"
            target = destination / saved.name
            if saved.exists() and not target.exists():
                try:
                    target.hardlink_to(saved)
                except OSError:
                    shutil.copyfile(saved, target)
    missing = [row for row in rows if not (destination / f"{row['view_id']}.png").exists()]
    if not missing:
        return []
    scene = json.loads((source / "catalog" / f"{missing[0]['scene_id']}.json").read_text(encoding="utf-8"))
    results = []
    try:
        from ..sources.aerial import open_rgb_dataset
        with open_rgb_dataset(scene) as dataset:
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


def cached_photos(run, root, candidates, config, canonical_size, relative_folder="photos"):
    """Materialize only missing views from stored RGB shards; never open a URL."""
    destination = root / relative_folder
    destination.mkdir(parents=True, exist_ok=True)
    missing = {}
    counts = {"reused_png": 0, "rgb_shard": 0, "remote": 0}
    for row in candidates:
        path = destination / f"{row['view_id']}.png"
        if path.exists():
            continue
        existing = run / "assets/thumbs" / path.name
        if not existing.exists():
            existing = run / "photos" / path.name
        if existing.exists():
            try:
                path.hardlink_to(existing)
            except OSError:
                shutil.copyfile(existing, path)
            counts["reused_png"] += 1
        else:
            missing[row["view_id"]] = row
    extra = Path(config["extra_rgb_cache"]) if config.get("extra_rgb_cache") else None
    refs = old_rgb_refs(extra, {r["tile_id"] for r in missing.values() if not r.get("rgb_cache_ref")})
    from ..collection.bundle import members
    index_root = Path(config["index"])
    sources = {manifest["index_id"]: directory
               for directory, manifest, _ in members(index_root, read_manifest(index_root))}
    groups = defaultdict(list)
    for row in missing.values():
        if row["tile_id"] in refs:
            path, position = refs[row["tile_id"]]
        elif row.get("rgb_cache_ref"):
            path = sources[row["index_member"]] / row["rgb_cache_ref"]
            position = row["rgb_cache_row"]
        else:
            raise ValueError("Top-K comparison requires locally cached RGB for every view")
        groups[path].append((position, row))
    for path, jobs in groups.items():
        array = read_rgb_rows(path, [position for position, _ in jobs])
        for position, row in jobs:
            image = Image.fromarray(np.asarray(array[position]))
            transformed = render_view(image, row, canonical_size)
            save_image(transformed, destination / f"{row['view_id']}.png")
            counts["rgb_shard"] += 1
    return counts


def astra_choices(root, config, targets, inverse, top):
    """Import actual model choices and existing SSIM reference scores, without reranking."""
    visual_root = Path(config["visual_run"])
    visual = read_manifest(visual_root)
    target, search = read_manifest(root / "targets"), read_manifest(root / "search")
    if (visual["status"] not in {"selected", "complete"}
            or visual["experiment_id"] != config["visual_experiment_id"]
            or visual["target_set_id"] != target["target_set_id"] or visual["index_id"] != search["index_id"]):
        raise ValueError("Astra selections must be complete and match the configured experiment, targets and index")
    source = Path(visual["source_run"])
    if read_manifest(source / "search")["run_id"] != search["run_id"]:
        raise ValueError("Astra selections use a different retrieval pool")
    saved = pq.read_table(visual_root / "selected.parquet", columns=["slot", "view_id"]).to_pylist()
    selected = {r["slot"]: r["view_id"] for r in saved}
    if len(selected) != len(saved) or set(selected) != {r["slot"] for r in targets}:
        raise ValueError("Astra selections must cover every original frame exactly once")
    measured = pq.read_table(source / "evaluations.parquet", columns=["slot", "dense_row", "ms_ssim"]).to_pylist()
    scores = {(r["slot"], r["dense_row"]): r["ms_ssim"] for r in measured}
    choices = []
    for row, query in zip(targets, inverse):
        pool = top[int(query)]
        winner = next((i for i, candidate in enumerate(pool) if candidate["view_id"] == selected[row["slot"]]), None)
        if winner is None:
            raise ValueError("Astra selected a view outside the original frame's candidate pool")
        choices.append({"slot": row["slot"], "selected_index": winner,
                        "scores": [scores[row["slot"], c["dense_row"]] for c in pool]})
    return choices, {"method": "existing-ms-ssim-reference-only", "source": str(source),
                     "version": RERANK_VERSION, "pairs": len(measured), "recomputed_pairs": 0,
                     "visual_experiment_id": visual["experiment_id"]}


def displayed_candidates(pool, choice, count):
    """Show the winner and up to count real distinct places from this pool."""
    if count < 1:
        raise ValueError("Display count must be positive")
    shown, seen = [choice], {choice["tile_id"]}
    for row in sorted(pool, key=lambda r: (r["rank"], r["view_id"])):
        if len(shown) >= count:
            break
        if row["tile_id"] not in seen:
            shown.append(row)
            seen.add(row["tile_id"])
            if len(shown) == count:
                break
    return sorted(shown, key=lambda r: (r["rank"], r["view_id"]))


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
    full_pool = config.get("rerank_top_k", config["display_top_k"]) > config["display_top_k"]
    top, thumbnails, selected = {}, {}, {}
    for query, rows in by_query.items():
        if full_pool:
            chosen = sorted(rows, key=lambda r: r["rank"])[:config["rerank_top_k"]]
            if len(chosen) != config["rerank_top_k"]:
                raise ValueError("Retrieval has fewer views than the configured reranking pool")
            top[query] = chosen
            continue
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
    source_docs = {}
    for candidate in matches:
        if candidate.get("source_id") == "linz-imagery" and not candidate.get("source_licensors"):
            key = candidate["index_member"], candidate["scene_id"]
            if key not in source_docs:
                source_docs[key] = json.loads((source_paths[key[0]] / "catalog" / (key[1] + ".json")).read_text(encoding="utf-8"))
            candidate["source_licensors"] = [p["name"] for p in source_docs[key].get("providers", []) if "licensor" in p.get("roles", [])]
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
    print((f"Assets: evaluate {config['rerank_top_k']} views before choosing display places" if full_pool else
           f"Assets: {len(thumbnails):,} candidate views, {len(selected):,} selected views"), flush=True)
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
                transformed = render_view(image, view, target["canonical_size"])
                save_image(transformed, thumbs / f"{view['view_id']}.png")
            if (number + 1) % 500 == 0:
                print(f"Candidate RGB: {number+1:,}/{len(by_tile):,} places", flush=True)
    targets = pq.read_table(root / "targets/targets.parquet").to_pylist()
    score_audit = None
    if full_pool:
        if config.get("selection_method") == "astra":
            choices, score_audit = astra_choices(root, config, targets, inverse, top)
        elif config.get("selection_method") == "ms-ssim":
            from .grid.rerank import evaluate
            metadata = {row["dense_row"]: row for pool in top.values() for row in pool}
            canonical_rows = [{k:v for k,v in row.items() if k not in {"query","rank","sse","raw_mse"}}
                              for row in metadata.values()]
            canonical_path = root / "candidates.tmp.parquet"
            columns = sorted({key for row in canonical_rows for key in row})
            pq.write_table(pa.Table.from_pylist([{key:row.get(key) for key in columns} for row in canonical_rows]), canonical_path, compression="zstd")
            canonical_path.replace(root / "candidates.parquet")
            shortlist = [{"slot":row["slot"], "dense_row":candidate["dense_row"]}
                         for row, query in zip(targets, inverse) for candidate in top[int(query)]]
            measured, score_audit = evaluate(root, root, {"config":config}, targets, metadata, shortlist,
                                             {**config, "workers":config["rerank_workers"]})
            scores = {(row["slot"],row["dense_row"]):row["ms_ssim"] for row in measured}
            choices = []
            for row, query in zip(targets, inverse):
                pool = top[int(query)]
                values = [scores[row["slot"],c["dense_row"]] for c in pool]
                winner = min(range(len(pool)), key=lambda i: (-values[i],pool[i]["rank"],pool[i]["view_id"]))
                choices.append({"slot":row["slot"], "selected_index":winner, "scores":values})
        else:
            choices = rerank(root, targets, inverse, top, config)
        for row, query, evaluation in zip(targets,inverse,choices):
            pool = top[int(query)]
            for candidate in displayed_candidates(pool, pool[evaluation["selected_index"]], config["display_top_k"]):
                thumbnails[candidate["view_id"]] = candidate
        cached_photos(Path(config.get("visual_run", config.get("reuse_run", root))), root, thumbnails.values(), config,
                      tuple(target["canonical_size"]), relative_folder="assets/thumbs")
    else:
        choices = rerank(root, targets, inverse, top, config)
    selected = {top[int(query)][choice["selected_index"]]["view_id"]: top[int(query)][choice["selected_index"]]
                for query, choice in zip(inverse, choices)}
    groups = defaultdict(list)
    for row in selected.values():
        if config.get("high_resolution_selected", True) and row["window"][2] > target["canonical_size"][0]:
            groups[(row["index_member"], row["scene_id"])].append(row)
    audit_path = folder / "native-state.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.exists() else []
    # No SCL re-download or downloaded-image verification. Only display detail
    # not present in the canonical cache is requested from the known asset.
    with ThreadPoolExecutor(max_workers=config["asset_workers"]) as pool:
        previous_native = Path(config["incremental_base_run"]) if config.get("incremental_base_run") else None
        jobs = {pool.submit(native_scene, source_paths[member], rows, hero, target["canonical_size"], previous_native): key
                for key, rows in groups.items() for member, _ in [key]}
        for number, future in enumerate(as_completed(jobs), 1):
            audit.extend(future.result())
            if number % 25 == 0 or number == len(jobs):
                print(f"Selected native detail: {number}/{len(jobs)} scene groups", flush=True)
                write_json(audit_path, audit)
    frames, selections = [], []
    years = sorted({r["acquired_at"][:4] for r in thumbnails.values()})
    from ..matching.output import source_credits
    credits, source_references = source_credits(thumbnails.values())
    for row, query, evaluation in zip(targets, inverse, choices):
        pool = top[int(query)]
        choice_index = evaluation["selected_index"]
        choice = pool[choice_index]
        candidates = displayed_candidates(pool, choice, config["display_top_k"])
        selected_index = next(i for i,c in enumerate(candidates) if c["view_id"] == choice["view_id"])
        candidate_scores = {c["view_id"]:score for c,score in zip(pool,evaluation["scores"])}
        image = hero / f"{choice['view_id']}.png"
        if not image.exists():
            image = thumbs / image.name
        frames.append({"slot": row["slot"], "source_pts": row["source_pts"], "source_time_base": row["source_time_base"],
                       "time": row["target_time"], "duration": target["duration_seconds"], "total": target["count"],
                       "places": search["candidate_count"], "source_image": f"targets/{row['frame']}",
                       "credit": frame_credit(choice),
                       "selected_candidate": selected_index,
                       "score_label": "MS-SSIM" if config.get("selection_method") in {"ms-ssim", "astra"} else "MSE",
                       "selected_image": image.relative_to(root).as_posix(), "caption": location_caption(choice),
                       "candidates": [{"path": (thumbs / f"{c['view_id']}.png").relative_to(root).as_posix(),
                                       "rank": c["rank"], "raw_mse": c["raw_mse"], "view_id": c["view_id"],
                                       "selection_score": candidate_scores[c["view_id"]]} for c in candidates]})
        selections.append({**choice, "slot": row["slot"], "source_pts": row["source_pts"],
                           "selected_candidate": selected_index, "selection_score": evaluation["scores"][choice_index],
                           "selection_method": config.get("selection_method", "mse"),
                           "score_role": "reference" if config.get("selection_method") == "astra" else "selection",
                           "source_time_base": row["source_time_base"], "image": image.relative_to(root).as_posix()})
    selection_path = root / "selected.tmp.parquet"
    columns = sorted({key for row in selections for key in row})
    pq.write_table(pa.Table.from_pylist([{key:row.get(key) for key in columns} for row in selections]), selection_path, compression="zstd")
    selection_path.replace(root / "selected.parquet")
    result = {"candidate_views": len(thumbnails), "selected_views": len(selected),
              "evaluated_views":len({c["view_id"] for pool in top.values() for c in pool}),
              "rerank_candidates_per_frame":config.get("rerank_top_k",config["display_top_k"]),
              "score_cache":score_audit,
              "selection_method": config.get("selection_method", "mse"),
              "selection_version": (read_manifest(Path(config["visual_run"]))["version"] if config.get("selection_method") == "astra"
                                    else RERANK_VERSION if config.get("selection_method") == "ms-ssim" else "gray-mse-[0,1]-v1"),
              "selected_places": len({r["tile_id"] for r in selected.values()}), "native_results": audit,
              "native_detail_available": sum((hero / f"{key}.png").exists() for key in selected),
              "acquisition_years": years,
              "credits": credits, "source_references": source_references,
              "metadata": "actual coordinates, acquisition date, source and ground dimensions; no inferred place names"}
    write_json(folder / "manifest.json", result)
    # Publication of the frame plan is the completion marker for this stage.
    plan_path = root / "frame-plan.tmp.parquet"
    pq.write_table(pa.Table.from_pylist(frames), plan_path, compression="zstd")
    plan_path.replace(root / "frame-plan.parquet")
    return result


def frame_credit(row):
    if row.get("source_id") == "beeldmateriaal-ortho":
        return "Beeldmateriaal Nederland / beeldmateriaal.nl / CC BY 4.0"
    if row.get("source_id") == "swisstopo-swissimage":
        return "©swisstopo / SWISSIMAGE / see product source notices"
    if row.get("source_id") == "planetarycomputer-naip":
        return "NAIP / USDA / Microsoft Planetary Computer"
    if row.get("source_id") == "linz-imagery":
        return "LINZ / " + ", ".join(row.get("source_licensors") or []) + " / CC BY 4.0"
    return "Contains modified Copernicus Sentinel data " + row["acquired_at"][:4]
