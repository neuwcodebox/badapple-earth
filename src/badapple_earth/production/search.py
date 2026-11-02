"""Packed exact MSE search, stable Top-K and bounded resumable target batches."""

from collections import defaultdict
import json
import os
from pathlib import Path
import time

from numba import njit, prange
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ..collection.bundle import members, read_manifest
from ..collection.storage import writer_lock
from ..collection.windows import identity
from ..evidence import write_json
from ..matching.distance import dot_dtype
from ..matching.views import VIEW_VERSION, prepare_views, rotation_recipe

VERSION = "packed-exact-mse-v1"


def pack(index: Path, output: Path):
    source = read_manifest(index)
    cache_id = identity([source["index_id"], VERSION, VIEW_VERSION])
    with writer_lock(output):
        path = output / "manifest.json"
        saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if saved and saved["cache_id"] != cache_id:
            raise ValueError("Packed index belongs to a different snapshot")
        if saved and saved["status"] == "complete":
            return saved
        sources = members(index, source)
        total = 4 * sum(m["rows"] - len(excluded) for _, m, excluded in sources)
        dimension = int(np.prod(source["coarse_shape"]))
        dtype = dot_dtype(dimension)
        shapes = {"features": (dtype, (total, dimension)), "norms": (np.int32, (total,)),
                  "ids": ("S64", (total,)), "source_rows": (np.int32, (total,))}
        arrays = {key: np.lib.format.open_memmap(output / f"{key}.npy", mode="r+" if saved else "w+", dtype=dt, shape=shape)
                  for key, (dt, shape) in shapes.items()}
        descriptions = []
        for root, manifest, excluded in sources:
            descriptions.extend((root / "shards" / s["shard_id"], False, manifest["index_id"], excluded)
                                for s in manifest["shards"] if s["rows"])
            view_root, views = prepare_views(root, manifest)
            descriptions.extend(((root / "shards" / s["source_shard"] / "views") if s.get("inline")
                                 else view_root / s["source_shard"], True, manifest["index_id"], excluded)
                                for s in views["shards"])
        state = saved or {"schema": VERSION, "cache_id": cache_id, "index_id": source["index_id"],
                          "status": "building", "rows": total, "dimension": dimension,
                          "places": total // 4, "next_descriptor": 0, "written": 0, "blocks": []}
        for number in range(state["next_descriptor"], len(descriptions)):
            directory, is_view, member, excluded = descriptions[number]
            metadata = directory / ("views.parquet" if is_view else "tiles.parquet")
            columns = set(pq.ParquetFile(metadata).schema_arrow.names)
            rows = pq.read_table(metadata, columns=[c for c in ("tile_id", "view_id") if c in columns]).to_pylist()
            positions = np.array([i for i, row in enumerate(rows) if row["tile_id"] not in excluded], dtype=np.int32)
            begin, end = state["written"], state["written"] + len(positions)
            data = np.load(directory / "gray_coarse.npy", mmap_mode="r", allow_pickle=False)
            if data.dtype != np.uint8 or data.shape != (len(rows), *source["coarse_shape"]):
                raise ValueError("Source feature rows, shape or dtype disagree with the snapshot")
            pixels = data[positions].reshape(len(positions), dimension)
            arrays["features"][begin:end] = pixels
            numeric = pixels.astype(np.int32)
            arrays["norms"][begin:end] = np.sum(numeric * numeric, axis=1)
            arrays["source_rows"][begin:end] = positions
            arrays["ids"][begin:end] = [rows[i].get("view_id", rows[i]["tile_id"]) for i in positions]
            state["blocks"].append({"start": begin, "stop": end, "member": member, "is_view": is_view,
                                    "path": Path(os.path.relpath(metadata, output)).as_posix()})
            state.update(written=end, next_descriptor=number + 1)
            if (number + 1) % 100 == 0 or number + 1 == len(descriptions):
                for array in arrays.values():
                    array.flush()
                write_json(path, state)
                print(f"Packed views: {end:,}/{total:,}", flush=True)
        if state["written"] != total:
            raise ValueError("Packed view count differs from the snapshot")
        order = np.argsort(arrays["ids"], kind="stable")
        tie_rank = np.lib.format.open_memmap(output / "tie_rank.npy", mode="w+", dtype=np.int32, shape=(total,))
        tie_rank[order] = np.arange(total, dtype=np.int32)
        tie_rank.flush()
        state["status"] = "complete"
        write_json(path, state)
        return state


@njit(cache=True, parallel=True)
def update_topk(dot, target_norm, candidate_norm, ties, offset, scores, positions, ranks):
    for row in prange(dot.shape[0]):
        k = scores.shape[1]
        for col in range(dot.shape[1]):
            value = target_norm[row] + candidate_norm[col] - 2 * int(dot[row, col])
            tie = ties[col]
            if value > scores[row, k - 1] or (value == scores[row, k - 1] and tie >= ranks[row, k - 1]):
                continue
            dest = k - 1
            while dest > 0 and (value < scores[row, dest - 1] or
                               (value == scores[row, dest - 1] and tie < ranks[row, dest - 1])):
                scores[row, dest], positions[row, dest], ranks[row, dest] = scores[row, dest - 1], positions[row, dest - 1], ranks[row, dest - 1]
                dest -= 1
            scores[row, dest], positions[row, dest], ranks[row, dest] = value, offset + col, tie


def scan(targets, features, norms, ties, k=20, candidate_batch=65536):
    dtype = features.dtype
    matrix = np.ascontiguousarray(targets.reshape(len(targets), -1), dtype=dtype)
    integers = matrix.astype(np.int32)
    target_norm = np.sum(integers * integers, axis=1).astype(np.int32)
    k = min(k, len(features))
    scores = np.full((len(matrix), k), np.iinfo(np.int32).max, np.int32)
    positions = np.full(scores.shape, -1, np.int64)
    ranks = np.full(scores.shape, np.iinfo(np.int32).max, np.int32)
    for begin in range(0, len(features), candidate_batch):
        end = min(begin + candidate_batch, len(features))
        dot = matrix @ features[begin:end].T
        update_topk(dot, target_norm, norms[begin:end], ties[begin:end], begin, scores, positions, ranks)
    return scores, positions


def resolve(cache: Path, manifest, positions):
    blocks = manifest["blocks"]
    stops = np.array([b["stop"] for b in blocks])
    row_map = np.load(cache / "source_rows.npy", mmap_mode="r", allow_pickle=False)
    groups = defaultdict(list)
    for position in np.unique(positions):
        groups[int(np.searchsorted(stops, position, side="right"))].append(int(position))
    result = {}
    for block_number, indices in groups.items():
        block = blocks[block_number]
        # Convert only retrieved rows to Python, rather than every metadata
        # field of every unselected view in a source shard.
        offsets = pa.array([int(row_map[position]) for position in indices], type=pa.int64())
        rows = pq.read_table(cache / block["path"]).take(offsets).to_pylist()
        for position, tile in zip(indices, rows):
            tile.update(index_member=block["member"], dense_row=position,
                        view_id=tile.get("view_id", tile["tile_id"]),
                        rotation_degrees=tile.get("rotation_degrees", 0),
                        rgb_cache_ref=tile.get("rgb_cache_ref"), rgb_cache_row=tile.get("rgb_cache_row"),
                        rotation_crop=tile.get("rotation_crop"), base_feature_row=tile.get("base_feature_row", tile["feature_row"]),
                        view_source_window=[float(v) for v in tile.get("view_source_window", tile.get("window", []))],
                        source_footprint_json=tile.get("source_footprint_json", tile.get("footprint_json")),
                        view_feature_row=tile.get("view_feature_row"), view_version=VIEW_VERSION,
                        rotation_direction="clockwise")
            result[position] = tile
    return result


def search(target_root: Path, index_root: Path, cache: Path, output: Path, k=20, target_batch=128, stop_after_batches=None):
    manifest = pack(index_root, cache)
    target_manifest = read_manifest(target_root)
    output.mkdir(parents=True, exist_ok=True)
    run_id = identity([target_manifest["target_set_id"], manifest["cache_id"], k])
    done = output / "manifest.json"
    if done.exists():
        saved = read_manifest(output)
        if saved["run_id"] != run_id:
            raise ValueError("Production search belongs to a different target/index")
        return saved
    checkpoint = output / "search-state.json"
    state = json.loads(checkpoint.read_text(encoding="utf-8")) if checkpoint.exists() else {"run_id": run_id, "completed": []}
    if state["run_id"] != run_id:
        raise ValueError("Search checkpoint belongs to another run")
    target_pixels = np.load(target_root / "gray_coarse.npy", allow_pickle=False)
    unique, inverse = np.unique(target_pixels.reshape(len(target_pixels), -1), axis=0, return_inverse=True)
    features, norms, ties = [np.load(cache / f"{key}.npy", mmap_mode="r", allow_pickle=False)
                             for key in ("features", "norms", "tie_rank")]
    if unique.shape[1] != manifest["dimension"]:
        raise ValueError("Target and packed feature dimensions differ")
    (output / "search-batches").mkdir(exist_ok=True)
    new = 0
    for begin in range(0, len(unique), target_batch):
        end = min(len(unique), begin + target_batch)
        filename = output / "search-batches" / f"{begin:06d}-{end:06d}.npz"
        if filename.name in state["completed"]:
            continue
        started = time.perf_counter()
        scores, positions = scan(unique[begin:end], features, norms, ties, k)
        temporary = filename.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream, scores=scores, positions=positions)
        temporary.replace(filename)
        state["completed"].append(filename.name)
        write_json(checkpoint, state)
        print(f"Exact MSE: {end:,}/{len(unique):,} unique queries, {time.perf_counter()-started:.2f}s/batch; all {len(inverse):,} source frames retained", flush=True)
        new += 1
        if stop_after_batches and new >= stop_after_batches:
            return {"status": "paused"}
    batches = [np.load(output / "search-batches" / f"{begin:06d}-{min(len(unique), begin+target_batch):06d}.npz", allow_pickle=False)
               for begin in range(0, len(unique), target_batch)]
    scores, positions = [np.concatenate([batch[key] for batch in batches]) for key in ("scores", "positions")]
    for batch in batches:
        batch.close()
    np.save(output / "query_inverse.npy", inverse, allow_pickle=False)
    tiles = resolve(cache, manifest, positions)
    rows = []
    size = target_manifest["canonical_size"]
    for query, (sse, choices) in enumerate(zip(scores, positions)):
        for rank, (score, position) in enumerate(zip(sse, choices), 1):
            row = {**tiles[int(position)], "query": query, "rank": rank, "sse": int(score),
                   "raw_mse": int(score) / (manifest["dimension"] * 255**2)}
            row["rotation_crop"] = row["rotation_crop"] or rotation_recipe(size, row["rotation_degrees"])
            rows.append(row)
    pq.write_table(pa.Table.from_pylist(rows), output / "matches.parquet", compression="zstd")
    result = {"run_id": run_id, "status": "complete", "target_set_id": target_manifest["target_set_id"],
              "index_id": manifest["index_id"], "candidate_count": manifest["places"], "view_count": manifest["rows"],
              "source_frames": len(inverse), "unique_queries": len(unique), "top_k": k,
              "metric": "gray-mse-[0,1]-v1", "accumulation": "integer-exact BLAS dot then integer SSE",
              "tie_break": "view_id ascending", "view_version": VIEW_VERSION,
              "rotations": [0, 90, 180, 270], "best_mse_mean": float(np.mean(scores[inverse, 0])) / (manifest["dimension"] * 255**2)}
    write_json(done, result)
    return result
