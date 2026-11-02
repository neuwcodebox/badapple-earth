"""Five-scale SSIM selection among the actual displayed MSE candidates."""

from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import lru_cache
from multiprocessing import get_context
from pathlib import Path
import time

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter

from ..evidence import write_json

VERSION = "ms-ssim-five-gaussian11-1.5-reflect2-v1"
WEIGHTS = np.array([0.0448, 0.2856, 0.3001, 0.2363, 0.1333], dtype=np.float64)


def blur(image):
    return gaussian_filter(image, sigma=1.5, radius=5, mode="reflect")


def pyramid(image):
    raw = np.asarray(image)
    current = raw.astype(np.float32)
    if raw.dtype == np.uint8:
        current = current / 255
    result = []
    for level in range(5):
        if min(current.shape) < 11:
            raise ValueError("Five-scale SSIM requires at least 11 pixels at its coarsest scale")
        mean = blur(current)
        variance = np.maximum(blur(current * current) - mean * mean, 0)
        result.append((current, mean, variance))
        if level < 4:
            padded = np.pad(current, ((0, current.shape[0] % 2), (0, current.shape[1] % 2)), mode="reflect")
            current = padded.reshape(padded.shape[0]//2, 2, padded.shape[1]//2, 2).mean(axis=(1, 3))
    return result


def similarity(left, right):
    values = []
    for level, ((x, mx, vx), (y, my, vy)) in enumerate(zip(left, right)):
        covariance = blur(x * y) - mx * my
        cs = (2 * covariance + .03**2) / (vx + vy + .03**2)
        if level == 4:
            luminance = (2 * mx * my + .01**2) / (mx * mx + my * my + .01**2)
            cs = cs * luminance
        # Valid Gaussian window; negative fractional powers are undefined.
        values.append(float(np.clip(np.mean(cs[5:-5, 5:-5], dtype=np.float64), 1e-8, 1)))
    return float(np.prod(np.asarray(values)**WEIGHTS))


@lru_cache(maxsize=160)
def image_stats(filename):
    with Image.open(filename) as image:
        return pyramid(np.asarray(image.convert("L"), dtype=np.float32) / 255)


def best_index(scores, candidates):
    # Preserve MSE order on an equal structural score. Never force a different
    # winner merely to animate the connecting line.
    return min(range(len(scores)), key=lambda i: (-scores[i], candidates[i]["rank"], candidates[i]["view_id"]))


def evaluate_chunk(jobs):
    results = []
    for slot, source, candidates in jobs:
        with Image.open(source) as image:
            target = pyramid(np.asarray(image.convert("L"), dtype=np.float32) / 255)
        scores = [similarity(target, image_stats(candidate["path"])) for candidate in candidates]
        results.append({"slot": slot, "selected_index": best_index(scores, candidates), "scores": scores})
    return results


def rerank(root, targets, inverse, top, config):
    method = config.get("selection_method", "mse")
    if method == "mse":
        return [{"slot": row["slot"], "selected_index": 0,
                 "scores": [candidate["raw_mse"] for candidate in top[int(query)]]} for row, query in zip(targets, inverse)]
    if method != "ms-ssim":
        raise ValueError("selection_method must be mse or ms-ssim")
    directory = root / "rerank"
    directory.mkdir(exist_ok=True)
    chunk = 180
    collected, pending = [], []
    for begin in range(0, len(targets), chunk):
        end = min(len(targets), begin + chunk)
        path = directory / f"{begin:06d}-{end:06d}.json"
        if path.exists():
            import json
            saved = json.loads(path.read_text(encoding="utf-8"))
            if saved["version"] != VERSION:
                raise ValueError("Reranking checkpoint belongs to another metric version")
            collected.extend(saved["frames"])
            continue
        jobs = []
        for row, query in zip(targets[begin:end], inverse[begin:end]):
            candidates = top[int(query)]
            jobs.append((row["slot"], str(root / "targets" / row["frame"]),
                         [{"path": str(root / "assets/thumbs" / f"{c['view_id']}.png"),
                           "rank": c["rank"], "view_id": c["view_id"]} for c in candidates]))
        pending.append((path, jobs))
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=config.get("rerank_workers", 8), mp_context=get_context("spawn")) as pool:
        futures = {pool.submit(evaluate_chunk, jobs): path for path, jobs in pending}
        for future in as_completed(futures):
            results = future.result()
            write_json(futures[future], {"version": VERSION, "frames": results})
            collected.extend(results)
            print(f"MS-SSIM selection: {len(collected):,}/{len(targets):,} source frames", flush=True)
    collected.sort(key=lambda row: row["slot"])
    distribution = np.bincount([row["selected_index"] for row in collected], minlength=5).tolist()
    write_json(directory / "manifest.json", {"version": VERSION, "source_frames": len(collected),
               "selected_display_positions": distribution, "different_from_mse": len(collected)-distribution[0],
               "elapsed_seconds_last_run": round(time.perf_counter()-started, 3),
               "interpretation": "candidate reranking experiment; perceptual playback improvement not assessed"})
    return collected
