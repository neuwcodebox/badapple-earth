import numpy as np
from PIL import Image

from badapple_earth.production.silhouette.temporal import luminance_descriptor


def test_luminance_ignores_hue_when_displayed_grayscale_is_identical():
    red = Image.new('RGB',(64,48),(255,0,0))
    green = Image.new('RGB',(64,48),(0,130,0))
    np.testing.assert_array_equal(np.asarray(red.convert('L')),np.asarray(green.convert('L')))
    mask = np.indices((48,64))[1]<23
    np.testing.assert_array_equal(luminance_descriptor(red,mask),luminance_descriptor(green,mask))


def test_foreground_area_does_not_change_region_brightness_descriptor():
    values = []
    for width in (8,40):
        mask = np.indices((48,64))[1]<width
        gray = np.where(mask,220,35).astype(np.uint8)
        values.append(luminance_descriptor(Image.fromarray(gray),mask))
    np.testing.assert_allclose(*values,atol=1e-7)


def test_soft_histogram_tracks_small_brightness_changes_continuously():
    mask = np.indices((48,64))[1]<32
    features = [luminance_descriptor(Image.new('L',(64,48),value),mask) for value in (102,103,160)]
    small = np.sum((features[1]-features[0])**2)
    large = np.sum((features[2]-features[0])**2)
    assert 0<small and large>20*small
