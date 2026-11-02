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
