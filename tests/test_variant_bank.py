import json

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from badapple_earth.production.variants.bank import Bank, build_group
from badapple_earth.production.variants.search import empty_state, scan_group, search
from badapple_earth.matching.affine import image_view


def fixture_bank(tmp_path):
    index, packed, root = [tmp_path/name for name in ("index","packed","bank")]
    for directory in (index/"catalog",packed):
        directory.mkdir(parents=True)
    size = (16,12)
    np.save(index/"rgb.npy",np.random.default_rng(4).integers(0,256,(17,12,16,3),dtype=np.uint8))
    rows = [{"tile_id":f"parent{i:02}","scene_id":"scene","rgb_cache_ref":"rgb.npy","rgb_cache_row":i,
             "window":[i,0,16,12],"footprint_json":"original"} for i in range(17)]
    pq.write_table(pa.Table.from_pylist(rows),packed/"tiles.parquet")
    (index/"catalog/scene.json").write_text(json.dumps({"affine":[10,0,500000,0,-10,1000000],"crs":"EPSG:32633"}))
    (index/"manifest.json").write_text(json.dumps({"index_id":"parent-index","canonical_size":size,"rows":17,
                                                  "config":{"cache_rgb":True}}))
    np.save(packed/"ids.npy",np.array([r["tile_id"] for r in rows],dtype="S64"))
    np.save(packed/"source_rows.npy",np.arange(17,dtype=np.int32))
    blocks = [{"is_view":False,"member":"parent-index","start":i,"stop":i+1,"path":"tiles.parquet"} for i in range(17)]
    (packed/"manifest.json").write_text(json.dumps({"status":"complete","cache_id":"packed","blocks":blocks}))
    config = {"index":str(index),"parent_packs":[str(packed)],"bank":str(root),"angle_step":30,"crop_scale":.75,
              "min_free_disk_gib":0,"extra_rgb_cache":str(tmp_path/"no-extra"),"top_k":2,
              "candidate_batch":13,"target_batch":1}
    return Bank(root,config),config


def test_bank_resume_uses_only_published_nonsequential_groups_and_resolves_actual_pixels(tmp_path):
    bank,config = fixture_bank(tmp_path)
    start,stop,values = build_group(bank.jobs[1],bank.specification,bank.size)
    bank.save_group(1,start,stop,values)
    bank = Bank(bank.root,config)
    assert bank.state["completed"] == [1]
    assert bank.state["completed_views"] == 20
    assert "descriptors" not in bank.state and (bank.root/"inventory.json").exists()
    targets = values[:1]
    with pytest.raises(RuntimeError,match="paused"):
        manifest = bank.state.copy()
        manifest["status"] = "paused"
        (bank.root/"manifest.json").write_text(json.dumps(manifest))
        search(bank,targets,[0],tmp_path/"search",config)
    # The first search read only the valid, last group, not an unwritten prefix.
    with np.load(tmp_path/"search/retrieval.npz") as saved:
        assert saved["completed"].tolist() == [1]
        assert np.all(saved["positions"] >= 16*20)
    start,stop,values = build_group(bank.jobs[0],bank.specification,bank.size)
    bank.save_group(0,start,stop,values)
    result = search(bank,targets,[0],tmp_path/"search",config)
    wanted = result["positions"].reshape(-1)
    metadata = bank.resolve(wanted,100)
    rgb = np.load(tmp_path/"index/rgb.npy")
    for position in wanted:
        row = metadata[int(position)+100]
        expected = image_view(Image.fromarray(rgb[position//20]),row).convert("L").resize((32,24),Image.Resampling.BOX)
        np.testing.assert_array_equal(expected,bank.values[position])
        assert row["dense_row"] == position+100


def test_dual_pool_search_is_integer_exact_with_ties_and_out_of_order_batches():
    rng = np.random.default_rng(15)
    targets = rng.integers(0,256,(3,24,32),dtype=np.uint8)
    values = rng.integers(0,256,(40,24,32),dtype=np.uint8)
    values[1] = values[25] = targets[0]
    ties = rng.permutation(40).astype(np.int32)
    spec = [{"scale":.75},{"scale":1}]
    state = empty_state(3,4)
    for start,stop in ((20,40),(0,20)):
        scan_group(targets,values,ties,spec,start,stop,state,7,2)
    sse = np.sum((targets[:,None].astype(np.int64)-values[None].astype(np.int64))**2,axis=(2,3))
    for route in range(2):
        allowed = np.arange(1 if route == 0 else 0,40,2 if route == 0 else 1)
        expected = np.array([sorted(allowed,key=lambda p:(row[p],ties[p]))[:4] for row in sse])
        np.testing.assert_array_equal(state["positions"][route],expected)
        np.testing.assert_array_equal(state["scores"][route],np.take_along_axis(sse,expected,axis=1))


def test_scores_from_different_view_banks_cannot_be_reused_by_geographic_index_alone(tmp_path):
    from badapple_earth.production.grid.rerank import evaluate
    from badapple_earth.production.rerank import VERSION
    run,cache,index = [tmp_path/name for name in ("run","cache","index")]
    for folder in (run/"targets",cache,index):
        folder.mkdir(parents=True)
    (run/"targets/manifest.json").write_text(json.dumps({"target_set_id":"targets"}))
    (index/"manifest.json").write_text(json.dumps({"index_id":"same-places"}))
    (cache/"manifest.json").write_text(json.dumps({"target_set_id":"targets","index_id":"same-places",
                    "ms_ssim_version":VERSION,"view_space_id":"other-views"}))
    with pytest.raises(ValueError,match="same explicit metric"):
        evaluate(tmp_path,run,{"config":{"index":str(index)}},[],{},[],
                 {"reuse_scores":[str(cache)],"workers":1},search_manifest={"view_space_id":"new-views"})
