"""Clockwise, inscribed affine views of one real photo, without padded corners."""

import math
from functools import lru_cache

from PIL import Image
from pyproj import Transformer, CRS
from rasterio.transform import Affine

from ..collection.windows import identity

VERSION = "nearest-inscribed-margin1-center-crop-v1"


@lru_cache(maxsize=256)
def geographic_tools(crs):
    return Transformer.from_crs(crs,"EPSG:4326",always_xy=True),CRS.from_user_input(crs).get_geod()


def variants(step=30, crop_scale=.75):
    if step != 30 or not 0 < crop_scale < 1:
        raise ValueError("This experiment uses 30 degree steps and one smaller crop")
    return [{"angle": angle, "scale": scale, "tag": f"r{angle:03d}-z{round(scale*1000):04d}"}
            for angle in range(0, 360, step) for scale in (crop_scale, 1.0)
            if scale != 1.0 or angle % 90]


def recipe(size, angle, scale=1.0):
    width, height = size
    if min(size) <= 4 or not 0 < scale <= 1 or not 0 <= angle < 360:
        raise ValueError("Invalid inscribed view geometry")
    theta = math.radians(angle)
    c, s = math.cos(theta), math.sin(theta)
    ratio = width / height
    # One source-pixel guard keeps resampling strictly inside the photo.
    view_width = min((width-2)/(abs(c)+abs(s)/ratio),
                     (height-2)/(abs(s)+abs(c)/ratio)) * scale
    view_height = view_width / ratio
    a, b = c*view_width/width, s*view_height/height
    d, e = -s*view_width/width, c*view_height/height
    return [a, b, width/2-c*view_width/2-s*view_height/2,
            d, e, height/2+s*view_width/2-c*view_height/2]


def transform(image, coefficients, canonical_size, output_size=None, resample=Image.Resampling.NEAREST):
    output_size = tuple(output_size or canonical_size)
    cw, ch = canonical_size
    ow, oh = output_size
    iw, ih = image.size
    a, b, c, d, e, f = coefficients
    actual = (a*cw/ow*iw/cw, b*ch/oh*iw/cw, c*iw/cw,
              d*cw/ow*ih/ch, e*ch/oh*ih/ch, f*ih/ch)
    return image.transform(output_size, Image.Transform.AFFINE, actual,resample=resample)


def image_view(image, row, output_size=None):
    if row.get("transform_version") != VERSION:
        raise ValueError("Unsupported affine view version")
    resample = Image.Resampling.BILINEAR if row.get("view_resampling")=="bilinear" else Image.Resampling.NEAREST
    return transform(image, row["view_affine"], row["transform_size"], output_size,resample)


def native_view(image, row):
    a, _, _, d, _, _ = row["view_affine"]
    cw, ch = row["transform_size"]
    width = min(1444, max(1, round(math.hypot(a*image.width, d*cw*image.height/ch))))
    height = max(1, round(width*ch/cw))
    return image_view(image, row, (width, height))


def metadata(tile, scene, size, variant):
    coefficients = recipe(size, variant["angle"], variant["scale"])
    width, height = size
    a, b, c, d, e, f = coefficients
    left, top, sw, sh = tile["window"]
    def pixel(x, y):
        return left+(a*x+b*y+c)*sw/width, top+(d*x+e*y+f)*sh/height
    pixels = [pixel(x,y) for x,y in ((0,0),(width,0),(width,height),(0,height))]
    affine = Affine(*scene["affine"])
    converter,geod = geographic_tools(scene["crs"])
    geographic = [list(converter.transform(*(affine*point))) for point in pixels]
    center = converter.transform(*(affine*pixel(width/2,height/2)))
    x, y = zip(*pixels)
    import json
    return {**tile, "view_id": tile["tile_id"]+"-"+variant["tag"]+"-"+identity([VERSION,list(size)])[:16],
            "rotation_degrees":variant["angle"], "rotation_direction":"clockwise",
            "rotation_crop":[0,0,width,height], "crop_scale":variant["scale"],
            "view_version":VERSION, "transform_version":VERSION,
            "view_affine":coefficients, "transform_size":list(size),
            "view_source_quad": [list(p) for p in pixels],
            "view_source_window":[min(x),min(y),max(x)-min(x),max(y)-min(y)],
            "source_footprint_json":tile["footprint_json"],
            "footprint_json":json.dumps({"type":"Polygon","coordinates":[geographic+[geographic[0]]]}),
            "longitude":center[0], "latitude":center[1],
            "ground_width_m":abs(geod.inv(*geographic[0],*geographic[1])[2]),
            "ground_height_m":abs(geod.inv(*geographic[0],*geographic[3])[2])}
