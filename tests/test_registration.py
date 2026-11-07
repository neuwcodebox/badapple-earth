import numpy as np
from PIL import Image
import pytest
import json
import pyarrow as pa
import pyarrow.parquet as pq

from badapple_earth.production.registration.search import descriptor
from badapple_earth.production.registration.geometry import coefficients,metadata,SIZE,parameters_from_matrix
from badapple_earth.production.registration.align import coarse_search,refine,warped_gray,set_target_cache,target_image
from badapple_earth.production.registration.workers import prepare_targets
from badapple_earth.matching.affine import transform
from badapple_earth.production.registration.selection import choose,reduce_options
from badapple_earth.production.registration.search import retrieve
from badapple_earth.production.registration.pipeline import fine_groups
from badapple_earth.production.registration.assets import relative_view,reuse_native
from badapple_earth.production.registration.motion import estimate,propagation
from badapple_earth.production.registration.tracking import tracking_groups
import cv2


def test_global_descriptor_preserves_translation_and_rejects_uniform_content():
    rng = np.random.default_rng(408)
    image = rng.integers(0,256,(24,32),dtype=np.uint8)
    shifted = np.roll(image,(5,-8),axis=(0,1))
    np.testing.assert_allclose(descriptor(image[None]),descriptor(shifted[None]),atol=2e-7)
    np.testing.assert_array_equal(descriptor(np.full((1,24,32),120,np.uint8)),0)


@pytest.mark.parametrize('angle',[0,17,90,137,270,359])
def test_every_allowed_pan_zoom_rotation_has_real_pixels_at_all_corners(angle):
    white = Image.new('L',SIZE,255)
    for u,v in ((0,0),(1,0),(0,1),(1,1),(.5,.5)):
        for zoom in (.35,.7,1):
            matrix = coefficients([u,v,zoom,angle])
            result = transform(white,matrix,SIZE,resample=Image.Resampling.BILINEAR)
            assert np.asarray(result).min()==255


def test_registration_recovers_off_centre_shape_with_rotation_and_scale(tmp_path):
    photo = np.full(SIZE[::-1],235,np.uint8)
    photo[25:140,70:115] = 15
    photo[100:140,70:180] = 15
    photo[40:70,145:180] = 45
    truth = [.82,.18,.7,12]
    target = transform(Image.fromarray(photo),coefficients(truth),SIZE,resample=Image.Resampling.BILINEAR)
    filename = tmp_path/'target.png'
    target.save(filename)
    score,initial = coarse_search(photo.astype(np.float32)/255,str(filename),0,
                                 dict(coarse_width=80,angle_offsets=[-12,0,12],crop_scales=[.5,.7,.85,1]))
    fitted = refine(photo.astype(np.float32)/255,str(filename),initial)
    measured = warped_gray(photo.astype(np.float32)/255,fitted)
    expected = np.asarray(target.resize((64,48),Image.Resampling.BOX),np.float32)/255
    assert score>.85
    assert np.mean((measured-expected)**2)<.012
    assert abs(fitted[0]-.5)>.15


def test_registered_metadata_tracks_shifted_source_and_original_asset():
    row = dict(tile_id='real-source',dense_row=17,window=[10,20,256,191],
               footprint_json='base',source_footprint_json='original',scene_id='scene',source_id='provider')
    scene = dict(affine=[.0001,0,100,0,-.0001,30],crs='EPSG:4326')
    parameters = [.9,.1,.5,30]
    out = metadata(row,scene,parameters)
    matrix = coefficients(parameters)
    cx = matrix[0]*128+matrix[1]*95.5+matrix[2]
    cy = matrix[3]*128+matrix[4]*95.5+matrix[5]
    assert out['longitude']==pytest.approx(100+.0001*(10+cx))
    assert out['latitude']==pytest.approx(30-.0001*(20+cy))
    assert out['base_dense_row']==17 and out['window']==row['window']
    assert out['scene_id']=='scene' and out['source_footprint_json']=='original'
    assert metadata(row,scene,[.1,.9,.5,30])['view_id']!=out['view_id']


def test_temporal_choice_follows_target_motion_instead_of_freezing_a_photo():
    rng = np.random.default_rng(13)
    first = rng.integers(0,256,(24,32),dtype=np.uint8)
    pixels = np.stack([first,np.roll(first,4,axis=1)])
    start = coefficients([.5,.5,.7,0])
    moved = start.copy()
    moved[2] -= moved[0]*32
    def row(name,matrix):
        return dict(view_id=name,tile_id='same-photo',shape_score=.7,luminance=[0.,0.],view_affine=matrix)
    initial,frozen,moving = row('start',start),row('frozen',start),row('moving',moved)
    result = choose([[initial],[frozen,moving]],pixels,dict(continuity_weight=.2,motion_weight=.1))
    assert result[1]['view_id']=='moving'


def test_registered_options_keep_baseline_quality_and_reject_invisible_shapes():
    baseline = dict(slot=0,view_id='baseline',shape_score=.6,balanced_mse=.2,visible=True,fallback=None)
    def row(name,score,visible):return {**baseline,'view_id':name,'shape_score':score,'visible':visible}
    options = [row('better',.61,True),row('worse',.59,True),row('invisible',.9,False)]
    config = dict(shape_tolerance=.025,max_candidates=16)
    result = reduce_options(options,[baseline],config)
    assert {r['view_id'] for r in result[0]}=={'baseline','better'}
    flat = {**baseline,'fallback':'flat_target'}
    assert reduce_options(options,[flat],config)==[[flat]]
    fallback = {**baseline,'visible':False,'fallback':'no_visible_candidate'}
    visible = row('real-boundary',.59,True)
    assert reduce_options([visible],[fallback],config)==[[visible]]


def test_position_tolerant_retrieval_resumes_exactly_without_revisiting_saved_rows(tmp_path):
    run,shape,root,reference = [tmp_path/p for p in ('run','shape','output','reference')]
    (run/'targets').mkdir(parents=True)
    (shape/'retrieval').mkdir(parents=True)
    rng = np.random.default_rng(6)
    values = rng.integers(0,256,(11,24,32),dtype=np.uint8)
    np.save(tmp_path/'values.npy',values)
    np.save(run/'targets/gray_fine.npy',values[[2,8]])
    (run/'targets/manifest.json').write_text(json.dumps({'target_set_id':'targets'}))
    frames = [dict(slot=i,source_pts=i,source_time_base='1') for i in (0,1)]
    pq.write_table(pa.Table.from_pylist(frames),run/'targets/targets.parquet')
    (shape/'manifest.json').write_text(json.dumps({'config':{'run':str(run)}}))
    (shape/'retrieval/sources.json').write_text(json.dumps([dict(identity='source',start=0,rows=11,features=str(tmp_path/'values.npy'))]))
    config = dict(source=str(shape),output=str(root),end_seconds=2,retrieval_k=3,candidate_batch=4,target_batch=2)
    root.mkdir()
    (root/'pause.request').touch()
    with pytest.raises(InterruptedError):retrieve(config,root/'retrieval')
    with np.load(root/'retrieval/retrieval.npz') as saved:assert int(saved['offset'])==4
    (root/'pause.request').unlink()
    resumed = retrieve(config,root/'retrieval')
    full = retrieve({**config,'output':str(reference)},reference/'retrieval')
    np.testing.assert_array_equal(resumed['positions'],full['positions'])


def test_refinement_keeps_current_and_neighbor_photos_despite_lower_coarse_rank(tmp_path):
    baseline = tmp_path/'baseline'
    baseline.mkdir()
    pq.write_table(pa.Table.from_pylist([dict(slot=0,tile_id='current'),dict(slot=1,tile_id='neighbor')]),baseline/'selected.parquet')
    path = tmp_path/'coarse.parquet'
    rows = [dict(slot=0,seed_dense_row=i,coarse_score=score,parameters=[.5,.5,.7,0])
            for i,score in enumerate((.4,.5,.9,.8))]
    pq.write_table(pa.Table.from_pylist(rows),path)
    meta = {i:dict(tile_id=tile,dense_row=i,_rgb_filename='local.npy',_rgb_offset=i)
            for i,tile in enumerate(('current','neighbor','other-a','other-b'))}
    groups = fine_groups([path],meta,dict(baseline=str(baseline),neighbor_seconds=1,neighbor_samples=3,refine_candidates=3))
    assert {row['tile_id'] for _,jobs in groups for _,row,_ in jobs}=={'current','neighbor','other-a'}


def test_shared_target_pixels_avoid_png_reopening_and_survive_interrupted_publish(tmp_path,monkeypatch):
    run,output = tmp_path/'run',tmp_path/'output'
    directory = run/'targets'
    (directory/'frames').mkdir(parents=True)
    (directory/'manifest.json').write_text(json.dumps(dict(target_set_id='input')))
    rows=[]
    for slot in range(3):
        path=f'frames/{slot:06d}.png'
        Image.new('L',SIZE,7+slot).save(directory/path)
        rows.append(dict(slot=slot,frame=path))
    pq.write_table(pa.Table.from_pylist(rows),directory/'targets.parquet')
    prepare_targets(run,output,3)
    file=output/'target-pixels.npy'
    np.testing.assert_array_equal(np.load(file)[:,0,0],[7,8,9])
    state=json.loads((output/'target-pixels.json').read_text())
    state['status']='building'
    (output/'target-pixels.json').write_text(json.dumps(state))
    def forbidden(*args,**kwargs):raise AssertionError('Published cache must not reopen PNG files')
    monkeypatch.setattr(Image,'open',forbidden)
    prepare_targets(run,output,3)
    set_target_cache(file)
    try:
        np.testing.assert_array_equal(target_image(str(directory/'frames/000002.png')),9)
    finally:set_target_cache(None)


def test_native_reuse_only_samples_an_existing_photo_crop_that_covers_the_view(tmp_path):
    baseline,root = tmp_path/'baseline',tmp_path/'output'
    folder = baseline/'assets/selected'
    folder.mkdir(parents=True)
    old=dict(tile_id='photo',view_id='old',view_affine=[1,0,0,0,1,0],rotation_degrees=0,window=[0,0,512,382])
    pq.write_table(pa.Table.from_pylist([old]),baseline/'selected.parquet')
    yy,xx=np.mgrid[:382,:512]
    image=Image.fromarray(np.stack([xx%256,yy%256,(xx+yy)%256],axis=-1).astype(np.uint8))
    image.save(folder/'old.png')
    parameters=[.7,.2,.5,15]
    new={**old,'view_id':'new','view_affine':coefficients(parameters),'registration_parameters':parameters}
    relative=relative_view(old,new)
    assert relative is not None
    reuse_native(root,baseline,[new])
    with Image.open(root/'assets/selected/new.png') as result:
        expected=transform(image,relative,SIZE,result.size,resample=Image.Resampling.BILINEAR)
        np.testing.assert_array_equal(result,expected)
    assert json.loads((root/'native-reuse.json').read_text())['reused_views']==1
    narrow={**old,'view_affine':coefficients([.5,.5,.4,0])}
    assert relative_view(narrow,new) is None


def test_full_photo_initialization_keeps_its_centre_when_source_guard_is_added():
    p=parameters_from_matrix([1,0,0,0,1,0])
    np.testing.assert_allclose(p,[.5,.5,1,0],atol=1e-10)


def test_source_similarity_estimation_and_pose_propagation_follow_rotation_and_zoom():
    previous=np.full((48,64),.9,np.float32)
    previous[10:35,20:27]=.1;previous[30:36,20:45]=.1
    previous[10:17,39:47]=.3
    expected=cv2.getRotationMatrix2D((32,24),3,1.02)
    expected[:,2]+=[2,1]
    current=cv2.warpAffine(previous,expected,(64,48),flags=cv2.INTER_LINEAR,borderValue=.9)
    actual,confidence=estimate(previous,current)
    scale=np.diag([64,48,1.])
    points=np.array([[20,20,1],[40,20,1],[32,35,1]])
    measured=points@(scale@actual@np.linalg.inv(scale)).T
    predicted=points@np.vstack([expected,[0,0,1]]).T
    assert confidence>.7
    np.testing.assert_allclose(measured,predicted,atol=1.)
    initial=np.array([.5,.5,.5,0.])
    translated=np.eye(3);translated[0,2]=.05
    moved=propagation(initial,translated)
    assert moved[0]<initial[0]


def test_motion_compensated_appearance_works_across_different_photo_ids():
    rng=np.random.default_rng(17)
    first=rng.integers(0,256,(24,32),dtype=np.uint8)
    second=np.roll(first,4,axis=1)
    def row(name,pixels):return dict(view_id=name,tile_id=name,shape_score=.7,luminance=[0.,0.],
                                    view_affine=[1,0,0,0,1,0],appearance=pixels.ravel().tolist())
    initial,frozen,moving=row('initial',first),row('other-frozen',first),row('other-moving',second)
    transforms=np.tile(np.eye(3),(2,1,1));transforms[1,0,2]=4/32
    selected=choose([[initial],[frozen,moving]],np.stack([first,second]),
                    dict(continuity_weight=0,motion_weight=0,appearance_weight=1),transforms,np.ones(2))
    assert selected[1]['view_id']=='other-moving'


def test_good_photo_pose_is_propagated_to_neighbor_frames_before_refinement():
    initial=[.5,.5,.6,0]
    selected=[dict(slot=i,tile_id=str(i),dense_row=17+i,base_dense_row=17+i,visible=True,
                   shape_score=.8-.1*i,registration_parameters=initial,view_affine=coefficients(initial)) for i in range(3)]
    meta={17+i:dict(tile_id=str(i),dense_row=17+i,_rgb_filename='photo.npy',_rgb_offset=i) for i in range(3)}
    transforms=np.tile(np.eye(3),(3,1,1));transforms[1:,0,2]=.03
    pixels=np.random.default_rng(9).integers(0,256,(3,24,32),dtype=np.uint8)
    groups=tracking_groups(selected,meta,transforms,np.zeros(3,np.int32),pixels,dict(tracking_radius=2))
    moved=[p for _,jobs in groups for _,row,frames in jobs if row['dense_row']==17 for slot,p in frames if slot==1]
    assert len(moved)==1 and moved[0][0]<initial[0]
