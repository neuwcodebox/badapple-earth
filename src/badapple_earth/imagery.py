"""Small, exact source-window reads; P2's bulk collector is a separate step."""

from contextlib import ExitStack
import hashlib
import json

import numpy as np
from PIL import Image
from pyproj import Transformer
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import Window
from shapely.geometry import Point, shape

EXTRACTION_VERSION = "p1-rgb-native-pillow-box-v1"
GDAL_OPTIONS = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
    "GDAL_HTTP_CONNECTTIMEOUT": "15",
    "GDAL_HTTP_TIMEOUT": "45",
    "GDAL_HTTP_MAX_RETRY": "2",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_CACHEMAX": 64 * 1024 * 1024,
}


def stable_id(scene: dict, window: Window) -> str:
    identity = [scene["source_id"], scene["collection_id"], scene["item_id"], "visual",
                list(window.flatten()), EXTRACTION_VERSION]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def window_location(transform, crs, window: Window) -> dict:
    converter = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    cx, cy = transform @ (window.col_off + window.width / 2, window.row_off + window.height / 2)
    lon, lat = converter.transform(cx, cy)
    corners = [(window.col_off, window.row_off),
               (window.col_off + window.width, window.row_off),
               (window.col_off + window.width, window.row_off + window.height),
               (window.col_off, window.row_off + window.height),
               (window.col_off, window.row_off)]
    footprint = [list(converter.transform(*(transform @ point))) for point in corners]
    return {"longitude": lon, "latitude": lat,
            "footprint": {"type": "Polygon", "coordinates": [footprint]}}


def candidate_windows(src, area: dict, widths: list[int], aspect: list[int], footprint=None):
    """A bounded deterministic grid of centers inside the actual AOI and scene."""
    west, south, east, north = area["bbox"]
    to_source = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
    points = [(lon, lat) for lat in np.linspace(south, north, 11)[1:-1]
              for lon in np.linspace(west, east, 11)[1:-1]]
    order = np.random.default_rng(42).permutation(len(points))
    seen = set()
    coverage = shape(footprint) if footprint else None
    for index in order:
        lon, lat = points[index]
        if coverage is not None and not coverage.covers(Point(lon, lat)):
            continue
        col, row = ~src.transform @ to_source.transform(lon, lat)
        for width in widths:
            height = round(width * aspect[1] / aspect[0])
            left, top = round(col - width / 2), round(row - height / 2)
            if left < 0 or top < 0 or left + width > src.width or top + height > src.height:
                continue
            window = Window(left, top, width, height)
            location = window_location(src.transform, src.crs, window)
            if not (west <= location["longitude"] <= east and south <= location["latitude"] <= north):
                continue
            key = tuple(window.flatten())
            if key not in seen:
                seen.add(key)
                yield window


def check_quality(valid: np.ndarray, scl: np.ndarray, cloud_max: float) -> dict:
    if valid.shape != scl.shape or valid.size == 0:
        raise ValueError("RGB mask and SCL must have the same nonempty grid")
    valid_ratio = float(np.mean(valid))
    cloud_ratio = float(np.mean(np.isin(scl, [3, 8, 9, 10])))
    invalid_scl_ratio = float(np.mean(np.isin(scl, [0, 1])))
    reasons = []
    if valid_ratio < 1:
        reasons.append("partial_valid")
    if invalid_scl_ratio > 0:
        reasons.append("invalid_scl")
    if cloud_ratio > cloud_max:
        reasons.append("cloud_contamination")
    return {"valid_ratio": valid_ratio, "local_cloud_ratio": cloud_ratio,
            "invalid_scl_ratio": invalid_scl_ratio, "reasons": reasons}


class SceneReader:
    def __init__(self, rgb_href: str, scl_href: str):
        self.rgb_href, self.scl_href = rgb_href, scl_href

    def __enter__(self):
        self.stack = ExitStack()
        try:
            self.stack.enter_context(rasterio.Env(**GDAL_OPTIONS))
            self.src = self.stack.enter_context(rasterio.open(self.rgb_href))
            src = self.src
            if src.count != 3 or any(dtype != "uint8" for dtype in src.dtypes) or not src.crs:
                raise ValueError("Expected a georeferenced 3-band uint8 visual asset")
            scl = self.stack.enter_context(rasterio.open(self.scl_href))
            if scl.count != 1 or scl.dtypes[0] != "uint8" or not scl.crs:
                raise ValueError("Expected a georeferenced 1-band uint8 SCL asset")
            self.scl = self.stack.enter_context(WarpedVRT(
                scl, crs=src.crs, transform=src.transform, width=src.width,
                height=src.height, resampling=Resampling.nearest,
            ))
            self.metadata = {
                "crs": src.crs.to_string(), "affine": list(src.transform)[:6],
                "width": src.width, "height": src.height, "dtype": list(src.dtypes),
                "bands": ["red", "green", "blue"], "block_shapes": src.block_shapes,
                "source_gsd": list(src.res), "scl_crs": scl.crs.to_string(),
                "scl_transform": list(scl.transform)[:6], "scl_shape": [scl.height, scl.width],
            }
            return self
        except BaseException:
            self.stack.close()
            raise

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def read(self, window: Window, preview_width: int, cloud_max: float):
        src = self.src
        rgb = src.read((1, 2, 3), window=window)
        # All RGB band masks must be valid, rather than an OR-combined dataset mask.
        valid = np.all(src.read_masks((1, 2, 3), window=window) > 0, axis=0)
        scl = self.scl.read(1, window=window)
        quality = check_quality(valid, scl, cloud_max)
        image = Image.fromarray(np.moveaxis(rgb, 0, 2))
        height = round(preview_width * window.height / window.width)
        image = image.resize((preview_width, height), Image.Resampling.BOX)
        return image, quality, hashlib.sha256(rgb.tobytes()).hexdigest()
