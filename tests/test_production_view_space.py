import json

import pyarrow as pa
import pyarrow.parquet as pq


def test_completed_production_preserves_search_view_space_for_score_reuse(tmp_path,monkeypatch):
    from badapple_earth.production import pipeline
    from badapple_earth.production.grid import search
    root = tmp_path/"run"
    config = {"width":1920,"height":1080,"display_top_k":5,"frame_mode":"all","top_k":20,
              "crop":[0,0,1444,1080],"input":"unused.mp4","search_grid":"fine","selection_method":"ms-ssim"}
    monkeypatch.setattr(pipeline,"reuse",lambda *a:None)
    monkeypatch.setattr(pipeline,"targets",lambda *a:{"count":1,"target_set_id":"targets","input_sha256":"input"})
    monkeypatch.setattr(search,"production_search",lambda *a:{"index_id":"index","run_id":"search","view_space_id":"affine-views"})
    def assets(*a):
        pq.write_table(pa.table({"slot":[0]}),root/"frame-plan.parquet")
        return {"selection_version":"ssim","acquisition_years":[]}
    monkeypatch.setattr(pipeline,"prepare",assets)
    monkeypatch.setattr(pipeline,"render",lambda *a:{"frames":1})
    result = pipeline.produce(config,root)
    assert result["view_space_id"] == "affine-views"
    assert json.loads((root/"manifest.json").read_text())["view_space_id"] == "affine-views"
