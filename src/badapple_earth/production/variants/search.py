"""Consume published feature groups, retaining exact Top-K for two view pools."""

from pathlib import Path
import time

import numpy as np

from ...collection.bundle import read_manifest
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.distance import dot_dtype

VERSION = "affine-published-groups-exact-two-pools-v1"


def empty_state(queries, k):
    shape = (2, queries, k)
    return {"scores": np.full(shape, np.iinfo(np.int32).max, np.int32),
            "positions": np.full(shape, -1, np.int64),
            "ranks": np.full(shape, np.iinfo(np.int32).max, np.int32)}


def update_pool(sse, ties, indices, state, route, begin, end):
    """Exact lexicographic SSE/tie order in int64; no runtime JIT is needed."""
    k = state["scores"].shape[-1]
    # int32 SSE/rank values, including the sentinel, safely fit this product.
    stride = np.int64(np.iinfo(np.int32).max)+1
    keys = sse.astype(np.int64)*stride+ties[None,:]
    previous = state["scores"][route,begin:end].astype(np.int64)*stride+state["ranks"][route,begin:end]
    keys = np.concatenate((previous,keys),axis=1)
    selected = np.argpartition(keys,k-1,axis=1)[:,:k]
    chosen = np.take_along_axis(keys,selected,axis=1)
    order = np.argsort(chosen,axis=1,kind="stable")
    selected = np.take_along_axis(selected,order,axis=1)
    chosen = np.take_along_axis(chosen,order,axis=1)
    positions = np.concatenate((state["positions"][route,begin:end],
                                np.broadcast_to(indices,(end-begin,len(indices)))),axis=1)
    state["scores"][route,begin:end] = chosen//stride
    state["ranks"][route,begin:end] = chosen%stride
    state["positions"][route,begin:end] = np.take_along_axis(positions,selected,axis=1)


def scan_group(targets, values, ties, specification, start, stop, state, candidate_batch, target_batch):
    matrix = np.ascontiguousarray(targets.reshape(len(targets), -1), dtype=dot_dtype(768))
    norms = np.sum(matrix.astype(np.int32)**2, axis=1, dtype=np.int32)
    full = np.array([v["scale"] == 1 for v in specification])
    for begin in range(start, stop, candidate_batch):
        end = min(begin+candidate_batch, stop)
        raw = np.asarray(values[begin:end]).reshape(end-begin, -1)
        if raw.shape[1] != matrix.shape[1]:
            raise ValueError("Affine and target feature dimensions disagree")
        candidates = np.asarray(raw, dtype=matrix.dtype)
        cn = np.sum(raw.astype(np.int32)**2, axis=1, dtype=np.int32)
        mask = full[np.arange(begin, end) % len(full)]
        for q in range(0, len(matrix), target_batch):
            last = min(q+target_batch, len(matrix))
            dot = matrix[q:last] @ candidates.T
            sse = norms[q:last,None]+cn[None,:]-2*dot.astype(np.int32)
            indices = np.arange(begin,end,dtype=np.int64)
            ranks = np.asarray(ties[begin:end])
            update_pool(sse,ranks,indices,state,1,q,last)
            update_pool(sse[:,mask],ranks[mask],indices[mask],state,0,q,last)
    return state


def search(bank, targets, slots, root, config):
    """A checkpoint includes both routes and exactly the consumed group IDs."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    unique, inverse = np.unique(targets.reshape(len(targets), -1), axis=0, return_inverse=True)
    search_id = identity([VERSION, bank.state["bank_id"], slots, unique.tolist(), config["top_k"]])
    path = root/"retrieval.npz"
    state, completed, elapsed = empty_state(len(unique), config["top_k"]), set(), 0.
    if path.exists():
        with np.load(path, allow_pickle=False) as saved:
            if str(saved["search_id"]) != search_id:
                raise ValueError("Affine search checkpoint belongs to different inputs")
            state = {k: saved[k].copy() for k in state}
            completed = set(saved["completed"].tolist())
            elapsed = float(saved["elapsed_seconds"])
    started = time.perf_counter()
    n = len(bank.specification)
    while True:
        published = read_manifest(bank.root)
        if published["bank_id"] != bank.state["bank_id"]:
            raise ValueError("Feature bank changed during retrieval")
        ready = sorted(set(published["completed"])-completed)
        for number in ready:
            job = bank.jobs[number]
            scan_group(unique, bank.values, bank.ties, bank.specification,
                       job[0]["start"]*n, job[-1]["stop"]*n, state,
                       config["candidate_batch"], config["target_batch"])
            completed.add(number)
            temporary = path.with_suffix(".tmp")
            with temporary.open("wb") as stream:
                np.savez(stream, **state, search_id=search_id, completed=sorted(completed), inverse=inverse,
                         elapsed_seconds=elapsed+time.perf_counter()-started)
            temporary.replace(path)
            count = sum((bank.jobs[i][-1]["stop"]-bank.jobs[i][0]["start"])*n for i in completed)
            write_json(root/"search-progress.json", {"search_id":search_id,"groups":len(completed),
                       "total_groups":len(bank.jobs),"views":count,"total_views":bank.state["rows"],
                       "unique_queries":len(unique),"elapsed_seconds":round(elapsed+time.perf_counter()-started, 3)})
            if len(completed) % 16 == 0 or len(completed) == len(bank.jobs):
                print(f"Affine exact search: {count:,}/{bank.state['rows']:,} views; {len(unique)} queries", flush=True)
        if len(completed) == len(bank.jobs):
            if published["status"] != "complete":
                raise ValueError("All feature groups must have a published completion marker")
            break
        if published["status"] in {"paused", "failed"}:
            raise RuntimeError(f"Feature generation is {published['status']}; search checkpoint preserved")
        time.sleep(2)
    if np.any(state["positions"] < 0):
        raise ValueError("Every route must have sufficient distinct feature candidates")
    return {**state, "inverse":inverse,"search_id":search_id,"unique_queries":len(unique),
            "elapsed_seconds":round(elapsed+time.perf_counter()-started, 3)}
