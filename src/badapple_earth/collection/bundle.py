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


def extend_disjoint(indexes: list[Path], output: Path):
    """Join already deduplicated bundles from different source namespaces.

    Tile IDs include the source ID, so Sentinel and aerial namespaces are
    disjoint without opening the baseline's million metadata records again.
    Same-provider expansion must use combine() and its explicit deduplication.
    """
    records, namespaces = [], set()
    totals = {name: Counter() for name in ("areas", "terrain_groups", "scales")}
    manifests = [read_manifest(root) for root in indexes]
    rows = 0
    for root, manifest in zip(indexes, manifests):
        if manifest.get("schema") != "index-bundle-v1":
            raise ValueError("Fast extension requires already deduplicated bundles")
        current_sources = set()
        selected = members(root, manifest)
        for _, member, _ in selected:
            config = member.get("config", {})
            source = config.get("source")
            if source is None and config.get("collection") == "sentinel-2-c1-l2a":
                source = "earthsearch-sentinel2-c1"
            if source not in {"earthsearch-sentinel2-c1", "planetarycomputer-naip", "linz-imagery"}:
                raise ValueError("Cannot prove disjoint source namespaces")
            current_sources.add(source)
        if namespaces & current_sources:
            raise ValueError("Overlapping source namespaces need explicit tile deduplication")
        namespaces.update(current_sources)
        for key in ("feature_version", "canonical_size", "coarse_shape"):
            if manifest[key] != manifests[0][key]:
                raise ValueError(f"Index members disagree on {key}")
        for record in manifest["members"]:
            directory = (root / record["path"]).resolve()
            records.append({**record, "path": Path(os.path.relpath(directory, output.resolve())).as_posix()})
        rows += manifest["rows"]
        for key, counter in totals.items():
            counter.update(manifest[key])
    result = {"schema": "index-bundle-v1", "index_id": identity([
                  {key:value for key,value in record.items() if key != "path"} for record in records]),
              "members": records, "rows": rows,
              **{key:manifests[0][key] for key in ("feature_version", "canonical_size", "coarse_shape")},
              **{key:dict(counter) for key,counter in totals.items()},
              "duplicate_tile_ids_excluded": sum(len(r["exclude_tile_ids"]) for r in records)}
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "manifest.json", result)
    return result


def members(root: Path, manifest: dict):
    if manifest.get("schema") != "index-bundle-v1":
        return [(root, manifest, set())]
    result = []
    for member in manifest["members"]:
        directory = (root / member["path"]).resolve()
        current = read_manifest(directory)
        frozen = member.get("snapshot_manifest")
        if frozen:
            published = {s["shard_id"]: s for s in current["shards"]}
            if (current["index_id"] != frozen["snapshot_source_index_id"]
                    or any(published.get(s["shard_id"]) != s for s in frozen["shards"])):
                raise ValueError("A published snapshot shard changed or disappeared")
            selected = frozen
        else:
            if current["index_id"] != member["index_id"] or [s["shard_id"] for s in current["shards"]] != member["shard_ids"]:
                raise ValueError("A bundle member changed; recreate the bundle snapshot")
            selected = current
        result.append((directory, selected, set(member["exclude_tile_ids"])))
    return result


def combine(indexes: list[Path], output: Path, *, freeze=False):
    if not indexes:
        raise ValueError("At least one index is required")
    if freeze and (output / "manifest.json").exists():
        saved = read_manifest(output)
        requested = {directory.resolve() for root in indexes for directory, _, _ in members(root, read_manifest(root))}
        captured = {directory.resolve() for directory, _, _ in members(output, saved)}
        if saved.get("schema") != "index-bundle-v1" or requested != captured or not all(m.get("snapshot_manifest") for m in saved["members"]):
            raise ValueError("Existing snapshot has different sources; choose a new output path")
        return saved
    if (output / "manifest.json").exists() and read_manifest(output).get("schema") != "index-bundle-v1":
        raise ValueError("Bundle output must not replace a source index")
    output.mkdir(parents=True, exist_ok=True)
    sources = []
    source_ids = {}
    for root in indexes:
        for directory, manifest, _ in members(root, read_manifest(root)):
            if freeze and "snapshot_source_index_id" not in manifest:
                source_id = manifest["index_id"]
                manifest = {**manifest, "snapshot_source_index_id": source_id,
                            "index_id": identity(["immutable-published-shards-v1", source_id,
                                                  [s["shard_id"] for s in manifest["shards"]]]),
                            "status": "snapshot", "rows": sum(s["rows"] for s in manifest["shards"])}
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
            filename = directory / "shards" / shard["shard_id"] / "tiles.parquet"
            fields = set(pq.ParquetFile(filename).schema_arrow.names)
            table = pq.read_table(filename, columns=[k for k in ("tile_id", "aoi_id", "terrain_group", "window") if k in fields])
            for tile in table.to_pylist():
                if tile["tile_id"] in seen:
                    excluded.append(tile["tile_id"])
                    continue
                seen.add(tile["tile_id"])
                if "aoi_id" in tile:
                    areas[tile["aoi_id"]] += 1
                    terrain[tile["terrain_group"]] += 1
                    scales[str(tile["window"][2])] += 1
        record = {"path": Path(os.path.relpath(directory.resolve(), output.resolve())).as_posix(),
                        "index_id": manifest["index_id"], "rows": manifest.get("rows", sum(s["rows"] for s in manifest["shards"])),
                        "shard_ids": [s["shard_id"] for s in manifest["shards"]], "exclude_tile_ids": excluded}
        if "snapshot_source_index_id" in manifest:
            record["snapshot_manifest"] = manifest
        records.append(record)
    snapshot_identity = [{key: value for key, value in row.items() if key != "path"} for row in records]
    result = {"schema": "index-bundle-v1", "index_id": identity(snapshot_identity), "members": records,
              "rows": len(seen), "feature_version": first["feature_version"],
              "canonical_size": first["canonical_size"], "coarse_shape": first["coarse_shape"],
              "areas": dict(areas), "terrain_groups": dict(terrain), "scales": dict(scales),
              "duplicate_tile_ids_excluded": sum(len(r["exclude_tile_ids"]) for r in records)}
    write_json(output / "manifest.json", result)
    return result
