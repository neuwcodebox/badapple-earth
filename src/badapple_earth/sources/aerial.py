"""Bounded anonymous NAIP and LINZ discovery, with unsigned persisted locators."""

from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
from urllib.parse import urljoin, urlsplit

import requests
import rasterio
from shapely.geometry import box, shape

from ..evidence import write_json
from ..imagery import GDAL_OPTIONS

NAIP = "planetarycomputer-naip"
LINZ = "linz-imagery"
SOURCES = (NAIP, LINZ)
PC = "https://planetarycomputer.microsoft.com/api/stac/v1"
LINZ_ROOT = "https://nz-imagery.s3.ap-southeast-2.amazonaws.com/"
QUALITY_POLICY = "aerial-rgb-dataset-mask-unassessed-cloud-v1"
_TOKENS = {}


def safe_asset(href, source):
    parsed = urlsplit(href)
    allowed = {NAIP: {"naipeuwest.blob.core.windows.net"},
               LINZ: {"nz-imagery.s3.ap-southeast-2.amazonaws.com", "nz-imagery.s3-ap-southeast-2.amazonaws.com"}}
    if (source not in allowed or parsed.scheme != "https" or parsed.hostname not in allowed[source]
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
            or not parsed.path.lower().endswith((".tif", ".tiff"))):
        raise ValueError("Unsupported aerial asset; require an unsigned approved HTTPS COG")
    if source == NAIP and not parsed.path.startswith("/naip/"):
        raise ValueError("NAIP container required")
    return href


def get_json(url, params=None):
    # No user API key, AWS credentials, Requester Pays or implicit netrc credentials.
    with requests.Session() as session:
        session.auth = lambda request: request
        for attempt in range(3):
            response = session.get(url, params=params, timeout=(15, 45))
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                response.raise_for_status()
                return response.json()
            time.sleep(2 ** attempt)


def cached_json(root, url, params=None):
    name = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    path = root / (name + ".json")
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    root.mkdir(parents=True, exist_ok=True)
    value = get_json(url, params)
    write_json(path, value)
    return value


def signed_href(href):
    safe_asset(href, NAIP)
    account = urlsplit(href).hostname.split(".")[0]
    key = (account, "naip")
    now = datetime.now(timezone.utc)
    token = _TOKENS.get(key)
    if not token or datetime.fromisoformat(token["msft:expiry"].replace("Z", "+00:00")) < now + timedelta(minutes=5):
        token = get_json(f"https://planetarycomputer.microsoft.com/api/sas/v1/token/{account}/naip",
                         {"_": int(now.timestamp())})
        if datetime.fromisoformat(token["msft:expiry"].replace("Z", "+00:00")) <= now:
            raise ValueError("Anonymous NAIP SAS token is expired")
        _TOKENS[key] = token
    return href + "?" + token["token"]


def normalize(item, source, collection, item_url=None, collection_doc=None):
    key = "image" if source == NAIP else "visual"
    asset = item["assets"][key]
    href = urljoin(item_url or "", asset["href"])
    safe_asset(href, source)
    props = item["properties"]
    start, end = props.get("start_datetime"), props.get("end_datetime")
    acquired = props.get("datetime")
    if not acquired:
        if not start or not end:
            raise ValueError("Aerial item must have an acquisition instant or interval")
        acquired = start if start == end else f"{start}/{end}"
    # LINZ sheet IDs repeat between surveys. Namespace the local scene filename.
    local_id = item["id"] if source == NAIP else "linz-" + hashlib.sha256(collection.encode()).hexdigest()[:16] + "-" + item["id"]
    return {"source_id": source, "collection_id": collection, "item_id": local_id,
            "source_item_id": item["id"], "spatial_id": local_id,
            "visual": href, "asset_key": key, "acquired_at": acquired,
            "acquired_start": start, "acquired_end": end,
            "geometry": item["geometry"], "bbox": item["bbox"],
            "gsd": props.get("gsd", (collection_doc or {}).get("gsd")),
            "cloud_cover": None, "quality_policy": QUALITY_POLICY,
            "license": (collection_doc or {}).get("license"),
            "providers": (collection_doc or {}).get("providers", []),
            "license_refs": [x["href"] for x in item.get("links", []) if x["rel"] == "license"],
            "catalog_item_url": item_url}


def discover(config, area, cache):
    source, limit = config["source"], config["catalog_limit_per_aoi"]
    excluded, selected = [], []
    if source == NAIP:
        collection = cached_json(cache, PC + "/collections/naip")
        document = cached_json(cache, PC + "/search", {"collections": "naip", "bbox": ",".join(map(str, area["bbox"])),
                                                      "limit": limit, "datetime": config["datetime"],
                                                      "sortby": "-datetime"})
        items = [(x, None) for x in document.get("features", [])]
        collection_id = "naip"
        truncated = any(x["rel"] == "next" for x in document.get("links", []))
    else:
        url = area["collection_url"]
        if not url.startswith(LINZ_ROOT) or not url.endswith("/collection.json"):
            raise ValueError("LINZ requires a public collection.json URL")
        collection = cached_json(cache, url)
        collection_id = collection["id"]
        links = [x for x in collection["links"] if x["rel"] == "item"]
        # Spread access over a survey; do not fetch every item to inspect its geometry.
        links.sort(key=lambda x: hashlib.sha256(f"{config['seed']}:{x['href']}".encode()).hexdigest())
        items = []
        for link in links[:limit]:
            item_url = urljoin(url, link["href"])
            if not item_url.startswith(LINZ_ROOT) or not item_url.endswith(".json"):
                raise ValueError("Unsupported LINZ item URL")
            items.append((cached_json(cache, item_url), item_url))
        truncated = len(links) > limit
    for item, item_url in items:
        try:
            candidate = normalize(item, source, collection_id, item_url, collection)
            overlap = shape(candidate["geometry"]).intersection(box(*area["bbox"])).area
            if not overlap:
                raise ValueError("no_footprint_overlap")
            candidate["aoi_coverage_fraction_degrees"] = overlap / box(*area["bbox"]).area
            selected.append(candidate)
        except (ValueError, KeyError) as error:
            excluded.append({"item_id": item["id"], "reason": type(error).__name__})
    if source == NAIP:
        # Retain one acquisition per physical NAIP quarter-quad; prioritize recent coverage.
        selected.sort(key=lambda x: (x["acquired_at"], x["aoi_coverage_fraction_degrees"], x["item_id"]), reverse=True)
        for candidate in selected:
            candidate["spatial_id"] = "_".join(candidate["source_item_id"].split("_")[:5])
    return {"area": area, "catalog_inspected": len(items), "truncated_by_limit": truncated,
            "excluded": excluded, "candidates": selected}


class AerialReader:
    """Read RGB and the provider's valid-data mask; cloud quality stays unknown."""

    def __init__(self, scene):
        self.scene, self.scl = scene, None

    def __enter__(self):
        self.stack = ExitStack()
        try:
            source = self.scene["source_id"]
            href = safe_asset(self.scene["visual"], source)
            if source == NAIP:
                href = signed_href(href)
            # GDAL failures may contain SAS URLs. Only surface a sanitized error type.
            logger = logging.getLogger("rasterio")
            self.stack.callback(logger.setLevel, logger.level)
            logger.setLevel(logging.CRITICAL)
            self.stack.enter_context(rasterio.Env(**{**GDAL_OPTIONS, "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff", "CPL_LOG": "NUL"}))
            self.src = self.stack.enter_context(rasterio.open(href))
            src = self.src
            if (not src.crs or src.count not in (3, 4) or src.dtypes[:3] != ("uint8",) * 3
                    or [x.name for x in src.colorinterp[:3]] != ["red", "green", "blue"]):
                raise ValueError("Expected georeferenced native uint8 RGB aerial imagery")
            self.metadata = {"crs": str(src.crs), "affine": list(src.transform)[:6],
                             "width": src.width, "height": src.height, "dtype": list(src.dtypes),
                             "bands": [x.name for x in src.colorinterp], "rgb_band_indices": [1, 2, 3],
                             "source_gsd": list(src.res), "block_shapes": src.block_shapes,
                             "mask_flags": [[x.name for x in flags] for flags in src.mask_flag_enums],
                             "quality_policy": QUALITY_POLICY, "cloud_quality": "unassessed"}
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def read_block(self, window):
        return self.src.read((1, 2, 3), window=window), self.src.dataset_mask(window=window) > 0, None
