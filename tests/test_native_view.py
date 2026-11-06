import numpy as np
from PIL import Image

from badapple_earth.production.assets import native_view


def test_native_unrotated_photo_without_explicit_crop_preserves_full_image():
    rgb = np.random.default_rng(62).integers(0,256,(382,512,3),dtype=np.uint8)
    image = Image.fromarray(rgb)
    result = native_view(image,{"rotation_degrees":0,"rotation_crop":None},(256,191))
    assert result.size == image.size
    np.testing.assert_array_equal(np.asarray(result),rgb)
