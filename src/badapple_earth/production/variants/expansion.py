"""Reference newly published photo shards without rescanning the existing pool."""

from collections import Counter
import os
from pathlib import Path

from ...collection.bundle import members, read_manifest
from ...collection.windows import identity
from ...evidence import write_json


def difference(previous, current, output):
    previous,current,output = map(Path,(previous,current,output))
    old,new = read_manifest(previous),read_manifest(current)
    before = {p.resolve():(m,excluded) for p,m,excluded in members(previous,old)}
    records = []
    for source,manifest,excluded in members(current,new):
        original,old_excluded = before.pop(source.resolve(),({},set()))
        if excluded or old_excluded:
            raise ValueError("Shard expansion requires already disjoint immutable photos")
        old_shards = {s["shard_id"]:s for s in original.get("shards",[])}
        available = {s["shard_id"]:s for s in manifest["shards"]}
        if any(available.get(key) != value for key,value in old_shards.items()):
            raise ValueError("An original photo shard changed or disappeared")
        shards = [s for s in manifest["shards"] if s["shard_id"] not in old_shards]
        if not shards:
            continue
        source_id = manifest.get("snapshot_source_index_id",manifest["index_id"])
        frozen = {**manifest,"snapshot_source_index_id":source_id,
                  "index_id":identity(["immutable-published-shards-v1",source_id,[s["shard_id"] for s in shards]]),
                  "shards":shards,"rows":sum(s["rows"] for s in shards),"status":"snapshot"}
        records.append({"path":Path(os.path.relpath(source,output.resolve())).as_posix(),
                        "index_id":frozen["index_id"],"rows":frozen["rows"],
                        "shard_ids":[s["shard_id"] for s in shards],"exclude_tile_ids":[],"snapshot_manifest":frozen})
    if before:
        raise ValueError("The full index must contain every baseline member")
    count = sum(r["rows"] for r in records)
    if count != new["rows"]-old["rows"] or count <= 0:
        raise ValueError("New shards must exactly cover the added geographic photos")
    result = {"schema":"index-bundle-v1","index_id":identity([{k:v for k,v in r.items() if k!='path'} for r in records]),
              "members":records,"rows":count,"duplicate_tile_ids_excluded":0,
              **{k:new[k] for k in ("feature_version","canonical_size","coarse_shape")},
              **{k:dict(Counter(new[k])-Counter(old[k])) for k in ("areas","terrain_groups","scales")}}
    output.mkdir(parents=True,exist_ok=True)
    if (output/"manifest.json").exists() and read_manifest(output)["index_id"] != result["index_id"]:
        raise ValueError("Expansion output belongs to other photo shards")
    write_json(output/"manifest.json",result)
    return result
