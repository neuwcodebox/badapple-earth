"""Cache required real photos and encode held frames with local FFmpeg."""

import json
from pathlib import Path
import subprocess

import numpy as np
from PIL import Image, ImageDraw
import pyarrow as pa
import pyarrow.parquet as pq

from ..evidence import checksum, contact_sheet, write_json
from .photos import PhotoCache
from .views import rotate_image


def materialize(target_root: Path, index_root: Path, run_root: Path):
    run = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    targets = json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))
    index = json.loads((index_root / "manifest.json").read_text(encoding="utf-8"))
    if run["target_set_id"] != targets["target_set_id"] or run["index_id"] != index["index_id"]:
        raise ValueError("Run, targets and index identities differ")
    rows = pq.read_table(run_root / "matches.parquet").to_pylist()
    by_slot = {}
    for row in rows:
        by_slot.setdefault(row["slot"], []).append(row)
    evaluation = set(np.linspace(0, targets["count"] - 1, min(20, targets["count"]), dtype=int).tolist())
    (run_root / "photos").mkdir(exist_ok=True)
    failures = []
    selected, comparisons = [], {}
    with PhotoCache(index_root, index) as photos:
        def photo(row):
            destination = run_root / "photos" / (row["view_id"] + ".png")
            if destination.exists():
                return destination
            image = rotate_image(photos.base(row), row["rotation_degrees"], index["canonical_size"],
                                 row.get("rotation_crop"))
            temporary = destination.with_suffix(".tmp")
            image.save(temporary, format="PNG")
            temporary.replace(destination)
            return destination

        for slot, candidates in sorted(by_slot.items()):
            candidates.sort(key=lambda row: row["rank"])
            chosen = None
            for row in candidates:
                try:
                    path = photo(row)
                    chosen = {**row, "photo": path.relative_to(run_root).as_posix(),
                              "photo_sha256": checksum(path),
                              "fallback_reason": None if row["rank"] == 1 else "higher_rank_materialization_failed"}
                    break
                except (OSError, ValueError) as error:
                    failures.append({"slot": slot, "tile_id": row["tile_id"], "rank": row["rank"],
                                     "error_type": type(error).__name__})
            if chosen is None:
                raise ValueError(f"No candidate could be materialized for target slot {slot}")
            selected.append(chosen)
            if slot in evaluation:
                paths = []
                for row in candidates[:5]:
                    try:
                        paths.append((photo(row), f"rank {row['rank']} {row['rotation_degrees']}deg MSE {row['raw_mse']:.4f}"))
                    except (OSError, ValueError) as error:
                        failures.append({"slot": slot, "tile_id": row["tile_id"], "rank": row["rank"],
                                         "stage": "comparison", "error_type": type(error).__name__})
                comparisons[slot] = paths
    pq.write_table(pa.Table.from_pylist(selected), run_root / "selected.parquet", compression="zstd")
    target_rows = pq.read_table(target_root / "targets.parquet").to_pylist()
    entries = []
    for slot, photos in comparisons.items():
        entries.append((target_root / target_rows[slot]["frame"], f"target {target_rows[slot]['target_time']:.3f}s"))
        entries.extend(photos)
    contact_sheet(entries, run_root / "top5.jpg", columns=6)
    run["materialized"] = len(selected)
    run["materialization_failures"] = failures
    run["evaluation_slots"] = sorted(evaluation)
    run["selected_unique_tiles"] = len({row["tile_id"] for row in selected})
    run["selected_unique_views"] = len({row["view_id"] for row in selected})
    run["selected_rotation_counts"] = {str(angle): sum(row["rotation_degrees"] == angle for row in selected)
                                       for angle in sorted({row["rotation_degrees"] for row in selected})}
    years = sorted({row["acquired_at"][:4] for row in selected})
    run["credits"] = [f"Contains modified Copernicus Sentinel data {year}" for year in years]
    run["source_reference"] = "https://registry.opendata.aws/sentinel-2-l2a-cogs/"
    write_json(run_root / "manifest.json", run)
    return run


def encode(frames: Path, destination: Path, sample_fps: float, output_fps: float, duration: float):
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-framerate", str(sample_fps), "-i", str(frames / "%04d.png"),
        "-vf", f"fps={output_fps},pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0",
        "-t", str(duration), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(destination),
    ], capture_output=True, check=True)


def render(target_root: Path, run_root: Path):
    target_manifest = json.loads((target_root / "manifest.json").read_text(encoding="utf-8"))
    run = json.loads((run_root / "manifest.json").read_text(encoding="utf-8"))
    if run["target_set_id"] != target_manifest["target_set_id"]:
        raise ValueError("Run and target set identities differ")
    config = target_manifest["config"]
    if config["audio_mode"] != "silent_pending_rights_review":
        raise ValueError("This first renderer supports the explicit silent mode only")
    selected = pq.read_table(run_root / "selected.parquet").to_pylist()
    selected.sort(key=lambda row: row["slot"])
    if [row["slot"] for row in selected] != list(range(target_manifest["count"])):
        raise ValueError("Selected photo slots are incomplete")
    targets = pq.read_table(target_root / "targets.parquet").to_pylist()
    for name in ["color", "gray", "comparison"]:
        (run_root / "frames" / name).mkdir(parents=True, exist_ok=True)
    for row in selected:
        slot = row["slot"]
        with Image.open(run_root / row["photo"]) as source:
            source = source.convert("RGB")
            source.save(run_root / "frames/color" / f"{slot:04d}.png")
            source.convert("L").save(run_root / "frames/gray" / f"{slot:04d}.png")
            with Image.open(target_root / targets[slot]["frame"]) as target:
                pair = Image.new("RGB", (source.width * 2, source.height))
                pair.paste(target, (0, 0))
                pair.paste(source, (source.width, 0))
                if row.get("low_visibility", False):
                    ImageDraw.Draw(pair).text((source.width + 4, source.height - 14), "LOW VISIBILITY", fill="#ff5050")
                pair.save(run_root / "frames/comparison" / f"{slot:04d}.png")
    for name in ["color", "gray", "comparison"]:
        encode(run_root / "frames" / name, run_root / f"{name}.mp4", config["sample_fps"],
               config["output_fps"], config["duration_seconds"])
    run["outputs"] = ["color.mp4", "gray.mp4", "comparison.mp4", "top5.jpg", "selected.parquet"]
    run["audio_mode"] = "silent_pending_rights_review"
    run["output_fps"] = config["output_fps"]
    run["duration_seconds"] = config["duration_seconds"]
    run["frame_mapping"] = "floor(output_time * sample_fps); hold photos, no interpolation"
    run["output_sha256"] = {name: checksum(run_root / name) for name in run["outputs"]}
    run["quality_conclusion"] = "technical_output_pending_playback_review"
    write_json(run_root / "manifest.json", run)
    return run
