"""All-frame production using retained quarter turns and complete affine banks."""

from pathlib import Path
import time

import numpy as np
import pyarrow.parquet as pq

from ...collection.bundle import members, read_manifest
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.views import rotation_recipe, VIEW_VERSION
from ...matching.affine import VERSION as AFFINE_VERSION
from ..grid.incremental import merge_topk
from ..search import resolve
from .bank import Bank
from .pipeline import write_table
from .search import empty_state, update_pool

VERSION = "all-frame-affine-expanded-pool-v1"


def scan_complete(targets, values, ties, root, scan_id, config, seed=None):
    """One exact retrieval route, resumable by candidate batch, with cached queries."""
    root.mkdir(parents=True,exist_ok=True)
    seed = seed or {}
    active = np.array([i for i in range(len(targets)) if i not in seed],dtype=np.int64)
    matrix = np.ascontiguousarray(targets.reshape(len(targets),-1)[active],dtype=np.float64)
    norms = np.sum(matrix.astype(np.int32)**2,axis=1,dtype=np.int32)
    state = empty_state(len(active),config["top_k"])
    path = root/"retrieval.npz"
    offset,previous = 0,0.
    if path.exists():
        with np.load(path,allow_pickle=False) as saved:
            if str(saved["scan_id"]) != scan_id:
                raise ValueError("Production affine checkpoint has different inputs")
            offset,previous = int(saved["offset"]),float(saved["elapsed_seconds"])
            for key in state:
                state[key][1] = saved[key]
    started = time.perf_counter()
    for begin in range(offset,len(values),config["candidate_batch"]):
        end = min(begin+config["candidate_batch"],len(values))
        raw = np.asarray(values[begin:end]).reshape(end-begin,-1)
        if raw.shape[1] != matrix.shape[1] or raw.dtype != np.uint8:
            raise ValueError("Production affine features disagree with the target pixels")
        candidates = np.asarray(raw,dtype=np.float64)
        cn = np.sum(raw.astype(np.int32)**2,axis=1,dtype=np.int32)
        indices = np.arange(begin,end,dtype=np.int64)
        ranks = np.asarray(ties[begin:end])
        for q in range(0,len(matrix),config["target_batch"]):
            stop = min(q+config["target_batch"],len(matrix))
            dot = matrix[q:stop] @ candidates.T
            sse = norms[q:stop,None]+cn[None,:]-2*dot.astype(np.int32)
            update_pool(sse,ranks,indices,state,1,q,stop)
        seconds = previous+time.perf_counter()-started
        temporary = path.with_suffix(".tmp")
        with temporary.open("wb") as stream:
            np.savez(stream,**{k:v[1] for k,v in state.items()},offset=end,scan_id=scan_id,elapsed_seconds=seconds)
        temporary.replace(path)
        write_json(root/"progress.json",{"stage":root.name,"completed_views":end,"total_views":len(values),
                   "unique_queries_searched":len(active),"unique_queries_reused":len(seed),
                   "elapsed_seconds":round(seconds,3),"remaining_seconds":round((len(values)-end)*seconds/end)})
        if begin == offset or end == len(values) or (end//config["candidate_batch"])%16 == 0:
            print(f"Production {root.name}: {end:,}/{len(values):,} views; {len(active):,} queries; {seconds:.1f}s",flush=True)
    positions = np.empty((len(targets),config["top_k"]),np.int64)
    scores = np.empty(positions.shape,np.int32)
    positions[active],scores[active] = state["positions"][1],state["scores"][1]
    for query,row in seed.items():
        positions[query],scores[query] = row
    if np.any(positions < 0):
        raise ValueError("Production retrieval must retain Top-K for every unique query")
    return scores,positions


def cached_queries(baseline, comparison, bank, unique, k):
    manifest = read_manifest(comparison)
    if (manifest["status"] != "complete" or manifest["view_space_id"] != bank.state["bank_id"]
            or manifest["target_set_id"] != read_manifest(baseline/"targets")["target_set_id"]
            or manifest["config"]["top_k"] != k):
        raise ValueError("Cached affine query results need the exact bank, targets and Top-K")
    pixels = np.load(baseline/"targets/gray_fine.npy",mmap_mode="r",allow_pickle=False)
    slots = list(range(manifest["first_slot"],manifest["last_slot"]+1))
    local,inverse = np.unique(pixels[slots].reshape(len(slots),-1),axis=0,return_inverse=True)
    lookup = {row.tobytes():i for i,row in enumerate(unique)}
    with np.load(comparison/"retrieval.npz",allow_pickle=False) as saved:
        if not np.array_equal(saved["inverse"],inverse) or len(saved["completed"]) != len(bank.jobs):
            raise ValueError("Cached affine retrieval is incomplete or has different query mapping")
        return {lookup[row.tobytes()]:(saved["positions"][1,i].copy(),saved["scores"][1,i].copy())
                for i,row in enumerate(local)}


def member_mapping(index, full):
    available = {p.resolve():(m,excluded) for p,m,excluded in members(full,read_manifest(full))}
    result = {}
    for path,manifest,excluded in members(index,read_manifest(index)):
        current,current_excluded = available[path.resolve()]
        published = {s["shard_id"]:s for s in current["shards"]}
        if excluded or current_excluded or any(published.get(s["shard_id"]) != s for s in manifest["shards"]):
            raise ValueError("Reused source metadata must be preserved in the full photo pool")
        result[manifest["index_id"]] = current["index_id"]
    return result


def production_search(target_root, root, config):
    started = time.perf_counter()
    root.mkdir(parents=True,exist_ok=True)
    baseline = Path(config["incremental_base_run"])
    base,target = read_manifest(baseline/"search"),read_manifest(target_root)
    full = Path(config["index"])
    full_manifest = read_manifest(full)
    bank_configs = [read_config(p) for p in config["affine_configs"]]
    old_bank = Bank(bank_configs[0]["bank"],bank_configs[0])
    if (base["status"] != "complete" or base["target_set_id"] != target["target_set_id"]
            or base["top_k"] != config["top_k"] or base.get("grid") != [32,24]
            or old_bank.state["parent_index_id"] != base["index_id"] or old_bank.state["status"] != "complete"):
        raise ValueError("Full affine production requires the completed fine-grid baseline and exact parent bank")
    run_id = identity([VERSION,target["target_set_id"],base["run_id"],old_bank.state["bank_id"],config])
    if (root/"manifest.json").exists():
        saved = read_manifest(root)
        if saved["run_id"] != run_id:
            raise ValueError("Full affine production search belongs to other inputs")
        return saved
    pixels = np.load(target_root/"gray_fine.npy",mmap_mode="r",allow_pickle=False)
    unique,inverse = np.unique(pixels.reshape(len(pixels),-1),axis=0,return_inverse=True)
    if not np.array_equal(inverse,np.load(baseline/"search/query_inverse.npy",allow_pickle=False)):
        raise ValueError("Reused baseline query mapping differs from full production targets")
    seed = cached_queries(baseline,Path(config["affine_comparison"]),old_bank,unique,config["top_k"])
    print(f"Full affine production: {len(unique):,} queries; {len(seed)} affine queries reused; baseline quarter turns retained",flush=True)
    sse,positions = scan_complete(unique,old_bank.values,old_bank.ties,root/"existing-affine",
                                  identity([run_id,"existing-affine"]),config,seed)
    offset = base["view_count"]
    metadata = old_bank.resolve(np.unique(positions),offset)
    mapping = member_mapping(Path(bank_configs[0]["index"]),full)
    rows = pq.read_table(baseline/"search/matches.parquet").to_pylist()
    for row in rows:
        row["index_member"] = mapping[row["index_member"]]
    rows = merge_topk(rows,result_rows(metadata,positions,sse,offset),config["top_k"],0)
    offset += old_bank.state["rows"]
    extra = Bank(bank_configs[1]["bank"],bank_configs[1])
    if (extra.state["status"] != "complete" or extra.state["parents"]+old_bank.state["parents"] != full_manifest["rows"]):
        raise ValueError("Additional affine bank must complete all newly collected parents")
    mapping.update(member_mapping(Path(bank_configs[1]["index"]),full))
    cache = Path(config["packed_index"])
    packed = read_manifest(cache)
    fine = np.load(Path(config["fine_cache"])/"gray_fine.npy",mmap_mode="r",allow_pickle=False)
    ties = np.load(cache/"tie_rank.npy",mmap_mode="r",allow_pickle=False)
    if packed["places"] != extra.state["parents"] or len(fine) != 4*extra.state["parents"]:
        raise ValueError("New quarter turns must cover exactly the additional parents")
    scores,choices = scan_complete(unique,fine,ties,root/"additional-quarter",
                                  identity([run_id,packed["cache_id"]]),config)
    new_metadata = {int(p)+offset:{**row,"dense_row":int(p)+offset}
                    for p,row in resolve(cache,packed,choices).items()}
    rows = merge_topk(rows,result_rows(new_metadata,choices,scores,offset),config["top_k"],0)
    offset += len(fine)
    scores,choices = scan_complete(unique,extra.values,extra.ties,root/"additional-affine",
                                  identity([run_id,extra.state["bank_id"]]),config)
    rows = merge_topk(rows,result_rows(extra.resolve(np.unique(choices),offset),choices,scores,offset),config["top_k"],0)
    for row in rows:
        row["index_member"] = mapping.get(row["index_member"],row["index_member"])
        row["rotation_crop"] = row.get("rotation_crop") or rotation_recipe(target["canonical_size"],row["rotation_degrees"])
    write_table(root/"matches.parquet",rows)
    np.save(root/"query_inverse.npy",inverse,allow_pickle=False)
    view_space = identity([base["run_id"],old_bank.state["bank_id"],packed["cache_id"],extra.state["bank_id"]])
    first = {r["query"]:r["raw_mse"] for r in rows if r["rank"] == 1}
    result = {**base,"status":"complete","run_id":run_id,"index_id":full_manifest["index_id"],
              "candidate_count":full_manifest["rows"],"view_count":offset+extra.state["rows"],
              "view_space_id":view_space,"rotations":list(range(0,360,30)),"crop_scales":[1,.75],
              "view_versions":[VIEW_VERSION,old_bank.state.get("transform_version",AFFINE_VERSION)],
              "baseline_run_id":base["run_id"],"existing_places_reused":base["candidate_count"],
              "new_places_searched":extra.state["parents"],
              "baseline_index_id":base["index_id"],"baseline_dense_rows_preserved":True,"baseline_view_count":base["view_count"],
              "preserved_view_spaces":[{"index_id":base["index_id"],"view_space_id":None,"view_count":base["view_count"]},
                 {"index_id":base["index_id"],"view_space_id":old_bank.state["bank_id"],"view_count":base["view_count"]+old_bank.state["rows"]}],
              "affine_cached_queries_reused":len(seed),"retrieval_seconds":round(time.perf_counter()-started,3),
              "best_mse_mean":float(np.mean([first[int(q)] for q in inverse]))}
    for field in ("view_version","incremental_index_id","incremental_version",
                  "baseline_metadata_views_reused","additional_retrieval_seconds"):
        result.pop(field,None)
    if result["view_count"] != 24*result["candidate_count"]:
        raise ValueError("Every full production photo must have all 24 requested views")
    write_json(root/"manifest.json",result)
    return result


def result_rows(metadata, positions, scores, offset=None):
    if offset is None:
        # Existing affine positions follow the preserved baseline view space.
        offset = min(metadata)-int(np.min(positions))
    return [{**metadata[int(p)+offset],"dense_row":int(p)+offset,"query":q,"rank":rank,
             "sse":int(sse),"raw_mse":int(sse)/(768*255**2)}
            for q,(choices,values) in enumerate(zip(positions,scores))
            for rank,(p,sse) in enumerate(zip(choices,values),1)]


def read_config(path):
    import json
    return json.loads(Path(path).read_text(encoding="utf-8"))
