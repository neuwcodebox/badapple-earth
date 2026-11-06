import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from badapple_earth.collection.bundle import combine, concatenate, extend_disjoint, members, read_manifest
from badapple_earth.production.grid.incremental import merge_topk


def publish(root, number):
    folder = root / "shards" / str(number)
    folder.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist([{"tile_id": str(number)}]), folder / "tiles.parquet")
    return {"shard_id": str(number), "rows": 1, "files": {"tiles.parquet": str(number)}}


def test_snapshot_keeps_published_rows_while_collection_appends_and_survives_bundle_flattening(tmp_path):
    source = tmp_path / "live"
    first = publish(source, 1)
    manifest = {"index_id": "live-source", "rows": 1, "shards": [first],
                "feature_version": "test", "canonical_size": [32, 24], "coarse_shape": [12, 16]}
    (source / "manifest.json").write_text(json.dumps(manifest))
    frozen = tmp_path / "frozen"
    saved = combine([source], frozen, freeze=True)
    manifest["shards"].append(publish(source, 2))
    manifest["rows"] = 2
    (source / "manifest.json").write_text(json.dumps(manifest))
    selected = members(frozen, read_manifest(frozen))[0][1]
    assert selected["rows"] == 1 and selected["shards"] == [first]
    assert selected["index_id"] != "live-source"
    assert combine([source], frozen, freeze=True)["index_id"] == saved["index_id"]
    assert read_manifest(frozen)["rows"] == 1
    flattened = combine([frozen], tmp_path / "flattened")
    assert flattened["rows"] == saved["rows"] == 1
    assert members(tmp_path / "flattened", flattened)[0][1]["shards"] == [first]
    manifest["shards"][0]["files"]["tiles.parquet"] = "changed"
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="snapshot shard"):
        members(frozen, read_manifest(frozen))


def test_incremental_union_topk_matches_exhaustive_search_and_preserves_dense_offsets():
    rng = np.random.default_rng(88)
    for k in (1, 4, 10):
        values = rng.integers(0, 4, (3, 60))
        names = [f"v{i:03d}" for i in range(60)]
        pools = []
        for start, stop in ((0, 30), (30, 60)):
            rows = []
            for query, scores in enumerate(values):
                order = sorted(range(start, stop), key=lambda i: (scores[i], names[i]))[:k]
                rows.extend({"query": query, "view_id": names[i], "sse": int(scores[i]),
                             "dense_row": i-start, "optional_provenance": "new" if start else None} for i in order)
            pools.append(rows)
        merged = merge_topk(*pools, k, 30)
        for query, scores in enumerate(values):
            expected = sorted(range(60), key=lambda i: (scores[i], names[i]))[:k]
            result = [r for r in merged if r["query"] == query]
            assert [r["view_id"] for r in result] == [names[i] for i in expected]
            assert [r["dense_row"] for r in result] == expected
            assert [r["rank"] for r in result] == list(range(1, k+1))


def test_incremental_reuses_targets_and_photos_but_never_the_baseline_search(tmp_path):
    from badapple_earth.production.reuse import reuse
    source = tmp_path / "baseline"
    source.mkdir()
    config = {"input":"source.mp4", "crop":[0,0,64,48], "frame_mode":"all", "index":"old", "top_k":20}
    (source / "manifest.json").write_text(json.dumps({"status":"complete", "config":config}))
    for name in ("targets/frame.png", "assets/selected/photo.png", "search/manifest.json"):
        file = source / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"immutable fixture")
    destination = tmp_path / "expanded"
    reuse(destination, {**config, "index":"expanded", "incremental_base_run":str(source)})
    assert (destination / "targets/frame.png").exists()
    assert not (destination / "assets").exists()
    assert not (destination / "search").exists()


def test_aerial_frame_caption_and_credit_preserve_actual_source_and_date_interval():
    from badapple_earth.production.layout import location_caption
    from badapple_earth.production.assets import frame_credit
    row = {"latitude":-42.0,"longitude":172.0,"acquired_at":"2023-01-01T00:00:00Z/2023-01-03T00:00:00Z",
           "source_id":"linz-imagery", "source_licensors":["Regional Council"]}
    assert "2023-01-01 – 2023-01-03" in location_caption(row)
    assert "Regional Council" in frame_credit(row) and "CC BY 4.0" in frame_credit(row)
    assert "Sentinel" not in frame_credit(row)


def test_disjoint_extension_matches_full_deduplication_without_reading_baseline_tiles(tmp_path, monkeypatch):
    roots = []
    for name, source, number in (("sentinel", None, 1), ("aerial", "planetarycomputer-naip", 2)):
        root = tmp_path / name
        shard = publish(root, number)
        config = {"source":source} if source else {"collection":"sentinel-2-c1-l2a"}
        manifest = {"index_id":name,"config":config,"rows":1,"shards":[shard],
                    "feature_version":"test","canonical_size":[32,24],"coarse_shape":[12,16]}
        (root / "manifest.json").write_text(json.dumps(manifest))
        bundle = tmp_path / (name+"-bundle")
        combine([root],bundle,freeze=bool(source))
        roots.append(bundle)
    expected = combine(roots,tmp_path/"expected")
    monkeypatch.setattr(pq, "read_table", lambda *args, **kwargs: pytest.fail("Disjoint extension must not scan old tile tables"))
    actual = extend_disjoint(roots,tmp_path/"fast")
    assert actual["index_id"] == expected["index_id"] and actual["rows"] == 2
    with pytest.raises(ValueError,match="Overlapping source"):
        extend_disjoint([roots[0],roots[0]],tmp_path/"invalid")


def test_reference_concatenation_keeps_duplicate_tiles_without_opening_any_tile_tables(tmp_path, monkeypatch):
    roots = []
    for name in ("old", "new"):
        root = tmp_path / name
        shard = publish(root, 1)  # The tile ID deliberately occurs in both indexes.
        manifest = {"index_id": name, "rows": 1, "shards": [shard], "feature_version": "test",
                    "canonical_size": [32, 24], "coarse_shape": [12, 16],
                    "areas": {"same-area": 1}, "terrain_groups": {"urban": 1}, "scales": {"256": 1}}
        (root / "manifest.json").write_text(json.dumps(manifest))
        roots.append(root)
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: pytest.fail("Must not scan tile metadata"))
    monkeypatch.setattr(pq, "ParquetFile", lambda *a, **k: pytest.fail("Must not open tile metadata"))
    result = concatenate(roots, tmp_path / "joined")
    assert result["rows"] == 2 and len(result["members"]) == 2
    assert result["areas"] == {"same-area": 2}
    assert result["tile_deduplication"] == "not_performed"
    assert not any(record["exclude_tile_ids"] for record in result["members"])
    assert len(members(tmp_path / "joined", result)) == 2


def test_selected_native_cache_is_reused_without_opening_a_source(tmp_path):
    from badapple_earth.production.assets import native_scene
    saved = tmp_path / "previous/assets/selected"
    saved.mkdir(parents=True)
    (saved / "view.png").write_bytes(b"cached image fixture")
    destination = tmp_path / "selected"
    destination.mkdir()
    assert native_scene(tmp_path / "no-source", [{"view_id":"view"}], destination, (32,24), tmp_path/"previous") == []
    assert (destination / "view.png").read_bytes() == b"cached image fixture"


def test_incremental_ssim_reuses_only_verified_baseline_rows_without_reading_rgb(tmp_path):
    from badapple_earth.production.grid.rerank import evaluate
    from badapple_earth.production.rerank import VERSION
    run, old, index = [tmp_path/name for name in ("run", "old", "index")]
    for folder in (run/"targets", run/"search", old, index):
        folder.mkdir(parents=True,exist_ok=True)
    (run/"targets/manifest.json").write_text(json.dumps({"target_set_id":"targets"}))
    (run/"search/manifest.json").write_text(json.dumps({"baseline_index_id":"old-index", "baseline_dense_rows_preserved":True}))
    (index/"manifest.json").write_text(json.dumps({"index_id":"expanded"}))
    (old/"manifest.json").write_text(json.dumps({"index_id":"old-index","target_set_id":"targets","ms_ssim_version":VERSION}))
    pq.write_table(pa.Table.from_pylist([{"slot":0,"dense_row":3,"ms_ssim":.75}]),old/"evaluations.parquet")
    arguments = (run,run,{"config":{"index":str(index)}},[{"slot":0,"frame":"not-read.png"}],
                 {3:{"dense_row":3}},[{"slot":0,"dense_row":3}],{"reuse_scores":[str(old)],"workers":1})
    rows,audit = evaluate(*arguments)
    assert rows == [{"slot":0,"dense_row":3,"ms_ssim":.75}]
    assert audit["reused_pairs"] == 1 and audit["new_pairs"] == 0
    (run/"search/manifest.json").write_text(json.dumps({"baseline_index_id":"old-index", "baseline_dense_rows_preserved":False}))
    with pytest.raises(ValueError,match="same explicit metric"):
        evaluate(*arguments)
