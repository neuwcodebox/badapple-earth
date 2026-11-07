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


def test_completed_candidate_reduction_skips_old_score_job_reconstruction(tmp_path,monkeypatch):
    from badapple_earth.production.silhouette.full import score_all
    (tmp_path/'choices.parquet').touch()
    def forbidden(*args,**kwargs):raise AssertionError('Completed scores must not be read or rebuilt')
    monkeypatch.setattr('badapple_earth.production.silhouette.full.pq.read_table',forbidden)
    assert score_all(tmp_path,tmp_path,[],{}, {},set()) == []


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
    pq.write_table(pa.Table.from_pylist([{'slot':r['slot'],'dense_row':r['dense_row']} for r in records]),root/'shortlists.parquet')
    records.append(dict(slot=1,dense_row=2,visible=True,shape_score=.99,balanced_mse=.01))
    pq.write_table(pa.Table.from_pylist(records),filename)
    class Cache:
        def __init__(self,*args):pass
        def __getitem__(self,slot):return (None,None,None,False)
    monkeypatch.setattr('badapple_earth.production.silhouette.full.TargetCache',Cache)
    reduced(root,run,[{'slot':0},{'slot':1}],meta,[filename],
            dict(shape_tolerance=.02,max_candidates=24,pause_file=str(root/'pause.request'),reuse_score_runs=['baseline']),[])
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


def test_appended_sources_reuse_baseline_and_match_complete_search_after_resume(tmp_path,monkeypatch):
    import importlib
    module = importlib.import_module('badapple_earth.production.silhouette.search')
    run,prior,root,reference = [tmp_path/name for name in ('run','prior','output','reference')]
    (run/'targets').mkdir(parents=True)
    for folder in (prior/'retrieval',root,reference):folder.mkdir(parents=True)
    rng = np.random.default_rng(83)
    values = rng.integers(0,256,(23,24,32),dtype=np.uint8)
    np.save(run/'targets/gray_fine.npy',values[[2,19]])
    (run/'targets/manifest.json').write_text(json.dumps({'target_set_id':'targets'}))
    specs = []
    for name,begin,end in (('old',0,12),('new',12,23)):
        filename = tmp_path/(name+'.npy')
        np.save(filename,values[begin:end])
        specs.append(dict(identity=name,rows=end-begin,start=begin,features=str(filename)))
    frames = [{'slot':0},{'slot':1}]
    config = dict(retrieval_k=3,candidate_batch=5,target_batch=2)
    search(run,prior/'retrieval',config,frames,specs[:1])
    (prior/'manifest.json').write_text(json.dumps({'status':'complete','config':{'baseline':True}}))
    monkeypatch.setattr(module,'sources',lambda config:specs[:1])
    incremental = {**config,'incremental_retrieval':str(prior),'pause_file':str(root/'pause.request')}
    batches_read = []
    original = module.feature_batches
    def tracked(path,*args,**kwargs):
        batches_read.append(path)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(module,'feature_batches',tracked)
    Path(incremental['pause_file']).touch()
    with pytest.raises(InterruptedError):search(run,root,incremental,frames,specs)
    Path(incremental['pause_file']).unlink()
    result = search(run,root,incremental,frames,specs)
    assert batches_read and set(batches_read)=={specs[1]['features']}
    monkeypatch.setattr(module,'feature_batches',original)
    complete = search(run,reference,config,frames,specs)
    np.testing.assert_array_equal(result['positions'],complete['positions'])
    np.testing.assert_array_equal(result['scores'],complete['scores'])
    mismatched = [{**specs[0],'identity':'changed'},specs[1]]
    with pytest.raises(ValueError,match='preserve every baseline'):
        search(run,root,incremental,frames,mismatched)


def test_shape_score_reuse_keeps_new_frame_view_pairs_in_previously_scored_slots(tmp_path,monkeypatch):
    import importlib
    module = importlib.import_module('badapple_earth.production.silhouette.full')
    run,root,prior,index = [tmp_path/name for name in ('run','output','prior','index')]
    for folder in (run,root,prior/'scores',index):folder.mkdir(parents=True)
    metric = dict(weights=[.35,.35,.3],region_contrast_min=.08,edge_contrast_min=.08,
                  edge_contrast_scale=.25,coverage_min=.5,reversed_max=.15)
    (prior/'manifest.json').write_text(json.dumps({'status':'complete','config':metric}))
    pq.write_table(pa.Table.from_pylist([{'slot':0,'dense_row':0}]),prior/'scores/00000.parquet')
    pq.write_table(pa.Table.from_pylist([{'slot':0,'dense_row':0},{'slot':0,'dense_row':1},
                                        {'slot':1,'dense_row':0}]),root/'shortlists.parquet')
    (run/'manifest.json').write_text(json.dumps({'config':{'index':str(index),'extra_rgb_cache':'missing'}}))
    (index/'manifest.json').write_text('{}')
    monkeypatch.setattr(module,'members',lambda *args:[(index,{'index_id':'member'},set())])
    scored = []
    def score(groups,*args):
        for filename,jobs in groups:
            for offset,row,slots in jobs:
                scored.extend((int(slot),row['dense_row']) for slot in slots)
        return [{'slot':slot,'dense_row':position} for slot,position in scored]
    monkeypatch.setattr(module,'score_group',score)
    def sequential(jobs,function,save,*args):
        for arguments,destination in jobs:save(destination,function(*arguments))
    monkeypatch.setattr(module,'batches',sequential)
    config = {**metric,'incremental_retrieval':str(prior),'reuse_score_runs':[str(prior)],
              'workers':1,'pause_file':str(root/'pause.request')}
    meta = {i:dict(dense_row=i,tile_id=str(i),index_member='member',rgb_cache_ref='rgb.npy',rgb_cache_row=i) for i in (0,1)}
    paths = module.score_all(root,run,[{'slot':0},{'slot':1}],meta,config,set())
    assert set(scored)=={(0,1),(1,0)}
    assert paths[0]==prior/'scores/00000.parquet'
