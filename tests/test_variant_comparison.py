import numpy as np
import pyarrow.parquet as pq

from badapple_earth.production.variants.pipeline import merge, write_table


def test_view_expansion_preserves_parents_and_merges_exact_topk_with_id_ties():
    baseline = [{"tile_id":"same-place","view_id":"old", "dense_row":3,"sse":20},
                {"tile_id":"other-place","view_id":"other", "dense_row":4,"sse":30}]
    extra = [{"tile_id":"same-place","view_id":"angle", "dense_row":100,"sse":20},
             {"tile_id":"other-place","view_id":"crop", "dense_row":101,"sse":5}]
    rows = merge(baseline,extra,3)
    assert [r["view_id"] for r in rows] == ["crop","angle","old"]
    assert [r["dense_row"] for r in rows] == [101,100,3]
    assert [r["rank"] for r in rows] == [1,2,3]
    assert len({r["tile_id"] for r in rows}) == 2


def test_comparison_tables_keep_affine_provenance_after_baseline_rows(tmp_path):
    path = tmp_path/"selected.parquet"
    write_table(path,[{"view_id":"old","rotation_degrees":90},
                     {"view_id":"new","rotation_degrees":30,"crop_scale":.75,
                      "transform_version":"affine","view_affine":[1.,0,2,0,1,3],
                      "view_source_quad":[[1.,2],[3,2],[3,4],[1,4]]}])
    rows = pq.read_table(path).to_pylist()
    assert rows[0]["transform_version"] is None
    assert rows[1]["crop_scale"] == .75
    np.testing.assert_array_equal(rows[1]["view_source_quad"],[[1,2],[3,2],[3,4],[1,4]])
