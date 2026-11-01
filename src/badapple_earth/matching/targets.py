"""Decode only the requested local segment and choose the closest actual PTS."""

from fractions import Fraction
import json
import math
from pathlib import Path

import av
import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq

from ..collection.windows import identity
from ..evidence import checksum, contact_sheet, write_json
from ..features import FEATURE_VERSION, extract, image_size


def prefer_previous(previous_time, current_time, target_time):
    return previous_time is not None and target_time - previous_time <= current_time - target_time


def targets(config: dict, output: Path):
    source = Path(config["input"])
    if not source.is_file():
        raise ValueError("Input MP4 does not exist")
    start, duration = Fraction(str(config["start_seconds"])), Fraction(str(config["duration_seconds"]))
    rate = Fraction(str(config["sample_fps"]))
    if start < 0 or duration <= 0 or rate <= 0:
        raise ValueError("Start must be nonnegative; duration and sample fps must be positive")
    crop = config["crop"]
    left, top, width, height = crop
    if any(type(value) is not int for value in crop) or left < 0 or top < 0 or width < 1 or height < 1:
        raise ValueError("Crop must be nonnegative integer offsets and positive integer dimensions")
    aspect = [width, height]
    output.mkdir(parents=True, exist_ok=False)
    (output / "frames").mkdir()
    fingerprint = checksum(source)
    target_set_id = identity([fingerprint, config, FEATURE_VERSION])
    times = [start + index / rate for index in range(math.ceil(duration * rate))]
    rows, coarse_values, fine_values = [], [], []
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        origin = (stream.start_time or 0) * stream.time_base
        if stream.duration is not None and start + duration > stream.duration * stream.time_base:
            raise ValueError("Requested interval exceeds the video stream")
        if left + width > stream.width or top + height > stream.height:
            raise ValueError("Crop is outside the decoded image")
        sar = stream.sample_aspect_ratio
        if sar and sar != 1:
            raise ValueError("Confirm a display-aspect crop for this non-square-pixel input first")
        if float(stream.metadata.get("rotate", 0)) != 0:
            raise ValueError("Confirm an oriented crop for this rotated input first")
        seek_time = max(Fraction(0), start - 1)
        container.seek(int((origin + seek_time) / stream.time_base), stream=stream, backward=True)
        previous, previous_time = None, None
        next_target = 0
        for frame in container.decode(stream):
            if frame.pts is None:
                raise ValueError("Decoded frame has no presentation timestamp")
            current_time = frame.pts * frame.time_base - origin
            if previous_time is not None and current_time < previous_time:
                raise ValueError("Presentation timestamps are not ordered")
            while next_target < len(times) and current_time >= times[next_target]:
                chosen = previous if prefer_previous(previous_time, current_time, times[next_target]) else frame
                chosen_time = chosen.pts * chosen.time_base
                image = chosen.to_image().convert("RGB").crop((left, top, left + width, top + height))
                image = image.resize(image_size(256, aspect), Image.Resampling.BOX)
                coarse, fine, _ = extract(image, aspect)
                filename = f"frames/{next_target:04d}.png"
                image.save(output / filename)
                rows.append({"target_id": identity([target_set_id, next_target]), "slot": next_target,
                             "target_time": float(times[next_target]), "source_pts": chosen.pts,
                             "source_time_base": str(chosen.time_base), "selected_source_time": float(chosen_time),
                             "frame": filename, "frame_sha256": checksum(output / filename)})
                coarse_values.append(coarse)
                fine_values.append(fine)
                next_target += 1
            if next_target == len(times):
                break
            previous, previous_time = frame, current_time
        if next_target != len(times):
            raise ValueError("Not enough decoded frames for the requested target times")
        stream_info = {"time_base": str(stream.time_base), "start_time": float(origin),
                       "average_rate": str(stream.average_rate), "width": stream.width,
                       "height": stream.height, "has_audio": bool(container.streams.audio)}
    np.save(output / "gray_coarse.npy", np.stack(coarse_values), allow_pickle=False)
    np.save(output / "gray_fine.npy", np.stack(fine_values), allow_pickle=False)
    pq.write_table(pa.Table.from_pylist(rows), output / "targets.parquet")
    manifest = {"target_set_id": target_set_id, "input_sha256": fingerprint, "input_name": source.name,
                "config": {key: value for key, value in config.items() if key != "input"},
                "feature_version": FEATURE_VERSION, "aspect_ratio": aspect,
                "canonical_size": list(image_size(256, aspect)), "count": len(rows), "stream": stream_info}
    write_json(output / "manifest.json", manifest)
    contact_sheet([(output / row["frame"], f"{row['target_time']:.3f}s") for row in rows], output / "contact-sheet.jpg")
    return manifest
