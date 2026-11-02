import numpy as np

from badapple_earth.matching.distance import exact_sse
from badapple_earth.production.search import scan


def test_fast_mse_and_topk_are_integer_exact_with_extremes_and_ties():
    rng = np.random.default_rng(23)
    candidates = rng.integers(0, 256, (207, 192), dtype=np.uint8)
    candidates[:4] = [np.zeros(192), np.full(192, 255), np.full(192, 255), np.full(192, 254)]
    queries = np.stack([candidates[0], candidates[1], candidates[3], candidates[100]])
    expected = np.sum((queries[:, None].astype(np.int64) - candidates.astype(np.int64))**2, axis=2)
    np.testing.assert_array_equal(exact_sse(queries, candidates), expected)
    ties = rng.permutation(len(candidates)).astype(np.int32)
    features = candidates.astype(np.float32)
    norms = np.sum(candidates.astype(np.int32)**2, axis=1).astype(np.int32)
    scores, positions = scan(queries, features, norms, ties, k=20, candidate_batch=31)
    for i in range(len(queries)):
        order = np.lexsort((ties, expected[i]))[:20]
        np.testing.assert_array_equal(positions[i], order)
        np.testing.assert_array_equal(scores[i], expected[i, order])
