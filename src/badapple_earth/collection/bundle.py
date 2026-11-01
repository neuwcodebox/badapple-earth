"""Reference immutable index snapshots without copying images or feature files."""

from collections import Counter
import json
import os
from pathlib import Path

import pyarrow.parquet as pq

from ..evidence import write_json
from .windows import identity


def read_manifest(root: Path):
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def members(root: Path, manifest: dict):
    if manifest.get("schema") != "index-bundle-v1":
        return [(root, manifest, set())]
    result = []
    for member in manifest["members"]:
        directory = (root / member["path"]).resolve()
        current = read_manifest(directory)
        if current["index_id"] != member["index_id"] or [s["shard_id"] for s in current["shards"]] != member["shard_ids"]:
            raise ValueError("A bundle member changed; recreate the bundle snapshot")
        result.append((directory, current, set(member["exclude_tile_ids"])))
    return result


def combine(indexes: list[Path], output: Path):
    if not indexes:
        raise ValueError("At least one index is required")
    if (output / "manifest.json").exists() and read_manifest(output).get("schema") != "index-bundle-v1":
        raise ValueError("Bundle output must not replace a source index")
    output.mkdir(parents=True, exist_ok=True)
    sources = []
    source_ids = {}
    for root in indexes:
        for directory, manifest, _ in members(root, read_manifest(root)):
            snapshot = [s["shard_id"] for s in manifest["shards"]]
            if manifest["index_id"] in source_ids and source_ids[manifest["index_id"]] != snapshot:
                raise ValueError("Choose one snapshot per index ID")
            if manifest["index_id"] not in source_ids:
                source_ids[manifest["index_id"]] = snapshot
                sources.append((directory, manifest))
    first = sources[0][1]
    seen, records = set(), []
    areas, terrain, scales = Counter(), Counter(), Counter()
    for directory, manifest in sources:
        for key in ["feature_version", "canonical_size", "coarse_shape"]:
            if manifest[key] != first[key]:
                raise ValueError(f"Index members disagree on {key}")
        excluded = []
        for shard in manifest["shards"]:
            if not shard["rows"]:
                continue
            table = pq.read_table(directory / "shards" / shard["shard_id"] / "tiles.parquet")
            for tile in table.to_pylist():
                if tile["tile_id"] in seen:
                    excluded.append(tile["tile_id"])
                    continue
                seen.add(tile["tile_id"])
                if "aoi_id" in tile:
                    areas[tile["aoi_id"]] += 1
                    terrain[tile["terrain_group"]] += 1
                    scales[str(tile["window"][2])] += 1
        records.append({"path": Path(os.path.relpath(directory.resolve(), output.resolve())).as_posix(),
                        "index_id": manifest["index_id"], "rows": manifest.get("rows", sum(s["rows"] for s in manifest["shards"])),
                        "shard_ids": [s["shard_id"] for s in manifest["shards"]], "exclude_tile_ids": excluded})
    snapshot_identity = [{key: value for key, value in row.items() if key != "path"} for row in records]
    result = {"schema": "index-bundle-v1", "index_id": identity(snapshot_identity), "members": records,
              "rows": len(seen), "feature_version": first["feature_version"],
              "canonical_size": first["canonical_size"], "coarse_shape": first["coarse_shape"],
              "areas": dict(areas), "terrain_groups": dict(terrain), "scales": dict(scales),
              "duplicate_tile_ids_excluded": sum(len(r["exclude_tile_ids"]) for r in records)}
    write_json(output / "manifest.json", result)
    return result
