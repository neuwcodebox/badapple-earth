"""The only P1 source: anonymous HTTPS Sentinel-2 Collection 1."""

from urllib.parse import urlsplit

from pystac_client import Client
from pystac_client.stac_api_io import StacApiIO
from shapely.geometry import box, shape
from urllib3.util.retry import Retry

SOURCE_ID = "earthsearch-sentinel2-c1"
COLLECTION = "sentinel-2-c1-l2a"
ENDPOINT = "https://earth-search.aws.element84.com/v1"


def safe_asset(href: str) -> str:
    """Reject signed/non-public/other-bucket locators before GDAL sees them."""
    parsed = urlsplit(href)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "e84-earth-search-sentinel-data.s3.us-west-2.amazonaws.com"
        or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
    ):
        raise ValueError("Unsupported asset locator; P1 permits only the public C1 HTTPS bucket")
    return href


def discover(config: dict, area: dict) -> dict:
    if config["endpoint"] != ENDPOINT or config["collection"] != COLLECTION:
        raise ValueError("P1 requires the Earth Search Sentinel-2 C1 source")
    if config.get("requester_pays") is not False:
        raise ValueError("P1 does not support Requester Pays")
    limit = config["catalog_limit_per_aoi"]
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ValueError("catalog_limit_per_aoi must be between 1 and 500")
    retries = Retry(total=2, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
    io = StacApiIO(timeout=30, max_retries=retries)
    client = Client.open(ENDPOINT, stac_io=io)
    # One extra item distinguishes an exhausted search from a client-side cap.
    search = client.search(
        collections=[COLLECTION], bbox=area["bbox"], datetime=config["datetime"],
        query={"eo:cloud_cover": {"lte": config["cloud_cover_max"]}},
        limit=min(limit + 1, 100), max_items=limit + 1,
    )
    items = list(search.items())
    selected, excluded = [], []
    for item in items[:limit]:
        if "visual" not in item.assets or "scl" not in item.assets:
            excluded.append({"item_id": item.id, "reason": "visual_or_scl_missing"})
            continue
        try:
            visual = safe_asset(item.assets["visual"].href)
            scl = safe_asset(item.assets["scl"].href)
        except ValueError:
            excluded.append({"item_id": item.id, "reason": "unsupported_asset_locator"})
            continue
        overlap = shape(item.geometry).intersection(box(*area["bbox"])).area
        if overlap == 0:
            excluded.append({"item_id": item.id, "reason": "no_footprint_overlap"})
            continue
        selected.append({
            "source_id": SOURCE_ID, "collection_id": COLLECTION, "item_id": item.id,
            "visual": visual, "scl": scl,
            "acquired_at": item.properties["datetime"],
            "cloud_cover": item.properties.get("eo:cloud_cover"),
            "geometry": item.geometry, "bbox": item.bbox,
            "aoi_coverage_fraction_degrees": overlap / box(*area["bbox"]).area,
            "license_refs": [link.href for link in item.links if link.rel == "license"],
        })
    # Coverage is a selection heuristic in degrees, not a physical ground area.
    selected.sort(key=lambda item: (-item["aoi_coverage_fraction_degrees"], item["cloud_cover"], item["item_id"]))
    return {
        "area": area, "catalog_inspected": min(len(items), limit),
        "truncated_by_limit": len(items) > limit,
        "excluded": excluded, "candidates": selected,
    }
