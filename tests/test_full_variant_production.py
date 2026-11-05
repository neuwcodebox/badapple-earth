import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from badapple_earth.collection.bundle import combine, members, read_manifest
from badapple_earth.production.variants.expansion import difference
from badapple_earth.production.variants.full import scan_complete, member_mapping


def test_full_production_scan_matches_exhaustive_integer_order_and_resumes_with_cached_queries(tmp_path):
    rng = np.random.default_rng(121)
    targets = rng.integers(0,256,(5,24,32),dtype=np.uint8)
    values = rng.integers(0,256,(40,24,32),dtype=np.uint8)
    values[17] = values[2]
    ties = rng.permutation(40).astype(np.int32)
    expected = np.sum((targets[:,None].astype(np.int64)-values[None].astype(np.int64))**2,axis=(2,3))
    order = np.array([np.lexsort((ties,row))[:3] for row in expected])
    config = {"top_k":3,"candidate_batch":7,"target_batch":2}
    seed = {1:(order[1],expected[1,order[1]])}
    scores,positions = scan_complete(targets,values,ties,tmp_path/"cached","fixed-inputs",config,seed)
    np.testing.assert_array_equal(positions,order)
    np.testing.assert_array_equal(scores,np.take_along_axis(expected,order,axis=1))
    scan_complete(targets,values[:14],ties[:14],tmp_path/"resume","fixed-inputs",config)
    scores,positions = scan_complete(targets,values,ties,tmp_path/"resume","fixed-inputs",config)
    np.testing.assert_array_equal(positions,order)
    scores,positions = scan_complete(targets,values,ties,tmp_path/"resume","fixed-inputs",config)
    np.testing.assert_array_equal(scores,np.take_along_axis(expected,order,axis=1))


def test_production_scan_retains_exact_view_rank_for_large_boundary_ties(tmp_path):
    targets = np.stack([np.zeros((24,32),np.uint8),np.full((24,32),255,np.uint8)])
    values = np.stack([targets[i%2] for i in range(60)])
    ties = np.random.default_rng(22).permutation(60).astype(np.int32)
    expected = np.sum((targets[:,None].astype(np.int64)-values[None].astype(np.int64))**2,axis=(2,3))
    for k in (1,20):
        scores,positions = scan_complete(targets,values,ties,tmp_path/str(k),str(k),
                                         {"top_k":k,"candidate_batch":7,"target_batch":1})
        order = np.array([np.lexsort((ties,row))[:k] for row in expected])
        np.testing.assert_array_equal(positions,order)
        np.testing.assert_array_equal(scores,np.take_along_axis(expected,order,axis=1))


def test_shard_expansion_retains_old_photos_without_rescanning_them(tmp_path,monkeypatch):
    source = tmp_path/"source"
    manifest = {"index_id":"source","shards":[],"feature_version":"test","canonical_size":[32,24],
                "coarse_shape":[12,16],"rows":0,"config":{"cache_rgb":True}}
    for i in (1,2):
        folder = source/"shards"/str(i)
        folder.mkdir(parents=True)
        pq.write_table(pa.Table.from_pylist([{"tile_id":str(i),"aoi_id":"area","terrain_group":"terrain",
                                              "window":[0,0,32,24]}]),folder/"tiles.parquet")
        manifest["shards"].append({"shard_id":str(i),"rows":1})
        manifest["rows"] = i
        (source/"manifest.json").write_text(json.dumps(manifest))
        combine([source],tmp_path/("old" if i == 1 else "full"),freeze=True)
    monkeypatch.setattr(pq,"read_table",lambda *a,**k: (_ for _ in ()).throw(AssertionError("Do not rescan old photos")))
    result = difference(tmp_path/"old",tmp_path/"full",tmp_path/"delta")
    assert result["rows"] == 1
    assert members(tmp_path/"delta",result)[0][1]["shards"] == [{"shard_id":"2","rows":1}]
    assert member_mapping(tmp_path/"old",tmp_path/"full") == {
        read_manifest(tmp_path/"old")["members"][0]["index_id"]:read_manifest(tmp_path/"full")["members"][0]["index_id"]}


def test_multiple_preserved_view_spaces_reuse_only_their_verified_dense_ranges(tmp_path):
    from badapple_earth.production.grid.rerank import evaluate
    from badapple_earth.production.rerank import VERSION
    run,index = tmp_path/"run",tmp_path/"index"
    (run/"targets").mkdir(parents=True)
    index.mkdir()
    (run/"targets/manifest.json").write_text(json.dumps({"target_set_id":"targets"}))
    (index/"manifest.json").write_text(json.dumps({"index_id":"full"}))
    caches,preserved = [],[]
    for name,space,position,limit in (("baseline",None,3,5),("affine","bank",8,10)):
        cache = tmp_path/name
        cache.mkdir()
        (cache/"manifest.json").write_text(json.dumps({"index_id":"old","view_space_id":space,
                              "ms_ssim_version":VERSION,"target_set_id":"targets"}))
        pq.write_table(pa.Table.from_pylist([{"slot":0,"dense_row":position,"ms_ssim":.75}]),cache/"evaluations.parquet")
        caches.append(str(cache))
        preserved.append({"index_id":"old","view_space_id":space,"view_count":limit})
    rows,audit = evaluate(run,run,{"config":{"index":str(index)}},[{"slot":0,"frame":"not-read.png"}],
                         {3:{},8:{}},[{"slot":0,"dense_row":p} for p in (3,8)],
                         {"workers":1,"reuse_scores":caches},search_manifest={"preserved_view_spaces":preserved})
    assert len(rows) == audit["reused_pairs"] == 2 and audit["new_pairs"] == 0


def test_full_search_merges_all_24_views_per_photo_and_remaps_frozen_source_members(tmp_path,monkeypatch):
    from badapple_earth.production.variants import full
    rng = np.random.default_rng(74)
    baseline,target_root = tmp_path/"baseline",tmp_path/"baseline/targets"
    for path in (baseline/"search",target_root,tmp_path/"old-source",tmp_path/"new-source",tmp_path/"index",
                 tmp_path/"old-index",tmp_path/"new-index",tmp_path/"packed",tmp_path/"fine"):
        path.mkdir(parents=True,exist_ok=True)
    def dump(path,value):
        path.write_text(json.dumps(value))
    def record(name,index_id):
        source = name+"-source"
        dump(tmp_path/source/"manifest.json",{"index_id":source,"shards":[{"shard_id":"one","rows":1}]})
        return {"path":"../"+source,"index_id":index_id,"exclude_tile_ids":[],
                "snapshot_manifest":{"index_id":index_id,"snapshot_source_index_id":source,
                                     "shards":[{"shard_id":"one","rows":1}]}}
    old = record("old","old-member")
    new = record("new","new-member")
    dump(tmp_path/"old-index/manifest.json",{"schema":"index-bundle-v1","index_id":"base-index","members":[old]})
    dump(tmp_path/"new-index/manifest.json",{"schema":"index-bundle-v1","index_id":"new-index","members":[new]})
    current = []
    for original in (old,new):
        updated = {**original,"index_id":"full-"+original["index_id"],
                   "snapshot_manifest":{**original["snapshot_manifest"],"index_id":"full-"+original["index_id"]}}
        current.append(updated)
    dump(tmp_path/"index/manifest.json",{"schema":"index-bundle-v1","index_id":"full-index","rows":2,"members":current})
    arrays = [rng.integers(0,256,(count,24,32),dtype=np.uint8) for count in (4,20,4,20)]
    queries = rng.integers(0,256,(3,24,32),dtype=np.uint8)
    unique,inverse = np.unique(queries.reshape(3,-1),axis=0,return_inverse=True)
    np.save(target_root/"gray_fine.npy",queries)
    dump(target_root/"manifest.json",{"target_set_id":"targets","canonical_size":[32,24]})
    dump(baseline/"search/manifest.json",{"run_id":"base","status":"complete","index_id":"base-index",
           "target_set_id":"targets","top_k":2,"grid":[32,24],"view_count":4,"candidate_count":1,
           "baseline_run_id":"older-base","incremental_index_id":"older-delta","new_places_searched":123})
    np.save(baseline/"search/query_inverse.npy",inverse)
    def metadata(stage,position,offset=0):
        return {"view_id":f"{stage}-{position:02}","tile_id":"old" if stage in ("a","b") else "new",
                "dense_row":int(position)+offset,"index_member":"old-member" if stage in ("a","b") else "new-member",
                "rotation_degrees":30 if stage in ("b","d") else 0,"rotation_crop":[0,0,32,24]}
    distance = np.sum((unique[:,None].astype(np.int64)-arrays[0].reshape(1,4,-1).astype(np.int64))**2,axis=2)
    base_rows = [{**metadata("a",int(p)),"query":q,"rank":rank,"sse":int(distance[q,p]),
                  "raw_mse":float(distance[q,p])/(768*255**2)}
                 for q in range(3) for rank,p in enumerate(np.argsort(distance[q])[:2],1)]
    pq.write_table(pa.Table.from_pylist(base_rows),baseline/"search/matches.parquet")
    configs = []
    for name in ("old","new"):
        path = tmp_path/(name+"-config.json")
        dump(path,{"bank":name,"index":str(tmp_path/(name+"-index"))})
        configs.append(str(path))
    class SmallBank:
        def __init__(self,name,config):
            self.stage,self.values = ("b",arrays[1]) if name == "old" else ("d",arrays[3])
            self.ties = np.arange(20,dtype=np.int32)
            self.state = {"bank_id":name,"parent_index_id":"base-index" if name == "old" else "new-index",
                          "status":"complete","parents":1,"rows":20}
        def resolve(self,positions,offset):
            return {int(p)+offset:metadata(self.stage,int(p),offset) for p in positions}
    monkeypatch.setattr(full,"Bank",SmallBank)
    monkeypatch.setattr(full,"cached_queries",lambda *a:{})
    monkeypatch.setattr(full,"resolve",lambda cache,packed,choices:{int(p):metadata("c",int(p)) for p in np.unique(choices)})
    dump(tmp_path/"packed/manifest.json",{"places":1,"cache_id":"packed"})
    np.save(tmp_path/"packed/tie_rank.npy",np.arange(4,dtype=np.int32))
    np.save(tmp_path/"fine/gray_fine.npy",arrays[2])
    config = {"index":str(tmp_path/"index"),"incremental_base_run":str(baseline),"affine_configs":configs,
              "affine_comparison":"unused","top_k":2,"candidate_batch":7,"target_batch":2,
              "packed_index":str(tmp_path/"packed"),"fine_cache":str(tmp_path/"fine")}
    result = full.production_search(target_root,tmp_path/"output",config)
    assert result["candidate_count"] == 2 and result["view_count"] == 48
    assert result["baseline_run_id"] == "base"
    assert result["existing_places_reused"] == result["new_places_searched"] == 1
    assert "incremental_index_id" not in result and len(result["view_versions"]) == 2
    rows = pq.read_table(tmp_path/"output/matches.parquet").to_pylist()
    values = np.concatenate(arrays).reshape(48,-1)
    all_distance = np.sum((unique[:,None].astype(np.int64)-values[None].astype(np.int64))**2,axis=2)
    names = [f"{stage}-{i:02}" for stage,array in zip("abcd",arrays) for i in range(len(array))]
    for q in range(3):
        expected = sorted(range(48),key=lambda i:(all_distance[q,i],names[i]))[:2]
        selected = sorted((r for r in rows if r["query"] == q),key=lambda r:r["rank"])
        assert [r["dense_row"] for r in selected] == expected
        assert [r["view_id"] for r in selected] == [names[i] for i in expected]
    assert {r["index_member"] for r in rows} <= {"full-old-member","full-new-member"}
