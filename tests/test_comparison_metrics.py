import numpy as np
import pytest

from badapple_earth.production import fsim
from badapple_earth.production.metrics import features, scores, winner


def silhouette():
    image = np.zeros((191, 256), dtype=np.float32)
    image[24:158, 45:140] = 1
    image[110:155, 140:210] = 1
    return image


def test_fsim_identity_symmetry_structure_and_featureless_inputs():
    target = silhouette()
    shifted = np.roll(target, 50, axis=1)
    left, right = fsim.features(target), fsim.features(shifted)
    error = float(np.mean((target-shifted)**2))
    assert fsim.similarity(left, left, 0) == pytest.approx(1, abs=1e-6)
    assert fsim.similarity(left, right, error) < .99
    assert fsim.similarity(left, right, error) == pytest.approx(fsim.similarity(right, left, error))
    black = fsim.features(np.zeros_like(target))
    white = fsim.features(np.ones_like(target))
    assert fsim.similarity(black, black, 0) == 1
    assert fsim.similarity(black, white, 1) == 0


def test_correlation_ignores_positive_brightness_and_contrast_changes_but_not_inversion():
    target = silhouette()
    left = features(target)
    equivalent = scores(left, features(.2 + .6*target), .1)
    inverted = scores(left, features(1-target), .1)
    assert equivalent["zncc"] == pytest.approx(1, abs=1e-6)
    assert inverted["zncc"] == pytest.approx(-1, abs=1e-6)
    assert scores(left, left, 0)["multi-mse"] == 0
    assert scores(left, features(np.roll(target, 50, axis=1)), .2)["multi-mse"] > 0
    constant = scores(features(np.zeros_like(target)), left, .1)
    assert all(np.isfinite(v) for v in constant.values())
    assert constant["zncc"] == -1


def test_metric_direction_and_ties_keep_original_retrieval_order():
    candidates = [{"rank": 1, "view_id": "a"}, {"rank": 4, "view_id": "b"},
                  {"rank": 9, "view_id": "c"}]
    for method in ("mse", "multi-mse"):
        assert winner(method, [.5, .1, .1], candidates) == 1
    for method in ("ms-ssim", "fsim", "zncc"):
        assert winner(method, [.1, .5, .5], candidates) == 1
