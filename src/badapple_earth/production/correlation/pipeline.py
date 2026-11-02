"""Compare independent wide retrieval against the user's Top-20 MS-SSIM baseline."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from fractions import Fraction
from multiprocessing import get_context
import os
from pathlib import Path
import time

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq

from ...collection.bundle import members, read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.views import rotation_recipe
from ..assets import old_rgb_refs
from ..comparison import cached_photos, grid_frame
from ..render import encoder
from ..search import resolve
from .features import VERSION
from .rerank import choose, display_five, score_groups
from .search import search

PIPELINE_VERSION = "dual512-fine64-128-visible-structure-brightness-ms-ssim-control-v1"
LABELS = {"ms20": "MS-SSIM / 20", "mswide": "MS-SSIM / WIDE",
          "zncc": "ZNCC / WIDE", "gc": "GC / WIDE", "zgc": "ZNCC + GC / WIDE"}


def write_table(path, rows):
    temporary = path.with_suffix(".tmp.parquet")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
    temporary.replace(path)


def resolve_part(cache_string, positions):
    cache = Path(cache_string)
    return list(resolve(cache, read_manifest(cache), positions).values())


def resolve_metadata(root, cache, positions, workers):
    folder = root / "metadata"
    folder.mkdir(exist_ok=True)
    parts = [part for part in np.array_split(positions, workers) if len(part)]
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
        jobs = {pool.submit(resolve_part, str(cache.resolve()), part): folder / f"{i:03d}.parquet"
                for i, part in enumerate(parts) if not (folder / f"{i:03d}.parquet").exists()}
        for future in as_completed(jobs):
            rows = future.result()
            write_table(jobs[future], rows)
            print(f"Resolved metadata part: {jobs[future].stem}", flush=True)
    return {r["dense_row"]: r for i in range(len(parts))
            for r in pq.read_table(folder / f"{i:03d}.parquet").to_pylist()}


def score_shortlist(root, run, source, targets, metadata, shortlists, config):
    target_map = {r["slot"]: {"slot": r["slot"], "path": str(run / "targets" / r["frame"])} for r in targets}
    by_position = defaultdict(list)
    for row in shortlists:
        by_position[row["dense_row"]].append(target_map[row["slot"]])
    index = Path(source["config"]["index"])
    sources = {m["index_id"]: directory for directory, m, _ in members(index, read_manifest(index))}
    extra = source["config"].get("extra_rgb_cache")
    refs = old_rgb_refs(Path(extra) if extra else None,
                        {r["tile_id"] for r in metadata.values() if not r.get("rgb_cache_ref")})
    grouped = defaultdict(list)
    for position in sorted(by_position):
        row = metadata[position]
        if row["tile_id"] in refs:
            path, offset = refs[row["tile_id"]]
        elif row.get("rgb_cache_ref"):
            path = sources[row["index_member"]] / row["rgb_cache_ref"]
            offset = row["rgb_cache_row"]
        else:
            raise ValueError("Wide reranking requires the existing local RGB cache")
        candidate = {k: row[k] for k in ("view_id", "dense_row", "rotation_degrees", "rotation_crop")}
        grouped[str(path.resolve())].append((offset, candidate, by_position[position]))
    groups = sorted(grouped.items())
    chunks = [groups[i:i+32] for i in range(0, len(groups), 32)]
    folder = root / "scores"
    folder.mkdir(exist_ok=True)
    started = time.perf_counter()
    # Spawned scoring processes should each use one BLAS thread. The parent
    # retains its already-initialized BLAS/Numba pools for vector retrieval.
    names = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "NUMBA_NUM_THREADS")
    old = {name: os.environ.get(name) for name in names}
    try:
        for name in names:
            os.environ[name] = "1"
        with ProcessPoolExecutor(max_workers=config["workers"], mp_context=get_context("spawn")) as pool:
            jobs = {pool.submit(score_groups, chunk, config): folder / f"{i:06d}.parquet"
                    for i, chunk in enumerate(chunks) if not (folder / f"{i:06d}.parquet").exists()}
            completed = len(chunks)-len(jobs)
            for future in as_completed(jobs):
                write_table(jobs[future], future.result())
                completed += 1
                if completed % 16 == 0 or completed == len(chunks):
                    print(f"Wide fine evaluation: {completed}/{len(chunks)} RGB groups", flush=True)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    rows = []
    for i in range(len(chunks)):
        rows.extend(pq.read_table(folder / f"{i:06d}.parquet").to_pylist())
    if {(r["slot"], r["dense_row"]) for r in rows} != {(r["slot"], r["dense_row"]) for r in shortlists}:
        raise ValueError("Fine evaluation must cover every source frame and every shortlisted view")
    write_table(root / "evaluations.parquet", rows)
    return rows, round(time.perf_counter()-started, 3)


def compare(config, output):
    if (config["retrieval_k"] < 5 or config["workers"] < 1 or config["candidate_batch"] < 1
            or config["target_batch"] < 1 or config["start"] < 0 or config["duration"] <= 0):
        raise ValueError("Invalid correlation experiment dimensions or interval")
    if (len(config["structure_weights"]) != 2 or min(config["structure_weights"]) < 0
            or abs(sum(config["structure_weights"])-1) > 1e-6):
        raise ValueError("Two nonnegative structure weights must sum to one")
    run, root = Path(config["run"]).resolve(), output.resolve()
    source, target_manifest = read_manifest(run), read_manifest(run / "targets")
    retrieval_manifest = read_manifest(run / "search")
    packed_manifest = read_manifest(Path(config["packed_index"]))
    baseline = Path(config["baseline"])
    baseline_manifest = read_manifest(baseline)
    if (source["status"] != "complete" or baseline_manifest["status"] != "complete"
            or packed_manifest["index_id"] != retrieval_manifest["index_id"]
            or baseline_manifest["search_run_id"] != retrieval_manifest["run_id"]
            or baseline_manifest["target_set_id"] != target_manifest["target_set_id"]
            or baseline_manifest["top_k"] != 20):
        raise ValueError("Wide retrieval and the Top-20 baseline must use the same completed snapshot")
    targets = [r for r in pq.read_table(run / "targets/targets.parquet").to_pylist()
               if Fraction(str(config["start"])) <= Fraction(r["source_pts"])*Fraction(r["source_time_base"])
               < Fraction(str(config["start"]+config["duration"]))]
    if (not targets or config["start"]+config["duration"] > target_manifest["duration_seconds"]+1e-6
            or [r["slot"] for r in targets] != list(range(targets[0]["slot"], targets[-1]["slot"]+1))):
        raise ValueError("The comparison interval must contain consecutive original frames within the video")
    experiment_id = identity([config, target_manifest["target_set_id"], packed_manifest["cache_id"],
                              baseline_manifest["experiment_id"], VERSION, PIPELINE_VERSION])
    root.mkdir(parents=True, exist_ok=True)
    with writer_lock(root):
        path = root / "manifest.json"
        if path.exists():
            saved = read_manifest(root)
            if saved["experiment_id"] != experiment_id:
                raise ValueError("Use a new output directory for a changed correlation experiment")
            if saved["status"] == "complete" and (root / "comparison.mp4").exists():
                return saved
        manifest = {"experiment_id": experiment_id, "status": "running", "config": config,
                    "feature_version": VERSION, "pipeline_version": PIPELINE_VERSION,
                    "target_set_id": target_manifest["target_set_id"], "index_id": packed_manifest["index_id"],
                    "source_frames": len(targets), "first_slot": targets[0]["slot"], "last_slot": targets[-1]["slot"],
                    "frame_rate": target_manifest["frame_rate"], "audio_mode": "silent",
                    "network_reads": 0, "display_pixel_policy": "unaltered canonical RGB, equal panel resolution"}
        write_json(path, manifest)
        started = time.perf_counter()
        retrieved = search(run, root, config, targets)
        manifest["retrieval_seconds"] = retrieved["elapsed_seconds"]
        manifest["unique_queries"] = retrieved["unique_queries"]
        old_inverse = np.load(run / "search/query_inverse.npy", allow_pickle=False)
        needed_queries = {int(old_inverse[r["slot"]]) for r in targets}
        original = defaultdict(list)
        for row in pq.read_table(run / "search/matches.parquet").to_pylist():
            if row["query"] in needed_queries:
                original[row["query"]].append(row)
        shortlists = []
        for i, target in enumerate(targets):
            union = {}
            for route, key in (("z", "zpos"), ("g", "gpos")):
                for rank, position in enumerate(retrieved[key][i], 1):
                    entry = union.setdefault(int(position), {"slot": target["slot"], "dense_row": int(position),
                                                             "z_rank": None, "g_rank": None, "mse_rank": None})
                    entry[f"{route}_rank"] = rank
            for row in original[int(old_inverse[target["slot"]])]:
                entry = union.setdefault(row["dense_row"], {"slot": target["slot"], "dense_row": row["dense_row"],
                                                            "z_rank": None, "g_rank": None, "mse_rank": None})
                entry["mse_rank"] = row["rank"]
            shortlists.extend(union.values())
        write_table(root / "shortlists.parquet", shortlists)
        positions = np.array(sorted({r["dense_row"] for r in shortlists}), dtype=np.int32)
        if (root / "candidates.parquet").exists():
            metadata = {r["dense_row"]: r for r in pq.read_table(root / "candidates.parquet").to_pylist()}
        else:
            metadata = resolve_metadata(root, Path(config["packed_index"]), positions, config["workers"])
            for row in metadata.values():
                row["rotation_crop"] = row["rotation_crop"] or rotation_recipe(target_manifest["canonical_size"], row["rotation_degrees"])
            write_table(root / "candidates.parquet", list(metadata.values()))
        if set(metadata) != set(positions.tolist()):
            raise ValueError("Resolved metadata differs from the retrieval union")
        manifest["candidate_views"] = len(metadata)
        manifest["candidate_places"] = len({r["tile_id"] for r in metadata.values()})
        manifest["frame_candidate_pairs"] = len(shortlists)
        counts = defaultdict(int)
        for r in shortlists:
            counts[r["slot"]] += 1
        manifest["shortlist_sizes"] = {"min": min(counts.values()), "max": max(counts.values()),
                                       "mean": float(np.mean(list(counts.values())))}
        write_json(path, manifest)
        print(f"Wide shortlist: {len(shortlists):,} frame/view pairs, {len(metadata):,} unique views", flush=True)
        evaluations, seconds = score_shortlist(root, run, source, targets, metadata, shortlists, config)
        manifest["fine_seconds_last_run"] = seconds
        by_slot = defaultdict(list)
        ranks = {(r["slot"], r["dense_row"]): r for r in shortlists}
        for row in evaluations:
            row["view_id"] = metadata[row["dense_row"]]["view_id"]
            by_slot[row["slot"]].append(row)
        control = {r["slot"]: r for r in pq.read_table(baseline / "selected.parquet").to_pylist()
                   if r["method"] == "ms-ssim"}
        selections, displays = [], []
        for target in targets:
            slot, rows = target["slot"], by_slot[target["slot"]]
            narrow = [r for r in rows if ranks[slot, r["dense_row"]]["mse_rank"] is not None]
            control_row = min(narrow, key=lambda r: (-r["ms_ssim"], ranks[slot, r["dense_row"]]["mse_rank"], r["view_id"]))
            control_position = control_row["dense_row"]
            if slot in control and control[slot]["view_id"] != control_row["view_id"]:
                raise ValueError("Recomputed Top-20 MS-SSIM must reproduce the saved baseline")
            with Image.open(run / "targets" / target["frame"]) as image:
                target_std = float(np.std(np.asarray(image.convert("L"), dtype=np.float32)/255))
            flat = target_std < .005
            mse_position = min(original[int(old_inverse[slot])], key=lambda r: r["rank"])["dense_row"]
            lookup = {r["dense_row"]: r for r in rows}
            wide = min(rows, key=lambda r: (-r["ms_ssim"], r["full_mse"], r["view_id"]))
            choices = {"ms20": (control_position, "saved_top20_control"),
                       "mswide": (wide["dense_row"], "max_ms_ssim")}
            for method, metric in (("zncc", "zncc"), ("gc", "gc"), ("zgc", "structure")):
                choices[method] = choose(rows, metric, config, mse_position if flat else control_position, flat)
            for method, (position, reason) in choices.items():
                row = lookup[position]
                score = row["ms_ssim"] if method.startswith("ms") else row[{"zncc":"zncc", "gc":"gc", "zgc":"structure"}[method]]
                selections.append({**metadata[position], **row, **ranks[slot, position], "method": method,
                                   "selection_reason": reason, "selection_score": score,
                                   "source_pts": target["source_pts"], "source_time_base": target["source_time_base"]})
            actual = choices["zgc"][0]
            # Final display candidates are chosen after scoring, never used as
            # a gate on which retrieval candidates may be evaluated.
            for number, position in enumerate(display_five(rows, actual, metadata, config), 1):
                displays.append({"slot": slot, "display_position": number, "dense_row": position,
                                 "view_id": metadata[position]["view_id"], "selected": position == actual})
        write_table(root / "selected.parquet", selections)
        write_table(root / "display-five.parquet", displays)
        unique_selected = {r["dense_row"]: r for r in selections}
        manifest["selected_photos"] = cached_photos(run, root, unique_selected.values(), source["config"],
                                                    tuple(target_manifest["canonical_size"]))
        chosen = {(r["slot"], r["method"]): r for r in selections}
        summary = {}
        for method in LABELS:
            rows = [r for r in selections if r["method"] == method]
            summary[method] = {"different_from_ms20": sum(r["view_id"] != chosen[r["slot"], "ms20"]["view_id"] for r in rows),
                               "outside_mse_top20": sum(r["mse_rank"] is None for r in rows),
                               "mean_ms_ssim": float(np.mean([r["ms_ssim"] for r in rows])),
                               "mean_contrast": float(np.mean([r["contrast"] for r in rows])),
                               "selected_places": len({r["tile_id"] for r in rows}),
                               "fallback_frames": sum(r["selection_reason"].startswith(("flat_", "no_visible")) for r in rows)}
        if any(chosen[t["slot"], "mswide"]["ms_ssim"]+2e-6 < chosen[t["slot"], "ms20"]["ms_ssim"] for t in targets):
            raise ValueError("The wide MS-SSIM union must retain the original Top-20 candidates")
        manifest["summary"] = summary
        write_json(path, manifest)
        print("Rendering wide retrieval versus Top-20 baseline", flush=True)
        temporary = root / "comparison.tmp.mp4"
        with (root / "encode.log").open("wb") as log:
            process = encoder(temporary, 1920, 1080, target_manifest["frame_rate"],
                              {"encoder_threads":4, "preset":"medium", "crf":18}, log)
            try:
                for i, target in enumerate(targets):
                    panels = [("SOURCE", str(run/"targets"/target["frame"]), None, None)]
                    for method, label in LABELS.items():
                        selection = chosen[target["slot"], method]
                        route = "Z#"+str(selection["z_rank"]) if selection["z_rank"] else "Z —"
                        route += "   G#"+str(selection["g_rank"]) if selection["g_rank"] else "   G —"
                        if method == "ms20":
                            route = f"MSE #{selection['mse_rank']:02d} / 20"
                        panels.append((label, str(root / "photos" / f"{selection['view_id']}.png"), route,
                                       selection["selection_score"]))
                    target = {**target, "path": str(run/"targets"/target["frame"])}
                    image = grid_frame(target, panels, source["config"], target_manifest["count"])
                    process.stdin.write(image.tobytes())
                    if i == len(targets)//2:
                        image.save(root / "preview.png")
                process.stdin.close()
                if process.wait() != 0:
                    raise ValueError("Wide comparison encoder failed; see encode.log")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        temporary.replace(root / "comparison.mp4")
        manifest.update(status="complete", elapsed_seconds_last_run=round(time.perf_counter()-started, 3),
                        layout=["SOURCE", *LABELS.values()], video="comparison.mp4",
                        video_duration_seconds=float(len(targets) / Fraction(target_manifest["frame_rate"])),
                        interpretation="wide retrieval experiment; score gains are not a perceptual-quality verdict")
        write_json(path, manifest)
        return manifest
