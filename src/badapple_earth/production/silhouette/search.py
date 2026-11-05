"""Search the complete frozen view pool with region and outline routes."""

from pathlib import Path
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import time

import numpy as np

from ...collection.bundle import read_manifest
from ...collection.windows import identity
from ...evidence import write_json
from ..correlation.features import multiscale
from .features import VERSION, balanced_weights


def feature_batches(path,begin,stop,batch,workers=3):
    """Bounded shared-memory thread prefetch; results retain their source order."""
    def prepare(start):
        end = min(start+batch,stop)
        source = np.load(path,mmap_mode="r",allow_pickle=False)
        raw = np.asarray(source[start:end],np.float32)/255
        small = raw.reshape(-1,12,2,16,2).mean(axis=(2,4))
        _,gradient = multiscale([small,raw])
        return start,end,raw,gradient
    starts = iter(range(begin,stop,batch))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        pending = deque()
        for _ in range(workers):
            start = next(starts,None)
            if start is not None:
                pending.append(pool.submit(prepare,start))
        while pending:
            result = pending.popleft().result()
            start = next(starts,None)
            if start is not None:
                pending.append(pool.submit(prepare,start))
            yield result


def sources(config):
    result,cursor = [],0
    def packed(spec):
        nonlocal cursor
        cache,fine = Path(spec["cache"]),Path(spec["fine"])
        manifest = read_manifest(cache)
        fm = read_manifest(fine)
        if manifest["status"] != "complete" or fm["status"] != "complete" or fm["source_cache_id"] != manifest["cache_id"]:
            raise ValueError("Silhouette search requires matching completed packed photo views")
        result.append({**spec,"kind":"packed","start":cursor,"rows":manifest["rows"],
                       "identity":fm["cache_id"],"features":str(fine/"gray_fine.npy")})
        cursor += manifest["rows"]
    def affine(path):
        nonlocal cursor
        import json
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
        manifest = read_manifest(Path(spec["bank"]))
        if manifest["status"] != "complete":
            raise ValueError("Silhouette search needs a completed affine bank")
        result.append({"kind":"affine","config":path,"index":spec["index"],"start":cursor,
                       "rows":manifest["rows"],"identity":manifest["bank_id"],
                       "features":str(Path(spec["bank"])/"gray_fine.npy")})
        cursor += manifest["rows"]
    for spec in config["packed_sources"]:
        packed(spec)
    for path in config["affine_configs"]:
        affine(path)
    packed(config["additional_packed"])
    affine(config["additional_affine"])
    expected = read_manifest(Path(config["run"])/"search")
    if cursor != expected["view_count"]:
        raise ValueError("Silhouette sources must exactly cover the completed production view space")
    return result


def keep(scores,positions,dot,indices):
    """Descending float32 score; exact boundary ties use frozen dense address."""
    k = scores.shape[1]
    values = np.concatenate((scores,dot),axis=1)
    addresses = np.concatenate((positions,np.broadcast_to(indices,(len(dot),len(indices)))),axis=1)
    selected = np.argpartition(-values,k-1,axis=1)[:,:k]
    cutoff = np.min(np.take_along_axis(values,selected,axis=1),axis=1)
    for row in np.flatnonzero(np.count_nonzero(values==cutoff[:,None],axis=1)>1):
        better = selected[row][values[row,selected[row]]>cutoff[row]]
        tied = np.flatnonzero(values[row]==cutoff[row])
        tied = tied[np.argsort(addresses[row,tied],kind="stable")[:k-len(better)]]
        selected[row] = np.concatenate((better,tied))
    value = np.take_along_axis(values,selected,axis=1)
    address = np.take_along_axis(addresses,selected,axis=1)
    for row in range(len(dot)):
        order = np.lexsort((address[row],-value[row]))
        scores[row],positions[row] = value[row,order],address[row,order]


def search(run,root,config,targets,specifications):
    slots = [r["slot"] for r in targets]
    gray = np.load(run/"targets/gray_fine.npy",mmap_mode="r",allow_pickle=False)[slots]
    unique,inverse = np.unique(gray.reshape(len(gray),-1),axis=0,return_inverse=True)
    fine = unique.reshape(-1,24,32).astype(np.float32)/255
    coarse = fine.reshape(-1,12,2,16,2).mean(axis=(2,4))
    _,queries = multiscale([coarse,fine])
    weights = np.stack([balanced_weights(image) for image in fine])
    weighted = 2*weights*unique.astype(np.float32)/255
    norms = np.sum(weights*(unique.astype(np.float32)/255)**2,axis=1)
    search_id = identity([VERSION,read_manifest(run/"targets")["target_set_id"],
                          [s["identity"] for s in specifications],slots,config["retrieval_k"]])
    k = config["retrieval_k"]
    scores = np.full((2,len(fine),k),-np.inf,np.float32)
    positions = np.full(scores.shape,-1,np.int64)
    source_begin,offset_begin,previous = 0,0,0.
    path = root/"retrieval.npz"
    if path.exists():
        with np.load(path,allow_pickle=False) as saved:
            if str(saved["search_id"]) != search_id:
                raise ValueError("Silhouette retrieval checkpoint belongs to other features or targets")
            scores,positions = saved["scores"].copy(),saved["positions"].copy()
            source_begin,offset_begin = int(saved["source"]),int(saved["offset"])
            previous = float(saved["elapsed_seconds"])
    started = time.perf_counter()
    total = sum(s["rows"] for s in specifications)
    def checkpoint(source,offset,completed):
        seconds = previous+time.perf_counter()-started
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream,scores=scores,positions=positions,inverse=inverse,search_id=search_id,
                     source=source,offset=offset,elapsed_seconds=seconds)
        temporary.replace(path)
        write_json(root/"progress.json",{"stage":"retrieval","completed_views":completed,"total_views":total,
                   "unique_queries":len(fine),"elapsed_seconds":round(seconds,3),
                   "remaining_seconds":round((total-completed)*seconds/max(completed,1))})
        print(f"Silhouette region + outline: {completed:,}/{total:,} views; {seconds:.1f}s",flush=True)
    for source in range(source_begin,len(specifications)):
        spec = specifications[source]
        values = np.load(spec["features"],mmap_mode="r",allow_pickle=False)
        if values.shape != (spec["rows"],24,32) or values.dtype != np.uint8:
            raise ValueError("Silhouette source feature address or pixel shape differs")
        begin_from = offset_begin if source==source_begin else 0
        for begin,end,raw,gradient in feature_batches(spec["features"],begin_from,len(values),config["candidate_batch"]):
            matrix = raw.reshape(len(raw),-1)
            squared = matrix*matrix
            indices = np.arange(spec["start"]+begin,spec["start"]+end,dtype=np.int64)
            for q in range(0,len(fine),config["target_batch"]):
                stop = min(q+config["target_batch"],len(fine))
                region = weighted[q:stop]@matrix.T-weights[q:stop]@squared.T-norms[q:stop,None]
                outline = queries[q:stop]@gradient.T
                keep(scores[0,q:stop],positions[0,q:stop],region,indices)
                keep(scores[1,q:stop],positions[1,q:stop],outline,indices)
            if begin==begin_from or end==len(values) or (end//config["candidate_batch"])%8==0:
                next_source,next_offset = (source+1,0) if end==len(values) else (source,end)
                checkpoint(next_source,next_offset,spec["start"]+end)
    if np.any(positions<0) or not np.isfinite(scores).all():
        raise ValueError("Each silhouette route needs sufficient valid candidates")
    return {"search_id":search_id,"scores":scores[:,inverse],"positions":positions[:,inverse],
            "unique_queries":len(fine),"elapsed_seconds":round(previous+time.perf_counter()-started,3)}
