"""Prepare independent or temporal model choices and render saved selections offline."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from functools import lru_cache
import json
from pathlib import Path
import shutil
import time

import numpy as np
from PIL import Image, ImageDraw
import pyarrow as pa
import pyarrow.parquet as pq

from ..collection.bundle import read_manifest
from ..collection.windows import identity
from ..evidence import write_json
from .assets import cached_photos
from .layout import BG, INK, MUTED, font, resized
from .render import encoder

VERSION = "blind-random-frame-and-candidate-order-v1"
SEQUENTIAL_VERSION = "chronological-overlap10-stable-photo-labels-v1"


def assignments(slots, seed, workers, mode="independent"):
    """Choose shuffled groups or contiguous sequences for temporal reviewers."""
    if workers < 1 or len(set(slots)) != len(slots):
        raise ValueError("Require positive workers and distinct source slots")
    if mode == "sequential":
        return [chunk.tolist() for chunk in np.array_split(sorted(slots), workers)]
    if mode != "independent":
        raise ValueError("Unknown selection mode")
    shuffled = np.random.default_rng(seed).permutation(slots).tolist()
    return [chunk.tolist() for chunk in np.array_split(shuffled, workers)]


@lru_cache(maxsize=512)
def sheet_photo(path):
    with Image.open(path) as image:
        return image.convert("RGB")


def comparison_sheet(case, root, regular, bold):
    canvas = Image.new("RGB", (1392, 1128), BG)
    draw = ImageDraw.Draw(canvas)
    draw.text((16, 4), case["case_id"] + " / TARGET", font=bold, fill=INK)
    with Image.open(case["source_image"]) as image:
        canvas.paste(image.convert("RGB"), (16, 32))
    for i, entry in enumerate(case["entries"]):
        x, y = 16 + i % 5 * 272, 240 + i // 5 * 218
        canvas.paste(sheet_photo(root / "photos" / f"{entry['view_id']}.png"), (x, y))
        label = entry["id"]
        if "photo_id" in entry:
            label += " / " + entry["photo_id"]
        draw.text((x, y + 194), label, font=regular, fill=INK)
    canvas.save(root / "sheets" / f"{case['case_id']}.png")


def prepare(run, root, start=38.0, duration=10.0, seed=20261103, workers=3, mode="independent", independent_run=None):
    started = time.perf_counter()
    run, root = run.resolve(), root.resolve()
    source, target = read_manifest(run), read_manifest(run / "targets")
    if duration is None:
        duration = target["duration_seconds"] - start
    if start < 0 or duration <= 0 or start + duration > target["duration_seconds"] + 1e-6:
        raise ValueError("Interval must lie inside the original video")
    targets = [r for r in pq.read_table(run / "targets/targets.parquet").to_pylist()
               if Fraction(str(start)) <= Fraction(r["source_pts"]) * Fraction(r["source_time_base"])
               < Fraction(str(start + duration))]
    if not targets:
        raise ValueError("No source frames in interval")
    groups = assignments([r["slot"] for r in targets], seed, workers, mode)
    version = SEQUENTIAL_VERSION if mode == "sequential" else VERSION
    settings = [version, source["production_id"], [r["slot"] for r in targets], seed, workers]
    if mode == "sequential" and independent_run is not None:
        independent_run = independent_run.resolve()
        independent = read_manifest(independent_run)
        if (independent["status"] != "complete" or independent["target_set_id"] != source["target_set_id"]
                or independent["index_id"] != source["index_id"] or Path(independent["source_run"]).resolve() != run):
            raise ValueError("Require completed independent choices from the same production run")
        settings.append(independent["experiment_id"])
    experiment_id = identity(settings)
    if (root / "manifest.json").exists():
        existing = read_manifest(root)
        if existing["experiment_id"] != experiment_id:
            raise ValueError("Use another output directory for different inputs or randomization")
        return existing
    root.mkdir(parents=True, exist_ok=True)
    for directory in ("sheets", "jobs", "choices"):
        (root / directory).mkdir(exist_ok=True)
    instructions = Path("config/astra-reviewer.txt")
    if instructions.exists() and not (root / "reviewer-instructions.txt").exists():
        shutil.copyfile(instructions, root / "reviewer-instructions.txt")
    inverse = np.load(run / "search/query_inverse.npy", allow_pickle=False)
    queries = sorted({int(inverse[r["slot"]]) for r in targets})
    matches = pq.read_table(run / "search/matches.parquet", filters=[("query", "in", queries), ("rank", "<=", 20)]).to_pylist()
    pools = defaultdict(list)
    for row in matches:
        pools[row["query"]].append(row)
    if any(len(pools[q]) != 20 for q in queries):
        raise ValueError("Require exactly twenty saved candidates for each source frame")
    photos = {r["view_id"]: r for r in matches}
    photo_counts = cached_photos(run, root, photos.values(), source["config"], tuple(target["canonical_size"]))
    by_slot = {r["slot"]: r for r in targets}
    photo_ids = {view_id: f"V{i + 1:04d}" for i, view_id in enumerate(sorted(photos))}
    cases, jobs = [], []
    for worker, slots in enumerate(groups, 1):
        job = {"job_id": f"review-{worker:02d}", "cases": []}
        if mode == "sequential":
            job["context"] = [{"case_id": c["case_id"], "image": str(root / "sheets" / f"{c['case_id']}.png")}
                              for c in cases[-10:]]
        for slot in slots:
            case_id = f"C{len(cases) + 1:03d}"
            pool = sorted(pools[int(inverse[slot])], key=lambda r: r["rank"])
            order = np.random.default_rng(np.random.SeedSequence([seed, slot])).permutation(20)
            row = by_slot[slot]
            case = {"case_id": case_id, "job_id": job["job_id"], "slot": slot,
                    "source_pts": row["source_pts"], "source_time_base": row["source_time_base"],
                    "target_time": row["target_time"], "source_image": str(run / "targets" / row["frame"]),
                    "entries": [{**pool[int(index)], "id": f"P{i + 1:02d}"} for i, index in enumerate(order)]}
            if mode == "sequential":
                for entry in case["entries"]:
                    entry["photo_id"] = photo_ids[entry["view_id"]]
            cases.append(case)
            job["cases"].append({"case_id": case_id, "image": str(root / "sheets" / f"{case_id}.png")})
        job["output"] = str(root / "choices" / f"{job['job_id']}.jsonl")
        write_json(root / "jobs" / f"{job['job_id']}.json", job)
        jobs.append(job["job_id"])
    regular = font(source["config"]["font_regular"], 18)
    bold = font(source["config"]["font_bold"], 20)
    stops = {job["cases"][-1]["case_id"]: job["job_id"] for job in
             [json.loads((root / "jobs" / f"{name}.json").read_text(encoding="utf-8")) for name in jobs]}
    def make_sheet(case):
        destination = root / "sheets" / f"{case['case_id']}.png"
        if not destination.exists():
            comparison_sheet(case, root, regular, bold)
        return case["case_id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        for case_id in pool.map(make_sheet, cases):
            if case_id in stops:
                (root / "jobs" / f"{stops[case_id]}.ready").touch()
                print(f"Job ready: {stops[case_id]}", flush=True)
    sheet_photo.cache_clear()
    write_json(root / "private-map.json", {"experiment_id": experiment_id, "cases": cases})
    manifest = {"experiment_id": experiment_id, "version": version, "mode": mode, "status": "awaiting_choices",
                "source_run": str(run), "target_set_id": source["target_set_id"], "index_id": source["index_id"],
                "start": start, "duration": duration, "frames": len(targets), "top_k": 20,
                "seed": seed, "workers": workers, "jobs": jobs, "frame_rate": target["frame_rate"],
                "context_frames": 10 if mode == "sequential" else 0,
                "canonical_size": target["canonical_size"], "photo_cache": photo_counts,
                "network": "none; existing local PNG/RGB only", "prepare_seconds": round(time.perf_counter() - started, 3)}
    if independent_run is not None:
        manifest["independent_run"] = str(independent_run)
    write_json(root / "manifest.json", manifest)
    return manifest


def read_choices(root, cases, job_ids):
    expected = {case["case_id"]: case for case in cases}
    result = {}
    for job_id in job_ids:
        path = root / "choices" / f"{job_id}.jsonl"
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            choice = json.loads(line)
            case_id, candidate = choice["case_id"], choice["candidate_id"]
            case = expected.get(case_id)
            if case is None or case["job_id"] != job_id or case_id in result:
                raise ValueError(f"Unexpected or duplicate choice: {case_id}")
            entries = {r["id"]: r for r in case["entries"]}
            if candidate not in entries:
                raise ValueError(f"Choice outside presented candidates: {case_id} {candidate}")
            result[case_id] = entries[candidate]
    if result.keys() != expected.keys():
        raise ValueError(f"Awaiting {len(expected.keys() - result.keys())} frame choices")
    return result


def transition_counts(rows):
    ids = [row["view_id"] for row in rows]
    runs = [1] if ids else []
    for previous, current in zip(ids, ids[1:]):
        if previous == current:
            runs[-1] += 1
        else:
            runs.append(1)
    return {"photo_changes": max(0, len(runs) - 1),
            "a_b_a_returns": sum(ids[i] == ids[i - 2] and ids[i] != ids[i - 1] for i in range(2, len(ids))),
            "longest_hold_frames": max(runs, default=0)}


def collect(root):
    root = root.resolve()
    manifest = read_manifest(root)
    cases = json.loads((root / "private-map.json").read_text(encoding="utf-8"))["cases"]
    choices = read_choices(root, cases, manifest["jobs"])
    cases.sort(key=lambda r: r["slot"])
    run = Path(manifest["source_run"])
    source = read_manifest(run)
    ms = {r["slot"]: r for r in pq.read_table(run / "selected.parquet").to_pylist()}
    records = []
    for case in cases:
        choice = choices[case["case_id"]]
        records.append({**choice, "case_id": case["case_id"], "slot": case["slot"],
                        "source_pts": case["source_pts"], "source_time_base": case["source_time_base"],
                        "target_time": case["target_time"], "selection_method": "astra-" + manifest.get("mode", "independent"),
                        "ms_ssim_view_id": ms[case["slot"]]["view_id"]})
    pq.write_table(pa.Table.from_pylist(records), root / "selected.parquet")
    manifest.update(status="selected", reviewer="gpt-6-astra", reasoning_effort="low",
                    different_from_ms_ssim=sum(r["view_id"] != r["ms_ssim_view_id"] for r in records),
                    different_from_mse=sum(r["rank"] != 1 for r in records),
                    transitions={"astra": transition_counts(records),
                                 "ms_ssim": transition_counts([ms[c["slot"]] for c in cases])})
    write_json(root / "manifest.json", manifest)
    return manifest, cases, records, ms


def render(root):
    root = root.resolve()
    manifest = read_manifest(root)
    if manifest["status"] == "complete":
        return manifest
    manifest, cases, records, ms = collect(root)
    run = Path(manifest["source_run"])
    source = read_manifest(run)
    started = time.perf_counter()
    config = {**source["config"], "encoder_threads": 4}
    regular = font(config["font_regular"], 16)
    bold = font(config["font_bold"], 21)
    temporary = root / "comparison.tmp.mp4"
    independent = None
    if manifest.get("independent_run"):
        baseline = Path(manifest["independent_run"])
        independent = {r["slot"]: r for r in pq.read_table(baseline / "selected.parquet").to_pylist()}
    with (root / "encode.log").open("wb") as log:
        process = encoder(temporary, 1920, 528, manifest["frame_rate"], config, log)
        try:
            for i, (case, record) in enumerate(zip(cases, records)):
                mse = min(case["entries"], key=lambda r: r["rank"])
                panels = [("ORIGINAL", case["source_image"], None),
                          (("ASTRA INDEPENDENT" if independent is not None else "MSE"),
                           (baseline / "photos" / f"{independent[case['slot']]['view_id']}.png" if independent is not None
                            else root / "photos" / f"{mse['view_id']}.png"),
                           (independent[case["slot"]]["rank"] if independent is not None else mse["rank"])),
                          ("MS-SSIM", root / "photos" / f"{ms[case['slot']]['view_id']}.png", ms[case["slot"]]["rank"]),
                          (("ASTRA SEQUENTIAL" if independent is not None else "ASTRA"),
                           root / "photos" / f"{record['view_id']}.png", record["rank"])]
                canvas = Image.new("RGB", (1920, 528), BG)
                draw = ImageDraw.Draw(canvas)
                for p, (label, path, rank) in enumerate(panels):
                    x = p * 480
                    draw.text((x + 12, 10), label, font=bold, fill=INK)
                    canvas.paste(resized(path, 480, 358), (x, 48))
                    if rank is not None:
                        draw.text((x + 12, 420), f"MSE #{rank:02d} / 20", font=regular, fill=MUTED)
                draw.text((12, 472), f"{case['target_time']:.3f}s   {i + 1:03d} / {len(cases)}", font=regular, fill=INK)
                if independent is not None:
                    draw.text((600, 472), f"SEG {int(case['job_id'].split('-')[-1]):02d} / {manifest['workers']:02d}",
                              font=regular, fill=MUTED)
                draw.text((1908, 506), "Contains modified Copernicus Sentinel data 2023-2026", font=regular, fill=MUTED, anchor="ra")
                if i == 90:
                    canvas.save(root / "preview.png")
                process.stdin.write(canvas.tobytes())
            process.stdin.close()
            if process.wait() != 0:
                raise ValueError("Encoding failed; see encode.log")
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait()
    temporary.replace(root / "comparison.mp4")
    manifest.update(status="complete", reviewer="gpt-6-astra", reasoning_effort="low",
                    audio="silent", panels=["ORIGINAL", "ASTRA INDEPENDENT", "MS-SSIM", "ASTRA SEQUENTIAL"]
                    if independent is not None else ["ORIGINAL", "MSE", "MS-SSIM", "ASTRA"],
                    different_from_ms_ssim=sum(r["view_id"] != r["ms_ssim_view_id"] for r in records),
                    different_from_mse=sum(r["rank"] != 1 for r in records),
                    render_seconds=round(time.perf_counter() - started, 3))
    manifest["transitions"] = {"astra": transition_counts(records),
                               "ms_ssim": transition_counts([ms[c["slot"]] for c in cases])}
    if independent is not None:
        manifest["transitions"]["independent_astra"] = transition_counts([independent[c["slot"]] for c in cases])
        manifest["different_from_independent"] = sum(r["view_id"] != independent[c["slot"]]["view_id"]
                                                     for c, r in zip(cases, records))
    write_json(root / "manifest.json", manifest)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "collect", "render"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run", type=Path, default=Path("data/runs/p6-grid32-full"))
    parser.add_argument("--start", type=float, default=38)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--all-frames", action="store_true")
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--mode", choices=("independent", "sequential"), default="independent")
    parser.add_argument("--independent-run", type=Path)
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.run, args.output, 0 if args.all_frames else args.start,
                         None if args.all_frames else args.duration, args.seed, args.workers, args.mode, args.independent_run)
    elif args.stage == "collect":
        result = collect(args.output)[0]
    else:
        result = render(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
