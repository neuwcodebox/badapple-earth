import json
from pathlib import Path

import numpy as np
from PIL import Image
import pytest

from badapple_earth.production.rerank import evaluate_chunk, pyramid, similarity
from badapple_earth.production import layout


def test_ms_ssim_prefers_the_matching_structure_and_keeps_retrieval_order_on_ties(tmp_path):
    target = np.zeros((191, 256), dtype=np.uint8)
    target[25:165, 80:190] = 255
    altered = np.flip(target, axis=1).copy()
    assert similarity(pyramid(target), pyramid(target)) == pytest.approx(1, abs=1e-6)
    assert similarity(pyramid(target), pyramid(altered)) < .99
    for name, pixels in [("source", target), ("first", altered), ("second", target), ("third", target)]:
        Image.fromarray(pixels).save(tmp_path / f"{name}.png")
    candidates = [{"path": str(tmp_path / f"{name}.png"), "rank": rank, "view_id": name}
                  for name, rank in [("first", 1), ("second", 3), ("third", 7)]]
    result = evaluate_chunk([(0, str(tmp_path / "source.png"), candidates)])[0]
    assert result["selected_index"] == 1
    assert result["scores"][1] == pytest.approx(result["scores"][2])
    assert [row["rank"] for row in candidates] == [1, 3, 7]


def test_highlight_follows_the_selected_candidate_and_counters_roll_over(tmp_path):
    project = Path(__file__).resolve().parents[1]
    config = json.loads((project / "config/production.json").read_text())
    if not (project / config["font_regular"]).exists():
        pytest.skip("A local render font is required")
    photo = tmp_path / "photo.png"
    Image.new("RGB", (256, 191), "white").save(photo)
    plan = {"source_image": str(photo), "selected_image": str(photo), "selected_candidate": 3,
            "candidates": [{"path": str(photo), "rank": i+1, "raw_mse": .01, "selection_score": .8} for i in range(5)],
            "caption": "Actual location", "time": 59.97, "duration": 232.13, "slot": 1797, "total": 6955,
            "score_label": "MS-SSIM"}
    canvas = layout.frame(plan, config)
    x, y, w, h = layout.THUMBS[3]
    assert canvas.getpixel((x-3, y-3)) == (73, 225, 194)
    x, y, w, h = layout.THUMBS[0]
    assert canvas.getpixel((x-3, y-3)) != (73, 225, 194)
    assert layout.timestamp(59.97) == "01:00.0"
