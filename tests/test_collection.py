import json
from pathlib import Path

import numpy as np
import pytest
from rasterio.transform import from_origin

from badapple_earth.collection import storage
from badapple_earth.collection.pipeline import collect
from badapple_earth.collection.windows import blocks
from badapple_earth.features import canonical_image, extract


def config():
    result = json.loads((Path(__file__).parents[1] / "config/collect.json").read_text())
    result.update(widths=[16, 32, 64], aspect_ratio=[4, 3], block_size=256,
                  preview_width=32, report_samples=5, scenes_per_aoi=1,
                  max_blocks_per_scene=100,
                  areas=[{"id": "test", "terrain_group": "test", "bbox": [0, 0, 1, 1]}])
    return result


class Scene:
    width, height = 512, 384
    crs = "EPSG:4326"
    transform = from_origin(0, 1, 1 / 512, 1 / 384)
    res = (1 / 512, 1 / 384)

    def read_masks(self, *args, out_shape):
        return np.full(out_shape, 255, np.uint8)


def scene():
    return {"item_id": "S2A_T00AAA_test_L2A", "source_id": "test", "collection_id": "test",
            "visual": "test-rgb", "scl": "test-scl", "acquired_at": "2026-01-01T00:00:00Z",
            "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}}


def test_block_halos_cover_every_global_window_once():
    settings = config()
    plans = blocks(Scene(), scene(), settings["areas"][0], settings)
    actual = [tuple(w) for p in plans for w in p["windows"]]
    expected = []
    for width in settings["widths"]:
        height = round(width * 3 / 4)
        expected.extend((x, y, width, height) for y in range(0, 385 - height, round(height / 2))
                        for x in range(0, 513 - width, round(width / 2)))
    assert len(actual) == len(set(actual))
    assert set(actual) == set(expected)
    for plan in plans:
        left, top, width, height = plan["read_window"]
        assert all(left <= w[0] and top <= w[1] and w[0] + w[2] <= left + width
                   and w[1] + w[3] <= top + height for w in plan["windows"])


def test_common_features_preserve_gray_and_shape_across_native_scales():
    for width in [256, 512, 1024]:
        rgb = np.zeros((3, round(width * 1080 / 1444), width), dtype=np.uint8)
        rgb[0] = 255
        image = canonical_image(rgb, (256, 191))
        coarse, fine, _ = extract(image, [1444, 1080])
        assert coarse.shape == (12, 16) and fine.shape == (24, 32)
        assert coarse.dtype == np.uint8
        assert np.all(coarse == 76) and np.all(fine == 76)


def test_published_shard_recovers_after_manifest_failure_without_duplicate_rows(tmp_path, monkeypatch):
    settings = config()
    index = storage.Index(tmp_path, settings)
    plan = {"job_id": "job", "windows": [[0, 0, 16, 12]]}
    index.register([plan])
    row = {"tile_id": "tile", "aoi_id": "test", "terrain_group": "test", "scene_id": "scene",
           "window": [0, 0, 16, 12], "dhash": "0000000000000000", "sample_key": "01",
           "sample_image": None, "near_duplicate_of": None}
    original = storage.write_json
    def fail_manifest(path, value):
        if path == tmp_path / "manifest.json":
            raise OSError("simulated process interruption after shard publication")
        original(path, value)
    monkeypatch.setattr(storage, "write_json", fail_manifest)
    with pytest.raises(OSError):
        index.commit(plan, 1, True, [row], [np.zeros((12, 16), np.uint8)],
                     [np.zeros((24, 32), np.uint8)], [], {})
    index.close()
    monkeypatch.setattr(storage, "write_json", original)
    recovered = storage.Index(tmp_path, settings)
    assert recovered.count() == recovered.manifest["rows"] == 1
    assert recovered.db.execute("SELECT state FROM jobs").fetchone()[0] == "DONE"
    recovered.close()
    reopened = storage.Index(tmp_path, settings)
    assert reopened.count() == 1
    reopened.close()


def test_collection_resumes_cached_blocks_and_completed_run_does_no_network(tmp_path, monkeypatch):
    from badapple_earth.collection import pipeline
    settings = config()
    read_calls = []
    class Reader:
        src = Scene()
        metadata = {"crs": "EPSG:4326"}
        class Classification:
            def read(self, *args, out_shape, **kwargs):
                return np.full(out_shape, 6, np.uint8)
        scl = Classification()
        def __init__(self, *args):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read_block(self, window):
            read_calls.append(tuple(window.flatten()))
            y, x = np.indices((int(window.height), int(window.width)))
            rgb = np.stack([(x % 250 + 1), (y % 250 + 1), ((x + y) % 250 + 1)]).astype(np.uint8)
            return rgb, np.ones(x.shape, bool), np.full(x.shape, 6, np.uint8)
    monkeypatch.setattr(pipeline, "SceneReader", Reader)
    monkeypatch.setattr(pipeline, "safe_asset", lambda value: value)
    monkeypatch.setattr(pipeline, "catalog", lambda *args: {"selected": {"test": [scene()]}})
    first = collect(settings, tmp_path, 100, stop_after_blocks=1)
    assert first["status"] == "paused"
    second = collect(settings, tmp_path, 100)
    assert second["status"] == "complete" and second["rows"] == 100
    calls = len(read_calls)
    larger = collect(settings, tmp_path, 120)
    assert larger["rows"] == 120
    # Previously fetched blocks used for newly requested windows come from cache.
    assert len(read_calls) == calls
    monkeypatch.setattr(pipeline, "catalog", lambda *args: pytest.fail("No network on a completed index"))
    assert collect(settings, tmp_path, 120)["rows"] == 120
