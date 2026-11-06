"""Encode every source frame in restartable chunks, then mux original audio."""

from concurrent.futures import ProcessPoolExecutor, as_completed
from fractions import Fraction
import json
from multiprocessing import get_context
from pathlib import Path
import subprocess

from ..collection.bundle import read_manifest
from ..collection.windows import identity
from ..evidence import write_json
from . import layout

LAYOUT_VERSION = "compact-hero-inline-provenance-v5"


def absolute_plan(row, root):
    return {**row, "source_image": str(root / row["source_image"]),
            "selected_image": str(root / row["selected_image"]),
            "candidates": [{**c, "path": str(root / c["path"])} for c in row["candidates"]]}


def encoder(path, width, height, fps, config, log):
    return subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-f", "rawvideo",
        "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-framerate", fps, "-i", "pipe:0",
        "-an", "-c:v", "libx264", "-threads", str(config["encoder_threads"]), "-preset", config["preset"],
        "-crf", str(config["crf"]), "-pix_fmt", "yuv420p", "-video_track_timescale", "30000", str(path)
    ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log)


def segment(root_string, rows, fps, config, render_id):
    root = Path(root_string)
    first, stop = rows[0]["slot"], rows[-1]["slot"] + 1
    directory = root / "segments"
    marker = directory / f"{first:06d}-{stop:06d}.json"
    paths = {name: directory / f"{name}-{first:06d}-{stop:06d}.mp4" for name in ("presentation", "satellite")}
    if marker.exists() and all(path.exists() for path in paths.values()):
        saved = json.loads(marker.read_text(encoding="utf-8"))
        if saved["render_id"] != render_id:
            raise ValueError("Completed segments belong to a different render configuration")
        return saved
    processes, handles, temporary = {}, [], {}
    try:
        for name, path in paths.items():
            temporary[name] = path.with_suffix(".tmp.mp4")
            handle = path.with_suffix(".log").open("wb")
            handles.append(handle)
            width, height = (1920, 1080) if name == "presentation" else tuple(config["crop"][2:])
            processes[name] = encoder(temporary[name], width, height, fps, config, handle)
        for row in rows:
            plan = absolute_plan(row, root)
            presentation = layout.frame(plan, config)
            processes["presentation"].stdin.write(presentation.tobytes())
            photo = layout.resized(plan["selected_image"], *config["crop"][2:])
            if config.get("monochrome"):photo = photo.convert("L").convert("RGB")
            processes["satellite"].stdin.write(photo.tobytes())
        for process in processes.values():
            process.stdin.close()
        for name, process in processes.items():
            if process.wait() != 0:
                raise ValueError(f"Segment encoding failed; see {paths[name].with_suffix('.log')}")
        for name, path in paths.items():
            temporary[name].replace(path)
        result = {"render_id": render_id, "first": first, "stop": stop, "frames": len(rows),
                  "first_source_pts": rows[0]["source_pts"], "last_source_pts": rows[-1]["source_pts"],
                  "presentation": paths["presentation"].name, "satellite": paths["satellite"].name}
        write_json(marker, result)
        return result
    finally:
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
                process.wait()
        for handle in handles:
            handle.close()


def mux(root, parts, kind, destination, config):
    listing = root / "segments" / f"{kind}-concat.txt"
    listing.write_text("".join(f"file '{part[kind]}'\n" for part in parts), encoding="utf-8")
    temporary = destination.with_suffix(".tmp.mp4")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(listing)]
    if config["audio_mode"] == "source_local":
        if config.get("end_seconds") is not None:
            command.extend(["-t",str(config["end_seconds"])])
        command.extend(["-i", str(Path(config["input"])), "-map", "0:v:0", "-map", "1:a:0?", "-c:a", "copy"])
    elif config["audio_mode"] == "silent":
        command.append("-an")
    else:
        raise ValueError("Production audio_mode must be source_local or silent")
    command.extend(["-c:v", "copy", "-map_metadata", "-1", "-movflags", "+faststart", str(temporary)])
    subprocess.run(command, capture_output=True, check=True)
    temporary.replace(destination)


def render(root: Path, config, rows):
    target = read_manifest(root / "targets")
    search = read_manifest(root / "search")
    render_id = identity([target["target_set_id"], search["run_id"], config, LAYOUT_VERSION])
    if [row["slot"] for row in rows] != list(range(target["count"])):
        raise ValueError("Frame plan must contain every source frame in order")
    (root / "segments").mkdir(exist_ok=True)
    chunk = config["segment_frames"]
    parts = []
    if config.get("pause_file"):
        from .silhouette.control import batches
        def saved(_,part):
            parts.append(part)
            print(f"Encoded source frames: {sum(p['frames'] for p in parts):,}/{len(rows):,} (two outputs)",flush=True)
        batches([((str(root.resolve()),rows[begin:begin+chunk],target["frame_rate"],config,render_id),begin)
                 for begin in range(0,len(rows),chunk)],segment,saved,config["render_workers"],config["pause_file"])
    else:
        with ProcessPoolExecutor(max_workers=config["render_workers"], mp_context=get_context("spawn")) as pool:
            jobs = [pool.submit(segment, str(root.resolve()), rows[begin:begin+chunk], target["frame_rate"], config, render_id)
                    for begin in range(0, len(rows), chunk)]
            for future in as_completed(jobs):
                part = future.result()
                parts.append(part)
                print(f"Encoded source frames: {sum(p['frames'] for p in parts):,}/{len(rows):,} (two outputs)", flush=True)
    parts.sort(key=lambda part: part["first"])
    if sum(part["frames"] for part in parts) != target["count"]:
        raise ValueError("Encoded segments do not cover all source frames")
    mux(root, parts, "presentation", root / "final-16x9.mp4", config)
    mux(root, parts, "satellite", root / "satellite-full.mp4", config)
    # A representative real frame is a thumbnail, never an added video frame.
    preview_slot = min(len(rows)-1, round(41 * float(Fraction(target["frame_rate"]))))
    layout.frame(absolute_plan(rows[preview_slot], root), config).save(root / "preview.png")
    return {"render_id": render_id, "frames": len(rows), "frame_rate": target["frame_rate"],
            "video_duration_seconds": target["duration_seconds"], "audio_mode": config["audio_mode"],
            "audio_policy": ("original local track prefix copied to the requested end; no audio reencoding"
                             if config.get("end_seconds") is not None else
                             "original local track copied in full; original trailing audio may outlast video slightly"),
            "presentation": "final-16x9.mp4", "satellite": "satellite-full.mp4", "preview": "preview.png",
            "layout": LAYOUT_VERSION, "frame_mapping": "one encoded video frame per decoded source frame; no dropping, duplication or interpolation"}
