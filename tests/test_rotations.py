import json

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from rasterio.transform import Affine
from rasterio.windows import Window

from badapple_earth.features import FEATURE_VERSION, extract
from badapple_earth.imagery import window_location
from badapple_earth.matching.output import materialize
from badapple_earth.matching.search import match
from badapple_earth.matching.views import rotate_image, rotation_recipe, view_metadata


def test_quarter_turn_crops_before_resizing_and_preserves_orientation():
    rgb = np.arange(6 * 8 * 3, dtype=np.uint8).reshape(6, 8, 3)
    image = Image.fromarray(rgb)
    assert rotation_recipe((8, 6), 90) == [0, 2, 6, 6]
    expected = Image.fromarray(rgb[:, 2:6]).transpose(Image.Transpose.ROTATE_270).resize((8, 6), Image.Resampling.BOX)
    actual = rotate_image(image, 90, (8, 6))
    assert actual.size == (8, 6)
    assert np.array_equal(np.asarray(actual), np.asarray(expected))
    assert np.array_equal(rotate_image(image, 180, (8, 6)), rgb[::-1, ::-1])
    with pytest.raises(ValueError):
        rotate_image(image, 45, (8, 6))


def test_rotated_view_records_the_effective_source_footprint():
    transform = Affine(10, 0, 500000, 0, -10, 1000000)
    tile = {"tile_id": "tile", "window": [100, 200, 80, 60], "footprint_json": "original", "feature_row": 7}
    scene = {"crs": "EPSG:32633", "affine": list(transform)[:6]}
    view = view_metadata(tile, scene, (8, 6), 90)
    assert view["view_source_window"] == [120, 200, 40, 60]
    assert view["window"] == tile["window"] and view["source_footprint_json"] == "original"
    expected = window_location(transform, scene["crs"], Window(120, 200, 40, 60))
    assert json.loads(view["footprint_json"]) == expected["footprint"]
    assert view["longitude"] == expected["longitude"] and view["base_feature_row"] == 7
    assert view["ground_width_m"] == pytest.approx(600, abs=1)
    assert view["ground_height_m"] == pytest.approx(400, abs=1)
    assert view["view_id"] != view_metadata(tile, scene, (8, 6), 180)["view_id"]


def test_rotation_search_and_materialization_use_the_same_pixels_without_network(tmp_path):
    index, targets, run = tmp_path / "index", tmp_path / "targets", tmp_path / "run"
    shard = index / "shards/one"
    shard.mkdir(parents=True)
    (index / "catalog").mkdir()
    targets.mkdir()
    rgb = np.random.default_rng(3).integers(0, 256, (6, 8, 3), dtype=np.uint8)
    image = Image.fromarray(rgb)
    image.save(index / "base.png")
    target = rotate_image(image, 90, (8, 6))
    target.save(targets / "target.png")
    coarse, fine, _ = extract(image, [4, 3])
    np.save(shard / "gray_coarse.npy", coarse[None])
    np.save(shard / "gray_fine.npy", fine[None])
    row = {"tile_id": "tile", "view_id": "tile", "feature_row": 0, "shard_id": "one",
           "scene_id": "scene", "aoi_id": "area", "sample_image": "base.png",
           "window": [100, 200, 80, 60], "rotation_degrees": 0,
           "footprint_json": "original", "acquired_at": "2026-01-01T00:00:00Z"}
    pq.write_table(pa.Table.from_pylist([row]), shard / "tiles.parquet")
    (index / "catalog/scene.json").write_text(json.dumps({
        "crs": "EPSG:32633", "affine": [10, 0, 500000, 0, -10, 1000000]}))
    (index / "manifest.json").write_text(json.dumps({
        "index_id": "index", "feature_version": FEATURE_VERSION, "canonical_size": [8, 6],
        "coarse_shape": [12, 16], "shards": [{"shard_id": "one", "rows": 1}],
        "config": {"aspect_ratio": [4, 3], "block_size": 2048, "max_cloud_ratio": 0.02}}))
    target_coarse, _, _ = extract(target, [4, 3])
    np.save(targets / "gray_coarse.npy", target_coarse[None])
    pq.write_table(pa.Table.from_pylist([{"target_id": "target", "slot": 0, "frame": "target.png", "target_time": 0}]),
                   targets / "targets.parquet")
    (targets / "manifest.json").write_text(json.dumps({
        "target_set_id": "targets", "feature_version": FEATURE_VERSION, "canonical_size": [8, 6], "count": 1}))
    result = match(targets, index, run, top_k=4)
    assert result["candidate_count"] == 1 and result["view_count"] == 4
    matches = pq.read_table(run / "matches.parquet").to_pylist()
    assert matches[0]["rotation_degrees"] == 90 and matches[0]["raw_mse"] == 0
    assert len({row["view_id"] for row in matches}) == 4
    materialize(targets, index, run)
    selected = pq.read_table(run / "selected.parquet").to_pylist()[0]
    with Image.open(run / selected["photo"]) as photo:
        assert np.array_equal(photo, target)
