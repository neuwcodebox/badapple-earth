import json

import numpy as np
import pyarrow.parquet as pq
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin
from rasterio.windows import Window

from badapple_earth.sources import aerial
from badapple_earth.imagery import check_quality, quality_tables, window_quality
from test_collection import config, Scene, scene


def linz_item():
    return {"id": "AB01_1000_0101", "properties": {"datetime": None,
            "start_datetime": "2023-01-01T00:00:00Z", "end_datetime": "2023-01-03T00:00:00Z"},
            "assets": {"visual": {"href": "./image.tiff"}},
            "geometry": scene()["geometry"], "bbox": [0, 0, 1, 1]}


def test_public_json_requests_do_not_send_basic_or_netrc_credentials(monkeypatch):
    import requests
    class Session(requests.Session):
        def send(self, request, **kwargs):
            assert "Authorization" not in request.headers
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"public":true}'
            return response
    monkeypatch.setattr(requests, "Session", Session)
    assert aerial.get_json(aerial.LINZ_ROOT + "catalog.json") == {"public": True}


def test_aerial_provenance_preserves_intervals_and_namespaces_repeated_sheet_ids():
    item = linz_item()
    url = aerial.LINZ_ROOT + "survey/rgb/2193/item.json"
    one = aerial.normalize(item, aerial.LINZ, "first-survey", url)
    two = aerial.normalize(item, aerial.LINZ, "second-survey", url)
    assert one["item_id"] != two["item_id"]
    assert one["source_item_id"] == item["id"]
    assert one["acquired_at"] == "2023-01-01T00:00:00Z/2023-01-03T00:00:00Z"
    assert one["acquired_start"] == item["properties"]["start_datetime"]
    assert one["visual"] == aerial.LINZ_ROOT + "survey/rgb/2193/image.tiff"
    assert one["asset_key"] == "visual"


@pytest.mark.parametrize("href,source", [
    (aerial.LINZ_ROOT + "image.tiff?sig=secret", aerial.LINZ),
    ("https://naip-source.s3.amazonaws.com/image.tif", aerial.NAIP),
    ("https://untrusted.example/image.tif", aerial.LINZ),
    ("https://naipeuwest.blob.core.windows.net/other/image.tif", aerial.NAIP),
])
def test_persisted_aerial_locator_rejects_signed_and_unapproved_paths(href, source):
    with pytest.raises(ValueError):
        aerial.safe_asset(href, source)


def test_unavailable_cloud_classification_stays_unknown_and_invalid_pixels_are_rejected():
    valid = np.ones((12, 16), bool)
    valid[2, 3] = False
    for sy, sx in [(slice(0, 12), slice(0, 16)), (slice(4, 10), slice(0, 16))]:
        result = window_quality(quality_tables(valid, None), sy, sx, 0)
        assert result == check_quality(valid[sy, sx], None, 0)
        assert result["local_cloud_ratio"] is None
        assert result["invalid_scl_ratio"] is None


def test_aerial_reader_uses_rgb_bands_and_alpha_without_mistaking_nir_for_blue(tmp_path, monkeypatch):
    filename = tmp_path / "aerial.tiff"
    values = np.full((4, 16, 16), 255, np.uint8)
    values[0], values[1], values[2] = 10, 20, 30
    values[3, 3, 4] = 0
    with rasterio.open(filename, "w", driver="GTiff", width=16, height=16, count=4,
                       dtype="uint8", crs="EPSG:2193", transform=from_origin(1600000, 5400000, .3, .3)) as dst:
        dst.write(values)
        dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha)
    monkeypatch.setattr(aerial, "safe_asset", lambda href, source: href)
    with aerial.AerialReader({"source_id": aerial.LINZ, "visual": str(filename)}) as reader:
        rgb, valid, scl = reader.read_block(Window(0, 0, 16, 16))
        assert rgb[:, 0, 0].tolist() == [10, 20, 30]
        assert not valid[3, 4] and valid[3, 5]
        assert scl is None


def test_mixed_source_credits_identify_actual_licensors_and_transformations():
    from badapple_earth.matching.output import source_credits
    credits, references = source_credits([
        {"source_id": aerial.NAIP},
        {"source_id": aerial.LINZ, "source_licensors": ["Regional Council"], "collection_id": "survey"},
        {"source_id": "earthsearch-sentinel2-c1", "acquired_at": "2025-01-01T00:00:00Z"}])
    assert any("Regional Council" in value and "CC BY 4.0" in value and "cropped" in value for value in credits)
    assert any("USDA" in value for value in credits)
    assert len(references) == 3


@pytest.mark.parametrize("source", [aerial.LINZ, aerial.SWISS, aerial.NETHERLANDS])
def test_aerial_collection_resumes_and_replays_cached_rgb_without_network(tmp_path, monkeypatch, source):
    from badapple_earth.collection import pipeline
    from badapple_earth.matching.photos import PhotoCache
    settings = {**config(), "source": source, "cache_rgb": True, "collect_rotations": True,
                "catalog_limit_per_aoi": 2}
    settings["areas"].append({"id": "missing", "terrain_group": "test", "bbox": [2, 0, 3, 1]})
    candidate = {**scene(), "source_id": source, "asset_key": "visual",
                 "acquisition_precision": "nominal_tile_year", "catalog_datetime": "2023-01-01T00:00:00Z",
                 "rights_notes": "source notices", "file_sha256": "a" * 64,
                 "acquired_start": "2023-01-01T00:00:00Z", "acquired_end": "2023-01-03T00:00:00Z"}
    class AerialScene(Scene):
        def dataset_mask(self, out_shape):
            return np.full(out_shape, 255, np.uint8)
    class Reader:
        src, scl = AerialScene(), None
        metadata = {"crs": "EPSG:4326", "affine": list(Scene.transform)[:6], "quality_policy": aerial.QUALITY_POLICY}
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read_block(self, window):
            y, x = np.indices((int(window.height), int(window.width)))
            return np.stack([x % 255, y % 255, (x + y) % 255]).astype(np.uint8), np.ones(x.shape, bool), None
    monkeypatch.setattr(aerial, "AerialReader", Reader)
    monkeypatch.setattr(pipeline, "catalog", lambda *args: {"selected": {"test": [candidate], "missing": []}})
    assert pipeline.collect(settings, tmp_path, 40, stop_after_blocks=1)["status"] == "paused"
    manifest = pipeline.collect(settings, tmp_path, 100, redistribute=True)
    assert manifest["status"] == "complete" and manifest["rows"] == 100
    assert manifest["quality_policy"] == aerial.QUALITY_POLICY
    assert manifest["areas"] == {"test": 100}
    rows = [row for s in manifest["shards"] for row in pq.read_table(tmp_path / "shards" / s["shard_id"] / "tiles.parquet").to_pylist()]
    assert all(r["cloud_quality"] == "unassessed" and r["local_cloud_ratio"] is None for r in rows)
    assert all(r["acquired_end"] == candidate["acquired_end"] for r in rows)
    assert all(r["acquisition_precision"] == "nominal_tile_year" and r["source_rights_notes"] == "source notices"
               and r["source_file_sha256"] == "a" * 64 for r in rows)
    monkeypatch.setattr(pipeline, "catalog", lambda *args: pytest.fail("completed collection must not query catalogue"))
    assert pipeline.collect(settings, tmp_path, 100)["rows"] == 100
    monkeypatch.setattr(aerial, "AerialReader", lambda *args: pytest.fail("cached photos must not fetch source"))
    with PhotoCache(tmp_path, manifest) as cache:
        assert cache.base(rows[-1]).size == (32, 24)
        assert cache.counts["remote"] == 0
