"""Compare scale runs with the same targets, metric and rotation policy."""

import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from ..evidence import write_json


def compare(baseline: Path, run: Path):
    before = json.loads((baseline / "manifest.json").read_text(encoding="utf-8"))
    after = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    for field in ["target_set_id", "metric", "rotations", "view_version"]:
        if before.get(field) != after.get(field):
            raise ValueError(f"Cannot compare runs with different {field}")
    def best(root):
        rows = pq.read_table(root / "matches.parquet").to_pylist()
        return {row["target_id"]: row for row in rows if row["rank"] == 1}
    left, right = best(baseline), best(run)
    if left.keys() != right.keys():
        raise ValueError("Run target sets differ")
    rows = [{"target_id": target, "slot": right[target]["slot"],
             "baseline_mse": left[target]["raw_mse"], "expanded_mse": right[target]["raw_mse"],
             "improvement": left[target]["raw_mse"] - right[target]["raw_mse"]} for target in left]
    mean_before = float(np.mean([row["baseline_mse"] for row in rows]))
    mean_after = float(np.mean([row["expanded_mse"] for row in rows]))
    result = {"baseline_run_id": before["run_id"], "run_id": after["run_id"],
              "target_set_id": after["target_set_id"], "metric": after["metric"], "rotations": after["rotations"],
              "baseline_places": before["candidate_count"], "expanded_places": after["candidate_count"],
              "baseline_mean_mse": mean_before, "expanded_mean_mse": mean_after,
              "mean_reduction_percent": 100 * (1 - mean_after / mean_before) if mean_before else 0,
              "improved_targets": sum(row["improvement"] > 0 for row in rows),
              "tied_targets": sum(row["improvement"] == 0 for row in rows),
              "regressed_targets": sum(row["improvement"] < 0 for row in rows),
              "per_target": sorted(rows, key=lambda row: row["slot"]),
              "interpretation": "dataset coverage and scale expansion; video recognition not assessed"}
    write_json(run / "baseline-comparison.json", result)
    return result
