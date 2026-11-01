import numpy as np
from PIL import Image
import pyarrow.parquet as pq

from badapple_earth.collection.parallel import partition
from badapple_earth.collection.storage import Index
from badapple_earth.features import extract
from badapple_earth.matching.photos import PhotoCache
from badapple_earth.matching.views import rotate_image
from test_collection import config


def test_parallel_partitions_preserve_total_and_disjoint_balanced_areas():
    settings = config()
    settings["areas"] = [{"id": f"{group}-{n}", "terrain_group": group, "bbox": [n, 0, n + 1, 1]}
                         for group in ("coast", "land") for n in range(8)]
    jobs = partition(settings, 900003, 4)
    assert sum(quota for _, quota in jobs) == 900003
    assigned = [area["id"] for part, _ in jobs for area in part["areas"]]
    assert len(assigned) == len(set(assigned)) == 16
    assert all(sum(a["terrain_group"] == "coast" for a in part["areas"]) == 2 for part, _ in jobs)
    assert partition(settings, 900003, 4) == jobs


def test_rgb_shard_survives_reopen_and_materializes_rotated_views_without_network(tmp_path, monkeypatch):
    from badapple_earth.matching import photos
    settings = {**config(), "cache_rgb": True, "collect_rotations": True}
    images = [Image.fromarray(np.random.default_rng(n).integers(0, 256, (24, 32, 3), dtype=np.uint8))
              for n in range(2)]
    index = Index(tmp_path, settings)
    plan = {"job_id": "job", "windows": [[0, 0, 32, 24], [32, 0, 32, 24]]}
    index.register([plan])
    rows = [{"tile_id": str(n), "aoi_id": "test", "terrain_group": "test", "scene_id": "scene",
             "window": plan["windows"][n], "dhash": "0000000000000000", "sample_key": str(n),
             "sample_image": None, "near_duplicate_of": None, "footprint_json": "{}"} for n in range(2)]
    values = [extract(image, settings["aspect_ratio"]) for image in images]
    index.commit(plan, 2, True, rows, [v[0] for v in values], [v[1] for v in values], [], {},
                 rotations={"images": images, "scene": {"affine": [0.001, 0, 0, 0, -0.001, 1], "crs": "EPSG:4326"}},
                 photos=images)
    index.close()
    reopened = Index(tmp_path, settings)
    assert reopened.count() == 2
    descriptor = reopened.manifest["shards"][0]
    directory = tmp_path / "shards" / descriptor["shard_id"]
    base = pq.read_table(directory / "tiles.parquet").to_pylist()
    views = pq.read_table(directory / "views/views.parquet").to_pylist()
    def forbidden(*args, **kwargs):
        raise AssertionError("Persisted RGB must avoid source reads")
    monkeypatch.setattr(photos, "SceneReader", forbidden)
    with PhotoCache(tmp_path, reopened.manifest) as cache:
        for row in base + views:
            expected = images[int(row["tile_id"])]
            actual = cache.base(row)
            np.testing.assert_array_equal(np.asarray(actual), np.asarray(expected))
            degrees = row.get("rotation_degrees", 0)
            np.testing.assert_array_equal(np.asarray(rotate_image(actual, degrees, expected.size)),
                                          np.asarray(rotate_image(expected, degrees, expected.size)))
        assert cache.counts["remote"] == 0 and cache.counts["shard"] == 8
    reopened.close()
