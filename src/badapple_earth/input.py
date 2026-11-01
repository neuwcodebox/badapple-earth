"""Read-only MP4 inspection. Preview seeks are not target-frame extraction."""

import json
from pathlib import Path
import subprocess

from .evidence import checksum, contact_sheet, write_json


def inspect_input(source: Path, output: Path) -> dict:
    if not source.is_file():
        raise ValueError("Input MP4 does not exist")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(source)],
        capture_output=True, check=True, encoding="utf-8",
    )
    metadata = json.loads(probe.stdout)
    if not any(s["codec_type"] == "video" for s in metadata["streams"]):
        raise ValueError("Input has no video stream")
    duration = float(metadata["format"]["duration"])
    if duration <= 0:
        raise ValueError("Input duration must be positive")
    metadata["format"].pop("filename", None)
    output.mkdir(parents=True, exist_ok=False)
    metadata.update({"input_name": source.name, "input_sha256": checksum(source),
                     "content_crop": "not_confirmed", "test_interval": "not_confirmed",
                     "audio_rights": "not_confirmed"})
    write_json(output / "probe.json", metadata)
    entries = []
    for index in range(12):
        timestamp = index * duration / 12
        path = output / f"preview-{index:02d}.png"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", str(timestamp),
            "-i", str(source), "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=360:-2",
            str(path),
        ], capture_output=True, check=True)
        entries.append((path, f"seek preview: {timestamp:.2f}s"))
    contact_sheet(entries, output / "contact-sheet.jpg", columns=4)
    return metadata
