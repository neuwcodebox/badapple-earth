from itertools import product

import numpy as np
from PIL import Image
import pytest

from badapple_earth.production.silhouette.temporal import candidate_pool,color_descriptor,path


def test_shape_band_rejects_better_color_with_weak_shape_and_preserves_fallback():
    rows = [{"dense_row":i,"shape_score":score,"balanced_mse":.1,"view_id":str(i),"visible":visible}
            for i,score,visible in ((0,.8,True),(1,.79,True),(2,.77,True),(3,.9,False))]
    assert [r['dense_row'] for r in candidate_pool(rows,rows[0],.02,24)] == [0,1]
    baseline = {**rows[0],"fallback":"no_visible_candidate"}
    assert candidate_pool(rows,baseline,.02,24) == [baseline]


def test_color_path_considers_future_instead_of_greedy_first_frame():
    pools = [[{"shape_score":1.},{"shape_score":.99}],[{"shape_score":1.}]]
    colors = [np.array([[1.,0.],[0.,1.]]),np.array([[0.,1.]])]
    assert path(pools,colors,1) == [1,0]
    assert path(pools,colors,0) == [0,0]


def test_color_path_matches_exhaustive_global_minimum():
    rng = np.random.default_rng(26)
    sizes = [3,2,3,1]
    scores = [rng.random(n) for n in sizes]
    colors = [rng.random((n,5)) for n in sizes]
    pools = [[{"shape_score":float(s)} for s in values] for values in scores]
    def cost(indices):
        return sum(s.max()-s[i] for s,i in zip(scores,indices))+.2*sum(
            np.sum((colors[t-1][indices[t-1]]-colors[t][indices[t]])**2) for t in range(1,4))
    result = path(pools,colors,.2)
    assert cost(result) == pytest.approx(min(map(cost,product(*(range(n) for n in sizes)))))


def test_region_palette_is_independent_of_foreground_area():
    descriptors = []
    for width in (8,40):
        mask = np.zeros((48,64),bool)
        mask[:,:width] = True
        rgb = np.zeros((48,64,3),np.uint8)
        rgb[mask] = [225,200,150]
        rgb[~mask] = [10,40,150]
        descriptors.append(color_descriptor(Image.fromarray(rgb),mask))
    np.testing.assert_allclose(*descriptors,atol=1e-6)
    changed = rgb.copy()
    changed[~mask] = [15,120,30]
    other = color_descriptor(Image.fromarray(changed),mask)
    assert np.sum((other-descriptors[-1])**2) > .05
