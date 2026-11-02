import numpy as np

from badapple_earth.production.grid.search import scan
from badapple_earth.production.grid.pipeline import best


def test_higher_grid_mse_keeps_single_pixel_errors_and_stable_ties_across_batches():
    rng = np.random.default_rng(92)
    targets = [np.full((2, 12, 16), 255, dtype=np.uint8), np.full((2, 24, 32), 255, dtype=np.uint8)]
    sources = []
    for target in targets:
        values = rng.integers(0, 256, (8, *target.shape[1:]), dtype=np.uint8)
        values[1] = target[0]
        values[2] = target[0]
        values[2, -1, -1] = 254
        values[3] = values[2]
        sources.append(values)
        target[1, :3, :4] = 0
    ties = np.array([7, 6, 5, 1, 2, 3, 4, 0], dtype=np.int32)
    result = scan(targets, [sources[0].reshape(8, -1).astype(np.float32), sources[1]], ties,
                  k=4, candidate_batch=3, target_batch=1)
    for route, (target, source) in enumerate(zip(targets, sources)):
        expected = np.sum((target[:, None].astype(np.int64)-source[None].astype(np.int64))**2, axis=(2, 3))
        order = np.array([np.lexsort((ties, row))[:4] for row in expected])
        assert np.array_equal(result[f"positions{route}"], order)
        assert np.array_equal(result[f"scores{route}"], np.take_along_axis(expected, order, axis=1))
        assert result[f"scores{route}"][0, :3].tolist() == [0, 1, 1]


def test_ms_ssim_selection_separates_resolution_from_candidate_width():
    rows = [{"view_id":"a", "rank16":1, "rank32":1, "ms_ssim":.6},
            {"view_id":"b", "rank16":50, "rank32":None, "ms_ssim":.8},
            {"view_id":"c", "rank16":None, "rank32":24, "ms_ssim":.75},
            {"view_id":"d", "rank16":2, "rank32":2, "ms_ssim":.7}]
    assert best(rows, "16-20", 512)["view_id"] == "d"
    assert best(rows, "16-wide", 512)["view_id"] == "b"
    assert best(rows, "32-20", 512)["view_id"] == "d"
    assert best(rows, "32-wide", 512)["view_id"] == "c"
    assert best(rows, "union", 512)["view_id"] == "b"
