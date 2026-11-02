"""Integer-exact MSE at both grid sizes, streaming the existing packed arrays."""

from pathlib import Path
import time

import numpy as np

from ...collection.bundle import read_manifest
from ...collection.windows import identity
from ...matching.distance import dot_dtype
from ..search import update_topk
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
