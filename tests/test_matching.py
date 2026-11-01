from fractions import Fraction
import json

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from badapple_earth.matching.search import match
from badapple_earth.matching.targets import prefer_previous


def test_nearest_pts_uses_previous_frame_on_exact_tie():
    assert prefer_previous(Fraction(0), Fraction(1, 10), Fraction(1, 20))
    assert not prefer_previous(Fraction(0), Fraction(1, 10), Fraction(3, 40))
    assert not prefer_previous(None, Fraction(1, 10), Fraction(1, 20))


def test_mse_ranking_handles_uint8_extremes_and_breaks_ties_by_id(tmp_path):
    targets, index, output = tmp_path / "targets", tmp_path / "index", tmp_path / "run"
    targets.mkdir()
    shard = index / "shards" / "one"
    shard.mkdir(parents=True)
    target_manifest = {"feature_version": "test", "canonical_size": [256, 191], "target_set_id": "targets"}
    (targets / "manifest.json").write_text(json.dumps(target_manifest))
    np.save(targets / "gray_coarse.npy", np.full((1, 12, 16), 255, np.uint8))
    pq.write_table(pa.Table.from_pylist([{"target_id": "target", "slot": 0}]), targets / "targets.parquet")
    ids = ["zero", "white-b", "white-a", "middle"]
    np.save(shard / "gray_coarse.npy", np.stack([np.full((12, 16), v, np.uint8) for v in [0, 255, 255, 128]]))
    pq.write_table(pa.Table.from_pylist([{"tile_id": value, "feature_row": i} for i, value in enumerate(ids)]), shard / "tiles.parquet")
    (index / "manifest.json").write_text(json.dumps({
        "feature_version": "test", "canonical_size": [256, 191], "coarse_shape": [12, 16],
        "index_id": "index", "shards": [{"shard_id": "one", "rows": 4}]}))
    result = match(targets, index, output, top_k=10)
    rows = pq.read_table(output / "matches.parquet").to_pylist()
    assert [row["tile_id"] for row in rows] == ["white-a", "white-b", "middle", "zero"]
    assert rows[0]["raw_mse"] == 0 and rows[-1]["raw_mse"] == 1
    assert result["candidate_count"] == 4
