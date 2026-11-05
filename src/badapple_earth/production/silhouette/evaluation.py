"""Resolve frozen view addresses and score cached original photos offline."""

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from multiprocessing import get_context
from pathlib import Path
import json
import os

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import members, read_manifest
from ...matching.photos import read_rgb_rows
from ...matching.views import render_view
from ..assets import old_rgb_refs
from ..search import resolve
from ..variants.bank import Bank
from ..variants.full import member_mapping
from ..variants.pipeline import write_table
from .features import load_target, photo_features, evaluate


def resolve_part(spec,positions,full_index):
    positions = np.asarray(positions,dtype=np.int64)
    local = positions-spec["start"]
    if spec["kind"] == "packed":
        cache = Path(spec["cache"])
        rows = [{**row,"dense_row":int(p)+spec["start"]} for p,row in resolve(cache,read_manifest(cache),local).items()]
    else:
        config = json.loads(Path(spec["config"]).read_text(encoding="utf-8"))
        bank = Bank(config["bank"],config)
        rows = list(bank.resolve(local,spec["start"]).values())
    mapping = member_mapping(Path(spec["index"]),Path(full_index))
    for row in rows:
        row["index_member"] = mapping[row["index_member"]]
    return rows


def resolve_candidates(root,run,specs,positions,workers):
    positions = sorted(set(int(p) for p in positions))
    table = pq.read_table(run/"candidates.parquet")
    table = table.filter(pc.is_in(table["dense_row"],value_set=pa.array(positions,type=pa.int64())))
    result = {r["dense_row"]:r for r in table.to_pylist()}
    missing = np.array(sorted(set(positions)-set(result)),np.int64)
    source = read_manifest(run)
    folder = root/"metadata"
    folder.mkdir(exist_ok=True)
    jobs = []
    for number,spec in enumerate(specs):
        selected = missing[(missing>=spec["start"]) & (missing<spec["start"]+spec["rows"])]
        for part,chunk in enumerate(np.array_split(selected,max(1,workers//2))):
            if len(chunk):
                jobs.append((spec,chunk,folder/f"{number:02d}-{part:02d}.parquet"))
    with ProcessPoolExecutor(max_workers=workers,mp_context=get_context("spawn")) as pool:
        pending = {pool.submit(resolve_part,spec,chunk,source["config"]["index"]):path
                   for spec,chunk,path in jobs if not path.exists()}
        for future in as_completed(pending):
            write_table(pending[future],future.result())
    for _,_,path in jobs:
        result.update({r["dense_row"]:r for r in pq.read_table(path).to_pylist()})
    if set(result) != set(positions):
        raise ValueError("Every retrieved view requires exact frozen source metadata")
    paths = {m["index_id"]:p for p,m,_ in members(Path(source["config"]["index"]),read_manifest(Path(source["config"]["index"])))}
    scenes = {}
    for row in result.values():
        if row.get("source_id") == "linz-imagery" and not row.get("source_licensors"):
            key = row["index_member"],row["scene_id"]
            if key not in scenes:
                scenes[key] = json.loads((paths[key[0]]/"catalog"/(key[1]+".json")).read_text(encoding="utf-8"))
            row["source_licensors"] = [p["name"] for p in scenes[key].get("providers",[]) if "licensor" in p.get("roles",[])]
    write_table(root/"candidates.parquet",list(result.values()))
    return result


def score_groups(groups,config):
    output = []
    for filename,jobs in groups:
        photos = read_rgb_rows(filename,[position for position,_,_ in jobs])
        for position,candidate,targets in jobs:
            base = Image.fromarray(photos[position])
            image = render_view(base,candidate,base.size)
            gray = np.asarray(image.convert("L"),np.float32)/255
            features = photo_features(gray)
            for target in targets:
                metrics = evaluate(load_target(target["path"]),features,gray,config)
                output.append({"slot":target["slot"],"dense_row":candidate["dense_row"],**metrics})
    return output


def score(root,run,targets,metadata,shortlists,config):
    source = read_manifest(run)
    index = Path(source["config"]["index"])
    directories = {m["index_id"]:p for p,m,_ in members(index,read_manifest(index))}
    target_map = {r["slot"]:{"slot":r["slot"],"path":str((run/"targets"/r["frame"]).resolve())} for r in targets}
    wanted = defaultdict(list)
    for row in shortlists:
        wanted[row["dense_row"]].append(target_map[row["slot"]])
    refs = old_rgb_refs(Path(source["config"]["extra_rgb_cache"]),
                        {r["tile_id"] for r in metadata.values() if not r.get("rgb_cache_ref")})
    grouped = defaultdict(list)
    for position in sorted(wanted):
        row = metadata[position]
        if row["tile_id"] in refs:
            path,offset = refs[row["tile_id"]]
        else:
            path,offset = directories[row["index_member"]]/row["rgb_cache_ref"],row["rgb_cache_row"]
        view = {k:row.get(k) for k in ("dense_row","rotation_degrees","rotation_crop",
                                     "transform_version","view_affine","transform_size")}
        grouped[str(path.resolve())].append((offset,view,wanted[position]))
    groups = sorted(grouped.items())
    chunks = [groups[i:i+24] for i in range(0,len(groups),24)]
    folder = root/"scores"
    folder.mkdir(exist_ok=True)
    names = ("OPENBLAS_NUM_THREADS","OMP_NUM_THREADS","NUMBA_NUM_THREADS")
    old = {k:os.environ.get(k) for k in names}
    try:
        for name in names:
            os.environ[name] = "1"
        with ProcessPoolExecutor(max_workers=config["workers"],mp_context=get_context("spawn")) as pool:
            pending = {pool.submit(score_groups,chunk,config):folder/f"{i:05d}.parquet"
                       for i,chunk in enumerate(chunks) if not (folder/f"{i:05d}.parquet").exists()}
            completed = len(chunks)-len(pending)
            for future in as_completed(pending):
                write_table(pending[future],future.result())
                completed += 1
                if completed%16==0 or completed==len(chunks):
                    print(f"Visible silhouette fine scores: {completed}/{len(chunks)} RGB groups",flush=True)
    finally:
        for name,value in old.items():
            if value is None:
                os.environ.pop(name,None)
            else:
                os.environ[name] = value
    rows = [row for i in range(len(chunks)) for row in pq.read_table(folder/f"{i:05d}.parquet").to_pylist()]
    expected = {(r["slot"],r["dense_row"]) for r in shortlists}
    if len(rows)!=len(expected) or {(r["slot"],r["dense_row"]) for r in rows} != expected:
        raise ValueError("Every silhouette frame/view pair must be evaluated exactly once")
    write_table(root/"evaluations.parquet",rows)
    return rows
