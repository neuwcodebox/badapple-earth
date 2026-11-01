import numpy as np
from PIL import Image
import pytest
import rasterio
from rasterio.transform import Affine, from_origin
from rasterio.windows import Window

from badapple_earth.imagery import SceneReader, candidate_windows, check_quality, stable_id, window_location
from badapple_earth.sample import validate_config
from badapple_earth.sources.sentinel import safe_asset


def test_window_center_and_footprint_use_pixel_edges_with_rotated_affine():
    transform = Affine(0.01, 0.002, 120, 0.001, -0.01, 40)
    location = window_location(transform, "EPSG:4326", Window(2, 4, 6, 8))
    assert location["longitude"] == pytest.approx(120.066)
    assert location["latitude"] == pytest.approx(39.925)
    corners = location["footprint"]["coordinates"][0]
    assert corners[0] == pytest.approx([120.028, 39.962])
    assert corners[-1] == corners[0]


def test_projected_center_is_longitude_latitude():
    location = window_location(from_origin(499980, 20, 10, 10), "EPSG:32633", Window(0, 0, 4, 4))
    assert location["longitude"] == pytest.approx(15)
    assert location["latitude"] == pytest.approx(0)


def test_water_snow_terrain_shadows_are_retained_and_clouds_are_rejected():
    valid = np.ones((2, 3), dtype=bool)
    quality = check_quality(valid, np.array([[2, 6, 11], [4, 5, 7]]), 0.02)
    assert quality["reasons"] == []
    quality = check_quality(valid, np.array([[3, 8, 9], [10, 0, 1]]), 0.02)
    assert set(quality["reasons"]) == {"cloud_contamination", "invalid_scl"}
    valid[0, 0] = False
    assert "partial_valid" in check_quality(valid, np.full((2, 3), 6), 0.02)["reasons"]


def test_native_rgb_read_and_scl_alignment_are_reproducible(tmp_path):
    rgb = np.arange(3 * 48 * 64, dtype=np.uint16).reshape(3, 48, 64) % 250 + 1
    rgb = rgb.astype(np.uint8)
    rgb_path, scl_path = tmp_path / "rgb.tif", tmp_path / "scl.tif"
    with rasterio.open(rgb_path, "w", driver="GTiff", width=64, height=48, count=3,
                       dtype="uint8", crs="EPSG:32633", transform=from_origin(500000, 1000, 10, 10), nodata=0) as dst:
        dst.write(rgb)
    scl = np.full((24, 32), 6, dtype=np.uint8)
    scl[:, 16:] = 8
    with rasterio.open(scl_path, "w", driver="GTiff", width=32, height=24, count=1,
                       dtype="uint8", crs="EPSG:32633", transform=from_origin(500000, 1000, 20, 20), nodata=0) as dst:
        dst.write(scl, 1)
    with SceneReader(str(rgb_path), str(scl_path)) as reader:
        image, quality, checksum = reader.read(Window(16, 4, 32, 24), 16, 0.02)
    expected = Image.fromarray(np.moveaxis(rgb[:, 4:28, 16:48], 0, 2)).resize((16, 12), Image.Resampling.BOX)
    assert np.array_equal(image, expected)
    assert quality["local_cloud_ratio"] == pytest.approx(0.5)
    with SceneReader(str(rgb_path), str(scl_path)) as reader:
        reread, _, checksum2 = reader.read(Window(16, 4, 32, 24), 16, 0.02)
    assert checksum == checksum2
    assert np.array_equal(image, reread)


def test_candidate_windows_are_unique_inside_scene_with_centers_in_aoi():
    class Scene:
        crs = "EPSG:4326"
        transform = from_origin(120, 40, 0.001, 0.001)
        width = height = 2000
    area = {"bbox": [120.3, 38.3, 121.7, 39.7]}
    windows = list(candidate_windows(Scene(), area, [256, 512, 1024], [1444, 1080]))
    assert len(windows) > 100
    assert len(set(tuple(w.flatten()) for w in windows)) == len(windows)
    for window in windows:
        assert window.col_off >= 0 and window.row_off >= 0
        assert window.col_off + window.width <= 2000
        assert window.row_off + window.height <= 2000
        assert window.height == round(window.width * 1080 / 1444)
        point = window_location(Scene.transform, Scene.crs, window)
        assert 120.3 <= point["longitude"] <= 121.7
        assert 38.3 <= point["latitude"] <= 39.7


def test_tile_identity_is_stable_and_distinguishes_source_windows():
    scene = {"source_id": "source", "collection_id": "collection", "item_id": "item"}
    window = Window(0, 0, 256, 192)
    assert stable_id(scene, window) == stable_id(dict(scene), Window(*window.flatten()))
    assert stable_id(scene, window) != stable_id(scene, Window(1, 0, 256, 192))
    assert stable_id(scene, window) != stable_id({**scene, "source_id": "another"}, window)


def test_candidates_exclude_centers_outside_real_scene_footprint():
    class Scene:
        crs = "EPSG:4326"
        transform = from_origin(120, 40, 0.001, 0.001)
        width = height = 2000
    area = {"bbox": [120.3, 38.3, 121.7, 39.7]}
    # The valid data covers a small triangular part of the bounding box.
    footprint = {"type": "Polygon", "coordinates": [
        [[120.3, 38.3], [121.7, 38.3], [120.3, 39.7], [120.3, 38.3]]
    ]}
    windows = list(candidate_windows(Scene(), area, [256], [4, 3], footprint))
    assert windows
    for window in windows:
        point = window_location(Scene.transform, Scene.crs, window)
        assert point["longitude"] + point["latitude"] <= 160.0001


@pytest.mark.parametrize("href", [
    "https://other.example/visual.tif", "s3://e84-earth-search-sentinel-data/visual.tif",
    "https://e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com/v.tif?token=secret",
    "https://user:secret@e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com/v.tif",
])
def test_unapproved_or_signed_assets_are_rejected(href):
    with pytest.raises(ValueError):
        safe_asset(href)


def test_small_sample_limits_and_cost_policy_are_enforced():
    import json
    from pathlib import Path
    config = json.loads((Path(__file__).parents[1] / "config/p1.json").read_text())
    validate_config(config, 120)
    with pytest.raises(ValueError):
        validate_config(config, 501)
    with pytest.raises(ValueError):
        validate_config({**config, "requester_pays": True}, 120)
