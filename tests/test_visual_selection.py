import json

import pytest

from badapple_earth.production.visual import assignments, read_choices


def test_review_assignment_covers_each_frame_once_in_random_order():
    slots = list(range(1139, 1439))
    groups = assignments(slots, seed=20261103, workers=3)
    flattened = [slot for group in groups for slot in group]
    assert sorted(flattened) == slots
    assert [len(group) for group in groups] == [100, 100, 100]
    assert flattened != slots
    assert groups == assignments(slots, seed=20261103, workers=3)


def test_choices_require_owned_complete_presented_candidates(tmp_path):
    (tmp_path / "choices").mkdir()
    cases = [{"case_id": "C001", "job_id": "review-01",
              "entries": [{"id": "P01", "view_id": "place-a"}, {"id": "P02", "view_id": "place-b"}]}]
    path = tmp_path / "choices/review-01.jsonl"
    def save(candidate_id, case_id="C001"):
        path.write_text(json.dumps({"case_id": case_id, "candidate_id": candidate_id}) + "\n", encoding="utf-8")

    save("P02")
    assert read_choices(tmp_path, cases, ["review-01"])["C001"]["view_id"] == "place-b"
    save("P99")
    with pytest.raises(ValueError, match="outside presented"):
        read_choices(tmp_path, cases, ["review-01"])
    save("P01", case_id="other-job")
    with pytest.raises(ValueError, match="Unexpected"):
        read_choices(tmp_path, cases, ["review-01"])
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Awaiting 1"):
        read_choices(tmp_path, cases, ["review-01"])


def test_sequential_review_assigns_contiguous_segments():
    assert assignments([12, 10, 11], seed=7, workers=1, mode="sequential") == [[10, 11, 12]]
    assert assignments([15, 12, 10, 11, 14, 13], seed=7, workers=3, mode="sequential") == [[10, 11], [12, 13], [14, 15]]


def test_production_import_preserves_astra_choice_instead_of_reference_maximum(tmp_path):
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    from badapple_earth.production.assets import astra_choices

    root, visual, source = [tmp_path / name for name in ("production", "visual", "source")]
    for directory in (root / "targets", root / "search", source / "search", visual):
        directory.mkdir(parents=True)
    def manifest(directory, value):
        (directory / "manifest.json").write_text(json.dumps(value), encoding="utf-8")
    manifest(root / "targets", {"target_set_id": "targets"})
    search = {"index_id": "index", "run_id": "retrieval"}
    manifest(root / "search", search)
    manifest(source / "search", search)
    manifest(visual, {"status": "selected", "experiment_id": "choices", "target_set_id": "targets",
                      "index_id": "index", "source_run": str(source)})
    pq.write_table(pa.Table.from_pylist([{"slot": 0, "view_id": "B"}]), visual / "selected.parquet")
    pq.write_table(pa.Table.from_pylist([{"slot": 0, "dense_row": 1, "ms_ssim": .9},
                                       {"slot": 0, "dense_row": 2, "ms_ssim": .4}]), source / "evaluations.parquet")
    pool = {0: [{"view_id": "A", "dense_row": 1}, {"view_id": "B", "dense_row": 2}]}
    config = {"visual_run": str(visual), "visual_experiment_id": "choices"}
    result, audit = astra_choices(root, config, [{"slot": 0}], np.array([0]), pool)
    assert result == [{"slot": 0, "selected_index": 1, "scores": [.9, .4]}]
    assert audit["recomputed_pairs"] == 0
    pq.write_table(pa.Table.from_pylist([{"slot": 0, "view_id": "outside"}]), visual / "selected.parquet")
    with pytest.raises(ValueError, match="outside"):
        astra_choices(root, config, [{"slot": 0}], np.array([0]), pool)
