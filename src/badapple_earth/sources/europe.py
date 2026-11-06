"""Swiss STAC and Dutch projected download indexes; preserve year precision."""

import hashlib
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from pyproj import Transformer
from shapely.geometry import box, mapping, shape
from shapely import transform

from . import aerial

SWISS_COLLECTION = "ch.swisstopo.swissimage-dop10"
SWISS_API = "https://data.geo.admin.ch/api/stac/v1/collections/" + SWISS_COLLECTION
DUTCH_INDEX = ("https://basisdata.nl/hwh-portal/20230609_tmp/links/nationaal/Nederland/"
               "BM_LRL2025O_RGB.json")
SWISS_TERMS = "https://www.swisstopo.admin.ch/fr/conditions-utilisation-geodonnees-et-geoservices-gratuit"
SWISS_NOTES = ("SWISSIMAGE nominal year covers at least 70% of a tile; exact seamline dates unavailable. "
               "Product lake centres may include material © (2016-2018) Planet. All rights reserved.")


def normalize(item, source, *, collection=None):
    if source == aerial.SWISS:
        assets = [(key, value) for key, value in item["assets"].items()
                  if value.get("gsd") == 0.1 and value.get("type", "").startswith("image/tiff")]
        if len(assets) != 1:
            raise ValueError("Exactly one 10cm SWISSIMAGE RGB asset required")
        asset_key, asset = assets[0]
        href = asset["href"]
        original_id = item["id"]
        # SWISSIMAGE timestamps encode a representative year, not an acquisition day.
        year = item["properties"]["datetime"][:4]
        if not year.isdigit():
            raise ValueError("Missing nominal SWISSIMAGE year")
        geom = item["geometry"]
        collection_id = SWISS_COLLECTION
        precision = "nominal_tile_year"
        license_id = (collection or {}).get("license", "proprietary")
        license_refs = [SWISS_TERMS]
        providers = [{"name": "Federal Office of Topography swisstopo", "roles": ["licensor", "producer"]}]
        spatial_id = original_id.rsplit("_", 1)[-1]
        extras = {"catalog_datetime": item["properties"]["datetime"], "rights_notes": SWISS_NOTES,
                  "catalog_item_url": SWISS_API + "/items/" + original_id}
    elif source == aerial.NETHERLANDS:
        props = item["properties"]
        href = props["file"]
        original_id = PurePosixPath(urlsplit(href).path).name
        if not original_id.startswith("2025_") or "_RGB_" not in original_id:
            raise ValueError("Expected 2025 Dutch RGB orthophoto")
        # This official index uses Rijksdriehoek coordinates, not GeoJSON CRS84.
        to_geo = Transformer.from_crs("EPSG:28992", "EPSG:4326", always_xy=True)
        geom = mapping(transform(shape(item["geometry"]), to_geo.transform, interleaved=False))
        year, collection_id, precision = "2025", "beeldmateriaal-2025-rgb-25cm", "product_year"
        asset_key, license_id, license_refs = "file", "CC-BY-4.0", ["https://www.beeldmateriaal.nl/dataroom"]
        providers = [{"name": "Beeldmateriaal Nederland", "roles": ["licensor"]}]
        spatial_id = original_id
        extras = {"file_sha256": props.get("sha256"), "file_mtime": props.get("mtime"),
                  "catalog_item_url": DUTCH_INDEX,
                  "rights_notes": "Product year only; file mtime is not the acquisition date."}
    else:
        raise ValueError("Unsupported European aerial source")
    aerial.safe_asset(href, source)
    geometry = shape(geom)
    local_id = source + "-" + hashlib.sha256(original_id.encode()).hexdigest()[:24]
    return {"source_id": source, "collection_id": collection_id, "item_id": local_id,
            "source_item_id": original_id, "spatial_id": spatial_id, "visual": href, "asset_key": asset_key,
            "acquired_at": year, "acquired_start": None, "acquired_end": None,
            "acquisition_precision": precision, "geometry": geom, "bbox": list(geometry.bounds),
            "gsd": 0.1 if source == aerial.SWISS else 0.25,
            "cloud_cover": None, "quality_policy": aerial.QUALITY_POLICY,
            "license": license_id, "license_refs": license_refs, "providers": providers, **extras}


def discover(config, area, cache):
    source, limit = config["source"], config["catalog_limit_per_aoi"]
    footprint = box(*area["bbox"])
    if source == aerial.SWISS:
        collection = aerial.cached_json(cache, SWISS_API)
        document = aerial.cached_json(cache, SWISS_API + "/items",
                                     {"bbox": ",".join(map(str, area["bbox"])), "limit": limit,
                                      "datetime": config["datetime"]})
        items = document.get("features", [])
        truncated = any(link["rel"] == "next" for link in document.get("links", []))
    elif source == aerial.NETHERLANDS:
        collection = None
        document = aerial.cached_json(cache, DUTCH_INDEX)
        if document.get("crs", {}).get("properties", {}).get("name") != "urn:ogc:def:crs:EPSG::28992":
            raise ValueError("Dutch download index must declare EPSG:28992")
        # Transform the small AOI once; do not transform thousands of index polygons.
        to_rd = Transformer.from_crs("EPSG:4326", "EPSG:28992", always_xy=True)
        rd_area = transform(footprint, to_rd.transform, interleaved=False)
        items = [feature for feature in document["features"] if shape(feature["geometry"]).intersects(rd_area)]
        items.sort(key=lambda f: hashlib.sha256(f"{config['seed']}:{f['properties']['file']}".encode()).hexdigest())
        truncated = len(items) > limit
        items = items[:limit]
    else:
        raise ValueError("Unsupported European source")
    selected, excluded = [], []
    for item in items:
        try:
            candidate = normalize(item, source, collection=collection)
            overlap = shape(candidate["geometry"]).intersection(footprint).area
            if not overlap:
                continue
            candidate["aoi_coverage_fraction_degrees"] = overlap / footprint.area
            selected.append(candidate)
        except (KeyError, ValueError) as error:
            excluded.append({"item_id": item.get("id", item.get("properties", {}).get("file")),
                             "reason": type(error).__name__})
    selected.sort(key=lambda s: (s["acquired_at"], s["aoi_coverage_fraction_degrees"], s["item_id"]), reverse=True)
    return {"area": area, "catalog_inspected": len(items), "truncated_by_limit": truncated,
            "excluded": excluded, "candidates": selected}
