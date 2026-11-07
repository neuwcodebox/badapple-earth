"""Resume full silhouette retrieval, bounded fine scoring, color path and production."""

from collections import defaultdict
from datetime import datetime,timezone
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
import argparse
import json
import os
import time

import numpy as np
from PIL import Image
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from ...collection.bundle import members,read_manifest
from ...collection.storage import writer_lock
from ...collection.windows import identity
from ...evidence import write_json
from ...matching.output import source_credits
from ...matching.photos import read_rgb_rows
from ...matching.views import render_view
from ..assets import cached_photos,old_rgb_refs,native_scene,frame_credit
from ..layout import location_caption
from ..render import render
from ..variants.pipeline import write_table
from .control import check_pause,batches
from .evaluation import resolve_candidates
from .features import VERSION,load_target,photo_features,evaluate
from .search import search,sources
from .target_cache import TargetCache,build as build_targets
from .temporal import path as color_path,descriptors,VERSION as COLOR_VERSION

FULL_VERSION = "streamed-full-silhouette-color-v1"


@lru_cache(maxsize=1)
def target_reader(root):
    return TargetCache(root)


def score_group(groups,cache,config):
    targets = target_reader(cache)
    rows = []
    for filename,jobs in groups:
        photos = read_rgb_rows(filename,[i for i,_,_ in jobs])
        for position,row,slots in jobs:
            image = render_view(Image.fromarray(photos[position]),row,(256,191))
            gray = np.asarray(image.convert("L"),np.float32)/255
            features = photo_features(gray)
            for slot in slots:
                slot = int(slot)
                rows.append({"slot":slot,"dense_row":row["dense_row"],
                             **evaluate(targets[slot],features,gray,config)})
    return rows


def shortlists(root,run,targets,result,reuse_runs=()):
    destination = root/"shortlists.parquet"
    if destination.exists():return
    inverse = np.load(run/"search/query_inverse.npy",allow_pickle=False)
    matches = pq.read_table(run/"search/matches.parquet",columns=["query","dense_row"]).to_pylist()
    base = defaultdict(list)
    for row in matches:base[row["query"]].append(row["dense_row"])
    preserved = defaultdict(list)
    for prior in reuse_runs:
        for name in ("choices.parquet","display.parquet"):
            for row in pq.read_table(Path(prior)/name,columns=["slot","dense_row"]).to_pylist():
                preserved[row["slot"]].append(row["dense_row"])
    positions,slots = [],[]
    for i,frame in enumerate(targets):
        chosen = np.unique(np.concatenate([*result["positions"][:,i],base[int(inverse[frame["slot"]])],
                                          np.asarray(preserved[frame["slot"]],np.int64)]))
        positions.append(chosen)
        slots.append(np.full(len(chosen),frame["slot"],np.int32))
    temporary = destination.with_suffix(".tmp")
    pq.write_table(pa.table({"slot":np.concatenate(slots),"dense_row":np.concatenate(positions)}),temporary,compression="zstd")
    temporary.replace(destination)


def reused_score_paths(config):
    paths = []
    for prior in config.get("reuse_score_runs",[]):
        prior = Path(prior)
        manifest = read_manifest(prior)
        if manifest["status"]!="complete":raise ValueError("Reuse only finished shape scores")
        if any(manifest["config"][key]!=config[key] for key in (
                "weights","region_contrast_min","edge_contrast_min","edge_contrast_scale","coverage_min","reversed_max")):
            raise ValueError("Reused shape scores need identical metric settings")
        if str(prior)!=config.get("incremental_retrieval"):
            raise ValueError("Reused scores must share the validated retrieval baseline")
        paths.extend(sorted((prior/"scores").glob("*.parquet")))
        paths.extend(Path(p)/"evaluations.parquet" for p in manifest["config"].get("reuse_searches",[]))
    return paths


def pair_keys(table,stride):
    return table["dense_row"].combine_chunks().to_numpy()*stride+table["slot"].combine_chunks().to_numpy()


def score_all(root,run,targets,metadata,config,reused_slots):
    if (root/"choices.parquet").exists():return []
    table = pq.read_table(root/"shortlists.parquet")
    positions = table["dense_row"].combine_chunks().to_numpy()
    slots = table["slot"].combine_chunks().to_numpy()
    keep = ~np.isin(slots,np.array(sorted(reused_slots),np.int32))
    prior_paths = reused_score_paths(config)
    if prior_paths:
        stride = max(r["slot"] for r in targets)+1
        known = np.unique(np.concatenate([pair_keys(pq.read_table(p,columns=["slot","dense_row"]),stride) for p in prior_paths]))
        keep &= ~np.isin(positions*stride+slots,known)
        print(f"Shape pairs reused: {int((~keep).sum()):,}; new: {int(keep.sum()):,}",flush=True)
    positions,slots = positions[keep],slots[keep]
    order = np.argsort(positions,kind="stable")
    positions,slots = positions[order],slots[order]
    starts = np.r_[0,np.flatnonzero(positions[1:]!=positions[:-1])+1,len(positions)] if len(positions) else np.array([0])
    source = read_manifest(run)
    index = Path(source["config"]["index"])
    directories = {m["index_id"]:p for p,m,_ in members(index,read_manifest(index))}
    refs = old_rgb_refs(Path(source["config"]["extra_rgb_cache"]),
                        {r["tile_id"] for r in metadata.values() if not r.get("rgb_cache_ref")})
    grouped = defaultdict(list)
    for begin,end in zip(starts,starts[1:]):
        position = int(positions[begin])
        row = metadata[position]
        if row["tile_id"] in refs:
            filename,offset = refs[row["tile_id"]]
        else:
            filename,offset = directories[row["index_member"]]/row["rgb_cache_ref"],row["rgb_cache_row"]
        view = {k:row.get(k) for k in ("dense_row","rotation_degrees","rotation_crop",
                                     "transform_version","view_affine","transform_size")}
        grouped[str(filename.resolve())].append((offset,view,slots[begin:end]))
    groups = sorted(grouped.items())
    chunks = [groups[i:i+16] for i in range(0,len(groups),16)]
    folder = root/"scores"
    folder.mkdir(exist_ok=True)
    paths = [folder/f"{i:05d}.parquet" for i in range(len(chunks))]
    done = sum(p.exists() for p in paths)
    started,initial = time.monotonic(),done
    def save(destination,rows):
        nonlocal done
        write_table(destination,rows)
        done += 1
        seconds = time.monotonic()-started
        write_json(root/"progress.json",{"stage":"fine-scores","completed_batches":done,"total_batches":len(paths),
                   "remaining_seconds":round((len(paths)-done)*seconds/max(done-initial,1))})
        print(f"Full silhouette scores: {done}/{len(paths)} batches",flush=True)
    batches([((chunk,str(root/"target-features"),config),paths[i]) for i,chunk in enumerate(chunks) if not paths[i].exists()],
            score_group,save,config["workers"],config["pause_file"])
    return [*prior_paths,*paths]


def reduced(root,run,targets,metadata,score_paths,config,reused_roots):
    destination = root/"choices.parquet"
    if destination.exists():return
    baseline = {r["slot"]:r for r in pq.read_table(run/"selected.parquet").to_pylist() if r["slot"]<len(targets)}
    allowed = set(baseline)
    wanted = None
    if config.get("reuse_score_runs"):
        stride = max(allowed)+1
        wanted = np.unique(pair_keys(pq.read_table(root/"shortlists.parquet"),stride))
    near,display,control = defaultdict(list),defaultdict(list),{}
    def key(row):return (-row["shape_score"],row["balanced_mse"],row["view_id"])
    def consume(rows):
        for row in rows:
            slot = row["slot"]
            if slot not in allowed:continue
            meta = metadata[row["dense_row"]]
            row = {**row,"view_id":meta["view_id"],"tile_id":meta["tile_id"]}
            if row["dense_row"]==baseline[slot]["dense_row"]:control[slot] = row
            if row["visible"]:
                current = near[slot]
                if not current or row["shape_score"]>=current[0]["shape_score"]-config["shape_tolerance"]:
                    if len(current)<config["max_candidates"] or key(row)<key(current[-1]):
                        current.append(row)
                        current.sort(key=key)
                        del current[config["max_candidates"]:]
            shown = display[slot]
            previous = next((r for r in shown if r["tile_id"]==row["tile_id"]),None)
            if previous is not None:
                if key(row)>=key(previous):continue
                shown.remove(previous)
            if len(shown)<5 or key(row)<key(shown[-1]):
                shown.append(row)
                shown.sort(key=key)
                del shown[5:]
    for previous in reused_roots:
        consume(pq.read_table(Path(previous)/"evaluations.parquet").to_pylist())
    for filename in score_paths:
        for batch in pq.ParquetFile(filename).iter_batches(batch_size=32768):
            table = pa.Table.from_batches([batch])
            if wanted is not None:
                keys = pair_keys(table,stride)
                offsets = np.searchsorted(wanted,keys)
                keep = (offsets<len(wanted)) & (wanted[np.minimum(offsets,len(wanted)-1)]==keys)
                table = table.filter(pa.array(keep))
            consume(table.to_pylist())
        check_pause(config["pause_file"])
    cache = TargetCache(root/"target-features")
    records,shown_records = [],[]
    for frame in targets:
        slot = frame["slot"]
        flat = cache[slot][-1]
        candidates = near[slot]
        if flat or not candidates:
            candidates = [control[slot]]
            reason = "flat_target" if flat else "no_visible_candidate"
        else:
            best = candidates[0]["shape_score"]
            candidates = [r for r in candidates if r["shape_score"]>=best-config["shape_tolerance"]]
            reason = None
        records.extend({**r,"fallback":reason,"independent":i==0} for i,r in enumerate(candidates))
        shown_records.extend(display[slot])
    write_table(root/"display.parquet",shown_records)
    write_table(destination,records)


def choose_colors(root,run,targets,metadata,config):
    destination = root/"selected.parquet"
    if destination.exists():return
    pools = defaultdict(list)
    for row in pq.read_table(root/"choices.parquet").to_pylist():pools[row["slot"]].append(row)
    needed = {r["dense_row"] for rows in pools.values() for r in rows}
    subset = {p:metadata[p] for p in needed}
    target_paths = {r["slot"]:str((run/"targets"/r["frame"]).resolve()) for r in targets}
    colors = descriptors(root,run,subset,pools,target_paths,config["workers"],config["pause_file"])
    candidates = [pools[f["slot"]] for f in targets]
    features = [np.stack([colors[f["slot"],r["dense_row"]] for r in rows]) for f,rows in zip(targets,candidates)]
    indices = color_path(candidates,features,config["color_weight"])
    records = [{**metadata[rows[i]["dense_row"]],**rows[i],"source_pts":frame["source_pts"],
                "source_time_base":frame["source_time_base"],"selection_method":"silhouette-color",
                "selection_score":rows[i]["shape_score"]} for frame,rows,i in zip(targets,candidates,indices)]
    write_table(destination,records)


def assets(root,run,targets,metadata,config,reuse_assets=None):
    if (root/"frame-plan.parquet").exists():return
    source = read_manifest(run)
    selected = pq.read_table(root/"selected.parquet").to_pylist()
    displays = defaultdict(list)
    for row in pq.read_table(root/"display.parquet").to_pylist():displays[row["slot"]].append(row)
    photos,shown = {},{}
    for frame,choice in zip(targets,selected):
        rows = displays[frame["slot"]]
        if not any(r["tile_id"]==choice["tile_id"] for r in rows):rows = rows[:4]+[choice]
        else:rows = [choice if r["tile_id"]==choice["tile_id"] else r for r in rows]
        rows.sort(key=lambda r:(-r["shape_score"],r["balanced_mse"],r["view_id"]))
        shown[frame["slot"]] = rows
        for row in rows:photos[row["view_id"]] = metadata[row["dense_row"]]
    values = list(photos.values())
    for start in range(0,len(values),128):
        cached_photos(Path(reuse_assets) if reuse_assets else run,root,values[start:start+128],source["config"],(256,191),"assets/thumbs")
        check_pause(config["pause_file"])
    index = Path(source["config"]["index"])
    directories = {m["index_id"]:p for p,m,_ in members(index,read_manifest(index))}
    native,groups = root/"assets/selected",defaultdict(dict)
    native.mkdir(exist_ok=True,parents=True)
    if reuse_assets:
        import shutil
        for row in selected:
            destination = native/(row["view_id"]+".png")
            saved = Path(reuse_assets)/"assets/selected"/destination.name
            if saved.exists() and not destination.exists():
                try:destination.hardlink_to(saved)
                except OSError:shutil.copyfile(saved,destination)
    for row in selected:
        if row["window"][2]>256:groups[row["index_member"],row["scene_id"]][row["view_id"]] = row
    markers = root/"native"
    markers.mkdir(exist_ok=True)
    jobs = []
    for (member,scene),rows in sorted(groups.items()):
        marker = markers/(identity([member,scene])+".json")
        if not marker.exists():jobs.append(((directories[member],list(rows.values()),native,(256,191),run),marker))
    batches(jobs,native_scene,lambda filename,result:write_json(filename,{"results":result}),
            config["workers"],config["pause_file"])
    fps = read_manifest(run/"targets")["frame_rate"]
    duration = float(len(targets)/Fraction(fps))
    places = read_manifest(index)["rows"]
    frames = []
    for frame,choice in zip(targets,selected):
        rows = shown[frame["slot"]]
        image = native/(choice["view_id"]+".png")
        if not image.exists():image = root/"assets/thumbs"/(choice["view_id"]+".png")
        frames.append({"slot":frame["slot"],"source_pts":frame["source_pts"],"source_time_base":frame["source_time_base"],
                       "time":frame["target_time"],"duration":duration,"total":len(targets),"places":places,
                       "source_image":str((run/"targets"/frame["frame"]).resolve()),"credit":frame_credit(choice),
                       "selected_candidate":next(i for i,r in enumerate(rows) if r["view_id"]==choice["view_id"]),
                       "score_label":"SHAPE / COLOR CONTINUITY","selected_image":str(image.resolve()),
                       "caption":location_caption(choice),"candidates":[{"path":str((root/"assets/thumbs"/(r["view_id"]+".png")).resolve()),
                       "rank":i+1,"raw_mse":r["balanced_mse"],"view_id":r["view_id"],"selection_score":r["shape_score"]}
                       for i,r in enumerate(rows)]})
    credits,references = source_credits(photos.values())
    write_json(root/"credits.json",{"source":credits,"references":references,"selected":"selected.parquet"})
    write_table(root/"frame-plan.parquet",frames)


def produce(config,output,resume=False,stop_after_search=False):
    root,run = Path(output).resolve(),Path(config["run"])
    pause_file = root/"pause.request"
    production_id = identity([FULL_VERSION,VERSION,COLOR_VERSION,config,read_manifest(run)["production_id"]])
    with writer_lock(root):
        state = read_manifest(root) if (root/"manifest.json").exists() else {
            "production_id":production_id,"version":FULL_VERSION,"config":config,"status":"starting",
            "started_at":datetime.now(timezone.utc).isoformat()}
        if state["production_id"]!=production_id:raise ValueError("Full output belongs to a different algorithm or input")
        if state["status"] in ("complete","candidates_ready"):return state
        if resume:
            if pause_file.exists():pause_file.unlink()
            state.pop("error_type",None)
            state.pop("paused_at",None)
            state["resumed_at"] = datetime.now(timezone.utc).isoformat()
        check_pause(pause_file)
        runtime = {**config,"pause_file":str(pause_file)}
        targets = [r for r in pq.read_table(run/"targets/targets.parquet").to_pylist()
                   if Fraction(r["source_pts"])*Fraction(r["source_time_base"])<Fraction(str(config["end_seconds"]))]
        def stage(name):
            state.update(status=name,source_frames=len(targets),updated_at=datetime.now(timezone.utc).isoformat())
            write_json(root/"manifest.json",state)
            check_pause(pause_file)
        try:
            stage("retrieval")
            folder = root/"retrieval"
            specifications = sources(config)
            with writer_lock(folder):
                write_json(folder/"sources.json",specifications)
                result = search(run,folder,runtime,targets,specifications)
            state["completed_stage"] = "retrieval"
            if stop_after_search:
                pause_file.touch()
                raise InterruptedError("Retrieval completed; paused before subsequent processing")
            shortlists(root,run,targets,result,config.get("reuse_score_runs",[]))
            del result
            stage("metadata")
            if (root/"candidates.parquet").exists():
                table = pq.read_table(root/"candidates.parquet")
                if (root/"choices.parquet").exists():
                    wanted = set()
                    for name in ("choices.parquet","display.parquet","selected.parquet"):
                        if (root/name).exists():
                            wanted.update(pq.read_table(root/name,columns=["dense_row"])["dense_row"].to_pylist())
                    table = table.filter(pc.is_in(table["dense_row"],value_set=pa.array(sorted(wanted),type=pa.int64())))
                metadata = {r["dense_row"]:r for r in table.to_pylist()}
            else:
                positions = np.unique(pq.read_table(root/"shortlists.parquet",columns=["dense_row"])["dense_row"].combine_chunks().to_numpy())
                metadata = resolve_candidates(root,run,specifications,positions,config["workers"],
                                              [*config["reuse_searches"],*config.get("reuse_score_runs",[])],str(pause_file))
            stage("target-features")
            build_targets(run,root/"target-features",targets,str(pause_file))
            load_target.cache_clear()
            reused_slots = set()
            for previous in config["reuse_searches"]:
                previous_manifest = read_manifest(Path(previous))
                if any(previous_manifest["config"][key]!=config[key] for key in (
                    "weights","region_contrast_min","edge_contrast_min","edge_contrast_scale","coverage_min","reversed_max")):
                    raise ValueError("Reused silhouette scores must have identical metric settings")
                reused_slots.update(pq.read_table(Path(previous)/"selected.parquet",columns=["slot"])["slot"].to_pylist())
            stage("fine-scores")
            paths = score_all(root,run,targets,metadata,runtime,reused_slots)
            stage("candidate-reduction")
            reduced(root,run,targets,metadata,paths,runtime,config["reuse_searches"])
            if config.get("candidates_only"):
                original = read_manifest(run/"targets")
                (root/"targets").mkdir(exist_ok=True)
                write_json(root/"targets/manifest.json",{**original,"count":len(targets),
                           "duration_seconds":float(len(targets)/Fraction(original["frame_rate"]))})
                state.update(status="candidates_ready",finished_at=datetime.now(timezone.utc).isoformat())
                write_json(root/"manifest.json",state)
                return state
            stage("color-continuity")
            choose_colors(root,run,targets,metadata,runtime)
            stage("assets")
            assets(root,run,targets,metadata,runtime)
            stage("render")
            original = read_manifest(run/"targets")
            for directory in (root/"targets",root/"search"):directory.mkdir(exist_ok=True)
            write_json(root/"targets/manifest.json",{**original,"target_set_id":identity([original["target_set_id"],config["end_seconds"]]),
                       "count":len(targets),"duration_seconds":float(len(targets)/Fraction(original["frame_rate"]))})
            write_json(root/"search/manifest.json",{"run_id":production_id,"candidate_count":read_manifest(Path(read_manifest(run)["config"]["index"]))["rows"]})
            render_config = {**read_manifest(run)["config"],**runtime,"selection_method":"silhouette-color",
                             "preset":"medium","crf":18,"audio_mode":"source_local"}
            result = render(root,render_config,pq.read_table(root/"frame-plan.parquet").to_pylist())
            state.update(status="complete",output=result,finished_at=datetime.now(timezone.utc).isoformat(),
                         playback_review="not_performed",external_publication="not_performed")
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",default="config/silhouette-full.json")
    parser.add_argument("--output",default="data/runs/p6-silhouette-color-full")
    parser.add_argument("--resume",action="store_true")
    parser.add_argument("--pause",action="store_true")
    parser.add_argument("--stop-after-search",action="store_true")
    args = parser.parse_args()
    if args.pause:
        root = Path(args.output)
        root.mkdir(exist_ok=True,parents=True)
        (root/"pause.request").touch()
        print("Pause requested; in-flight batches will be saved",flush=True)
    else:
        result = produce(json.loads(Path(args.config).read_text(encoding="utf-8")),args.output,args.resume,args.stop_after_search)
        print(json.dumps({"status":result["status"]}),flush=True)
