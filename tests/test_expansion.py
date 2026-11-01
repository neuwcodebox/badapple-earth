import json

import numpy as np
from PIL import Image
import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from badapple_earth.collection.storage import Index
from badapple_earth.features import extract
from badapple_earth.imagery import check_quality, quality_tables, window_quality
from badapple_earth.matching.views import prepare_views
from test_collection import config


def test_shortfall_stays_in_terrain_group_and_does_not_retry_an_exhausted_aoi():
    from badapple_earth.collection.windows import refill_limits
    areas = [{"id": "coast-exhausted", "terrain_group": "coast"},
             {"id": "coast-spare", "terrain_group": "coast"},
             {"id": "land", "terrain_group": "land"}]
    counts = {"coast-exhausted": 4, "coast-spare": 10, "land": 20}
    prior = {"coast-exhausted": 10, "coast-spare": 10, "land": 20}
    result = refill_limits(40, areas, counts, prior)
    assert result == {"coast-exhausted": 4, "coast-spare": 16, "land": 20}
    assert sum(result.values()) == 40


def test_overlapping_window_quality_matches_native_mask_contract():
    rng = np.random.default_rng(7)
    valid = rng.random((48, 64)) > 0.03
    scl = rng.choice([0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11], size=(48, 64))
    tables = quality_tables(valid, scl)
    for sy, sx in [(slice(0, 48), slice(0, 64)), (slice(0, 12), slice(0, 16)),
                   (slice(7, 29), slice(13, 51)), (slice(47, 48), slice(63, 64))]:
        expected = check_quality(valid[sy, sx], scl[sy, sx], 0.02)
        result = window_quality(tables, sy, sx, 0.02)
        assert result["reasons"] == expected["reasons"]
        for key in ["valid_ratio", "local_cloud_ratio", "invalid_scl_ratio"]:
            assert result[key] == pytest.approx(expected[key])


def test_rgb_dataset_mask_keeps_zero_channels_and_repairs_cached_nodata_masks(tmp_path):
    import rasterio
    from rasterio.transform import from_origin
    from rasterio.windows import Window
    from badapple_earth.imagery import SceneReader
    from badapple_earth.collection.pipeline import cached_block
    rgb = np.full((3, 16, 16), 60, dtype=np.uint8)
    rgb[0, 4, 4] = 0
    rgb[:, 5, 5] = 0
    profile = {"driver": "GTiff", "width": 16, "height": 16, "dtype": "uint8",
               "crs": "EPSG:32633", "transform": from_origin(500000, 1000, 10, 10), "nodata": 0}
    with rasterio.open(tmp_path / "rgb.tif", "w", count=3, **profile) as dst:
        dst.write(rgb)
    with rasterio.open(tmp_path / "scl.tif", "w", count=1, **profile) as dst:
        dst.write(np.full((16, 16), 4, np.uint8), 1)
    with SceneReader(str(tmp_path / "rgb.tif"), str(tmp_path / "scl.tif")) as reader:
        _, valid, scl = reader.read_block(Window(0, 0, 16, 16))
        assert valid[4, 4] and not valid[5, 5]
        directory = tmp_path / "cache/blocks"
        directory.mkdir(parents=True)
        np.savez(directory / "old.npz", rgb=rgb, valid=np.all(rgb > 0, axis=0), scl=scl)
        class StoredIndex:
            root = tmp_path
        _, repaired, _ = cached_block(StoredIndex(), reader, {"job_id": "old"})
        assert repaired[4, 4] and not repaired[5, 5]


def test_inline_rotations_are_published_with_batch_and_need_no_source_read(tmp_path, monkeypatch):
    from badapple_earth.matching.photos import PhotoCache
    settings = config()
    settings["collect_rotations"] = True
    index = Index(tmp_path, settings)
    plan = {"job_id": "inline", "windows": [[0, 0, 16, 12]]}
    index.register([plan])
    image = Image.fromarray(np.random.default_rng(4).integers(0, 255, (24, 32, 3), dtype=np.uint8))
    coarse, fine, dhash = extract(image, [4, 3])
    row = {"tile_id": "tile", "view_id": "tile", "aoi_id": "test", "terrain_group": "test",
           "scene_id": "scene", "window": [0, 0, 16, 12], "dhash": dhash, "sample_key": "01",
           "sample_image": None, "near_duplicate_of": None, "footprint_json": "original"}
    scene = {"crs": "EPSG:32633", "affine": [10, 0, 500000, 0, -10, 1000000]}
    index.commit(plan, 1, True, [row], [coarse], [fine], [], {}, {"images": [image], "scene": scene})
    manifest = index.manifest
    index.close()
    monkeypatch.setattr(PhotoCache, "base", lambda *args: pytest.fail("Inline views must not fetch a source photo"))
    root, views = prepare_views(tmp_path, manifest)
    assert views["view_count"] == 3 and views["shards"][0]["inline"]
    shard = tmp_path / "shards" / views["shards"][0]["source_shard"] / "views"
    assert np.load(shard / "gray_coarse.npy").shape == (3, 12, 16)


def test_bundle_reuses_members_deduplicates_and_materializes_from_the_winning_member(tmp_path):
    from badapple_earth.collection.bundle import combine, members, read_manifest
    from badapple_earth.features import FEATURE_VERSION
    from badapple_earth.matching.comparison import compare
    from badapple_earth.matching.output import materialize
    from badapple_earth.matching.search import match
    def make(name, value, tile_id):
        root = tmp_path / name
        shard = root / "shards/one"
        shard.mkdir(parents=True)
        (root / "catalog").mkdir()
        image = Image.new("RGB", (32, 24), (value, value, value))
        image.save(root / "base.png")
        coarse, fine, _ = extract(image, [4, 3])
        np.save(shard / "gray_coarse.npy", coarse[None])
        np.save(shard / "gray_fine.npy", fine[None])
        row = {"tile_id": tile_id, "view_id": tile_id, "feature_row": 0, "shard_id": "one",
               "scene_id": "scene", "sample_image": "base.png", "window": [0, 0, 32, 24],
               "aoi_id": name, "terrain_group": "test", "footprint_json": "original",
               "rotation_degrees": 0, "acquired_at": "2026-01-01T00:00:00Z"}
        pq.write_table(pa.Table.from_pylist([row]), shard / "tiles.parquet")
        (root / "catalog/scene.json").write_text(json.dumps({"crs": "EPSG:32633", "affine": [10, 0, 500000, 0, -10, 1000000]}))
        (root / "manifest.json").write_text(json.dumps({
            "schema": "feature-index-v1", "index_id": name, "rows": 1, "feature_version": FEATURE_VERSION,
            "canonical_size": [32, 24], "coarse_shape": [12, 16], "shards": [{"shard_id": "one", "rows": 1}],
            "config": {"aspect_ratio": [4, 3], "block_size": 2048, "max_cloud_ratio": 0.02}}))
        return root
    one, two, duplicate = make("one", 128, "first"), make("two", 255, "second"), make("duplicate", 128, "first")
    targets = tmp_path / "targets"
    targets.mkdir()
    Image.new("RGB", (32, 24), "white").save(targets / "frame.png")
    np.save(targets / "gray_coarse.npy", np.full((1, 12, 16), 255, np.uint8))
    pq.write_table(pa.Table.from_pylist([{"target_id": "target", "slot": 0, "frame": "frame.png", "target_time": 0}]), targets / "targets.parquet")
    (targets / "manifest.json").write_text(json.dumps({"target_set_id": "targets", "feature_version": FEATURE_VERSION,
                                                     "canonical_size": [32, 24], "count": 1}))
    bundle = tmp_path / "bundle"
    combined = combine([one, two, duplicate], bundle)
    assert combined["rows"] == 2 and combined["duplicate_tile_ids_excluded"] == 1
    assert not (bundle / "shards").exists()
    with pytest.raises(ValueError):
        combine([two], one)
    baseline, expanded = tmp_path / "baseline", tmp_path / "expanded"
    match(targets, one, baseline)
    result = match(targets, bundle, expanded)
    assert result["candidate_count"] == 2 and result["view_count"] == 8
    materialize(targets, bundle, expanded)
    selected = pq.read_table(expanded / "selected.parquet").to_pylist()[0]
    assert selected["index_member"] == "two"
    with Image.open(expanded / selected["photo"]) as photo:
        assert np.all(np.asarray(photo) == 255)
    comparison = compare(baseline, expanded)
    assert comparison["improved_targets"] == 1 and comparison["regressed_targets"] == 0
    altered = read_manifest(one)
    altered["shards"].append({"shard_id": "later", "rows": 0})
    (one / "manifest.json").write_text(json.dumps(altered))
    with pytest.raises(ValueError):
        members(bundle, read_manifest(bundle))
