"""Propagate good real-photo poses and select motion-compensated appearance paths."""

import argparse
from collections import defaultdict
from datetime import datetime,timezone
from fractions import Fraction
import json
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

from ...collection.bundle import read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ..render import render
from ..silhouette.control import check_pause
from ..silhouette.full import assets
from .assets import reuse_native
from .geometry import parameters_from_matrix
from .motion import build,propagation,VERSION as MOTION_VERSION
from .pipeline import run_jobs,write_selection,verify,read_manifest_file
from .selection import matrix,reduce_options,choose,display_options
from .workers import fine_job,appearance_job

VERSION = "anchor-propagation-appearance-similarity-path-v1"


def tracking_groups(selected,meta,transforms,segments,pixels,config):
    grouped = defaultdict(list)
    radius = config["tracking_radius"]
    def relative(anchor,target):
        low,high = sorted((anchor,target))
        result = np.eye(3)
        for i in range(low+1,high+1):result = transforms[i]@result
        return np.linalg.inv(result) if anchor>target else result
    for slot in range(len(selected)):
        if pixels[slot].std()<1e-3:continue
        anchors = []
        for first,last in ((max(0,slot-radius),slot),(slot+1,min(len(selected),slot+radius+1))):
            candidates = [i for i in range(first,last) if segments[i]==segments[slot] and selected[i].get("visible",False)]
            if candidates:anchors.append(max(candidates,key=lambda i:selected[i]["shape_score"]-.0005*abs(i-slot)))
        seen = set()
        for anchor in anchors:
            row = selected[anchor]
            initial = (np.asarray(row["registration_parameters"],np.float64) if row.get("registration_parameters") is not None else
                       parameters_from_matrix(matrix(row)))
            predicted = propagation(initial,relative(anchor,slot))
            key = row["tile_id"],tuple(np.round(predicted,7))
            if key in seen:continue
            seen.add(key)
            address = row.get("base_dense_row")
            if address is None:address = row["dense_row"]
            grouped[address].append((slot,predicted.tolist()))
    files = defaultdict(list)
    for address,frames in sorted(grouped.items()):
        row = meta[address]
        files[row["_rgb_filename"]].append((row["_rgb_offset"],row,frames))
    return sorted(files.items())


def appearance_groups(rows,meta):
    unique = {r["view_id"]:r for r in rows}
    files = defaultdict(list)
    for row in sorted(unique.values(),key=lambda r:r["view_id"]):
        address = row.get("base_dense_row")
        if address is None:address = row["dense_row"]
        base = meta[address]
        files[base["_rgb_filename"]].append((base["_rgb_offset"],row))
    return sorted(files.items())


def select(root,run,baseline,quality,meta,transforms,confidence,pixels,paths,source,config):
    if (root/"selected.parquet").exists():return
    options = pq.read_table(baseline/"choices.parquet").to_pylist()
    original = pq.read_table(quality/"selected.parquet").to_pylist()
    needed = {(r["slot"],r["dense_row"]) for r in original}
    luminance = {}
    for path in sorted((quality/"luminance").glob("*.parquet")):
        for row in pq.read_table(path).to_pylist():
            key = row["slot"],row["dense_row"]
            if key in needed:luminance[key] = row["luminance"]
    for row in original:
        row["base_dense_row"] = row["dense_row"]
        row["luminance"] = luminance[row["slot"],row["dense_row"]]
    appearance_paths = run_jobs(root,run,source,appearance_groups([*options,*original],meta),"appearance",appearance_job,config)
    appearance = {}
    for path in appearance_paths:
        for row in pq.read_table(path).to_pylist():appearance[row["view_id"]] = row["appearance"]
    for row in [*options,*original]:row["appearance"] = appearance[row["view_id"]]
    for path in paths:options.extend(pq.read_table(path).to_pylist())
    pools = reduce_options(options,original,config)
    chosen = choose(pools,pixels,config,transforms,confidence)
    display_candidates = [*original,*options]
    for path in sorted((baseline/"fine").glob("*.parquet")):
        display_candidates.extend(pq.read_table(path).to_pylist())
    shown = display_options(display_candidates,chosen,config["shape_tolerance"])
    write_selection(root,run,original,pools,chosen,read_manifest(baseline/"retrieval")["view_count"],"tracked-motion-appearance",shown)


def produce(config,resume=False):
    root,baseline,quality,source = map(Path,(config["output"],config["baseline"],config["quality_baseline"],config["source"]))
    if read_manifest(baseline)["status"]!="complete":raise ValueError("Tracking requires completed real-photo registration")
    run = Path(read_manifest(source)["config"]["run"])
    production_id = identity([VERSION,MOTION_VERSION,config,read_manifest(baseline)["production_id"],read_manifest(quality)["production_id"]])
    with writer_lock(root):
        state = read_manifest(root) if (root/"manifest.json").exists() else {
            "production_id":production_id,"config":config,"status":"starting","started_at":datetime.now(timezone.utc).isoformat()}
        if state["production_id"]!=production_id:raise ValueError("Tracking output belongs to another configuration")
        if state["status"]=="complete":return state
        if resume:
            (root/"pause.request").unlink(missing_ok=True)
            state.pop("error_type",None)
            state.pop("paused_at",None)
        def stage(name):
            state.update(status=name,updated_at=datetime.now(timezone.utc).isoformat())
            write_json(root/"manifest.json",state)
            check_pause(root/"pause.request")
        try:
            targets = [r for r in pq.read_table(run/"targets/targets.parquet").to_pylist()
                       if Fraction(r["source_pts"])*Fraction(r["source_time_base"])<Fraction(str(config["end_seconds"]))]
            pixels = np.load(run/"targets/gray_fine.npy",mmap_mode="r",allow_pickle=False)[:len(targets)]
            stage("source-motion")
            transforms,confidence,segments = build(config["target_pixel_cache"],root,len(targets),root/"pause.request")
            if not (root/"selected.parquet").exists():
                meta = {r["dense_row"]:r for r in pq.read_table(baseline/"base-candidates.parquet").to_pylist()}
                selected = pq.read_table(baseline/"selected.parquet").to_pylist()
                stage("pose-propagation")
                groups = tracking_groups(selected,meta,transforms,segments,pixels,config)
                stage("tracked-alignment")
                paths = run_jobs(root,run,source,groups,"track",fine_job,config)
                stage("appearance-selection")
                select(root,run,baseline,quality,meta,transforms,confidence,pixels,paths,source,config)
                del meta,selected,groups
            state["selection_summary"] = read_manifest_file(root/"selection-summary.json")
            meta = {r["dense_row"]:r for r in pq.read_table(root/"candidates.parquet").to_pylist()}
            runtime = {**config,"pause_file":str(root/"pause.request")}
            stage("assets")
            reuse_native(root,baseline,pq.read_table(root/"selected.parquet").to_pylist(),[quality])
            assets(root,run,targets,meta,runtime,reuse_assets=baseline)
            stage("render")
            for folder in (root/"targets",root/"search"):folder.mkdir(exist_ok=True)
            write_json(root/"targets/manifest.json",read_manifest(quality/"targets"))
            index = Path(read_manifest(run)["config"]["index"])
            write_json(root/"search/manifest.json",{"run_id":production_id,"candidate_count":read_manifest(index)["rows"]})
            render_config = {**read_manifest(run)["config"],**runtime,"preset":"medium","crf":18,"audio_mode":"source_local"}
            state["output"] = render(root,render_config,pq.read_table(root/"frame-plan.parquet").to_pylist())
            stage("verification")
            verify(root,targets,quality)
            state.update(status="complete",finished_at=datetime.now(timezone.utc).isoformat(),playback_review="not_performed")
            write_json(root/"manifest.json",state)
            return state
        except (InterruptedError,KeyboardInterrupt):
            state.update(status="paused",paused_at=datetime.now(timezone.utc).isoformat())
            write_json(root/"manifest.json",state)
            return state
        except BaseException as error:
            state.update(status="needs_attention",error_type=type(error).__name__)
            write_json(root/"manifest.json",state)
            raise


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',default='config/tracking-full.json')
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--pause',action='store_true')
    args=parser.parse_args()
    config=read_manifest_file(Path(args.config))
    if args.pause:
        root=Path(config['output']);root.mkdir(parents=True,exist_ok=True)
        (root/'pause.request').touch()
    else:print(json.dumps({'status':produce(config,args.resume)['status']}),flush=True)
