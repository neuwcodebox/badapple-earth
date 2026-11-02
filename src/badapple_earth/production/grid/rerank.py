"""MS-SSIM only: reuse compatible scores and read RGB solely for missing pairs."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from multiprocessing import get_context
import os
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import members, read_manifest
from ...matching.photos import read_rgb_rows
from ...matching.views import rotate_image
from ..assets import old_rgb_refs
from ..correlation.pipeline import write_table
from ..rerank import VERSION as SSIM_VERSION, pyramid, similarity


@lru_cache(maxsize=384)
def target_stats(path):
    with Image.open(path) as image:
        return pyramid(np.asarray(image.convert("L")))


def score_groups(groups):
    result = []
    for filename, entries in groups:
        photos = read_rgb_rows(filename, [entry[0] for entry in entries])
        for position, candidate, targets in entries:
            photo = Image.fromarray(photos[position])
            view = rotate_image(photo, candidate["rotation_degrees"], photo.size, candidate["rotation_crop"])
            stats = pyramid(np.asarray(view.convert("L")))
            for target in targets:
                result.append({"slot": target["slot"], "dense_row": candidate["dense_row"],
                               "ms_ssim": similarity(target_stats(target["path"]), stats)})
    return result


def evaluate(root, run, source, targets, metadata, shortlists, config):
    wanted = {(r["slot"], r["dense_row"]) for r in shortlists}
    slots = pa.array([r["slot"] for r in targets], type=pa.int64())
    positions = pa.array(sorted(metadata), type=pa.int64())
    reused, audit = {}, []
    target_id = read_manifest(run / "targets")["target_set_id"]
    index_id = read_manifest(Path(source["config"]["index"]))["index_id"]
    for name in config.get("reuse_scores", []):
        cache = Path(name)
        manifest = read_manifest(cache)
        if (manifest.get("ms_ssim_version") != SSIM_VERSION or manifest.get("target_set_id") != target_id
                or manifest.get("index_id") != index_id):
            raise ValueError("Reusable MS-SSIM scores need the same explicit metric version")
        table = pq.read_table(cache / "evaluations.parquet", columns=["slot", "dense_row", "ms_ssim"])
        table = table.filter(pc.and_(pc.is_in(table["slot"], value_set=slots),
                                    pc.is_in(table["dense_row"], value_set=positions)))
        before = len(reused)
        for row in table.to_pylist():
            key = row["slot"], row["dense_row"]
            if key in wanted:
                reused[key] = row
        audit.append({"run": name, "score_pairs": len(reused)-before})
    if reused:
        write_table(root / "reused-scores.parquet", list(reused.values()))
    else:
        pq.write_table(pa.table({"slot": pa.array([], type=pa.int64()),
                                 "dense_row": pa.array([], type=pa.int64()),
                                 "ms_ssim": pa.array([], type=pa.float64())}), root / "reused-scores.parquet")
    targets_by_slot = {r["slot"]: {"slot": r["slot"], "path": str(run/"targets"/r["frame"])} for r in targets}
    by_position = defaultdict(list)
    for row in shortlists:
        if (row["slot"], row["dense_row"]) not in reused:
            by_position[row["dense_row"]].append(targets_by_slot[row["slot"]])
    index = Path(source["config"]["index"])
    sources = {m["index_id"]: p for p, m, _ in members(index, read_manifest(index))}
    extra = source["config"].get("extra_rgb_cache")
    refs = old_rgb_refs(Path(extra) if extra else None,
                        {metadata[p]["tile_id"] for p in by_position if not metadata[p].get("rgb_cache_ref")})
    grouped = defaultdict(list)
    for position in sorted(by_position):
        row = metadata[position]
        if row["tile_id"] in refs:
            path, offset = refs[row["tile_id"]]
        elif row.get("rgb_cache_ref"):
            path = sources[row["index_member"]] / row["rgb_cache_ref"]
            offset = row["rgb_cache_row"]
        else:
            raise ValueError("MSE grid comparison requires locally cached RGB")
        candidate = {k: row[k] for k in ("dense_row", "rotation_degrees", "rotation_crop")}
        grouped[str(path.resolve())].append((offset, candidate, by_position[position]))
    groups = sorted(grouped.items())
    chunks = [groups[i:i+32] for i in range(0, len(groups), 32)]
    folder = root / "scores"
    folder.mkdir(exist_ok=True)
    print(f"MS-SSIM scores: {len(reused):,} reused, {len(wanted)-len(reused):,} new pairs", flush=True)
    started = time.perf_counter()
    names = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "NUMBA_NUM_THREADS")
    old = {key: os.environ.get(key) for key in names}
    try:
        for key in names:
            os.environ[key] = "1"
        with ProcessPoolExecutor(max_workers=config["workers"], mp_context=get_context("spawn")) as pool:
            jobs = {pool.submit(score_groups, chunk): folder / f"{i:06d}.parquet"
                    for i, chunk in enumerate(chunks) if not (folder / f"{i:06d}.parquet").exists()}
            completed = len(chunks)-len(jobs)
            for future in as_completed(jobs):
                write_table(jobs[future], future.result())
                completed += 1
                if completed % 16 == 0 or completed == len(chunks):
                    print(f"MS-SSIM grid evaluation: {completed}/{len(chunks)} RGB groups", flush=True)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    rows = list(reused.values())
    for i in range(len(chunks)):
        rows.extend(pq.read_table(folder / f"{i:06d}.parquet").to_pylist())
    if len(rows) != len(wanted) or {(r["slot"], r["dense_row"]) for r in rows} != wanted:
        raise ValueError("Every source frame and shortlisted view must have exactly one MS-SSIM score")
    write_table(root / "evaluations.parquet", rows)
    return rows, {"reused_pairs": len(reused), "new_pairs": len(wanted)-len(reused), "sources": audit,
                  "elapsed_seconds_last_run": round(time.perf_counter()-started, 3)}
