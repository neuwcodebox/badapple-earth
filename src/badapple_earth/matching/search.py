"""Exact batch MSE ranking on a fixed snapshot of immutable feature shards."""

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..collection.windows import identity
from ..collection.bundle import members
from ..evidence import write_json
from .views import ANGLES, VIEW_VERSION, prepare_views, rotation_recipe


def top_indices(scores: np.ndarray, ids: np.ndarray, k: int):
    return np.lexsort((ids, scores))[:k]


def match(target_root: Path, index_root: Path, output: Path, top_k=20,
          rotations=ANGLES, photo_cache: Path | None = None):
    if not 1 <= top_k <= 100:
        raise ValueError("top_k must be between 1 and 100")
    target_manifest = json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))
    index_manifest = json.loads((index_root / "manifest.json").read_text(encoding="utf-8"))
    rotations = tuple(sorted(set(rotations)))
    if not rotations or any(angle not in ANGLES for angle in rotations):
        raise ValueError("Specify rotations from 0,90,180,270 degrees clockwise")
    if target_manifest["feature_version"] != index_manifest["feature_version"]:
        raise ValueError("Target and index feature versions differ")
    if target_manifest["canonical_size"] != index_manifest["canonical_size"]:
        raise ValueError("Target and index image sizes differ")
    target_rows = pq.read_table(target_root / "targets.parquet").to_pylist()
    target_data = np.load(target_root / "gray_coarse.npy", allow_pickle=False)
    if list(target_data.shape[1:]) != index_manifest["coarse_shape"]:
        raise ValueError("Target and index feature shapes differ")
    if target_data.dtype != np.uint8 or len(target_rows) != len(target_data):
        raise ValueError("Target row mapping or dtype is invalid")
    # Integral pixel distance avoids cancellation at near-identical candidates.
    # Stored data remains uint8; distance accumulation fits int32 at this dimension.
    matrix = target_data.reshape(len(target_rows), -1).astype(np.int32)
    target_norm = np.sum(matrix * matrix, axis=1)
    retained = [[] for _ in target_rows]
    sources = members(index_root, index_manifest)
    candidate_count = sum(sum(s["rows"] for s in m["shards"]) - len(excluded) for _, m, excluded in sources)
    shard_ids = [m["index_id"] + "/" + s["shard_id"] for _, m, _ in sources for s in m["shards"]]
    descriptors = []
    for source_root, source_manifest, excluded in sources:
        if 0 in rotations:
            descriptors.extend((source_root / "shards" / s["shard_id"], s["rows"], False, source_manifest["index_id"], excluded)
                               for s in source_manifest["shards"] if s["rows"])
        if any(angle for angle in rotations):
            view_root, view_manifest = prepare_views(source_root, source_manifest, photo_cache)
            descriptors.extend(((source_root / "shards" / s["source_shard"] / "views") if s.get("inline")
                                else view_root / s["source_shard"], s["rows"], True, source_manifest["index_id"], excluded)
                               for s in view_manifest["shards"])
    view_count = 0
    for directory, row_count, is_view, source_id, excluded in descriptors:
        rows = pq.read_table(directory / ("views.parquet" if is_view else "tiles.parquet")).to_pylist()
        data = np.load(directory / "gray_coarse.npy", mmap_mode="r", allow_pickle=False)
        if data.shape != (len(rows), *index_manifest["coarse_shape"]) or len(rows) != row_count or data.dtype != np.uint8:
            raise ValueError("Index shard row mapping or feature shape is invalid")
        if any(row["view_feature_row" if is_view else "feature_row"] != position for position, row in enumerate(rows)):
            raise ValueError("Index feature-row mapping is invalid")
        if is_view or excluded:
            positions = [position for position, row in enumerate(rows)
                         if (not is_view or row["rotation_degrees"] in rotations) and row["tile_id"] not in excluded]
            rows = [rows[position] for position in positions]
            data = data[positions]
        for row in rows:
            row["index_member"] = source_id
        for begin in range(0, len(rows), 8192):
            batch_rows = rows[begin:begin + 8192]
            batch = np.asarray(data[begin:begin + 8192]).reshape(len(batch_rows), -1).astype(np.int32)
            distance = target_norm[:, None] + np.sum(batch * batch, axis=1)[None, :] - 2 * (matrix @ batch.T)
            ids = np.array([row.get("view_id", row["tile_id"]) for row in batch_rows])
            for target_number, scores in enumerate(distance):
                keep = top_indices(scores, ids, top_k)
                retained[target_number].extend((int(scores[position]), batch_rows[position]) for position in keep)
                retained[target_number].sort(key=lambda entry: (entry[0], entry[1].get("view_id", entry[1]["tile_id"])))
                del retained[target_number][top_k:]
            view_count += len(batch_rows)
    if not view_count:
        raise ValueError("Index contains no searchable candidates")
    output.mkdir(parents=True, exist_ok=False)
    run_id = identity([target_manifest["target_set_id"], index_manifest["index_id"],
                       shard_ids, top_k, rotations, VIEW_VERSION])
    matches = []
    for target, candidates in zip(target_rows, retained):
        for rank, (distance, tile) in enumerate(candidates, 1):
            matches.append({"run_id": run_id, "target_id": target["target_id"], "slot": target["slot"], "rank": rank,
                            "raw_mse": distance / (matrix.shape[1] * 255**2), **tile,
                            "rotation_crop": tile.get("rotation_crop", rotation_recipe(index_manifest["canonical_size"], 0)),
                            "rotation_direction": "clockwise", "view_version": VIEW_VERSION,
                            "view_source_window": [float(v) for v in tile.get("view_source_window", tile.get("window", []))],
                            "source_footprint_json": tile.get("source_footprint_json", tile.get("footprint_json")),
                            "base_feature_row": tile.get("base_feature_row", tile["feature_row"]),
                            "view_feature_row": tile.get("view_feature_row")})
    pq.write_table(pa.Table.from_pylist(matches), output / "matches.parquet", compression="zstd")
    best = [entry[0][0] / (matrix.shape[1] * 255**2) for entry in retained]
    manifest = {"run_id": run_id,
                "target_set_id": target_manifest["target_set_id"], "index_id": index_manifest["index_id"],
                "shard_ids": shard_ids, "index_members": [m["index_id"] for _, m, _ in sources],
                "candidate_count": candidate_count, "view_count": view_count, "rotations": list(rotations),
                "view_version": VIEW_VERSION, "top_k": top_k, "target_count": len(target_rows),
                "metric": "gray-mse-[0,1]-v1", "accumulation": "exact int32 SSE, scaled to MSE",
                "tie_break": "view_id ascending", "best_mse_mean": float(np.mean(best)),
                "best_mse_median": float(np.median(best)), "visual_review": "not_performed"}
    write_json(output / "manifest.json", manifest)
    return manifest
