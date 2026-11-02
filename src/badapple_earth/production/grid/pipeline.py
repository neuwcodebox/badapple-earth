"""Separate grid-resolution gains from shortlist-width gains, keeping MS-SSIM fixed."""

from collections import defaultdict
from fractions import Fraction
from pathlib import Path
import time

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.views import rotation_recipe
from ..comparison import cached_photos, grid_frame
from ..correlation.pipeline import resolve_metadata, write_table
from ..render import encoder
from ..rerank import VERSION as SSIM_VERSION
from .rerank import evaluate
from .search import VERSION, search

PIPELINE_VERSION = "grid16-32-k20-wide-union-ms-ssim-v1"
LABELS = {"16-20": "16x12 / 20", "16-wide": "16x12 / WIDE",
          "32-20": "32x24 / 20", "32-wide": "32x24 / WIDE", "union": "16 + 32 / UNION"}


def best(rows, method, k):
    if method == "union":
        pool = rows
        key = lambda r: (-r["ms_ssim"], r["rank32"] or k+1, r["rank16"] or k+1, r["view_id"])
    else:
        field = "rank16" if method.startswith("16-") else "rank32"
        limit = 20 if method.endswith("-20") else k
        pool = [r for r in rows if r[field] is not None and r[field] <= limit]
        key = lambda r: (-r["ms_ssim"], r[field], r["view_id"])
    return min(pool, key=key)


def compare(config, output):
    if (config["top_k"] <= 20 or config["workers"] < 1 or config["candidate_batch"] < 1
            or config["target_batch"] < 1 or config["start"] < 0 or config["duration"] <= 0):
        raise ValueError("Grid comparison needs K > 20 and a valid original-frame interval")
    run, root = Path(config["run"]).resolve(), output.resolve()
    source, target = read_manifest(run), read_manifest(run / "targets")
    packed = read_manifest(Path(config["packed_index"]))
    baseline = Path(config["baseline"])
    baseline_manifest = read_manifest(baseline)
    source_search = read_manifest(run / "search")
    if (source["status"] != "complete" or packed["index_id"] != source_search["index_id"]
            or baseline_manifest["target_set_id"] != target["target_set_id"]
            or baseline_manifest["index_id"] != packed["index_id"] or baseline_manifest["top_k"] != 20):
        raise ValueError("All grid comparisons must use the same completed source/index snapshot")
    caches = []
    for name in config.get("reuse_scores", []):
        saved = read_manifest(Path(name))
        if (saved["status"] != "complete" or saved["index_id"] != packed["index_id"]
                or saved["target_set_id"] != target["target_set_id"] or saved.get("ms_ssim_version") != SSIM_VERSION):
            raise ValueError("Reusable scores must share source frames, index and MS-SSIM version")
        caches.append(saved["experiment_id"])
    begin, end = Fraction(str(config["start"])), Fraction(str(config["start"]))+Fraction(str(config["duration"]))
    targets = [r for r in pq.read_table(run / "targets/targets.parquet").to_pylist()
               if begin <= Fraction(r["source_pts"])*Fraction(r["source_time_base"]) < end]
    if (not targets or float(end) > target["duration_seconds"]+1e-6
            or [r["slot"] for r in targets] != list(range(targets[0]["slot"], targets[-1]["slot"]+1))):
        raise ValueError("Use a consecutive interval within the original video")
    experiment_id = identity([config, target["target_set_id"], packed["cache_id"], caches,
                              baseline_manifest["experiment_id"], VERSION, SSIM_VERSION, PIPELINE_VERSION])
    root.mkdir(parents=True, exist_ok=True)
    with writer_lock(root):
        path = root / "manifest.json"
        if path.exists():
            saved = read_manifest(root)
            if saved["experiment_id"] != experiment_id:
                raise ValueError("Use a new output directory for changed grid settings")
            if saved["status"] == "complete" and (root / "comparison.mp4").exists():
                return saved
        manifest = {"experiment_id": experiment_id, "status": "running", "config": config,
                    "target_set_id": target["target_set_id"], "index_id": packed["index_id"],
                    "ms_ssim_version": SSIM_VERSION, "search_version": VERSION, "pipeline_version": PIPELINE_VERSION,
                    "source_frames": len(targets), "first_slot": targets[0]["slot"], "last_slot": targets[-1]["slot"],
                    "frame_rate": target["frame_rate"], "audio_mode": "silent", "network_reads": 0,
                    "ms_ssim_resolution": target["canonical_size"], "grids": [[16, 12], [32, 24]]}
        write_json(path, manifest)
        started = time.perf_counter()
        retrieved = search(run, root, config, targets)
        manifest["retrieval_seconds"] = retrieved["elapsed_seconds"]
        manifest["unique_queries"] = retrieved["unique_queries"]
        shortlists = []
        for i, frame in enumerate(targets):
            union = {}
            for width in (16, 32):
                for rank, (position, sse) in enumerate(zip(retrieved[f"positions{width}"][i], retrieved[f"sse{width}"][i]), 1):
                    row = union.setdefault(int(position), {"slot": frame["slot"], "dense_row": int(position),
                                                           "rank16": None, "rank32": None, "sse16": None, "sse32": None})
                    row[f"rank{width}"], row[f"sse{width}"] = rank, int(sse)
            shortlists.extend(union.values())
        inverse = np.load(run / "search/query_inverse.npy", allow_pickle=False)
        queries = {int(inverse[r["slot"]]) for r in targets}
        original = defaultdict(list)
        table = pq.read_table(run / "search/matches.parquet", columns=["query", "rank", "dense_row"],
                              filters=[("query", "in", sorted(queries))])
        for row in table.to_pylist():
            original[row["query"]].append(row)
        for i, frame in enumerate(targets):
            expected = [r["dense_row"] for r in sorted(original[int(inverse[frame["slot"]])], key=lambda r: r["rank"])]
            if retrieved["positions16"][i, :20].tolist() != expected:
                raise ValueError("Wider 16x12 retrieval must preserve the exact original Top-20")
        write_table(root / "shortlists.parquet", shortlists)
        positions = pa.array(sorted({r["dense_row"] for r in shortlists}), type=pa.int64())
        if (root / "candidates.parquet").exists():
            metadata = {r["dense_row"]: r for r in pq.read_table(root / "candidates.parquet").to_pylist()}
        else:
            metadata = {}
            for name in config.get("reuse_scores", []):
                table = pq.read_table(Path(name) / "candidates.parquet")
                table = table.filter(pc.is_in(table["dense_row"], value_set=positions))
                metadata.update({r["dense_row"]: r for r in table.to_pylist()})
            manifest["metadata_reused_views"] = len(metadata)
            missing = np.array(sorted(set(positions.to_pylist())-set(metadata)), dtype=np.int32)
            metadata.update(resolve_metadata(root, Path(config["packed_index"]), missing, config["workers"]))
            for row in metadata.values():
                row["rotation_crop"] = row["rotation_crop"] or rotation_recipe(target["canonical_size"], row["rotation_degrees"])
            write_table(root / "candidates.parquet", list(metadata.values()))
        if set(metadata) != set(positions.to_pylist()):
            raise ValueError("Grid metadata does not match the retrieved views")
        counts = defaultdict(int)
        for row in shortlists:
            counts[row["slot"]] += 1
        manifest.update(candidate_views=len(metadata), candidate_places=len({r["tile_id"] for r in metadata.values()}),
                        frame_candidate_pairs=len(shortlists),
                        shortlist_sizes={"min":min(counts.values()), "max":max(counts.values()),
                                         "mean":float(np.mean(list(counts.values())))})
        write_json(path, manifest)
        print(f"MSE grid union: {len(shortlists):,} frame/view pairs, {len(metadata):,} unique views", flush=True)
        evaluations, audit = evaluate(root, run, source, targets, metadata, shortlists, config)
        manifest["score_cache"] = audit
        scores = {(r["slot"], r["dense_row"]): r["ms_ssim"] for r in evaluations}
        by_slot = defaultdict(list)
        for row in shortlists:
            by_slot[row["slot"]].append({**row, "view_id": metadata[row["dense_row"]]["view_id"],
                                         "ms_ssim": scores[row["slot"], row["dense_row"]]})
        control = {r["slot"]: r["view_id"] for r in pq.read_table(baseline / "selected.parquet",
                    columns=["slot", "method", "view_id"], filters=[("method", "=", "ms-ssim")]).to_pylist()}
        selections, display = [], []
        for frame in targets:
            rows = by_slot[frame["slot"]]
            chosen = {method: best(rows, method, config["top_k"]) for method in LABELS}
            if frame["slot"] in control and chosen["16-20"]["view_id"] != control[frame["slot"]]:
                raise ValueError("Top-20 MS-SSIM control must reproduce the completed comparison")
            if (chosen["16-wide"]["ms_ssim"] < chosen["16-20"]["ms_ssim"]
                    or chosen["32-wide"]["ms_ssim"] < chosen["32-20"]["ms_ssim"]
                    or chosen["union"]["ms_ssim"] < max(chosen["16-wide"]["ms_ssim"], chosen["32-wide"]["ms_ssim"])):
                raise ValueError("A larger candidate set must retain the smaller set's best score")
            for method, row in chosen.items():
                selections.append({**metadata[row["dense_row"]], **row, "method": method,
                                   "source_pts": frame["source_pts"], "source_time_base": frame["source_time_base"]})
            seen = set()
            for row in sorted(rows, key=lambda r: (-r["ms_ssim"], r["rank32"] or config["top_k"]+1,
                                                   r["rank16"] or config["top_k"]+1, r["view_id"])):
                tile = metadata[row["dense_row"]]["tile_id"]
                if tile not in seen:
                    seen.add(tile)
                    display.append({"slot": frame["slot"], "position": len(seen), "dense_row": row["dense_row"],
                                    "view_id": row["view_id"], "ms_ssim": row["ms_ssim"]})
                    if len(seen) == 5:
                        break
        write_table(root / "selected.parquet", selections)
        write_table(root / "display-five.parquet", display)
        photos = {r["view_id"]: r for r in selections}
        manifest["selected_photos"] = cached_photos(run, root, photos.values(), source["config"], tuple(target["canonical_size"]))
        chosen = {(r["slot"], r["method"]): r for r in selections}
        manifest["summary"] = {method: {"different_from_16_20": sum(r["view_id"] != chosen[r["slot"], "16-20"]["view_id"] for r in selections if r["method"] == method),
                                           "mean_ms_ssim": float(np.mean([r["ms_ssim"] for r in selections if r["method"] == method])),
                                           "selected_places": len({r["tile_id"] for r in selections if r["method"] == method})}
                               for method in LABELS}
        write_json(path, manifest)
        print("Rendering grid resolution and candidate width comparison", flush=True)
        temporary = root / "comparison.tmp.mp4"
        with (root / "encode.log").open("wb") as log:
            process = encoder(temporary, 1920, 1080, target["frame_rate"],
                              {"encoder_threads":4, "preset":"medium", "crf":18}, log)
            try:
                for i, frame in enumerate(targets):
                    panels = [("SOURCE", str(run/"targets"/frame["frame"]), None, None)]
                    for method, label in LABELS.items():
                        row = chosen[frame["slot"], method]
                        if method == "union":
                            detail = f"16 #{row['rank16'] or '—'}    32 #{row['rank32'] or '—'}"
                        else:
                            rank = row["rank16"] if method.startswith("16-") else row["rank32"]
                            limit = 20 if method.endswith("-20") else config["top_k"]
                            detail = f"#{rank:03d} / {limit}"
                        panels.append((label, str(root/"photos"/f"{row['view_id']}.png"), detail, row["ms_ssim"]))
                    image = grid_frame(frame, panels, source["config"], target["count"])
                    process.stdin.write(image.tobytes())
                    if i == len(targets)//2:
                        image.save(root / "preview.png")
                process.stdin.close()
                if process.wait() != 0:
                    raise ValueError("Grid comparison encoding failed; see encode.log")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        temporary.replace(root / "comparison.mp4")
        manifest.update(status="complete", elapsed_seconds_last_run=round(time.perf_counter()-started, 3),
                        video="comparison.mp4", video_duration_seconds=float(len(targets)/Fraction(target["frame_rate"])),
                        layout=["SOURCE", *LABELS.values()], interpretation="MS-SSIM fixed; perceptual playback improvement requires viewing")
        write_json(path, manifest)
        return manifest
