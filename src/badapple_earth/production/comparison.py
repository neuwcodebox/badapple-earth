"""Compare five metrics on saved Top-K views, without searching or network I/O."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from fractions import Fraction
import json
from multiprocessing import get_context
from pathlib import Path
import shutil
import time

import numpy as np
from PIL import Image, ImageDraw
import pyarrow as pa
import pyarrow.parquet as pq

from ..collection.bundle import read_manifest
from ..collection.storage import writer_lock
from ..collection.windows import identity
from ..evidence import write_json
from ..matching.views import rotate_image
from ..matching.photos import read_rgb_rows
from .assets import old_rgb_refs, save_image
from .layout import BG, INK, MUTED, font, resized, timestamp
from .metrics import LABELS, METHODS, VERSION, image_features, scores, winner
from .render import encoder


def cached_photos(run, root, candidates, config, canonical_size):
    """Materialize only missing views from stored RGB shards; never open a URL."""
    destination = root / "photos"
    destination.mkdir(exist_ok=True)
    missing = {}
    counts = {"reused_png": 0, "rgb_shard": 0, "remote": 0}
    for row in candidates:
        path = destination / f"{row['view_id']}.png"
        if path.exists():
            continue
        existing = run / "assets/thumbs" / path.name
        if existing.exists():
            try:
                path.hardlink_to(existing)
            except OSError:
                shutil.copyfile(existing, path)
            counts["reused_png"] += 1
        else:
            missing[row["view_id"]] = row
    extra = Path(config["extra_rgb_cache"]) if config.get("extra_rgb_cache") else None
    refs = old_rgb_refs(extra, {r["tile_id"] for r in missing.values() if not r.get("rgb_cache_ref")})
    from ..collection.bundle import members
    index_root = Path(config["index"])
    sources = {manifest["index_id"]: directory
               for directory, manifest, _ in members(index_root, read_manifest(index_root))}
    groups = defaultdict(list)
    for row in missing.values():
        if row["tile_id"] in refs:
            path, position = refs[row["tile_id"]]
        elif row.get("rgb_cache_ref"):
            path = sources[row["index_member"]] / row["rgb_cache_ref"]
            position = row["rgb_cache_row"]
        else:
            raise ValueError("Top-K comparison requires locally cached RGB for every view")
        groups[path].append((position, row))
    for path, jobs in groups.items():
        array = read_rgb_rows(path, [position for position, _ in jobs])
        for position, row in jobs:
            image = Image.fromarray(np.asarray(array[position]))
            transformed = rotate_image(image, row["rotation_degrees"], canonical_size, row["rotation_crop"])
            save_image(transformed, destination / f"{row['view_id']}.png")
            counts["rgb_shard"] += 1
    return counts


def evaluate(jobs):
    results = []
    for target, candidates in jobs:
        left = image_features(target["path"])
        measured = [scores(left, image_features(c["path"]), c["raw_mse"]) for c in candidates]
        values = {method: [row[method] for row in measured] for method in METHODS}
        choices = {method: winner(method, values[method], candidates) for method in METHODS}
        # Separate the expanded shortlist's effect from changing the metric.
        seen, first_five = set(), []
        for i, candidate in enumerate(candidates):
            if candidate["tile_id"] not in seen:
                first_five.append(i)
                seen.add(candidate["tile_id"])
                if len(first_five) == 5:
                    break
        local = winner("ms-ssim", [values["ms-ssim"][i] for i in first_five],
                       [candidates[i] for i in first_five])
        results.append({"slot": target["slot"], "scores": values, "choices": choices,
                        "ms_ssim_five_index": first_five[local]})
    return results


def frame(target, candidates, evaluation, config, total):
    panels = [("SOURCE", target["path"], None, None)]
    for method in METHODS:
        index = evaluation["choices"][method]
        candidate = candidates[index]
        panels.append((LABELS[method], candidate["path"], f"#{candidate['rank']:02d} / {len(candidates):02d}",
                       evaluation["scores"][method][index]))
    return grid_frame(target, panels, config, total)


def grid_frame(target, panels, config, total):
    canvas = Image.new("RGB", (1920, 1080), BG)
    draw = ImageDraw.Draw(canvas)
    normal = font(config["font_regular"], 18)
    label = font(config["font_bold"], 23)
    for i, (name, path, detail, score) in enumerate(panels):
        x, y = 24 + i % 3 * 632, 16 + i // 3 * 516
        draw.text((x, y), name, font=label, fill=INK)
        canvas.paste(resized(path, 608, 454), (x, y+36))
        if detail is not None:
            draw.text((x, y+493), detail, font=normal, fill=MUTED)
            draw.text((x+608, y+493), f"{score:.5f}", font=normal, fill=INK, anchor="ra")
    draw.text((24, 1060), f"{timestamp(target['target_time'])}    {target['slot']+1:,} / {total:,}",
              font=font(config["font_regular"], 14), fill=MUTED)
    draw.text((1896, 1060), "Contains modified Copernicus Sentinel data 2023–2026",
              font=font(config["font_regular"], 12), fill=MUTED, anchor="ra")
    return canvas


def compare_metrics(run: Path, root: Path, start=38.0, duration=10.0, top_k=20, workers=8):
    if start < 0 or duration <= 0 or top_k < 2 or workers < 1:
        raise ValueError("Require start >= 0, duration > 0, top_k >= 2 and workers >= 1")
    run, root = run.resolve(), root.resolve()
    source = read_manifest(run)
    target_manifest = read_manifest(run / "targets")
    search = read_manifest(run / "search")
    if source["status"] != "complete" or top_k > search["top_k"]:
        raise ValueError("A completed production run with sufficient saved Top-K is required")
    targets = pq.read_table(run / "targets/targets.parquet").to_pylist()
    targets = [row for row in targets if Fraction(str(start)) <= Fraction(row["source_pts"])
               * Fraction(row["source_time_base"]) < Fraction(str(start+duration))]
    if not targets or [r["slot"] for r in targets] != list(range(targets[0]["slot"], targets[-1]["slot"]+1)):
        raise ValueError("Requested interval must contain consecutive original frames")
    if start + duration > target_manifest["duration_seconds"] + 1e-6:
        raise ValueError("Requested interval extends beyond the source video")
    experiment_id = identity([search["run_id"], target_manifest["target_set_id"],
                              [r["slot"] for r in targets], top_k, VERSION])
    root.mkdir(parents=True, exist_ok=True)
    with writer_lock(root):
        manifest_path = root / "manifest.json"
        if manifest_path.exists():
            saved = read_manifest(root)
            if saved["experiment_id"] != experiment_id:
                raise ValueError("Use another output directory for changed comparison settings")
            if saved["status"] == "complete" and (root / "comparison.mp4").exists():
                return saved
        config = source["config"]
        manifest = {"experiment_id": experiment_id, "status": "running", "version": VERSION,
                    "source_run": str(run), "target_set_id": target_manifest["target_set_id"],
                    "search_run_id": search["run_id"], "index_id": search["index_id"],
                    "requested_start": start, "requested_duration": duration, "top_k": top_k,
                    "first_slot": targets[0]["slot"], "last_slot": targets[-1]["slot"],
                    "source_frames": len(targets), "frame_rate": target_manifest["frame_rate"],
                    "canonical_size": target_manifest["canonical_size"], "methods": list(METHODS),
                    "audio_mode": "silent", "network": "none; local canonical RGB only"}
        write_json(manifest_path, manifest)
        started = time.perf_counter()
        inverse = np.load(run / "search/query_inverse.npy", allow_pickle=False)
        wanted = {int(inverse[row["slot"]]) for row in targets}
        by_query = defaultdict(list)
        for row in pq.read_table(run / "search/matches.parquet").to_pylist():
            if row["query"] in wanted and row["rank"] <= top_k:
                by_query[row["query"]].append(row)
        for rows in by_query.values():
            rows.sort(key=lambda r: r["rank"])
        if any(len(by_query[q]) != top_k for q in wanted):
            raise ValueError("Incomplete saved Top-K for the requested frames")
        unique = {r["view_id"]: r for rows in by_query.values() for r in rows}
        print(f"Compare {len(targets)} original frames, {top_k} views each, {len(unique)} unique photos", flush=True)
        manifest["photos"] = cached_photos(run, root, unique.values(), config,
                                           tuple(target_manifest["canonical_size"]))
        manifest["candidate_views"] = len(unique)
        manifest["candidate_places"] = len({r["tile_id"] for r in unique.values()})
        for row in targets:
            row["path"] = str(run / "targets" / row["frame"])
        for rows in by_query.values():
            for row in rows:
                row["path"] = str(root / "photos" / f"{row['view_id']}.png")
        checkpoint = root / "scores"
        checkpoint.mkdir(exist_ok=True)
        collected, pending = [], []
        for begin in range(0, len(targets), 20):
            chunk = targets[begin:begin+20]
            path = checkpoint / f"{chunk[0]['slot']:06d}-{chunk[-1]['slot']+1:06d}.json"
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved["experiment_id"] != experiment_id:
                    raise ValueError("Comparison checkpoint belongs to different inputs")
                collected.extend(saved["frames"])
            else:
                jobs = [(row, by_query[int(inverse[row["slot"]])]) for row in chunk]
                pending.append((path, jobs))
        computing = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers, mp_context=get_context("spawn")) as pool:
            futures = {pool.submit(evaluate, jobs): path for path, jobs in pending}
            for future in as_completed(futures):
                rows = future.result()
                write_json(futures[future], {"experiment_id": experiment_id, "frames": rows})
                collected.extend(rows)
                print(f"Five metrics: {len(collected)}/{len(targets)} frames", flush=True)
        collected.sort(key=lambda row: row["slot"])
        if [r["slot"] for r in collected] != [r["slot"] for r in targets]:
            raise ValueError("Comparison must evaluate every original frame in the interval")
        manifest["metric_seconds_last_run"] = round(time.perf_counter() - computing, 3)
        evaluations, selections = [], []
        for target, evaluation in zip(targets, collected):
            candidates = by_query[int(inverse[target["slot"]])]
            for i, candidate in enumerate(candidates):
                evaluations.append({**candidate, "slot": target["slot"], "source_pts": target["source_pts"],
                                    "source_time_base": target["source_time_base"],
                                    **{method: evaluation["scores"][method][i] for method in METHODS}})
            for method, index in evaluation["choices"].items():
                candidate = candidates[index]
                selections.append({**candidate, "slot": target["slot"], "source_pts": target["source_pts"],
                                   "source_time_base": target["source_time_base"], "method": method,
                                   "score": evaluation["scores"][method][index]})
        for name, rows in (("evaluations", evaluations), ("selected", selections)):
            temporary = root / f"{name}.tmp.parquet"
            pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd")
            temporary.replace(root / f"{name}.parquet")
        summary = {}
        for method in METHODS:
            rows = [r for r in selections if r["method"] == method]
            changed = sum(e["choices"][method] != e["choices"]["mse"] for e in collected)
            summary[method] = {"different_from_mse": changed,
                               "mean_retrieval_mse": float(np.mean([r["raw_mse"] for r in rows])),
                               "selected_places": len({r["tile_id"] for r in rows}),
                               "mse_rank_histogram": np.bincount([r["rank"] for r in rows], minlength=top_k+1)[1:].tolist()}
        summary["ms-ssim"]["different_from_five_place_rerank"] = sum(
            e["choices"]["ms-ssim"] != e["ms_ssim_five_index"] for e in collected)
        manifest["summary"] = summary
        write_json(manifest_path, manifest)
        print("Rendering synchronized comparison grid", flush=True)
        temporary = root / "comparison.tmp.mp4"
        with (root / "encode.log").open("wb") as log:
            process = encoder(temporary, 1920, 1080, target_manifest["frame_rate"],
                              {"encoder_threads": 4, "preset": "medium", "crf": 18}, log)
            try:
                for i, (target, evaluation) in enumerate(zip(targets, collected)):
                    picture = frame(target, by_query[int(inverse[target["slot"]])], evaluation,
                                    config, target_manifest["count"])
                    process.stdin.write(picture.tobytes())
                    if i == len(targets)//2:
                        picture.save(root / "preview.png")
                process.stdin.close()
                if process.wait() != 0:
                    raise ValueError("Comparison encoder failed; see encode.log")
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        temporary.replace(root / "comparison.mp4")
        manifest.update(status="complete", elapsed_seconds_last_run=round(time.perf_counter()-started, 3),
                        video="comparison.mp4", preview="preview.png",
                        layout=["SOURCE", "MSE", "MS-SSIM", "FSIM", "Multi-MSE", "ZNCC"],
                        video_duration_seconds=float(len(targets) / Fraction(target_manifest["frame_rate"])),
                        frame_mapping="one frame per consecutive source PTS; no skipped or interpolated frames",
                        interpretation="scores use different units; changed choices are not evidence of visual improvement")
        write_json(manifest_path, manifest)
        return manifest
