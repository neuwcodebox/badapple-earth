"""Small offline contracts for the complete shape pipeline, not a quality benchmark."""

import json
from pathlib import Path

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from scipy.spatial.distance import cdist

from badapple_earth.shape.features import VERSION, descriptors
from badapple_earth.shape.geometry import chamfer, edges, region_distance, score, sdf, select, target
from badapple_earth.shape.search import DEFAULT, retrieve, topk


def test_rank_ties_auc_and_phog_flat_polarity():
    gray = np.full((48, 64), 128, np.uint8)
    phog, rank, meta = descriptors(gray)
    assert phog.shape == (1360,) and not phog.any() and not rank.any() and meta['flat_structure']
    gray[:, :24] = 30
    gray[:, 40:] = 230
    phog, rank, _ = descriptors(gray)
    from badapple_earth.shape.features import resize_float, structures
    grid = resize_float(structures(gray)[0], (32, 24)).ravel()
    mask = np.tile(np.arange(32) < 12, 24)
    k, n = mask.sum(), mask.size
    auc = .5 + n * (-mask.astype(np.float32) @ rank) / (k * (n-k))
    pairs = grid[mask, None] - grid[None, ~mask]
    direct = np.mean(pairs < 0) + .5*np.mean(pairs == 0)
    assert auc == pytest.approx(direct, abs=1e-6)
    assert np.linalg.norm(phog) == pytest.approx(1)


def test_exact_directional_joint_minimum_empty_and_border():
    first = np.zeros((31, 42), bool); first[2:19, 0:12] = True
    second = np.zeros_like(first); second[5:21, 3:16] = True
    a, b = edges(first), edges(second)
    radius = .05*min(first.shape)
    spatial = np.minimum(cdist(a['points'], b['points'])/radius, 1)
    angle = np.abs(a['theta'][:, None] - b['theta'][None])
    angle = np.minimum(angle, np.pi-angle)/(np.pi/2)
    costs = .8*spatial + .2*angle
    expected = .5*(costs.min(axis=1).mean()+costs.min(axis=0).mean())
    assert chamfer(a, b, radius) == pytest.approx(expected, abs=1e-6)
    empty = edges(np.zeros_like(first))
    assert chamfer(empty, empty, radius) == 0 and chamfer(a, empty, radius) == 1
    full = np.ones_like(first)
    assert not len(edges(full)['points']) and np.all(sdf(full)==1) and np.all(sdf(~full)==-1)


def test_fused_sdf_equals_exact_reference_without_outside_padding():
    from scipy.ndimage import distance_transform_edt
    rng=np.random.default_rng(71)
    for shape in [(1,1),(1,19),(17,1),(96,128),(191,256)]:
        for fraction in [0,.001,.1,.5,.9,.999,1]:
            mask=rng.random(shape)<fraction
            radius=.05*min(shape)
            reference=(np.full(shape,-1,np.float32) if not mask.any() else np.ones(shape,np.float32) if mask.all()
                       else np.clip((distance_transform_edt(mask)-distance_transform_edt(~mask)).astype(np.float32),-radius,radius)/radius)
            assert np.array_equal(sdf(mask),reference)


def test_mask_cache_is_byte_bounded_and_keeps_grid_identity():
    from badapple_earth.shape.cache import MaskCache, mask_key
    cache=MaskCache(512)
    key=mask_key(np.ones((2,8),bool))
    assert key != mask_key(np.ones((4,4),bool))
    first=cache.get(key,lambda:np.arange(32,dtype=np.float32))
    assert cache.get(key,lambda:pytest.fail('Same mask must reuse its value')) is first
    for i in range(20):
        cache.get(('other',i),lambda:np.zeros(32,np.float32))
        assert cache.bytes<=cache.budget
    assert cache.hits==1 and len(cache.items)<20


def test_geometry_holes_location_polarity_fade_and_shape_first_selection():
    image = np.full((48, 64), 255, np.uint8); image[12:36, 20:44] = 0
    prepared = target(image)
    same = score(image, prepared, True)['D_shape']
    hole = image.copy(); hole[19:29, 27:37] = 255
    shifted = np.roll(image, 12, axis=1)
    assert same == pytest.approx(0)
    assert score(hole, prepared, True)['D_shape'] > same
    assert score(shifted, prepared, True)['D_shape'] > same
    assert score(255-image, prepared, True)['D_shape'] > same
    assert not target(image//3+90)['near_uniform']
    rows = [dict(view_id='best', D_shape=.10, D_tone=.5, contrast_delta=.2),
            dict(view_id='tone', D_shape=.105, D_tone=.1, contrast_delta=.1),
            dict(view_id='wrong', D_shape=.3, D_tone=0, contrast_delta=.3)]
    assert select(rows)['view_id']=='tone'
    assert select(rows, tolerance=0)['view_id']=='best'
    assert select([{**r,'contrast_delta':0} for r in rows])['low_visibility']


def test_exact_shard_topk_including_cutoff_ties(tmp_path):
    from badapple_earth.shape.index import Store
    from badapple_earth.shape.search import load_retrieval
    root=tmp_path/'index'; root.mkdir()
    shards=[]
    vectors=np.array([[1,0],[1,0],[0,1],[.5,.5]], np.float32)
    ids=np.array(['z','a','c','b'], dtype='S64')
    for name, positions in [('one',[0,2]),('two',[1,3])]:
        folder=root/'shards'/name; folder.mkdir(parents=True)
        np.save(folder/'phog.npy', vectors[positions]); np.save(folder/'rank.npy', vectors[positions])
        np.save(folder/'ids.npy',ids[positions]); shards.append({'shard_id':name,'rows':2})
    (root/'manifest.json').write_text(json.dumps({'index_id':'fixed','shards':shards}))
    prepared=[{'near_uniform':False,'phog':np.array([1,0],np.float32),'rank_queries':[]}]
    config={**DEFAULT,'retrieval_k':1,'query_batch':1,'vector_batch':1}
    out=tmp_path/'out';out.mkdir()
    union=retrieve(Store(root),prepared,config,out)
    assert next(iter(union[0].values()))['view_id']=='a'
    assert topk(vectors[:,0], ids, 1).tolist()==[1]
    assert load_retrieval(out,1)==union


def test_complete_local_pipeline_resume_and_original_color_output(tmp_path):
    from badapple_earth.shape.index import build, Store
    from badapple_earth.shape.search import match
    from badapple_earth.shape.output import materialize
    source=tmp_path/'source'; (source/'shards/one').mkdir(parents=True); (source/'catalog').mkdir()
    square=np.full((48,64,3),235,np.uint8); square[12:36,20:44]=[20,35,50]
    white=np.full_like(square,255)
    mixed=np.empty_like(square); mixed[:24,:32]=15; mixed[:24,32:]=85; mixed[24:,:32]=170; mixed[24:,32:]=240
    photos=[square,white,mixed]
    rows=[]
    for i,image in enumerate(photos):
        Image.fromarray(image).save(source/f'{i}.png')
        rows.append({'tile_id':f'tile{i}','view_id':f'tile{i}','aoi_id':'test','scene_id':'scene','feature_row':i,
                     'sample_image':f'{i}.png','window':[i*64,0,64,48], 'acquired_at':'2026-01-01T00:00:00Z',
                     'footprint_json':'original','rotation_degrees':0})
    pq.write_table(pa.Table.from_pylist(rows),source/'shards/one/tiles.parquet')
    (source/'catalog/scene.json').write_text(json.dumps({'crs':'EPSG:32633','affine':[10,0,500000,0,-10,1000000]}))
    (source/'manifest.json').write_text(json.dumps({'index_id':'source','rows':3,'canonical_size':[64,48],
                                                   'config':{'block_size':2048},'shards':[{'shard_id':'one','rows':3}]}))
    analysis=tmp_path/'analysis'
    m=build(source,analysis,workers=2)
    assert m['places']==3 and m['view_count']==12
    store=Store(analysis)
    from badapple_earth.matching.views import rotate_image
    for shard in store.manifest['shards']:
        for position,row in enumerate(store.rows(shard['shard_id'])):
            original=Image.fromarray(np.asarray(store.array(shard['shard_id'],'rgb.npy')[row['base_row']]))
            expected=rotate_image(original,row['rotation_degrees'],original.size,row['rotation_crop'])
            assert np.array_equal(np.asarray(store.rgb((shard['shard_id'],position))),np.asarray(expected))
    assert build(source,analysis,workers=2)['view_count']==12
    targets=tmp_path/'targets'; targets.mkdir()
    target_rows=[]
    for i,image in enumerate(photos):
        Image.fromarray(image).save(targets/f'{i}.png')
        target_rows.append({'target_id':f'target{i}','slot':i,'source_pts':i,'target_time':i,'frame':f'{i}.png'})
    pq.write_table(pa.Table.from_pylist(target_rows),targets/'targets.parquet')
    (targets/'manifest.json').write_text(json.dumps({'target_set_id':'targets','canonical_size':[64,48],'count':3,
       'config':{'audio_mode':'silent_pending_rights_review','sample_fps':1,'output_fps':4,'duration_seconds':3}}))
    run=tmp_path/'run'; config={'workers':2,'retrieval_k':8,'coarse_k':4,'path_keep':2}
    result=match(targets,analysis,run,config)
    assert result['status']=='complete' and result['near_uniform_count']==1
    assert match(targets,analysis,run,config)['status']=='complete'
    materialize(targets,analysis,run)
    chosen=pq.read_table(run/'selected.parquet').to_pylist()
    assert len(chosen)==3 and (run/'comparison.mp4').stat().st_size>0 and (run/'report.html').exists()
    store=Store(analysis)
    for row in chosen:
        ref=(row['shape_shard'],row['feature_row'])
        with Image.open(run/row['photo']) as image:
            assert np.array_equal(np.asarray(image),np.asarray(store.rgb(ref)))
    assert chosen[1]['near_uniform']
