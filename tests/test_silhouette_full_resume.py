from io import BytesIO
from pathlib import Path
import json

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from badapple_earth.production.silhouette.control import batches
from badapple_earth.production.silhouette.features import target_features,photo_features,evaluate
from badapple_earth.production.silhouette.search import search
from badapple_earth.production.silhouette.target_cache import pack,unpack
from badapple_earth.production.silhouette.full import reduced


def square(value):return value*value


def test_pause_drains_saved_batch_and_resume_runs_only_unfinished_jobs(tmp_path):
    flag = tmp_path/'pause.request'
    saved = {}
    def save(key,result):
        saved[key] = result
        flag.touch()
    with pytest.raises(InterruptedError):
        batches([((i,),i) for i in range(4)],square,save,1,flag)
    assert saved == {0:0}
    flag.unlink()
    batches([((i,),i) for i in range(4) if i not in saved],square,lambda k,v:saved.update({k:v}),1,flag)
    assert saved == {0:0,1:1,2:4,3:9}


def test_shared_target_cache_preserves_exact_metric_arrays_and_results():
    gray = np.zeros((191,256),np.float32)
    gray[40:150,70:200] = 1
    original = target_features(gray)
    stream = BytesIO()
    schema = pack(original,stream)
    restored = unpack(schema,np.frombuffer(stream.getvalue(),np.uint8))
    config = dict(weights=[.35,.35,.3],region_contrast_min=.08,edge_contrast_min=.08,
                  edge_contrast_scale=.25,coverage_min=.5,reversed_max=.15)
    photo = photo_features(gray)
    assert evaluate(original,photo,gray,config) == evaluate(restored,photo,gray,config)
    assert not restored[-1]


def test_retrieval_pause_saves_next_offset_and_resume_matches_uninterrupted(tmp_path):
    run,root,reference = tmp_path/'run',tmp_path/'output',tmp_path/'reference'
    (run/'targets').mkdir(parents=True)
    root.mkdir();reference.mkdir()
    rng = np.random.default_rng(42)
    values = rng.integers(0,256,(19,24,32),dtype=np.uint8)
    np.save(run/'targets/gray_fine.npy',values[[3,8]])
    (run/'targets/manifest.json').write_text(json.dumps({'target_set_id':'targets'}))
    np.save(tmp_path/'values.npy',values)
    specs = [{'identity':'source','rows':19,'start':0,'features':str(tmp_path/'values.npy')}]
    config = dict(retrieval_k=3,candidate_batch=5,target_batch=2,pause_file=str(root/'pause.request'))
    frames = [{'slot':0},{'slot':1}]
    Path(config['pause_file']).touch()
    with pytest.raises(InterruptedError):search(run,root,config,frames,specs)
    with np.load(root/'retrieval.npz') as saved:
        assert int(saved['offset']) == 5 and int(saved['source']) == 0
    Path(config['pause_file']).unlink()
    resumed = search(run,root,config,frames,specs)
    baseline = search(run,reference,config,frames,specs)
    np.testing.assert_array_equal(resumed['scores'],baseline['scores'])
    np.testing.assert_array_equal(resumed['positions'],baseline['positions'])


def test_streamed_reduction_keeps_global_shape_band_and_explicit_fallback(tmp_path,monkeypatch):
    root,run = tmp_path/'output',tmp_path/'run'
    root.mkdir();run.mkdir()
    pq.write_table(pa.Table.from_pylist([{'slot':0,'dense_row':0},{'slot':1,'dense_row':3}]),run/'selected.parquet')
    meta = {i:{'view_id':str(i),'tile_id':str(i)} for i in range(4)}
    records = [dict(slot=0,dense_row=i,visible=True,shape_score=s,balanced_mse=.1)
               for i,s in enumerate((.5,.8,.79))]
    records += [dict(slot=1,dense_row=3,visible=False,shape_score=.4,balanced_mse=.2)]
    filename = root/'scores.parquet'
    pq.write_table(pa.Table.from_pylist(records),filename)
    class Cache:
        def __init__(self,*args):pass
        def __getitem__(self,slot):return (None,None,None,False)
    monkeypatch.setattr('badapple_earth.production.silhouette.full.TargetCache',Cache)
    reduced(root,run,[{'slot':0},{'slot':1}],meta,[filename],
            dict(shape_tolerance=.02,max_candidates=24,pause_file=str(root/'pause.request')),[])
    rows = pq.read_table(root/'choices.parquet').to_pylist()
    assert [(r['slot'],r['dense_row'],r['fallback']) for r in rows] == [
        (0,1,None),(0,2,None),(1,3,'no_visible_candidate')]


def test_reuse_searches_only_missing_queries_and_keeps_completed_results(tmp_path,monkeypatch):
    import importlib
    module = importlib.import_module('badapple_earth.production.silhouette.search')
    run,prior,root,reference = [tmp_path/name for name in ('run','prior','output','reference')]
    (run/'targets').mkdir(parents=True)
    for folder in (prior,root,reference):folder.mkdir()
    rng = np.random.default_rng(71)
    values = rng.integers(0,256,(17,24,32),dtype=np.uint8)
    np.save(run/'targets/gray_fine.npy',values[[2,9]])
    (run/'targets/manifest.json').write_text(json.dumps({'target_set_id':'targets'}))
    frames = [{'slot':0,'source_pts':0,'source_time_base':'1'},
              {'slot':1,'source_pts':1,'source_time_base':'1'}]
    pq.write_table(pa.Table.from_pylist(frames),run/'targets/targets.parquet')
    np.save(tmp_path/'values.npy',values)
    specs = [{'identity':'source','rows':17,'start':0,'features':str(tmp_path/'values.npy')}]
    config = dict(retrieval_k=3,candidate_batch=5,target_batch=64)
    previous = search(run,prior,config,frames[:1],specs)
    (prior/'manifest.json').write_text(json.dumps({'status':'complete','config':{'start':0,'duration':1}}))
    counts = []
    keep = module.keep
    def counted(scores,positions,dot,indices):
        counts.append(dot.shape[0])
        keep(scores,positions,dot,indices)
    monkeypatch.setattr(module,'keep',counted)
    result = search(run,root,{**config,'reuse_searches':[str(prior)]},frames,specs)
    assert counts and set(counts)=={1}
    np.testing.assert_array_equal(result['positions'][:,0],previous['positions'][:,0])
    np.testing.assert_array_equal(result['scores'][:,0],previous['scores'][:,0])
    monkeypatch.setattr(module,'keep',keep)
    baseline = search(run,reference,config,frames,specs)
    np.testing.assert_array_equal(result['positions'],baseline['positions'])
