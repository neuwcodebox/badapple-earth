import json

import numpy as np
import pytest

from badapple_earth.production.silhouette.features import balanced_weights,target_features,photo_features,evaluate,boundary_pairs
from badapple_earth.production.silhouette.pipeline import choose
from badapple_earth.production.silhouette.search import keep,search

CONFIG = {"weights":[.35,.35,.30],"edge_contrast_scale":.25,"edge_contrast_min":.08,
          "region_contrast_min":.08,"coverage_min":.5,"reversed_max":.15}


def test_region_balance_gives_each_class_equal_total_weight():
    target = np.ones((10,10),np.float32)
    target[0,0] = 0
    weights = balanced_weights(target)
    assert weights[target.ravel()==0].sum() == pytest.approx(.5)
    assert weights[target.ravel()==1].sum() == pytest.approx(.5)
    assert np.sum(weights*(target.ravel()-1)**2) == pytest.approx(.5)
    assert np.sum(weights*target.ravel()**2) == pytest.approx(.5)
    np.testing.assert_allclose(balanced_weights(np.zeros((10,10))),.01)


@pytest.mark.parametrize("invert",[False,True])
def test_visibility_requires_polarity_and_real_contrast_even_when_threshold_masks_match(invert):
    target = np.zeros((96,128),np.float32)
    target[20:80,30:95] = 1
    if invert:
        target = 1-target
    t = target_features(target)
    perfect = evaluate(t,photo_features(target),target,CONFIG)
    low = .5+.02*(target-.5)
    weak = evaluate(t,photo_features(low),low,CONFIG)
    inverse = evaluate(t,photo_features(1-target),1-target,CONFIG)
    assert perfect["visible"] and perfect["balanced_mse"] == pytest.approx(0)
    assert perfect["shape_score"] > weak["shape_score"] > inverse["shape_score"]
    np.testing.assert_array_equal(low>.5,target>.5)
    assert weak["gc"] > .99 and not weak["visible"]
    assert inverse["gc"] < -.99 and inverse["boundary_reversed"] > .9 and not inverse["visible"]
    pairs = boundary_pairs(target)
    white,black,_,_ = pairs
    assert target.ravel()[white].min() == 1 and target.ravel()[black].max() == 0


def test_visible_selection_and_explicit_baseline_fallback():
    rows = [{"dense_row":0,"view_id":"baseline","balanced_mse":.1,"shape_score":.9,"visible":False},
            {"dense_row":1,"view_id":"visible","balanced_mse":.2,"shape_score":.8,"visible":True}]
    selected,reason = choose(rows,0)
    assert selected["current"] == selected["balanced"] == rows[0]
    assert selected["visible"] == rows[1] and reason is None
    selected,reason = choose(rows,0,flat=True)
    assert selected["visible"] == rows[0] and reason == "flat_target"
    rows[1]["visible"] = False
    selected,reason = choose(rows,0)
    assert selected["visible"] == rows[0] and reason == "no_visible_candidate"


def test_float_topk_has_stable_global_address_ties_across_out_of_order_batches():
    values = np.array([[.2,.3,.3,.1,.3,.3],[0,0,0,0,0,0]],np.float32)
    scores = np.full((2,3),-np.inf,np.float32)
    positions = np.full((2,3),-1,np.int64)
    for start,stop in ((3,6),(0,3)):
        keep(scores,positions,values[:,start:stop],np.arange(start,stop))
    expected = np.array([np.lexsort((np.arange(6),-row))[:3] for row in values])
    np.testing.assert_array_equal(positions,expected)


def test_complete_pool_region_retrieval_matches_weighted_pixel_mse_and_reuses_checkpoint(tmp_path,monkeypatch):
    run,root = tmp_path/"run",tmp_path/"output"
    (run/"targets").mkdir(parents=True)
    root.mkdir()
    rng = np.random.default_rng(10)
    values = rng.integers(0,256,(12,24,32),dtype=np.uint8)
    targets = np.stack([values[4],values[4],values[8]])
    np.save(run/"targets/gray_fine.npy",targets)
    (run/"targets/manifest.json").write_text(json.dumps({"target_set_id":"targets"}))
    np.save(tmp_path/"values.npy",values)
    specs = [{"identity":"source","rows":12,"start":0,"features":str(tmp_path/"values.npy")}]
    config = {"retrieval_k":3,"candidate_batch":5,"target_batch":2}
    frames = [{"slot":i} for i in range(3)]
    result = search(run,root,config,frames,specs)
    for i,target in enumerate(targets):
        weights = balanced_weights(target.astype(np.float32)/255)
        error = np.sum(weights[None,:]*(values.reshape(12,-1).astype(np.float64)/255-target.ravel()/255)**2,axis=1)
        expected = np.lexsort((np.arange(12),error))[:3]
        np.testing.assert_array_equal(result["positions"][0,i],expected)
    def forbidden(*a,**k):
        raise AssertionError("A complete retrieval must not read candidate batches again")
    monkeypatch.setattr('badapple_earth.production.silhouette.search.feature_batches',forbidden)
    repeated = search(run,root,config,frames,specs)
    np.testing.assert_array_equal(result["positions"],repeated["positions"])


@pytest.mark.parametrize("affine",[False,True])
def test_cached_rgb_scoring_uses_the_same_actual_view_as_target(tmp_path,affine):
    from PIL import Image
    from badapple_earth.matching.affine import VERSION,recipe
    from badapple_earth.matching.views import render_view
    from badapple_earth.production.silhouette.evaluation import score_groups
    rgb = np.full((191,256,3),20,np.uint8)
    rgb[35:160,65:195] = 230
    np.save(tmp_path/"rgb.npy",rgb[None])
    row = {"dense_row":17,"rotation_degrees":180,"rotation_crop":None}
    if affine:
        row.update(transform_version=VERSION,view_affine=recipe((256,191),30,.75),transform_size=[256,191])
    image = render_view(Image.fromarray(rgb),row,(256,191))
    image.save(tmp_path/"target.png")
    groups = [(str(tmp_path/"rgb.npy"),[(0,row,[{"slot":3,"path":str(tmp_path/"target.png")}])])]
    results = score_groups(groups,CONFIG)
    assert len(results)==1 and results[0]["slot"]==3 and results[0]["dense_row"]==17
    assert results[0]["balanced_mse"] == pytest.approx(0,abs=1e-7)
    assert results[0]["gc"] > .999 and results[0]["visible"]
