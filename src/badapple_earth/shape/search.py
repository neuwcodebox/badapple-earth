"""Full-view exact retrieval, complete coarse/fine geometry, shape-first choice."""

from concurrent.futures import ProcessPoolExecutor
from functools import lru_cache
import json
from pathlib import Path
from multiprocessing import get_context
import sqlite3
import time

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
import scipy
import numba

from ..collection.windows import identity
from ..collection.storage import writer_lock
from ..evidence import write_json
from .features import VERSION
from .geometry import CHAMFER_BACKEND, EDT_BACKEND, score, select, target, tone
from .cache import MaskCache
from .index import Store

DEFAULT = {"retrieval_k": 2048, "coarse_k": 256, "path_keep": 64,
           "shape_tolerance": .01, "min_contrast": .03, "workers": 8,
           "uniform_energy_max": .005, "uniform_variance_max": .0025,
           "query_batch": 16, "vector_batch": 8192}


def init_worker(index_root, target_root):
    global _store, _target_root, _target_rows, _mask_cache
    _store = Store(Path(index_root))
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    _target_root = Path(target_root)
    _target_rows = pq.read_table(_target_root / "targets.parquet").to_pylist()
    _mask_cache = MaskCache()


@lru_cache(maxsize=2)
def worker_target(slot):
    with Image.open(_target_root / _target_rows[slot]["frame"]) as image:
        return target(np.asarray(image.convert("L")))


def worker_score(task):
    slot, ref, fine = task
    prepared = worker_target(slot)
    gray = _store.gray(ref)
    result = score(gray, prepared, fine, _mask_cache)
    if fine:
        result.update(tone(gray, prepared))
    else:
        result = result["D_coarse"]
    return ref, result


def topk(scores, ids, k):
    k = min(k, len(scores))
    if not k:
        return np.empty(0, int)
    cutoff = np.partition(scores, len(scores) - k)[len(scores) - k]
    candidates = np.flatnonzero(scores >= cutoff)
    return candidates[np.lexsort((ids[candidates], -scores[candidates]))[:k]]


def retrieve(store, prepared, config, output):
    """Each channel gets an independent exact global Top-K, including ties."""
    tasks = []
    for slot, item in enumerate(prepared):
        if not item["near_uniform"]:
            tasks.append((slot, "phog", "phog", item["phog"]))
            tasks.extend((slot, f"rank-{item['rank_query_levels'][level]:g}", "rank", query) for level, query in enumerate(item["rank_queries"]))
    retained = [[] for _ in tasks]
    started = time.monotonic()
    for number, descriptor in enumerate(store.manifest["shards"]):
        shard = descriptor["shard_id"]
        ids = store.array(shard, "ids.npy")
        for kind in ["phog", "rank"]:
            selected = [i for i, t in enumerate(tasks) if t[2] == kind]
            if not selected:
                continue
            data = store.array(shard, kind + ".npy")
            if len(data) != descriptor["rows"] or len(ids) != len(data) or data.dtype != np.float32:
                raise ValueError("Shape descriptor row mapping or dtype differs")
            for qbegin in range(0, len(selected), config["query_batch"]):
                query_ids = selected[qbegin:qbegin + config["query_batch"]]
                queries = np.stack([tasks[i][3] for i in query_ids]).astype(np.float32)
                for begin in range(0, len(data), config["vector_batch"]):
                    batch = np.asarray(data[begin:begin + config["vector_batch"]])
                    scores = queries @ batch.T
                    for query_id, values in zip(query_ids, scores):
                        keep = topk(values, ids[begin:begin+len(batch)], config["retrieval_k"])
                        retained[query_id].extend((float(values[i]), bytes(ids[begin+i]).decode(), shard, begin+int(i)) for i in keep)
                        retained[query_id].sort(key=lambda e: (-e[0], e[1]))
                        del retained[query_id][config["retrieval_k"]:]
        if (number + 1) % 100 == 0:
            print(f"Shape retrieval: {number+1}/{len(store.manifest['shards'])} shards", flush=True)
    unions = [dict() for _ in prepared]
    for (slot, name, kind, query), entries in zip(tasks, retained):
        for rank, (value, view_id, shard, position) in enumerate(entries, 1):
            row = unions[slot].setdefault((shard, position), {"view_id": view_id, "retrieved_by": [], "path_ranks": {},
                                                           "phog_score": None, "rank_scores": {}})
            row["retrieved_by"].append(name)
            row["path_ranks"][name] = rank
            if kind == "phog":
                row["phog_score"] = value
            else:
                k, n = int(-query.sum()), len(query)
                row["rank_scores"][name] = {"inner_product": value, "auc_dark": .5 + n*value/(k*(n-k))}
    serializable = []
    for slot, union in enumerate(unions):
        for (shard, position), row in union.items():
            serializable.append({"slot": slot, "shape_shard": shard, "feature_row": position,
                                 "view_id": row["view_id"], "retrieved_by": row["retrieved_by"],
                                 "phog_score": row["phog_score"], "rank_scores_json": json.dumps(row["rank_scores"]),
                                 "path_ranks_json": json.dumps(row["path_ranks"])})
    if serializable:
        pq.write_table(pa.Table.from_pylist(serializable), output / "candidates.parquet", compression="zstd")
    write_json(output / "retrieval.json", {"index_id": store.manifest["index_id"], "feature_version": VERSION,
                                          "config": config, "elapsed_seconds": time.monotonic()-started,
                                          "candidate_counts": [len(u) for u in unions], "retrieval_truncated": True,
                                          "backend": "numpy-float32-exact", "tie_break": "view_id including kth-score ties"})
    return unions


def load_retrieval(output, count):
    unions = [dict() for _ in range(count)]
    path = output / "candidates.parquet"
    if not path.exists():
        return unions
    for row in pq.read_table(path).to_pylist():
        unions[row["slot"]][(row["shape_shard"], row["feature_row"])] = {
            "view_id": row["view_id"], "retrieved_by": row["retrieved_by"], "phog_score": row["phog_score"],
            "rank_scores": json.loads(row["rank_scores_json"]), "path_ranks": json.loads(row["path_ranks_json"])}
    return unions


def uniform_choices(store, prepared, config):
    accepted, relaxed = [], []
    target_mean = float(prepared["original"].mean())
    for shard in store.manifest["shards"]:
        for position, row in enumerate(store.rows(shard["shard_id"])):
            record = {"ref": (shard["shard_id"], position), "view_id": row["view_id"],
                      "D_tone": abs(row["luma_mean"] - target_mean), "D_shape": 0., "D_sdf": 0.,
                      "D_chamfer": 0., "D_region": 0., "D_coarse": 0., "contrast_delta": 0.,
                      "chosen_thresholds": [], "scale_scores": [], "retrieved_by": ["uniform-full-scan"],
                      "phog_score": None, "rank_scores": {}, "path_ranks": {}}
            if row["structure_energy"] <= config["uniform_energy_max"] and row["luma_variance"] <= config["uniform_variance_max"]:
                accepted.append(record)
                accepted.sort(key=lambda r: (r["D_tone"], r["view_id"])); del accepted[20:]
            relaxed.append((row["structure_energy"], row["luma_variance"], record))
            relaxed.sort(key=lambda e: (e[0], e[1], e[2]["D_tone"], e[2]["view_id"])); del relaxed[20:]
    rows = accepted or [row[2] for row in relaxed]
    return rows, {**rows[0], "low_visibility": not bool(accepted), "uniform_threshold_unmet": not bool(accepted),
                  "eligible_count": len(accepted)}


def match(target_root: Path, index_root: Path, output: Path, config=None, stop_after_targets=None):
    with writer_lock(output):
        return _match(target_root, index_root, output, config, stop_after_targets)


def _match(target_root, index_root, output, config, stop_after_targets):
    config = {**DEFAULT, **(config or {})}
    for name in ["retrieval_k", "coarse_k", "path_keep", "workers", "query_batch", "vector_batch"]:
        if type(config[name]) is not int or config[name] < 1:
            raise ValueError(f"{name} must be a positive integer")
    store = Store(index_root)
    if store.manifest["status"] != "complete":
        raise ValueError("Shape analysis index is incomplete")
    target_manifest = json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))
    if target_manifest["canonical_size"] != store.manifest["canonical_size"]:
        raise ValueError("Shape targets and candidates have different sizes")
    output.mkdir(parents=True, exist_ok=True)
    (output / "scores").mkdir(exist_ok=True)
    run_id = identity([store.manifest["index_id"], target_manifest["target_set_id"], VERSION, config])
    manifest_path = output / "manifest.json"
    manifest = {"run_id": run_id, "index_id": store.manifest["index_id"], "feature_version": VERSION,
                "target_set_id": target_manifest["target_set_id"], "config": config, "metric": "shape-sdf-directional-chamfer-region-v2.1",
                "candidate_count": store.manifest["places"], "view_count": store.manifest["view_count"],
                "target_count": target_manifest["count"], "rotations": store.manifest["rotations"],
                "view_version": store.manifest["view_version"], "status": "matching", "numpy_version": np.__version__,
                "scipy_version": scipy.__version__, "numba_version": numba.__version__,
                "backend": "numpy-scipy-numba-cpu; fused exact EDT and capped-grid Chamfer",
                "edt_backend": EDT_BACKEND,
                "chamfer_backend": CHAMFER_BACKEND, "mask_cache_payload_bytes_per_worker": 128*1024**2,
                "selection": "visibility, shape tolerance, tone, shape, view_id", "visual_review": "not_performed"}
    if manifest_path.exists() and json.loads(manifest_path.read_text())["run_id"] != run_id:
        raise ValueError("Run configuration differs; use another output")
    write_json(manifest_path, manifest)
    target_rows = pq.read_table(target_root / "targets.parquet").to_pylist()
    prepared = []
    for row in target_rows:
        with Image.open(target_root / row["frame"]) as image:
            prepared.append(target(np.asarray(image.convert("L"))))
    retrieval_path = output / "retrieval.json"
    if retrieval_path.exists():
        retrieval = json.loads(retrieval_path.read_text())
        if retrieval["index_id"] != store.manifest["index_id"] or retrieval["config"] != config:
            raise ValueError("Retrieval checkpoint configuration differs")
        unions = load_retrieval(output, len(prepared))
    else:
        unions = retrieve(store, prepared, config, output)
    db = sqlite3.connect(output / "progress.sqlite")
    db.execute("CREATE TABLE IF NOT EXISTS targets (slot INTEGER PRIMARY KEY, state TEXT, error_type TEXT)")
    db.execute("UPDATE targets SET state='PENDING' WHERE state='RUNNING'"); db.commit()
    started = time.monotonic()
    processed = 0
    try:
        with ProcessPoolExecutor(max_workers=config["workers"], mp_context=get_context("spawn"),
                                 initializer=init_worker, initargs=(str(index_root), str(target_root))) as executor:
            for slot, (item, source_target, union) in enumerate(zip(prepared, target_rows, unions)):
                final_path = output / "scores" / f"{slot:04d}.json"
                if final_path.exists():
                    prior = json.loads(final_path.read_text())
                    if "edt_backend" not in prior:
                        prior["edt_backend"] = "scipy-distance-transform-edt"
                        prior["chamfer_backend"] = "scipy-cKDTree-exact-capped"
                        write_json(final_path, prior)
                    db.execute("INSERT OR REPLACE INTO targets VALUES (?, 'DONE', NULL)", (slot,)); db.commit()
                    continue
                target_started = time.monotonic()
                db.execute("INSERT OR REPLACE INTO targets VALUES (?,?,?)", (slot, "RUNNING", None)); db.commit()
                if item["near_uniform"]:
                    ranked, chosen = uniform_choices(store, item, config)
                    write_json(output / "scores" / f"{slot:04d}-fine.json", ranked)
                    coarse_count = fine_count = len(ranked)
                else:
                    coarse_path = output / "scores" / f"{slot:04d}-coarse.parquet"
                    if coarse_path.exists():
                        coarse_rows = pq.read_table(coarse_path).to_pylist()
                        coarse = {(r["shape_shard"], r["feature_row"]): r["D_coarse"] for r in coarse_rows}
                    else:
                        refs = sorted(union)  # Process neighbouring views from the same RGB shard together.
                        print(f"Shape screening {slot+1}/{len(prepared)}: {len(refs)} views", flush=True)
                        coarse = dict(executor.map(worker_score, ((slot, ref, False) for ref in refs), chunksize=16))
                        temporary = coarse_path.with_suffix(".tmp")
                        pq.write_table(pa.Table.from_pylist([{"shape_shard": ref[0], "feature_row": ref[1],
                                                            "view_id": union[ref]["view_id"], "D_coarse": value}
                                                           for ref, value in coarse.items()]), temporary, compression="zstd")
                        temporary.replace(coarse_path)
                    ordered = sorted(coarse, key=lambda r: (coarse[r], union[r]["view_id"]))
                    fine_refs = set(ordered[:config["coarse_k"]])
                    fine_refs.update(ref for ref, row in union.items() if any(rank <= config["path_keep"] for rank in row["path_ranks"].values()))
                    fine_refs = sorted(fine_refs)
                    fine_path = output / "scores" / f"{slot:04d}-fine.json"
                    if fine_path.exists():
                        ranked = json.loads(fine_path.read_text())
                    else:
                        print(f"Shape fine {slot+1}/{len(prepared)}: {len(fine_refs)} views", flush=True)
                        ranked = [{"ref": list(ref), **union[ref], **metrics, "D_coarse": coarse[ref]}
                                  for ref, metrics in executor.map(worker_score, ((slot, ref, True) for ref in fine_refs), chunksize=4)]
                        write_json(fine_path, ranked)
                    chosen = select(ranked, config["shape_tolerance"], config["min_contrast"])
                    coarse_count, fine_count = len(coarse), len(ranked)
                result = {"slot": slot, "target_id": source_target["target_id"], "target_pts": source_target["source_pts"],
                          "edt_backend": EDT_BACKEND,
                          "chamfer_backend": CHAMFER_BACKEND,
                          "target_time": source_target["target_time"], "chosen": chosen,
                          "target_profile": {k: item[k] for k in ["near_uniform", "dark_fraction", "luma_range", "normalization", "gray_fraction", "levels", "rank_query_levels"]},
                          "candidate_count": coarse_count, "fine_count": fine_count,
                          "elapsed_seconds": time.monotonic()-target_started,
                          "retrieval_truncated": not item["near_uniform"] and coarse_count < store.manifest["view_count"],
                          "rerank_truncated": not item["near_uniform"] and fine_count < coarse_count}
                write_json(final_path, result)
                db.execute("UPDATE targets SET state='DONE',error_type=NULL WHERE slot=?", (slot,)); db.commit()
                print(f"Shape match {slot+1}/{len(prepared)}: {coarse_count} retrieved, {fine_count} fine; D_shape={chosen['D_shape']:.5f}, contrast={chosen['contrast_delta']:.4f}", flush=True)
                processed += 1
                if stop_after_targets is not None and processed >= stop_after_targets:
                    manifest.update({"status": "paused", "completed_targets": slot+1})
                    return manifest
        results = [json.loads((output / "scores" / f"{slot:04d}.json").read_text()) for slot in range(len(prepared))]
        manifest.update({"status": "complete", "low_visibility_count": sum(r["chosen"]["low_visibility"] for r in results),
                         "completed_targets": len(results), "edt_backends_by_slot": {str(r['slot']): r['edt_backend'] for r in results},
                         "chamfer_backends_by_slot": {str(r['slot']): r.get('chamfer_backend','scipy-cKDTree-exact-capped') for r in results},
                         "near_uniform_count": sum(r["target_profile"]["near_uniform"] for r in results),
                         "selected_shape_mean": float(np.mean([r["chosen"]["D_shape"] for r in results]))})
        return manifest
    except BaseException as error:
        db.execute("UPDATE targets SET state='FAILED',error_type=? WHERE state='RUNNING'", (type(error).__name__,)); db.commit()
        manifest["status"] = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
        raise
    finally:
        manifest["elapsed_seconds_last_run"] = time.monotonic()-started
        write_json(manifest_path, manifest)
        db.close()
