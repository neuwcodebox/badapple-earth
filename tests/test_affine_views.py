import json

import numpy as np
from PIL import Image
import pytest
from rasterio.transform import Affine
from shapely.geometry import Polygon

from badapple_earth.matching.affine import VERSION, variants, recipe, transform, metadata, image_view
from badapple_earth.matching.views import render_view


SIZE = (256, 191)


@pytest.mark.parametrize("variant", variants())
def test_affine_views_stay_inside_the_photo_and_match_search_pixels(variant):
    coefficients = recipe(SIZE, variant["angle"], variant["scale"])
    white = Image.new("L", SIZE, 255)
    assert np.all(np.asarray(transform(white, coefficients, SIZE)) == 255)
    rng = np.random.default_rng(42)
    photo = Image.fromarray(rng.integers(0, 256, (*SIZE[::-1], 3), dtype=np.uint8))
    row = {"transform_version": VERSION, "view_affine": coefficients, "transform_size": SIZE}
    output = render_view(photo, row, SIZE)
    expected = np.asarray(output.convert("L").resize((32, 24), Image.Resampling.BOX))
    actual = np.asarray(transform(photo.convert("L"), coefficients, SIZE).resize((32, 24), Image.Resampling.BOX))
    np.testing.assert_array_equal(actual, expected)
    np.testing.assert_array_equal(output, image_view(photo, row))


@pytest.mark.parametrize("angle", range(0, 360, 30))
def test_crop_footprint_is_the_actual_rotated_source_quadrilateral(angle):
    scene = {"affine": [1, 0, 170, 0, -1, -40], "crs": "EPSG:4326"}
    tile = {"tile_id": "parent", "window": [0, 0, 0.02, 0.015], "footprint_json": "original"}
    full = metadata(tile, scene, SIZE, {"angle": angle, "scale": 1, "tag": "full"})
    crop = metadata(tile, scene, SIZE, {"angle": angle, "scale": .75, "tag": "crop"})
    outer = Polygon(full["view_source_quad"])
    inner = Polygon(crop["view_source_quad"])
    assert outer.contains(inner)
    assert inner.area / outer.area == pytest.approx(.75**2)
    affine = Affine(*scene["affine"])
    expected = [affine * tuple(p) for p in crop["view_source_quad"]]
    actual = json.loads(crop["footprint_json"])["coordinates"][0]
    np.testing.assert_allclose(actual[:-1], expected, atol=1e-12)
    assert actual[-1] == actual[0]
    assert crop["tile_id"] == "parent" and crop["window"] == tile["window"]


def test_clockwise_and_view_inventory():
    all_views = variants()
    assert len(all_views) == len({v["tag"] for v in all_views}) == 20
    assert sum(v["scale"] == 1 for v in all_views) == 8
    # Inverse mapping at 90 degrees: the right output edge samples the top of the input.
    a, b, c, d, e, f = recipe(SIZE, 90)
    assert d < 0 and b > 0
    assert c + b*SIZE[1]/2 == pytest.approx(SIZE[0]/2)
    assert f + d*SIZE[0]/2 == pytest.approx(SIZE[1]/2)
