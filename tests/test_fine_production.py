import json

import pytest

from badapple_earth.production.assets import displayed_candidates
from badapple_earth.production.reuse import reuse


@pytest.mark.parametrize("winner", [2, 17])
def test_display_is_chosen_after_reranking_and_keeps_the_actual_winning_orientation(winner):
    pool = [{"tile_id":str(i//4), "view_id":str(i), "rank":i+1} for i in range(20)]
    shown = displayed_candidates(pool, pool[winner], 5)
    assert pool[winner] in shown
    assert len(shown) == len({r["tile_id"] for r in shown}) == 5
    assert next(r for r in shown if r["tile_id"] == pool[winner]["tile_id"])["view_id"] == str(winner)
    assert [r["rank"] for r in shown] == sorted(r["rank"] for r in shown)


def test_reuse_keeps_images_but_does_not_reuse_a_search_from_another_grid(tmp_path):
    source = tmp_path / "previous"
    source.mkdir()
    config = {"input":"input.mp4", "crop":[0,0,64,48], "frame_mode":"all", "index":"index", "top_k":20}
    (source / "manifest.json").write_text(json.dumps({"status":"complete", "config":config}))
    for name in ("targets/frame.png", "search/matches.parquet", "assets/thumbs/photo.png"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"test fixture")
    target = tmp_path / "fine"
    reuse(target, {**config, "reuse_run":str(source), "search_grid":"fine"})
    assert (target / "targets/frame.png").exists()
    assert (target / "assets/thumbs/photo.png").exists()
    assert not (target / "search").exists()
    coarse = tmp_path / "coarse"
    reuse(coarse, {**config, "reuse_run":str(source), "search_grid":"coarse"})
    assert (coarse / "search/matches.parquet").exists()
    wider = tmp_path / "wider"
    reuse(wider, {**config, "reuse_run":str(source), "search_grid":"coarse", "top_k":64})
    assert (wider / "assets/thumbs/photo.png").exists()
    assert not (wider / "search").exists()
