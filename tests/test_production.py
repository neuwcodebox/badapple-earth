from fractions import Fraction
import json
from pathlib import Path
import shutil
import subprocess

import av
import numpy as np
from PIL import Image
import pyarrow.parquet as pq
import pytest

from badapple_earth.matching.targets import targets


def test_all_targets_keep_every_frame_and_its_original_pts(tmp_path):
    video = tmp_path / "source.mp4"
    with av.open(str(video), "w") as container:
        stream = container.add_stream("libx264", rate=Fraction(30000, 1001))
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for i in range(7):
            frame = av.VideoFrame.from_image(Image.new("RGB", (64, 48), (i * 30, 0, 0)))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    config = {"input": str(video), "crop": [0, 0, 64, 48], "frame_mode": "all"}
    result = targets(config, tmp_path / "targets")
    rows = pq.read_table(tmp_path / "targets/targets.parquet").to_pylist()
    with av.open(str(video)) as container:
        original = [frame.pts for frame in container.decode(video=0)]
    assert result["count"] == 7 and result["frame_rate"] == "30000/1001"
    assert [row["source_pts"] for row in rows] == original
    assert len(list((tmp_path / "targets/frames").glob("*.png"))) == 7
    assert targets(config, tmp_path / "targets")["target_set_id"] == result["target_set_id"]
    video.write_bytes(video.read_bytes() + b"changed-input-identity")
    with pytest.raises(ValueError, match="another input"):
        targets(config, tmp_path / "targets")


@pytest.mark.parametrize("outputs",[("presentation","satellite"),("presentation",)])
def test_encoded_segments_keep_all_frames_and_resume_without_encoding_again(tmp_path, monkeypatch,outputs):
    from badapple_earth.production import render
    project = Path(__file__).resolve().parents[1]
    config = json.loads((project / "config/production.json").read_text())
    if not shutil.which("ffmpeg") or not (project / config["font_regular"]).exists():
        pytest.skip("FFmpeg and a local render font are required")
    config["font_regular"] = str(project / config["font_regular"])
    config["font_bold"] = str(project / config["font_bold"])
    config.update(preset="ultrafast", crop=[0, 0, 64, 48],render_outputs=outputs)
    (tmp_path / "segments").mkdir()
    Image.new("RGB", (64, 48), "white").save(tmp_path / "photo.png")
    rows = [{"slot": i, "source_pts": i * 1001, "source_time_base": "1/30000", "time": i*1001/30000,
             "total": 3, "duration": .1001, "places": 10, "source_image": "photo.png",
             "selected_image": "photo.png", "caption": "Real image metadata",
             "candidates": [{"path": "photo.png", "rank": n+1, "raw_mse": .01} for n in range(5)]} for i in range(3)]
    result = render.segment(str(tmp_path), rows, "30000/1001", config, "render")
    assert ("satellite" in result)==("satellite" in outputs)
    if "satellite" not in outputs:assert not list((tmp_path/"segments").glob("satellite-*.mp4"))
    for kind, size in [("presentation", (1920, 1080)), ("satellite", (64, 48))]:
        if kind not in outputs:continue
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=nb_frames,width,height,avg_frame_rate",
                                "-of", "json", str(tmp_path / "segments" / result[kind])], capture_output=True, check=True)
        stream = json.loads(probe.stdout)["streams"][0]
        assert int(stream["nb_frames"]) == 3 and stream["avg_frame_rate"] == "30000/1001"
        assert (stream["width"], stream["height"]) == size
    def forbidden(*args, **kwargs):
        raise AssertionError("A completed segment must be reused")
    monkeypatch.setattr(render, "encoder", forbidden)
    assert render.segment(str(tmp_path), rows, "30000/1001", config, "render") == result
