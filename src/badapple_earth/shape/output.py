"""Real-color output, full-frame diagnostics and MSE/shape video comparison."""

import html
import json
import os
from pathlib import Path
import subprocess
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from functools import lru_cache

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq

from ..evidence import checksum, contact_sheet, write_json
from ..matching.output import render
from ..matching.views import view_metadata
from .features import structures
from .geometry import score, sdf, target, tone
from .index import Store


def baseline_score(paths):
    target_path, photo_path = paths
    with Image.open(target_path) as image:
        prepared = target(np.asarray(image.convert("L")))
    with Image.open(photo_path) as image:
        gray = np.asarray(image.convert("L"))
    if prepared["near_uniform"]:
        return {"D_shape": None, **tone(gray, prepared)}
    return {"D_shape": score(gray, prepared, True)["D_shape"], **tone(gray, prepared)}


@lru_cache(maxsize=64)
def source_rows(source, shard):
    return pq.read_table(Path(source) / "shards" / shard / "tiles.parquet", use_threads=False).to_pylist()


def evaluate_baseline(target_root, baseline, run_root, target_rows, selected, workers):
    before = sorted(pq.read_table(baseline / "selected.parquet").to_pylist(), key=lambda row: row["slot"])
    if [r["slot"] for r in before] != list(range(len(target_rows))):
        raise ValueError("Baseline selected slots differ")
    paths = [(target_root / frame["frame"], baseline / row["photo"]) for frame, row in zip(target_rows, before)]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        measured = list(pool.map(baseline_score, paths, chunksize=4))
    rows = [{"slot": i, "baseline_D_shape": old["D_shape"], "shape_D_shape": new["D_shape"],
             "baseline_contrast": old["contrast_delta"], "shape_contrast": new["contrast_delta"],
             "near_uniform": new["near_uniform"]} for i, (old, new) in enumerate(zip(measured, selected))]
    nonuniform = [r for r in rows if r["baseline_D_shape"] is not None]
    result = {"metric": "same v2.1 geometry on both displayed photos; diagnostic only, does not alter retrieval",
              "baseline_shape_mean": float(np.mean([r["baseline_D_shape"] for r in nonuniform])) if nonuniform else None,
              "new_shape_mean": float(np.mean([r["shape_D_shape"] for r in nonuniform])) if nonuniform else None,
              "shape_better_frames": sum(r["shape_D_shape"] < r["baseline_D_shape"] for r in nonuniform),
              "shape_equal_frames": sum(r["shape_D_shape"] == r["baseline_D_shape"] for r in nonuniform),
              "shape_worse_frames": sum(r["shape_D_shape"] > r["baseline_D_shape"] for r in nonuniform),
              "per_target": rows}
    write_json(run_root / "baseline-shape-evaluation.json", result)
    return result


def materialize(target_root: Path, index_root: Path, run_root: Path, baseline: Path | None = None):
    store = Store(index_root)
    manifest = json.loads((run_root / "manifest.json").read_text())
    if manifest["status"] != "complete" or manifest["index_id"] != store.manifest["index_id"]:
        raise ValueError("Shape run is incomplete or belongs to another index")
    target_manifest = json.loads((target_root / "manifest.json").read_text())
    if manifest["target_set_id"] != target_manifest["target_set_id"]:
        raise ValueError("Output target set differs")
    target_rows = pq.read_table(target_root / "targets.parquet").to_pylist()
    for name in ["photos", "diagnostics"]:
        (run_root / name).mkdir(exist_ok=True)
    selected, matches, overview, diagnostic_overview, report_rows = [], [], [], [], []
    evaluation = set(np.linspace(0, len(target_rows)-1, min(20, len(target_rows)), dtype=int))
    scene_cache = {}

    def photo(ref):
        row = store.row(tuple(ref))
        path = run_root / "photos" / (row["view_id"] + ".png")
        if not path.exists():
            temporary = path.with_suffix(".tmp")
            store.rgb(tuple(ref)).save(temporary, format="PNG")
            temporary.replace(path)
        return path

    def record(candidate, result, rank):
        ref = tuple(candidate["ref"])
        row = store.row(ref)
        source = (index_root / store.shards[ref[0]]["source_root"]).resolve()
        key = (str(source), row["scene_id"])
        if key not in scene_cache:
            scene_cache[key] = json.loads((source / "catalog" / (key[1] + ".json")).read_text())
        provenance = view_metadata(row, scene_cache[key], store.manifest["canonical_size"], row["rotation_degrees"])
        original = source_rows(str(source), row["source_shard_id"])[row["source_feature_row"]] if row.get("source_shard_id") else row
        if original["tile_id"] != row["tile_id"]:
            raise ValueError("Source provenance row mapping differs")
        clean = {key: value for key, value in candidate.items() if key not in {"ref", "rank_scores", "path_ranks", "scale_scores"}}
        return {**provenance, **clean, "view_id": row["view_id"], "base_feature_row": row["source_feature_row"],
                "item_id": scene_cache[key].get("item_id", row["scene_id"]),
                "feature_version": store.manifest["feature_version"],
                "run_id": manifest["run_id"], "slot": result["slot"], "target_id": result["target_id"],
                "target_pts": result["target_pts"], "rank": rank, "rank_scores_json": json.dumps(candidate.get("rank_scores", {})),
                "path_ranks_json": json.dumps(candidate.get("path_ranks", {})),
                "scale_scores_json": json.dumps(candidate["scale_scores"]), "config_hash": manifest["run_id"],
                "source_recipe_json": json.dumps({"index_member": row["index_member"], "scene_id": row["scene_id"],
                                                    "source_id": row.get("source_id"), "collection_id": row.get("collection_id"),
                                                    "item_id": scene_cache[key].get("item_id", row["scene_id"]),
                                                    "asset_key": row.get("asset_key"), "extraction_version": row.get("extraction_version"),
                                                    "source_window": row["window"], "rotation": row["rotation_degrees"],
                                                    "crop": row["rotation_crop"], "view_version": store.manifest["view_version"],
                                                    "canonical_size": store.manifest["canonical_size"]}),
                "near_uniform": result["target_profile"]["near_uniform"],
                "uniform_threshold_unmet": bool(result["chosen"].get("uniform_threshold_unmet", False)),
                "eligible_count": result["chosen"]["eligible_count"],
                "best_visible_D_shape": result["chosen"].get("best_visible_D_shape"),
                "low_visibility": bool(result["chosen"]["low_visibility"]),
                "retrieval_truncated": result["retrieval_truncated"], "rerank_truncated": result["rerank_truncated"],
                "quality_policy": original.get("quality_policy") or "rgb-all-band-mask-v1"}

    for slot, frame in enumerate(target_rows):
        result = json.loads((run_root / "scores" / f"{slot:04d}.json").read_text())
        chosen = result["chosen"]
        fine_path = run_root / "scores" / f"{slot:04d}-fine.json"
        others = json.loads(fine_path.read_text()) if fine_path.exists() else []
        others = sorted((r for r in others if r["view_id"] != chosen["view_id"]), key=lambda r: (r["D_shape"], r["view_id"]))
        ranked = [chosen] + others[:19]
        for rank, candidate in enumerate(ranked, 1):
            matches.append(record(candidate, result, rank))
        path = photo(chosen["ref"])
        row = record(chosen, result, 1)
        row.update({"photo": path.relative_to(run_root).as_posix(), "photo_sha256": checksum(path), "fallback_reason": None})
        row["selected_rgb_cache_ref"] = row["photo"]
        selected.append(row)
        if slot in evaluation:
            overview.append((target_root / frame["frame"], f"target {frame['target_time']:.3f}s"))
            overview.extend((photo(r["ref"]), f"{'selected' if rank == 0 else 'shape'} {r['D_shape']:.4f} tone {r.get('D_tone', 0):.3f}")
                            for rank, r in enumerate(ranked[:5]))
        with Image.open(target_root / frame["frame"]) as image:
            gray_target = np.asarray(image.convert("L"))
        prepared = target(gray_target)
        gray = store.gray(tuple(chosen["ref"]))
        if chosen["scale_scores"]:
            scores = chosen["scale_scores"][0]["levels"]
            position = min(range(len(scores)), key=lambda i: abs(scores[i]["target_level"] - .5))
            first = prepared["geometry"][gray.shape[1]][0][position]["mask"]
            second = structures(gray, gray.shape[1])[0] <= np.float64(scores[position]["threshold"])
        else:
            first, second = prepared["normalized"] <= .5, gray / 255 <= .5
        difference = (sdf(second) - sdf(first)) / 2
        heat = np.stack([np.maximum(difference, 0), 1 - np.abs(difference), np.maximum(-difference, 0)], axis=-1)
        prefix = run_root / "diagnostics" / f"{slot:04d}"
        t_path, c_path, d_path, g_path = [prefix.with_name(prefix.name + suffix + ".png") for suffix in ["-target", "-candidate", "-sdf", "-gray"]]
        Image.fromarray((first.astype(np.uint8) * 255)).save(t_path)
        Image.fromarray((second.astype(np.uint8) * 255)).save(c_path)
        Image.fromarray(np.clip(heat * 255, 0, 255).astype(np.uint8)).save(d_path)
        Image.fromarray(gray).save(g_path)
        if slot in evaluation:
            diagnostic_overview.extend([(target_root / frame["frame"], f"target {frame['target_time']:.3f}s"),
                                        (path, f"shape {chosen['D_shape']:.4f} contrast {chosen['contrast_delta']:.3f}"),
                                        (t_path, "target region"), (c_path, "photo region (analysis only)"),
                                        (d_path, "signed distance difference"), (g_path, "actual photo luma")])
        source_target = Path(os.path.relpath((target_root / frame["frame"]).resolve(), run_root.resolve())).as_posix()
        pictures = [source_target, row["photo"], t_path.relative_to(run_root).as_posix(), c_path.relative_to(run_root).as_posix(), d_path.relative_to(run_root).as_posix()]
        report_rows.append(f"<tr><td>{frame['target_time']:.3f}s<br>shape {chosen['D_shape']:.5f}<br>tone {chosen['D_tone']:.4f}<br>contrast {chosen['contrast_delta']:.4f}<br>low visibility: {chosen['low_visibility']}<br>lon {row['longitude']:.5f}, lat {row['latitude']:.5f}</td>"
                           + ''.join(f'<td><img loading="lazy" src="{html.escape(p, quote=True)}"></td>' for p in pictures) + '</tr>')
    pq.write_table(pa.Table.from_pylist(matches), run_root / "matches.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(selected), run_root / "selected.parquet", compression="zstd")
    contact_sheet(overview, run_root / "top5.jpg", columns=6)
    contact_sheet(diagnostic_overview, run_root / "diagnostics.jpg", columns=6)
    (run_root / "report.html").write_text('<!doctype html><meta charset="utf-8"><title>Shape matching</title><style>body{background:#202020;color:white;font:14px sans-serif}td{padding:5px}img{width:192px;height:auto}th{position:sticky;top:0;background:#202020}</style><h1>Shape-first real photo matching</h1><p>Masks and SDF are analysis diagnostics; output uses the original color photo. Green SDF pixels indicate agreement, red/blue indicate signed differences.</p><table><tr><th>Scores</th><th>Target</th><th>Photo</th><th>Target mask</th><th>Photo mask</th><th>SDF difference</th></tr>' + ''.join(report_rows) + '</table>', encoding="utf-8")
    manifest.update({"materialized": len(selected), "materialization_failures": [],
                     "selected_unique_tiles": len({r["tile_id"] for r in selected}),
                     "selected_unique_views": len({r["view_id"] for r in selected}),
                     "credits": sorted({f"Contains modified Copernicus Sentinel data {str(r['acquired_at'])[:4]}" for r in selected})})
    write_json(run_root / "manifest.json", manifest)
    manifest = render(target_root, run_root)
    manifest["outputs"].extend(["diagnostics.jpg", "report.html"])
    if baseline is not None:
        before = json.loads((baseline / "manifest.json").read_text())
        if before["target_set_id"] != manifest["target_set_id"]:
            raise ValueError("Method comparison targets differ")
        if before["index_id"] != store.manifest["source_index_id"]:
            raise ValueError("Method comparison source pools differ")
        evaluate_baseline(target_root, baseline, run_root, target_rows, selected, manifest["config"]["workers"])
        manifest["outputs"].append("baseline-shape-evaluation.json")
        filters = "[0:v]split=2[a][b];[a]crop=iw/2:ih:0:0[t];[b]crop=iw/2:ih:iw/2:0[s];[t][1:v][s]hstack=inputs=3[out]"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(run_root / "comparison.mp4"),
                        "-i", str(baseline / "color.mp4"), "-filter_complex", filters, "-map", "[out]", "-an", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(run_root / "method-comparison.mp4")], capture_output=True, check=True)
        manifest["outputs"].append("method-comparison.mp4")
        manifest["comparison_panel_order"] = ["original", "100k-mse", "100k-shape-v2"]
        manifest["baseline_run_id"] = before["run_id"]
    manifest["quality_conclusion"] = "shape-first-output; video-recognition-not-assessed"
    manifest["output_sha256"] = {name: checksum(run_root / name) for name in manifest["outputs"]}
    write_json(run_root / "manifest.json", manifest)
    return manifest
