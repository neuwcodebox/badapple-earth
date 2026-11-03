"""Exact union Top-K: reuse a completed baseline and search new imagery only."""

from collections import defaultdict
from pathlib import Path
import time

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ...collection.bundle import members, read_manifest
from ...collection.windows import identity
from ...evidence import write_json

VERSION = "fine-mse-immutable-delta-union-v1"


def merge_topk(base_rows, new_rows, k, offset):
    """A union's Top-K is contained in the two constituent Top-K lists."""
    by_query = defaultdict(list)
    for rows, shift in ((base_rows, 0), (new_rows, offset)):
        for original in rows:
            row = {**original, "dense_row": original["dense_row"] + shift}
            by_query[row["query"]].append(row)
    result = []
    for query, rows in sorted(by_query.items()):
        seen = set()
        for row in sorted(rows, key=lambda r: (r["sse"], r["view_id"])):
            if row["view_id"] in seen:
                continue
            seen.add(row["view_id"])
            result.append({**row, "rank": len(seen)})
            if len(seen) == k:
                break
        if len(seen) < k:
            raise ValueError("Union has too few distinct candidate views")
    return result


def production_search(target_root, root, config):
    from .search import production_search as full_search
    started = time.perf_counter()
    baseline = Path(config["incremental_base_run"])
    base = read_manifest(baseline / "search")
    target = read_manifest(target_root)
    combined = read_manifest(Path(config["index"]))
    delta_root = Path(config["incremental_index"])
    delta_index = read_manifest(delta_root)
    if (base["status"] != "complete" or base["target_set_id"] != target["target_set_id"]
            or base.get("grid") != [32, 24] or base["top_k"] != config["top_k"]
            or base["rotations"] != [0, 90, 180, 270]
            or base["metric"] != "gray-mse-[0,1]-v1"):
        raise ValueError("Incremental retrieval requires matching complete fine-grid baseline targets and Top-K")
    base_index = Path(read_manifest(baseline)["config"]["index"])
    base_members = members(base_index, read_manifest(base_index))
    delta_members = members(delta_root, delta_index)
    full_ids = {m["index_id"] for _, m, _ in members(Path(config["index"]), combined)}
    if (full_ids != {m["index_id"] for _, m, _ in base_members + delta_members}
            or combined["rows"] != base["candidate_count"] + delta_index["rows"]):
        raise ValueError("Incremental index must be the disjoint baseline and delta union")
    run_id = identity([VERSION, base["run_id"], delta_index["index_id"], combined["index_id"], config["top_k"]])
    root.mkdir(parents=True, exist_ok=True)
    if (root / "manifest.json").exists():
        saved = read_manifest(root)
        if saved["run_id"] != run_id:
            raise ValueError("Incremental search belongs to a different frozen snapshot")
        return saved
    delta_config = {k: v for k, v in config.items() if k not in {"incremental_base_run", "reuse_run", "reuse_scores"}}
    delta_config["index"] = str(delta_root)
    extra = full_search(target_root, root / "additional", delta_config)
    left = np.load(baseline / "search/query_inverse.npy", allow_pickle=False)
    right = np.load(root / "additional/query_inverse.npy", allow_pickle=False)
    if not np.array_equal(left, right):
        raise ValueError("Baseline and delta query mappings disagree")
    old = pq.read_table(baseline / "search/matches.parquet").to_pylist()
    new = pq.read_table(root / "additional/matches.parquet").to_pylist()
    old_queries, new_queries = {r["query"] for r in old}, {r["query"] for r in new}
    if old_queries != new_queries:
        raise ValueError("Baseline and delta searches must cover the same unique queries")
    rows = merge_topk(old, new, config["top_k"], base["view_count"])
    columns = sorted({k for row in rows for k in row})
    temporary = root / "matches.tmp.parquet"
    pq.write_table(pa.Table.from_pylist([{k:r.get(k) for k in columns} for r in rows]), temporary, compression="zstd")
    temporary.replace(root / "matches.parquet")
    np.save(root / "query_inverse.npy", left, allow_pickle=False)
    first = {r["query"]: r["raw_mse"] for r in rows if r["rank"] == 1}
    result = {**base, "run_id": run_id, "index_id": combined["index_id"],
              "candidate_count": combined["rows"], "view_count": base["view_count"] + extra["view_count"],
              "baseline_index_id": base["index_id"], "baseline_run_id": base["run_id"],
              "baseline_dense_rows_preserved": True, "baseline_view_count": base["view_count"],
              "incremental_index_id": delta_index["index_id"], "new_places_searched": delta_index["rows"],
              "existing_places_reused": base["candidate_count"], "incremental_version": VERSION,
              "baseline_metadata_views_reused": len({r["dense_row"] for r in old}),
              "additional_retrieval_seconds": extra["retrieval_seconds"],
              "retrieval_seconds": round(time.perf_counter()-started, 3),
              "best_mse_mean": float(np.mean([first[int(q)] for q in left]))}
    result.pop("metadata_reused_views", None)
    write_json(root / "manifest.json", result)
    print(f"Reused {base['candidate_count']:,} baseline places; searched {delta_index['rows']:,} new places", flush=True)
    return result
