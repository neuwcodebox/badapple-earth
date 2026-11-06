import json

import pytest
from shapely.geometry import box, mapping, shape

from badapple_earth.sources import aerial, europe
from badapple_earth.collection.pipeline import validate_config
from badapple_earth.matching.output import source_credits
from badapple_earth.production.assets import frame_credit
from badapple_earth.production.layout import location_caption
from test_collection import config


def swiss_item(year=2024):
    return {"id": f"swissimage-dop10_{year}_2682-1247", "geometry": mapping(box(8.53, 47.37, 8.54, 47.38)),
            "properties": {"datetime": f"{year}-01-01T00:00:00Z"},
            "assets": {"native": {"href": "https://data.geo.admin.ch/ch.swisstopo.swissimage-dop10/a/native.tif",
                                  "gsd": .1, "type": "image/tiff; application=geotiff"},
                       "overview": {"href": "https://data.geo.admin.ch/ch.swisstopo.swissimage-dop10/a/small.tif",
                                    "gsd": 2, "type": "image/tiff"}}}


def dutch_item():
    return {"geometry": mapping(box(120000, 486000, 124000, 490000)),
            "properties": {"file": "https://fsn1.your-objectstorage.com/hwh-ortho/2025/2025_120000_490000_RGB_lrl.tif",
                           "mtime": "2026-01-09T12:00:00Z", "sha256": "a" * 64}}


def test_swiss_preserves_representative_year_and_native_asset_without_inventing_a_day():
    value = europe.normalize(swiss_item(), aerial.SWISS)
    assert value["acquired_at"] == "2024" and value["acquired_start"] is None
    assert value["catalog_datetime"] == "2024-01-01T00:00:00Z"
    assert value["acquisition_precision"] == "nominal_tile_year"
    assert value["visual"].endswith("native.tif")
    assert "Planet" in value["rights_notes"] and europe.SWISS_TERMS in value["license_refs"]
    older = europe.normalize(swiss_item(2021), aerial.SWISS)
    assert older["spatial_id"] == value["spatial_id"] and older["item_id"] != value["item_id"]


def test_dutch_coordinates_and_product_year_are_not_file_modification_date():
    value = europe.normalize(dutch_item(), aerial.NETHERLANDS)
    west, south, east, north = value["bbox"]
    assert 4 < west < east < 6 and 51 < south < north < 54
    assert shape(value["geometry"]).is_valid
    assert value["acquired_at"] == "2025" and value["acquisition_precision"] == "product_year"
    assert value["file_mtime"].startswith("2026") and value["file_sha256"] == "a" * 64


@pytest.mark.parametrize("source,href", [
    (aerial.SWISS, "https://data.geo.admin.ch/other/a.tif"),
    (aerial.SWISS, "https://data.geo.admin.ch/ch.swisstopo.swissimage-dop10/a.tif?token=secret"),
    (aerial.NETHERLANDS, "https://basisdata.nl/hwh-stereo/a.tif"),
    (aerial.NETHERLANDS, "https://untrusted.example/hwh-ortho/a.tif"),
])
def test_european_assets_reject_other_products_or_signed_urls(source, href):
    with pytest.raises(ValueError):
        aerial.safe_asset(href, source)


def test_dutch_discovery_respects_declared_crs_and_aoi(monkeypatch, tmp_path):
    document = {"crs": {"properties": {"name": "urn:ogc:def:crs:EPSG::28992"}}, "features": [dutch_item()]}
    monkeypatch.setattr(aerial, "cached_json", lambda *args: document)
    settings = {"source": aerial.NETHERLANDS, "catalog_limit_per_aoi": 10, "seed": 3}
    inside = europe.normalize(dutch_item(), aerial.NETHERLANDS)["bbox"]
    assert len(aerial.discover(settings, {"bbox": inside}, tmp_path)["candidates"]) == 1
    assert not aerial.discover(settings, {"bbox": [0, 0, 1, 1]}, tmp_path)["candidates"]
    document["crs"]["properties"]["name"] = "EPSG:4326"
    with pytest.raises(ValueError, match="EPSG:28992"):
        aerial.discover(settings, {"bbox": inside}, tmp_path)


def test_european_credits_and_captions_do_not_claim_copernicus_or_exact_dates():
    rows = [europe.normalize(swiss_item(), aerial.SWISS), europe.normalize(dutch_item(), aerial.NETHERLANDS)]
    credits, refs = source_credits(rows)
    assert len(refs) == 2 and not any("Copernicus" in credit for credit in credits)
    assert any("swisstopo" in credit and "Planet" in credit for credit in credits)
    assert "Beeldmateriaal" in frame_credit(rows[1])
    assert "representative year" in location_caption({**rows[0], "latitude": 47.3, "longitude": 8.5})
    assert "product year" in location_caption({**rows[1], "latitude": 52.3, "longitude": 4.9})


def test_large_aerial_windows_keep_existing_resource_and_dimension_checks():
    settings = {**config(), "source": aerial.LINZ, "cache_rgb": True, "catalog_limit_per_aoi": 1,
                "widths": [512, 1024, 2048, 4096]}
    settings.pop("scale_weights", None)
    validate_config(settings)
    with pytest.raises(ValueError):
        validate_config({**settings, "widths": [8192]})
