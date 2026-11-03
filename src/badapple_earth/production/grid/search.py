"""Integer-exact MSE at both grid sizes, streaming the existing packed arrays."""

from pathlib import Path
import time

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.distance import dot_dtype
from ..search import update_topk, pack
from ..correlation.search import pack_fine

VERSION = "mse-grid16-32-wide-f64-exact-v1"


def scan(targets, sources, ties, k, candidate_batch=32768, target_batch=128, state=None, checkpoint=None):
    matrices = [np.ascontiguousarray(t.reshape(len(t), -1), dtype=dot_dtype(int(np.prod(t.shape[1:]))))
                for t in targets]
    if any(m.shape[1]*255**2 > np.iinfo(np.int32).max for m in matrices):
        raise ValueError("MSE grid exceeds the supported integer SSE bound")
    norms = [np.sum(m.astype(np.int32)**2, axis=1, dtype=np.int32) for m in matrices]
    k = min(k, len(ties))
    if state is None:
        state = {"next_offset": 0}
        for route in range(len(matrices)):
            shape = (len(matrices[route]), k)
            state[f"scores{route}"] = np.full(shape, np.iinfo(np.int32).max, np.int32)
            state[f"positions{route}"] = np.full(shape, -1, np.int64)
            state[f"ranks{route}"] = np.full(shape, np.iinfo(np.int32).max, np.int32)
    for begin in range(int(state["next_offset"]), len(ties), candidate_batch):
        end = min(begin+candidate_batch, len(ties))
        for route, (target, source, target_norm) in enumerate(zip(matrices, sources, norms)):
            raw = np.asarray(source[begin:end]).reshape(end-begin, -1)
            if raw.shape[1] != target.shape[1]:
                raise ValueError("Target and candidate MSE grid dimensions disagree")
            candidates = np.asarray(raw, dtype=target.dtype)
            candidate_norm = np.sum(raw.astype(np.int32)**2, axis=1, dtype=np.int32)
            for q in range(0, len(target), target_batch):
                stop = min(q+target_batch, len(target))
                dot = target[q:stop] @ candidates.T
                update_topk(dot, target_norm[q:stop], candidate_norm, ties[begin:end], begin,
                            state[f"scores{route}"][q:stop], state[f"positions{route}"][q:stop],
                            state[f"ranks{route}"][q:stop])
        state["next_offset"] = end
        if checkpoint:
            checkpoint(state)
    return state


def search(run, root, config, targets):
    cache, fine_cache = Path(config["packed_index"]), Path(config["fine_cache"])
    manifest = read_manifest(cache)
    target_manifest = read_manifest(run / "targets")
    raw_fine = np.load(run / "targets/gray_fine.npy", mmap_mode="r", allow_pickle=False)
    fine_manifest = pack_fine(cache, fine_cache, raw_fine.shape[1:], config["workers"])
    slots = np.array([r["slot"] for r in targets])
    coarse = np.load(run / "targets/gray_coarse.npy", mmap_mode="r", allow_pickle=False)[slots]
    fine = np.asarray(raw_fine[slots])
    both = np.concatenate((coarse.reshape(len(slots), -1), fine.reshape(len(slots), -1)), axis=1)
    _, first, inverse = np.unique(both, axis=0, return_index=True, return_inverse=True)
    search_id = identity([manifest["cache_id"], fine_manifest["cache_id"],
                          target_manifest["target_set_id"], slots.tolist(), config["top_k"], VERSION])
    path = root / "retrieval.npz"
    state, previous = None, 0.0
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            if str(saved["search_id"]) != search_id:
                raise ValueError("MSE grid checkpoint has different inputs")
            state = {key: saved[key].copy() for key in saved.files if key.startswith(("scores", "positions", "ranks"))}
            state["next_offset"] = int(saved["next_offset"])
            previous = float(saved["elapsed_seconds"])
    sources = [np.load(cache / "features.npy", mmap_mode="r", allow_pickle=False),
               np.load(fine_cache / "gray_fine.npy", mmap_mode="r", allow_pickle=False)]
    ties = np.load(cache / "tie_rank.npy", mmap_mode="r", allow_pickle=False)
    if len(sources[0]) != len(sources[1]) or len(ties) != manifest["rows"]:
        raise ValueError("Packed grid caches disagree about the source view order")
    started = time.perf_counter()

    def checkpoint(saved):
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream, **saved, search_id=search_id, inverse=inverse,
                     elapsed_seconds=previous+time.perf_counter()-started)
        temporary.replace(path)
        offset = saved["next_offset"]
        if offset == len(ties) or (offset//config["candidate_batch"]) % 16 == 0:
            print(f"Exact MSE 16+32: {offset:,}/{len(ties):,} views, K={config['top_k']}", flush=True)

    state = scan([coarse[first], fine[first]], sources, ties, config["top_k"],
                 config["candidate_batch"], config["target_batch"], state, checkpoint)
    return {"search_id": search_id, "unique_queries": len(first),
            "elapsed_seconds": round(previous+time.perf_counter()-started, 3),
            "positions16": state["positions0"][inverse], "positions32": state["positions1"][inverse],
            "sse16": state["scores0"][inverse], "sse32": state["scores1"][inverse]}


def production_search(target_root, root, config):
    """All-frame fine-grid production; same exact scanner, one retrieval route."""
    if config.get("incremental_base_run"):
        from .incremental import production_search as incremental_search
        return incremental_search(target_root, root, config)
    from ..correlation.pipeline import resolve_metadata
    from ...matching.views import rotation_recipe, VIEW_VERSION
    cache, fine_cache = Path(config["packed_index"]), Path(config["fine_cache"])
    packed, target = pack(Path(config["index"]), cache), read_manifest(target_root)
    pixels = np.load(target_root / "gray_fine.npy", mmap_mode="r", allow_pickle=False)
    fine = pack_fine(cache, fine_cache, pixels.shape[1:], config["rerank_workers"])
    run_id = identity([target["target_set_id"], packed["cache_id"], fine["cache_id"], config["top_k"], VERSION, "fine-production-v1"])
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        saved = read_manifest(root)
        if saved["run_id"] != run_id:
            raise ValueError("Fine production search belongs to a different snapshot or grid")
        return saved
    unique, inverse = np.unique(pixels.reshape(len(pixels), -1), axis=0, return_inverse=True)
    checkpoint_path = root / "retrieval.npz"
    state, previous = None, 0.0
    if checkpoint_path.exists():
        with np.load(checkpoint_path, allow_pickle=False) as saved:
            if str(saved["run_id"]) != run_id:
                raise ValueError("Fine search checkpoint has different inputs")
            state = {key: saved[key].copy() for key in ("scores0", "positions0", "ranks0")}
            state["next_offset"] = int(saved["next_offset"])
            previous = float(saved["elapsed_seconds"])
    candidates = np.load(fine_cache / "gray_fine.npy", mmap_mode="r", allow_pickle=False)
    ties = np.load(cache / "tie_rank.npy", mmap_mode="r", allow_pickle=False)
    if len(candidates) != packed["rows"] or candidates.shape[1:] != pixels.shape[1:]:
        raise ValueError("Fine source features disagree with the production index")
    started = time.perf_counter()
    def checkpoint(saved):
        temporary = checkpoint_path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream, **saved, run_id=run_id, elapsed_seconds=previous+time.perf_counter()-started)
        temporary.replace(checkpoint_path)
        offset = saved["next_offset"]
        if offset == len(ties) or (offset//config["candidate_batch"]) % 16 == 0:
            print(f"Full 32x24 MSE: {offset:,}/{len(ties):,} views, {len(unique):,} queries", flush=True)
    state = scan([unique], [candidates], ties, config["top_k"], config["candidate_batch"],
                 config["target_batch"], state, checkpoint)
    seconds = previous+time.perf_counter()-started
    positions = state["positions0"]
    wanted = pa.array(np.unique(positions).astype(np.int64))
    metadata = {}
    for name in config.get("reuse_scores", []):
        if read_manifest(Path(name))["index_id"] != packed["index_id"]:
            raise ValueError("Reusable view metadata has a different index")
        table = pq.read_table(Path(name) / "candidates.parquet")
        table = table.filter(pc.is_in(table["dense_row"], value_set=wanted))
        metadata.update({r["dense_row"]: r for r in table.to_pylist()})
    if config.get("reuse_run"):
        previous_run = Path(config["reuse_run"])
        previous_search = read_manifest(previous_run / "search")
        if previous_search["index_id"] != packed["index_id"]:
            raise ValueError("Cached metadata has a different index")
        table = pq.read_table(previous_run / "search/matches.parquet")
        _, first = np.unique(table["dense_row"].to_numpy(), return_index=True)
        table = table.take(pa.array(first.astype(np.int64())))
        missing = pa.array(sorted(set(wanted.to_pylist())-set(metadata)), type=pa.int64())
        table = table.filter(pc.is_in(table["dense_row"], value_set=missing))
        metadata.update({r["dense_row"]: r for r in table.to_pylist()})
    reused = len(metadata)
    missing = np.array(sorted(set(wanted.to_pylist())-set(metadata)), dtype=np.int32)
    metadata.update(resolve_metadata(root, cache, missing, config["rerank_workers"]))
    rows, dimension = [], unique.shape[1]
    for query, (scores, choices) in enumerate(zip(state["scores0"], positions)):
        for rank, (sse, position) in enumerate(zip(scores, choices), 1):
            row = {**metadata[int(position)], "query":query, "rank":rank,
                   "sse":int(sse), "raw_mse":int(sse)/(dimension*255**2)}
            row["rotation_crop"] = row.get("rotation_crop") or rotation_recipe(target["canonical_size"], row["rotation_degrees"])
            rows.append(row)
    temporary = root / "matches.tmp.parquet"
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    temporary.replace(root / "matches.parquet")
    np.save(root / "query_inverse.npy", inverse, allow_pickle=False)
    result = {"run_id":run_id, "status":"complete", "target_set_id":target["target_set_id"],
              "index_id":packed["index_id"], "candidate_count":packed["places"], "view_count":packed["rows"],
              "source_frames":len(inverse), "unique_queries":len(unique), "top_k":config["top_k"],
              "grid":[pixels.shape[2],pixels.shape[1]], "dimension":dimension,
              "metric":"gray-mse-[0,1]-v1", "accumulation":"integer-exact float64 BLAS then integer SSE",
              "tie_break":"view_id ascending", "view_version":VIEW_VERSION, "rotations":[0,90,180,270],
              "retrieval_seconds":round(seconds,3), "metadata_reused_views":reused,
              "best_mse_mean":float(np.mean(state["scores0"][inverse,0]))/(dimension*255**2)}
    write_json(root / "manifest.json", result)
    return result
