"""Stream all saved views with floating-point Top-K, retaining two independent routes."""

from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
import time

from numba import njit, prange
import numpy as np

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from .features import VERSION, multiscale


def read_fine_group(cache_string, blocks, shape):
    cache = Path(cache_string)
    source_rows = np.load(cache / "source_rows.npy", mmap_mode="r", allow_pickle=False)
    start, stop = blocks[0]["start"], blocks[-1]["stop"]
    result = np.empty((stop-start, *shape), dtype=np.uint8)
    for block in blocks:
        positions = source_rows[block["start"]:block["stop"]]
        values = np.load((cache / block["path"]).parent / "gray_fine.npy", mmap_mode="r", allow_pickle=False)
        result[block["start"]-start:block["stop"]-start] = values[positions]
    return start, stop, result


def pack_fine(cache, destination, shape, workers):
    source = read_manifest(cache)
    if source["status"] != "complete":
        raise ValueError("Complete the existing packed view metadata first")
    cache_id = identity([source["cache_id"], "uint8-fine-same-view-order-v1", shape])
    destination.mkdir(parents=True, exist_ok=True)
    with writer_lock(destination):
        path = destination / "manifest.json"
        saved = read_manifest(destination) if path.exists() else None
        if saved and saved["cache_id"] != cache_id:
            raise ValueError("Fine feature cache belongs to different source views")
        if saved and saved["status"] == "complete":
            return saved
        state = saved or {"cache_id": cache_id, "source_cache_id": source["cache_id"],
                          "status": "building", "rows": source["rows"], "shape": list(shape), "completed": []}
        data = np.lib.format.open_memmap(destination / "gray_fine.npy", mode="r+" if saved else "w+",
                                        dtype=np.uint8, shape=(source["rows"], *shape))
        write_json(path, state)
        groups = [source["blocks"][i:i+200] for i in range(0, len(source["blocks"]), 200)]
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
            futures = {pool.submit(read_fine_group, str(cache.resolve()), blocks, shape): i
                       for i, blocks in enumerate(groups) if i not in state["completed"]}
            for future in as_completed(futures):
                begin, end, values = future.result()
                data[begin:end] = values
                state["completed"].append(futures.pop(future))
                if len(state["completed"]) % 10 == 0 or len(state["completed"]) == len(groups):
                    data.flush()
                    write_json(path, state)
                    print(f"Fine RGB-derived features: {len(state['completed'])}/{len(groups)} groups", flush=True)
        data.flush()
        state["status"] = "complete"
        write_json(path, state)
        return state


@njit(cache=True, parallel=True)
def update_topk(dot, ties, offset, scores, positions, ranks):
    """Descending real similarities, with deterministic view-ID tie order."""
    for row in prange(dot.shape[0]):
        k = scores.shape[1]
        for col in range(dot.shape[1]):
            value, tie = dot[row, col], ties[col]
            if value < scores[row, k-1] or (value == scores[row, k-1] and tie >= ranks[row, k-1]):
                continue
            dest = k-1
            while dest > 0 and (value > scores[row, dest-1] or
                               (value == scores[row, dest-1] and tie < ranks[row, dest-1])):
                scores[row, dest], positions[row, dest], ranks[row, dest] = scores[row, dest-1], positions[row, dest-1], ranks[row, dest-1]
                dest -= 1
            scores[row, dest], positions[row, dest], ranks[row, dest] = value, offset+col, tie


def search(run, root, config, targets):
    cache, fine_cache = Path(config["packed_index"]), Path(config["fine_cache"])
    metadata = read_manifest(cache)
    target_manifest = read_manifest(run / "targets")
    raw_fine = np.load(run / "targets/gray_fine.npy", mmap_mode="r", allow_pickle=False)
    pack_fine(cache, fine_cache, raw_fine.shape[1:], config["workers"])
    source_coarse = np.load(cache / "features.npy", mmap_mode="r", allow_pickle=False)
    source_fine = np.load(fine_cache / "gray_fine.npy", mmap_mode="r", allow_pickle=False)
    ties = np.load(cache / "tie_rank.npy", mmap_mode="r", allow_pickle=False)
    slots = np.array([row["slot"] for row in targets])
    coarse = np.load(run / "targets/gray_coarse.npy", mmap_mode="r", allow_pickle=False)[slots]
    fine = raw_fine[slots]
    # Query sharing requires equality of both source arrays, never just 16x12.
    combined = np.concatenate((coarse.reshape(len(slots), -1), fine.reshape(len(slots), -1)), axis=1)
    _, first, inverse = np.unique(combined, axis=0, return_index=True, return_inverse=True)
    queries = multiscale([coarse[first], fine[first]])
    k = min(config["retrieval_k"], len(source_coarse))
    search_id = identity([metadata["cache_id"], target_manifest["target_set_id"], slots.tolist(), k, VERSION])
    path = root / "retrieval.npz"
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            if str(saved["search_id"]) != search_id:
                raise ValueError("Correlation retrieval checkpoint has different inputs")
            zscores, zpos, zranks, gscores, gpos, granks = [saved[key].copy() for key in
                                                        ("zscores", "zpos", "zranks", "gscores", "gpos", "granks")]
            begin = int(saved["next_offset"])
            previous_seconds = float(saved["elapsed_seconds"])
    else:
        shape = (len(first), k)
        zscores, gscores = [np.full(shape, -np.inf, np.float32) for _ in range(2)]
        zpos, gpos = [np.full(shape, -1, np.int32) for _ in range(2)]
        zranks, granks = [np.full(shape, np.iinfo(np.int32).max, np.int32) for _ in range(2)]
        begin, previous_seconds = 0, 0.0
    started = time.perf_counter()
    batch, target_batch = config["candidate_batch"], config["target_batch"]
    for offset in range(begin, len(source_coarse), batch):
        stop = min(offset+batch, len(source_coarse))
        coarse_pixels = np.asarray(source_coarse[offset:stop], dtype=np.uint8).reshape(-1, *coarse.shape[1:])
        candidates = multiscale([coarse_pixels, source_fine[offset:stop]])
        for qbegin in range(0, len(first), target_batch):
            qend = min(qbegin+target_batch, len(first))
            for query, candidate, scores, positions, ranks in zip(
                    queries, candidates, (zscores, gscores), (zpos, gpos), (zranks, granks)):
                dot = query[qbegin:qend] @ candidate.T
                update_topk(dot, ties[offset:stop], offset, scores[qbegin:qend], positions[qbegin:qend], ranks[qbegin:qend])
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream, search_id=search_id, next_offset=stop, inverse=inverse,
                     zscores=zscores, zpos=zpos, zranks=zranks, gscores=gscores, gpos=gpos, granks=granks,
                     elapsed_seconds=previous_seconds+time.perf_counter()-started)
        temporary.replace(path)
        if stop == len(source_coarse) or (offset//batch+1) % 16 == 0:
            print(f"ZNCC + GC retrieval: {stop:,}/{len(source_coarse):,} views, {len(first)} queries", flush=True)
    return {"search_id": search_id, "zpos": zpos[inverse], "gpos": gpos[inverse],
            "zscores": zscores[inverse], "gscores": gscores[inverse], "unique_queries": len(first),
            "elapsed_seconds": round(previous_seconds+time.perf_counter()-started, 3)}
